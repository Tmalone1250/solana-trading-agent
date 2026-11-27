"""Safe, consistent read-only DuckDB snapshot acquisition, schema validation & episode grouping.

Guarantees (Option A1 — Strict Offline Fail-Closed Snapshot):
- Never opens the source `.duckdb` or `.duckdb.wal` with DuckDB directly (zero source mutation).
- Probes for active writer locks via read-only `/proc/locks` and POSIX `fcntl(F_GETLK)` before
  touching or copying any file.
- Fails closed immediately (`LIVE_WRITER_LOCK_HELD`) whenever a writer holds a lock, without
  copying the live database or WAL.
- For an unlocked source, verifies pre/post `(inode, size, mtime_ns, sha256)` stability across the
  copy to `/tmp` (note: file fingerprints verify copy stability on an unlocked source, not
  transactional atomicity against an active writer).
- Opens only the isolated `/tmp` snapshot with `duckdb.connect(..., read_only=True)` and cleans up
  temporary files in `finally:`.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import struct
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import duckdb

from simulator import RUNTIME_CODE_PROVENANCE


class SnapshotConsistencyError(RuntimeError):
    """Raised when a consistent read-only database snapshot cannot be safely acquired."""


class SchemaValidationError(RuntimeError):
    """Raised when required simulator tables or columns are missing from the snapshot."""


REQUIRED_SCHEMA: dict[str, tuple[str, ...]] = {
    "action_log": (
        "id",
        "timestamp_utc",
        "mode",
        "action",
        "symbol",
        "asset_id",
        "qty",
        "notional",
        "price_or_value",
        "pnl_amount",
        "pnl_pct",
        "strategy",
        "reason",
        "sol_equity",
        "daily_realized_sol",
        "mc_usd",
        "liq_usd",
        "soak_minutes",
        "rsi",
        "leg2_score",
        "narrative_score",
        "momentum_score",
        "vamp_score",
        "buy_confidence",
        "peak_pnl_pct",
        "hold_seconds",
        "breakout_score",
        "breakout_state",
        "breakout_timeframe",
        "breakout_vol_ratio",
        "breakout_ema_ext_pct",
        "breakout_failed_gates",
        "jupiter_price_impact_raw",
        "monitor_sol_value",
        "price_source",
        "position_mfe_pct",
        "position_mae_pct",
    ),
    "trades": (
        "id",
        "timestamp",
        "mode",
        "side",
        "mint",
        "symbol",
        "in_amount",
        "out_amount",
        "sol_value",
        "pnl_sol",
        "pnl_pct",
        "reason",
        "tx_signature",
        "rationale",
        "strategy",
        "opened_at",
        "jupiter_price_impact_raw",
    ),
    "candidate_outcomes": (
        "id",
        "mint",
        "symbol",
        "source",
        "discovered_at",
        "decision",
        "decision_reason",
        "strategy",
        "entry_price_usd",
        "entry_mc_usd",
        "entry_liq_usd",
        "breakout_score",
        "breakout_state",
        "leg2_score",
        "vamp_score",
        "rsi",
        "price_1m_pct",
        "price_5m_pct",
        "price_15m_pct",
        "price_30m_pct",
        "mfe_pct",
        "mae_pct",
        "finalized",
    ),
    "decisions_log": (
        "id",
        "timestamp",
        "mint",
        "symbol",
        "phase",
        "action",
        "confidence",
        "scores_json",
        "rationale",
    ),
    "agent_state": (
        "id",
        "focus_state",
        "sol_balance",
        "daily_start_sol",
        "daily_pnl_sol",
        "updated_at",
    ),
    "cooldowns": ("mint", "expires_at", "reason"),
    "active_watchlist": ("mint", "symbol", "pair_address", "added_at"),
}

EPISODE_GAP_SECONDS = 300.0  # > 5m silence between evaluations marks a distinct watchlist episode


def parse_iso_utc(raw: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string into a timezone-aware UTC datetime."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class FileFingerprint:
    path: str
    exists: bool
    inode: int = 0
    size: int = 0
    mtime_ns: int = 0
    sha256: str = ""


@dataclass(frozen=True)
class SnapshotProvenance:
    source_db_path: str
    source_wal_path: str
    wal_included: bool
    snapshot_method: str
    acquired_at_utc: str
    db_sha256: str
    wal_sha256: str
    db_size_bytes: int
    wal_size_bytes: int
    runtime_code_provenance: str = RUNTIME_CODE_PROVENANCE


@dataclass(frozen=True)
class DecisionEpisode:
    """Chronological grouping of repeated observations for a single mint visit on the watchlist."""

    episode_id: str
    mint: str
    symbol: str
    episode_index: int
    started_at: str
    ended_at: str
    observation_count: int
    decision1_rows: tuple[dict[str, Any], ...]
    terminal_action: Optional[str]
    terminal_reason: Optional[str]
    terminal_row_id: Optional[int]
    linked_outcome_ids: tuple[str, ...]
    linked_decision_log_ids: tuple[int, ...]


@dataclass
class HistoricalDataset:
    """In-memory, read-only historical dataset loaded from a verified DuckDB snapshot."""

    provenance: SnapshotProvenance
    table_columns: dict[str, tuple[str, ...]]
    action_log: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    candidate_outcomes: list[dict[str, Any]]
    decisions_log: list[dict[str, Any]]
    agent_state: list[dict[str, Any]]
    cooldowns: list[dict[str, Any]]
    active_watchlist: list[dict[str, Any]]
    episodes: list[DecisionEpisode] = field(default_factory=list)
    episodes_by_mint: dict[str, list[DecisionEpisode]] = field(default_factory=dict)


def compute_file_fingerprint(path: Path) -> FileFingerprint:
    """Compute a purely read-only (inode, size, mtime_ns, sha256) fingerprint."""
    if not path.exists():
        return FileFingerprint(path=str(path), exists=False)
    st = os.stat(path)
    hasher = hashlib.sha256()
    fd = os.open(str(path), os.O_RDONLY)
    try:
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    finally:
        os.close(fd)
    st_after = os.stat(path)
    if st_after.st_size != st.st_size or st_after.st_mtime_ns != st.st_mtime_ns or st_after.st_ino != st.st_ino:
        raise SnapshotConsistencyError(
            f"FILE_MUTATED_DURING_HASH: {path} changed while computing read-only fingerprint."
        )
    return FileFingerprint(
        path=str(path),
        exists=True,
        inode=int(st.st_ino),
        size=int(st.st_size),
        mtime_ns=int(st.st_mtime_ns),
        sha256=hasher.hexdigest(),
    )


def detect_active_writer_lock(path: Path) -> Optional[str]:
    """Detect whether any process holds an active WRITE lock on `path` without modifying it.

    Checks both Linux `/proc/locks` (catches same-process & cross-process locks) and
    POSIX `fcntl(F_GETLK)` via an `O_RDONLY` descriptor (catches cross-process locks on all mounts).
    """
    if not path.is_file():
        return None
    st = os.stat(path)
    major = os.major(st.st_dev)
    minor = os.minor(st.st_dev)
    ino_str = str(st.st_ino)
    dev_hex = f"{major:02x}:{minor:02x}:{ino_str}"
    dev_dec = f"{major}:{minor}:{ino_str}"

    proc_locks = Path("/proc/locks")
    if proc_locks.is_file():
        try:
            for line in proc_locks.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split()
                # Format: 1: POSIX ADVISORY WRITE 12345 fd:01:67890 0 EOF
                if len(parts) >= 6 and parts[3].upper() == "WRITE":
                    target = parts[5]
                    if target == dev_hex or target == dev_dec or target.endswith(f":{ino_str}"):
                        if target.split(":")[0] in (f"{major:02x}", str(major)):
                            return f"PROC_LOCKS_WRITE(pid={parts[4]}, target={target})"
        except OSError:
            pass

    fd = os.open(str(path), os.O_RDONLY)
    try:
        lockdata = struct.pack("hhqqi", fcntl.F_WRLCK, os.SEEK_SET, 0, 0, 0)
        res = fcntl.fcntl(fd, fcntl.F_GETLK, lockdata)
        l_type, _, _, _, l_pid = struct.unpack("hhqqi", res)
        if l_type != fcntl.F_UNLCK:
            return f"FCNTL_WRITE_LOCK(pid={l_pid}, type={l_type})"
    except OSError:
        pass
    finally:
        os.close(fd)

    return None


def validate_snapshot_schema(conn: duckdb.DuckDBPyConnection) -> dict[str, tuple[str, ...]]:
    """Verify all required tables and columns exist in the read-only snapshot connection."""
    existing_tables = {
        str(r[0])
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    missing_tables = sorted(set(REQUIRED_SCHEMA.keys()) - existing_tables)
    if missing_tables:
        raise SchemaValidationError(
            f"Missing required DuckDB table(s): {', '.join(missing_tables)}"
        )

    validated_cols: dict[str, tuple[str, ...]] = {}
    for table_name, req_cols in REQUIRED_SCHEMA.items():
        info_rows = conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
        actual_cols = tuple(str(r[1]) for r in info_rows)
        missing_cols = [c for c in req_cols if c not in actual_cols]
        if missing_cols:
            raise SchemaValidationError(
                f"Table '{table_name}' missing required column(s): {', '.join(missing_cols)}"
            )
        validated_cols[table_name] = actual_cols
    return validated_cols


def _fetch_table_dicts(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
    order_by_sql: str = "",
) -> list[dict[str, Any]]:
    sql = f"SELECT * FROM {table_name}"
    if order_by_sql:
        sql = f"{sql} ORDER BY {order_by_sql}"
    cur = conn.execute(sql)
    cols = [d[0] for d in (cur.description or [])]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def group_decision_episodes(
    action_rows: list[dict[str, Any]],
    decisions_rows: list[dict[str, Any]],
    outcomes_rows: list[dict[str, Any]],
    *,
    since_utc: Optional[str] = None,
) -> tuple[list[DecisionEpisode], dict[str, list[DecisionEpisode]]]:
    """Group repeated `action_log` observations per mint into distinct watchlist episodes."""
    since_dt = parse_iso_utc(since_utc) if since_utc else None

    # Index decisions_log by mint -> sorted (dt, id)
    dec_by_mint: dict[str, list[tuple[datetime, int]]] = {}
    for d in decisions_rows:
        m = str(d.get("mint") or "").strip()
        dt = parse_iso_utc(d.get("timestamp"))
        if m and dt is not None and d.get("id") is not None:
            dec_by_mint.setdefault(m, []).append((dt, int(d["id"])))
    for m_list in dec_by_mint.values():
        m_list.sort(key=lambda x: x[0])

    # Index candidate_outcomes by mint -> sorted (dt, id)
    out_by_mint: dict[str, list[tuple[datetime, str]]] = {}
    for o in outcomes_rows:
        m = str(o.get("mint") or "").strip()
        dt = parse_iso_utc(o.get("discovered_at"))
        oid = str(o.get("id") or "").strip()
        if m and dt is not None and oid:
            out_by_mint.setdefault(m, []).append((dt, oid))
    for m_list in out_by_mint.values():
        m_list.sort(key=lambda x: x[0])

    # Filter relevant Chart actions ordered chronologically
    chart_actions = {"DECISION_1", "EVICT", "BUY", "REENTRY_BUY"}
    per_mint_rows: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
    for row in action_rows:
        act = str(row.get("action") or "").upper()
        if act not in chart_actions:
            continue
        if str(row.get("strategy") or "").upper() == "WHALE_SCALP":
            continue
        mint = str(row.get("asset_id") or "").strip()
        dt = parse_iso_utc(row.get("timestamp_utc"))
        if not mint or dt is None:
            continue
        if since_dt is not None and dt < since_dt:
            continue
        per_mint_rows.setdefault(mint, []).append((dt, row))

    all_episodes: list[DecisionEpisode] = []
    by_mint: dict[str, list[DecisionEpisode]] = {}

    for mint in sorted(per_mint_rows.keys()):
        seq = sorted(per_mint_rows[mint], key=lambda item: (item[0], int(item[1].get("id") or 0)))
        ep_idx = 0
        curr_d1: list[dict[str, Any]] = []
        prev_d1_dt: Optional[datetime] = None
        prev_soak: Optional[float] = None
        curr_symbol = ""

        def _finalize_episode(
            term_act: Optional[str],
            term_reason: Optional[str],
            term_id: Optional[int],
            end_iso: Optional[str],
        ) -> None:
            nonlocal ep_idx, curr_d1, prev_d1_dt, prev_soak
            if not curr_d1:
                return
            ep_idx += 1
            start_iso = str(curr_d1[0]["timestamp_utc"])
            final_end_iso = end_iso or str(curr_d1[-1]["timestamp_utc"])
            start_dt = parse_iso_utc(start_iso)
            end_dt = parse_iso_utc(final_end_iso)

            linked_out: list[str] = []
            if start_dt and end_dt:
                for odt, oid in out_by_mint.get(mint, []):
                    if (start_dt.timestamp() - 2.0) <= odt.timestamp() <= (end_dt.timestamp() + 2.0):
                        linked_out.append(oid)

            linked_dec: list[int] = []
            if start_dt and end_dt:
                for ddt, did in dec_by_mint.get(mint, []):
                    if (start_dt.timestamp() - 2.0) <= ddt.timestamp() <= (end_dt.timestamp() + 2.0):
                        linked_dec.append(did)

            ep = DecisionEpisode(
                episode_id=f"{mint[:8]}-ep{ep_idx:03d}",
                mint=mint,
                symbol=curr_symbol or str(curr_d1[-1].get("symbol") or mint[:8]),
                episode_index=ep_idx,
                started_at=start_iso,
                ended_at=final_end_iso,
                observation_count=len(curr_d1),
                decision1_rows=tuple(curr_d1),
                terminal_action=term_act,
                terminal_reason=term_reason,
                terminal_row_id=term_id,
                linked_outcome_ids=tuple(linked_out),
                linked_decision_log_ids=tuple(linked_dec),
            )
            all_episodes.append(ep)
            by_mint.setdefault(mint, []).append(ep)
            curr_d1 = []
            prev_d1_dt = None
            prev_soak = None

        for dt, row in seq:
            act = str(row.get("action") or "").upper()
            if row.get("symbol"):
                curr_symbol = str(row["symbol"])
            if act == "DECISION_1":
                soak_val = float(row["soak_minutes"]) if row.get("soak_minutes") is not None else None
                gap_split = (
                    prev_d1_dt is not None
                    and (dt - prev_d1_dt).total_seconds() > EPISODE_GAP_SECONDS
                )
                soak_reset = (
                    prev_soak is not None
                    and soak_val is not None
                    and (soak_val + 0.25) < prev_soak
                )
                if curr_d1 and (gap_split or soak_reset):
                    _finalize_episode("IMPLIED_EXIT_OR_GAP", None, None, None)
                curr_d1.append(row)
                prev_d1_dt = dt
                prev_soak = soak_val
            elif act in ("EVICT", "BUY", "REENTRY_BUY"):
                if curr_d1:
                    _finalize_episode(
                        act,
                        str(row.get("reason") or "") or None,
                        int(row["id"]) if row.get("id") is not None else None,
                        str(row.get("timestamp_utc") or ""),
                    )

        if curr_d1:
            _finalize_episode(None, None, None, None)

    all_episodes.sort(key=lambda e: (e.started_at, e.mint, e.episode_index))
    return all_episodes, by_mint


def load_historical_dataset(
    db_path: str | Path,
    *,
    since_utc: Optional[str] = None,
) -> HistoricalDataset:
    """Acquire a verified offline read-only DuckDB (+ WAL) snapshot, validate schema, and load dataset.

    Option A1 workflow:
    1. Probe for an active writer lock on `.duckdb` and `.duckdb.wal`.
    2. Reject immediately (`LIVE_WRITER_LOCK_HELD`) without copying if a writer lock is held.
    3. Fingerprint unlocked source `.duckdb` and `.duckdb.wal`.
    4. Copy to an isolated temporary directory.
    5. Verify source-file stability and unlocked state across the copy.
    6. Open only the temporary copy with `read_only=True`.
    7. Validate schemas and WAL replay.
    8. Clean up temporary files in `finally:`.
    """
    src_db = Path(db_path).resolve()
    if not src_db.is_file():
        raise FileNotFoundError(f"Source DuckDB file does not exist: {src_db}")
    src_wal = Path(f"{src_db}.wal")

    # 1-2. Probe for an active writer lock and fail closed before any copy
    lock_info = detect_active_writer_lock(src_db) or (
        detect_active_writer_lock(src_wal) if src_wal.is_file() else None
    )
    if lock_info is not None:
        raise SnapshotConsistencyError(
            f"LIVE_WRITER_LOCK_HELD on {src_db} ({lock_info}). "
            "Option A1 enforces fail-closed rejection; use a separate offline unlocked snapshot."
        )

    # 3-8. Fingerprint unlocked source, copy to isolated /tmp, verify stability, open read-only, clean up
    tmp_dir = Path(tempfile.mkdtemp(prefix="breakout_replay_snap_"))
    try:
        pre_db_fp = compute_file_fingerprint(src_db)
        pre_wal_fp = compute_file_fingerprint(src_wal)

        snap_db = tmp_dir / src_db.name
        snap_wal = Path(f"{snap_db}.wal")
        shutil.copy2(src_db, snap_db)
        if pre_wal_fp.exists:
            shutil.copy2(src_wal, snap_wal)

        post_db_fp = compute_file_fingerprint(src_db)
        post_wal_fp = compute_file_fingerprint(src_wal)
        post_lock = detect_active_writer_lock(src_db) or (
            detect_active_writer_lock(src_wal) if src_wal.is_file() else None
        )

        if post_lock is not None:
            raise SnapshotConsistencyError(
                f"LIVE_WRITER_LOCK_ACQUIRED_DURING_COPY on {src_db} ({post_lock})."
            )
        if pre_db_fp != post_db_fp or pre_wal_fp != post_wal_fp:
            raise SnapshotConsistencyError(
                f"SOURCE_MODIFIED_DURING_COPY: "
                f"db_changed={pre_db_fp != post_db_fp}, wal_changed={pre_wal_fp != post_wal_fp}"
            )

        try:
            conn = duckdb.connect(str(snap_db), read_only=True)
        except Exception as exc:
            raise SnapshotConsistencyError(
                f"DUCKDB_READONLY_OPEN_OR_WAL_REPLAY_FAILED: {exc}"
            ) from exc

        try:
            validated_cols = validate_snapshot_schema(conn)
            action_rows = _fetch_table_dicts(conn, "action_log", "timestamp_utc ASC, id ASC")
            trade_rows = _fetch_table_dicts(conn, "trades", "timestamp ASC, id ASC")
            outcome_rows = _fetch_table_dicts(conn, "candidate_outcomes", "discovered_at ASC, id ASC")
            decision_rows = _fetch_table_dicts(conn, "decisions_log", "timestamp ASC, id ASC")
            state_rows = _fetch_table_dicts(conn, "agent_state", "id ASC")
            cooldown_rows = _fetch_table_dicts(conn, "cooldowns", "expires_at ASC, mint ASC")
            watchlist_rows = _fetch_table_dicts(conn, "active_watchlist", "added_at ASC, mint ASC")
        finally:
            conn.close()

        provenance = SnapshotProvenance(
            source_db_path=str(src_db),
            source_wal_path=str(src_wal) if pre_wal_fp.exists else "",
            wal_included=pre_wal_fp.exists,
            snapshot_method="OFFLINE_LOCK_FREE_SHA256_VERIFIED_SNAPSHOT",
            acquired_at_utc=datetime.now(timezone.utc).isoformat(),
            db_sha256=pre_db_fp.sha256,
            wal_sha256=pre_wal_fp.sha256,
            db_size_bytes=pre_db_fp.size,
            wal_size_bytes=pre_wal_fp.size,
            runtime_code_provenance=RUNTIME_CODE_PROVENANCE,
        )
        episodes, ep_by_mint = group_decision_episodes(
            action_rows, decision_rows, outcome_rows, since_utc=since_utc
        )
        return HistoricalDataset(
            provenance=provenance,
            table_columns=validated_cols,
            action_log=action_rows,
            trades=trade_rows,
            candidate_outcomes=outcome_rows,
            decisions_log=decision_rows,
            agent_state=state_rows,
            cooldowns=cooldown_rows,
            active_watchlist=watchlist_rows,
            episodes=episodes,
            episodes_by_mint=ep_by_mint,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
