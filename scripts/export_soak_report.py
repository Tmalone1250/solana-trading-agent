#!/usr/bin/env python3
"""Read-Only Repo-Native DuckDB & CSV Soak Audit Report Exporter (scripts/export_soak_report.py)."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import duckdb

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.soak_report_sections import (  # noqa: E402
    fmt_num,
    parse_iso_utc,
    render_decisions_section,
    render_outcomes_and_pipeline_sections,
    render_trades_section,
    safe_json,
)

DEFAULT_SINCE_UTC = "2026-11-25T20:51:57Z"
DEFAULT_OUT_PATH = "./soak_audit_report.md"

KNOWN_TABLES: list[tuple[str, Optional[str]]] = [
    ("agent_state", "updated_at"),
    ("active_watchlist", "added_at"),
    ("cooldowns", "expires_at"),
    ("trades", "timestamp"),
    ("decisions_log", "timestamp"),
    ("action_log", "timestamp_utc"),
    ("candidate_outcomes", "discovered_at"),
    ("whale_events", "timestamp_utc"),
    ("whale_signals", "last_seen_utc"),
    ("whale_decisions", "updated_at"),
    ("social_signals", "last_seen_utc"),
    ("x_seen_tweets", "fetched_at_utc"),
    ("x_worker_state", "last_poll_utc"),
]


def _raise_csv_field_limit() -> None:
    """Safely raise csv.field_size_limit without OverflowError on 32/64-bit platforms."""
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit = int(limit // 10)


def load_tables_from_csv_dir(csv_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[str]], set[str]]:
    _raise_csv_field_limit()
    tables: dict[str, list[dict[str, Any]]] = {}
    columns: dict[str, list[str]] = {}
    present: set[str] = set()
    for table_name, _ in KNOWN_TABLES:
        csv_path = csv_dir / f"{table_name}.csv"
        if not csv_path.is_file():
            tables[table_name] = []
            columns[table_name] = []
            continue
        present.add(table_name)
        with csv_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            cols = list(reader.fieldnames or [])
            rows = [dict(row) for row in reader]
        tables[table_name] = rows
        columns[table_name] = cols
    return tables, columns, present


def load_tables_from_duckdb_snapshot(db_path: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[str]], set[str]]:
    """Copy live .duckdb (+ .duckdb.wal if present) to /tmp before opening read-only; clean up in finally."""
    if not db_path.is_file():
        raise FileNotFoundError(f"DuckDB file not found: {db_path}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="soak_audit_snap_"))
    try:
        snap_db = tmp_dir / db_path.name
        shutil.copy2(db_path, snap_db)
        wal_path = Path(f"{db_path}.wal")
        if wal_path.is_file():
            shutil.copy2(wal_path, Path(f"{snap_db}.wal"))

        tables: dict[str, list[dict[str, Any]]] = {}
        columns: dict[str, list[str]] = {}
        present: set[str] = set()
        conn = duckdb.connect(str(snap_db), read_only=True)
        try:
            existing = {
                str(r[0])
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
                ).fetchall()
            }
            for table_name, _ in KNOWN_TABLES:
                if table_name not in existing:
                    tables[table_name] = []
                    columns[table_name] = []
                    continue
                present.add(table_name)
                cur = conn.execute(f"SELECT * FROM {table_name}")
                cols = [d[0] for d in (cur.description or [])]
                raw_rows = cur.fetchall()
                rows: list[dict[str, Any]] = []
                for raw in raw_rows:
                    rows.append({col: ("" if val is None else val) for col, val in zip(cols, raw)})
                tables[table_name] = rows
                columns[table_name] = cols
        finally:
            conn.close()
        return tables, columns, present
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def split_by_since(
    rows: list[dict[str, Any]],
    ts_col: Optional[str],
    since_dt: datetime,
    fallback_col: Optional[str] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Optional[datetime], Optional[datetime]]:
    pre: list[dict[str, Any]] = []
    soak: list[dict[str, Any]] = []
    earliest: Optional[datetime] = None
    latest: Optional[datetime] = None
    for r in rows:
        raw_ts = r.get(ts_col) if ts_col else None
        if not raw_ts and fallback_col:
            raw_ts = r.get(fallback_col)
        dt = parse_iso_utc(str(raw_ts)) if raw_ts is not None else None
        if dt is not None:
            if earliest is None or dt < earliest:
                earliest = dt
            if latest is None or dt > latest:
                latest = dt
            if dt >= since_dt:
                soak.append(r)
            else:
                pre.append(r)
        else:
            pre.append(r)
    return pre, soak, earliest, latest


def extract_open_mints(agent_state_rows: list[dict[str, Any]]) -> set[str]:
    open_mints: set[str] = set()
    if not agent_state_rows:
        return open_mints
    st = agent_state_rows[0]
    for col in ("chart_positions_json", "whale_positions_json"):
        raw = st.get(col)
        if isinstance(raw, str) and raw.strip():
            try:
                arr = json.loads(raw) if "json" in globals() else []
            except Exception:
                arr = []
            if isinstance(arr, list):
                for p in arr:
                    if isinstance(p, dict) and p.get("mint") and int(p.get("token_amount") or 0) > 0:
                        open_mints.add(str(p["mint"]))
    for col in ("active_position_json", "whale_position_json"):
        p = safe_json(st.get(col))
        if p.get("mint") and int(p.get("token_amount") or 0) > 0:
            open_mints.add(str(p["mint"]))
    return open_mints


def generate_soak_report(
    *,
    csv_dir: Optional[Path] = None,
    db_path: Optional[Path] = None,
    since_str: str = DEFAULT_SINCE_UTC,
    out_path: Optional[Path] = None,
) -> str:
    if bool(csv_dir) == bool(db_path):
        raise ValueError("Specify exactly one of --csv-dir or --db")

    since_dt = parse_iso_utc(since_str)
    if since_dt is None:
        raise ValueError(f"Invalid ISO-8601 --since timestamp: {since_str}")

    if csv_dir is not None:
        tables, columns, present = load_tables_from_csv_dir(csv_dir.expanduser().resolve())
        source_desc = f"CSV Directory (`{csv_dir}`)"
    else:
        assert db_path is not None
        tables, columns, present = load_tables_from_duckdb_snapshot(db_path.expanduser().resolve())
        source_desc = f"DuckDB Snapshot (`{db_path}` via `/tmp` copy)"

    splits: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]], Optional[datetime], Optional[datetime]]] = {}
    global_latest: Optional[datetime] = None
    for t_name, ts_col in KNOWN_TABLES:
        fb = "last_evaluated_at" if t_name == "active_watchlist" else ("opened_at" if t_name == "trades" else None)
        pre, soak, e_dt, l_dt = split_by_since(tables.get(t_name, []), ts_col, since_dt, fallback_col=fb)
        splits[t_name] = (pre, soak, e_dt, l_dt)
        if l_dt is not None and (global_latest is None or l_dt > global_latest):
            global_latest = l_dt

    elapsed_sec = max(0.0, (global_latest - since_dt).total_seconds()) if global_latest and global_latest > since_dt else 0.0
    hrs = int(elapsed_sec // 3600)
    mins = int((elapsed_sec % 3600) // 60)
    open_mints = extract_open_mints(tables.get("agent_state", []))

    sec3_md, soak_trade_kpis = render_trades_section(
        splits["trades"][0], splits["trades"][1], tables.get("action_log", []), open_mints
    )
    sec4_md, soak_dec_kpis = render_decisions_section(
        splits["decisions_log"][0], splits["decisions_log"][1]
    )
    sec5_to_8_md = render_outcomes_and_pipeline_sections(
        splits["candidate_outcomes"][0],
        splits["candidate_outcomes"][1],
        splits["action_log"][0],
        splits["action_log"][1],
        splits["whale_events"][1],
        tables.get("whale_events", []),
        tables.get("whale_signals", []),
        tables.get("whale_decisions", []),
        tables,
        since_dt,
    )

    gen_utc = datetime.now(timezone.utc).isoformat()
    soak_whale_cnt = len(splits["whale_events"][1])
    header_lines = [
        "# Solana Trading Agent — Post-Soak Telemetry & Audit Report",
        "",
        "## 1. Header & Executive Summary",
        "",
        f"- **Generated At (UTC):** `{gen_utc}`",
        f"- **Soak Cutoff (`--since`):** `{since_dt.isoformat()}`",
        f"- **Latest Telemetry Timestamp (UTC):** `{global_latest.isoformat() if global_latest else '—'}`",
        f"- **Active Soak Duration Elapsed:** **{hrs}h {mins}m** (`{elapsed_sec / 3600.0:.2f}` hours)",
        f"- **Input Source:** {source_desc}",
        "",
        "### Headline Active Soak KPIs",
        "| KPI | Active Soak Value |",
        "| :--- | ---: |",
        f"| Soak Trade Events (`BUY` / Exits) | {soak_trade_kpis['total_rows']} ({soak_trade_kpis['buys']} Entries / {soak_trade_kpis['closed']} Exits) |",
        f"| Soak Win Rate (Closed/Partial Exits) | {soak_trade_kpis['win_rate']:.1f}% ({soak_trade_kpis['wins']}/{soak_trade_kpis['closed']}) |",
        f"| Soak Net Realized PnL (SOL) | {fmt_num(soak_trade_kpis['total_pnl_sol'], 4, True)} SOL |",
        f"| Non-NONE Setups / Breakouts Triggered | {soak_dec_kpis['breakout_count']} |",
        f"| Unique Tokens Evaluated by Alpha | {soak_dec_kpis['unique_tokens']} |",
        f"| Active Soak Whale Events Recorded | {soak_whale_cnt} |",
        "",
        "## 2. Table Inventory & Schema Overview",
        "",
        "| Table Name | Present | Total Rows | Pre-Soak Rows | Active Soak Rows | Earliest Timestamp (UTC) | Latest Timestamp (UTC) | Columns |",
        "| :--- | :--- | ---: | ---: | ---: | :--- | :--- | :--- |",
    ]
    for t_name, _ in KNOWN_TABLES:
        pre, soak, e_dt, l_dt = splits[t_name]
        tot = len(tables.get(t_name, []))
        cols = columns.get(t_name, [])
        col_str = ", ".join(f"`{c}`" for c in cols) if cols else "*(missing/empty)*"
        header_lines.append(
            f"| `{t_name}` | `{'YES' if t_name in present else 'NO'}` | {tot} | {len(pre)} | {len(soak)} | `{e_dt.isoformat() if e_dt else '—'}` | `{l_dt.isoformat() if l_dt else '—'}` | {col_str} |"
        )
    header_lines.append("")

    full_report = "\n".join(header_lines) + "\n" + sec3_md + "\n" + sec4_md + "\n" + sec5_to_8_md
    if out_path is not None:
        out_resolved = out_path.expanduser().resolve()
        out_resolved.parent.mkdir(parents=True, exist_ok=True)
        out_resolved.write_text(full_report, encoding="utf-8")
    return full_report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Export exhaustive Markdown soak audit report from DuckDB or CSVs.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--csv-dir", type=Path, help="Directory containing exported DuckDB CSVs.")
    group.add_argument("--db", type=Path, help="Path to live DuckDB file (safely snapshotted to /tmp with .wal).")
    parser.add_argument("--since", type=str, default=DEFAULT_SINCE_UTC, help=f"ISO-8601 UTC soak start cutoff (default: {DEFAULT_SINCE_UTC}).")
    parser.add_argument("--out", type=Path, default=Path(DEFAULT_OUT_PATH), help=f"Output Markdown file path (default: {DEFAULT_OUT_PATH}).")
    args = parser.parse_args(argv)
    generate_soak_report(csv_dir=args.csv_dir, db_path=args.db, since_str=args.since, out_path=args.out)
    print(f"[SoakReport] Wrote report to {args.out.expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
