"""Lightweight Shadow Outcome Tracker (outcome_tracker.py) using batched DexScreener polling."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from config import AgentConfig

logger = logging.getLogger(__name__)

MAX_TRACKED_OUTCOMES = 90
DEDUP_WINDOW_SECONDS = 900.0  # 15 minutes
FINALIZE_AFTER_SECONDS = 1800.0  # 30 minutes
OUTCOME_POLL_INTERVAL_SECONDS = 60.0

OUTCOME_COLUMNS: list[str] = [
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
    "whale_sol_flow",
    "triggering_wallet",
    "price_1m_pct",
    "price_5m_pct",
    "price_15m_pct",
    "price_30m_pct",
    "mfe_pct",
    "mae_pct",
    "finalized",
]

# Bounded in-memory active tracking map: outcome_id -> dict
_active_outcomes: dict[str, dict[str, Any]] = {}
# Dedup map: mint -> last_recorded_unix_ts
_recent_mint_timestamps: dict[str, float] = {}


def clear_outcome_tracker() -> None:
    """Clear in-memory shadow outcome tracking state (for tests)."""
    _active_outcomes.clear()
    _recent_mint_timestamps.clear()


def get_active_outcome_count() -> int:
    """Return current number of active tracked shadow outcomes in memory."""
    return len(_active_outcomes)


def ensure_outcomes_table(conn: Any) -> None:
    """Create the candidate_outcomes table in DuckDB if it does not exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_outcomes (
            id VARCHAR PRIMARY KEY,
            mint VARCHAR NOT NULL,
            symbol VARCHAR NOT NULL,
            source VARCHAR NOT NULL,
            discovered_at VARCHAR NOT NULL,
            decision VARCHAR NOT NULL,
            decision_reason VARCHAR,
            strategy VARCHAR,
            entry_price_usd DOUBLE,
            entry_mc_usd DOUBLE,
            entry_liq_usd DOUBLE,
            breakout_score INTEGER,
            breakout_state VARCHAR,
            leg2_score DOUBLE,
            vamp_score DOUBLE,
            rsi DOUBLE,
            whale_sol_flow DOUBLE,
            triggering_wallet VARCHAR,
            price_1m_pct DOUBLE,
            price_5m_pct DOUBLE,
            price_15m_pct DOUBLE,
            price_30m_pct DOUBLE,
            mfe_pct DOUBLE,
            mae_pct DOUBLE,
            finalized BOOLEAN NOT NULL DEFAULT FALSE
        )
        """
    )


def _flush_outcomes_to_db(db: Any, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with db._lock:
        for r in rows:
            db._conn.execute(
                """
                UPDATE candidate_outcomes
                SET entry_price_usd = ?, entry_mc_usd = ?, entry_liq_usd = ?,
                    price_1m_pct = ?, price_5m_pct = ?, price_15m_pct = ?, price_30m_pct = ?,
                    mfe_pct = ?, mae_pct = ?, finalized = ?
                WHERE id = ?
                """,
                [
                    r["entry_price_usd"],
                    r["entry_mc_usd"],
                    r["entry_liq_usd"],
                    r["price_1m_pct"],
                    r["price_5m_pct"],
                    r["price_15m_pct"],
                    r["price_30m_pct"],
                    r["mfe_pct"],
                    r["mae_pct"],
                    bool(r["finalized"]),
                    r["id"],
                ],
            )


def record_outcome_snapshot(
    db: Any,
    *,
    mint: str,
    symbol: str,
    source: str,
    decision: str,
    decision_reason: str = "",
    strategy: str = "",
    entry_price_usd: Optional[float] = None,
    entry_mc_usd: Optional[float] = None,
    entry_liq_usd: Optional[float] = None,
    breakout_score: Optional[int] = None,
    breakout_state: Optional[str] = None,
    leg2_score: Optional[float] = None,
    vamp_score: Optional[float] = None,
    rsi: Optional[float] = None,
    whale_sol_flow: Optional[float] = None,
    triggering_wallet: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Record a decision-time candidate snapshot with 15m per-mint dedup and 90-candidate memory cap."""
    clean_mint = (mint or "").strip()
    if not clean_mint:
        return None

    now_dt = now or datetime.now(timezone.utc)
    now_ts = now_dt.timestamp()

    # Prune expired dedup entries (> 15m) and check per-mint deduplication
    for m_key, m_ts in list(_recent_mint_timestamps.items()):
        if (now_ts - m_ts) >= DEDUP_WINDOW_SECONDS:
            _recent_mint_timestamps.pop(m_key, None)

    prev_ts = _recent_mint_timestamps.get(clean_mint)
    if prev_ts is not None and (now_ts - prev_ts) < DEDUP_WINDOW_SECONDS:
        return None

    # Enforce hard 90-candidate in-memory cap by evicting oldest tracked entry
    while len(_active_outcomes) >= MAX_TRACKED_OUTCOMES:
        oldest_id = min(_active_outcomes.keys(), key=lambda k: float(_active_outcomes[k]["discovered_ts"]))
        evicted = _active_outcomes.pop(oldest_id)
        evicted["finalized"] = True
        _flush_outcomes_to_db(db, [evicted])

    outcome_id = f"{clean_mint[:8]}-{int(now_ts)}-{uuid.uuid4().hex[:6]}"
    discovered_iso = now_dt.isoformat()
    e_price = float(entry_price_usd) if entry_price_usd is not None and float(entry_price_usd) > 0 else None
    e_mc = float(entry_mc_usd) if entry_mc_usd is not None and float(entry_mc_usd) > 0 else None
    e_liq = float(entry_liq_usd) if entry_liq_usd is not None and float(entry_liq_usd) > 0 else None

    row: dict[str, Any] = {
        "id": outcome_id,
        "mint": clean_mint,
        "symbol": (symbol or f"{clean_mint[:6]}..{clean_mint[-4:]}").strip(),
        "source": source.upper(),
        "discovered_at": discovered_iso,
        "discovered_ts": now_ts,
        "decision": decision.upper(),
        "decision_reason": (decision_reason or "")[:300],
        "strategy": strategy or "",
        "entry_price_usd": e_price,
        "entry_mc_usd": e_mc,
        "entry_liq_usd": e_liq,
        "breakout_score": int(breakout_score) if breakout_score is not None else None,
        "breakout_state": breakout_state,
        "leg2_score": round(float(leg2_score), 2) if leg2_score is not None else None,
        "vamp_score": round(float(vamp_score), 3) if vamp_score is not None else None,
        "rsi": round(float(rsi), 2) if rsi is not None else None,
        "whale_sol_flow": round(float(whale_sol_flow), 6) if whale_sol_flow is not None else None,
        "triggering_wallet": triggering_wallet,
        "price_1m_pct": None,
        "price_5m_pct": None,
        "price_15m_pct": None,
        "price_30m_pct": None,
        "mfe_pct": 0.0 if e_price is not None else None,
        "mae_pct": 0.0 if e_price is not None else None,
        "finalized": False,
    }

    with db._lock:
        ensure_outcomes_table(db._conn)
        db._conn.execute(
            f"INSERT INTO candidate_outcomes ({', '.join(OUTCOME_COLUMNS)}) VALUES ({', '.join('?' for _ in OUTCOME_COLUMNS)})",
            [row[c] for c in OUTCOME_COLUMNS],
        )

    _active_outcomes[outcome_id] = row
    _recent_mint_timestamps[clean_mint] = now_ts
    return outcome_id


async def poll_active_outcomes_once(
    db: Any,
    config: AgentConfig,
    client: httpx.AsyncClient,
    *,
    now: Optional[datetime] = None,
) -> dict[str, int]:
    """Batch-fetch current prices for active shadow outcomes via DexScreener (chunks of 30) and update MFE/MAE."""
    if not _active_outcomes:
        return {"polled": 0, "updated": 0, "finalized": 0}

    now_dt = now or datetime.now(timezone.utc)
    now_ts = now_dt.timestamp()
    unique_mints = list(dict.fromkeys(r["mint"] for r in _active_outcomes.values()))

    best_pair_by_mint: dict[str, dict[str, Any]] = {}
    for idx in range(0, len(unique_mints), 30):
        chunk = unique_mints[idx : idx + 30]
        url = f"{config.DEXSCREENER_BASE_URL}/tokens/v1/solana/{','.join(chunk)}"
        try:
            resp = await client.get(url, timeout=10.0)
            resp.raise_for_status()
            payload = resp.json()
            pairs = payload if isinstance(payload, list) else (payload.get("pairs") or [])
            for p in pairs:
                if not isinstance(p, dict) or str(p.get("chainId") or "").lower() != "solana":
                    continue
                m = str((p.get("baseToken") or {}).get("address") or "").strip()
                if not m:
                    continue
                liq = float((p.get("liquidity") or {}).get("usd") or 0.0)
                prev_liq = float((best_pair_by_mint.get(m, {}).get("liquidity") or {}).get("usd") or 0.0)
                if m not in best_pair_by_mint or liq > prev_liq:
                    best_pair_by_mint[m] = p
        except Exception as exc:
            logger.warning("[OutcomeTracker] DexScreener batch fetch failed: %s", exc)

    to_flush: list[dict[str, Any]] = []
    finalized_ids: list[str] = []

    for oid, rec in list(_active_outcomes.items()):
        elapsed = max(0.0, now_ts - float(rec["discovered_ts"]))
        pair = best_pair_by_mint.get(rec["mint"])
        if isinstance(pair, dict):
            curr_price = float(pair.get("priceUsd") or 0.0)
            curr_mc = float(pair.get("marketCap") or pair.get("fdv") or 0.0)
            curr_liq = float((pair.get("liquidity") or {}).get("usd") or 0.0)
            if (rec["entry_price_usd"] is None or rec["entry_price_usd"] <= 0) and curr_price > 0:
                rec["entry_price_usd"] = curr_price
                if rec["entry_mc_usd"] is None and curr_mc > 0:
                    rec["entry_mc_usd"] = curr_mc
                if rec["entry_liq_usd"] is None and curr_liq > 0:
                    rec["entry_liq_usd"] = curr_liq
                rec["mfe_pct"] = 0.0
                rec["mae_pct"] = 0.0

            entry_p = float(rec["entry_price_usd"] or 0.0)
            if entry_p > 0 and curr_price > 0:
                pct = round(((curr_price - entry_p) / entry_p) * 100.0, 2)
                rec["mfe_pct"] = round(max(float(rec["mfe_pct"] if rec["mfe_pct"] is not None else pct), pct), 2)
                rec["mae_pct"] = round(min(float(rec["mae_pct"] if rec["mae_pct"] is not None else pct), pct), 2)
                if elapsed >= 60.0 and rec["price_1m_pct"] is None:
                    rec["price_1m_pct"] = pct
                if elapsed >= 300.0 and rec["price_5m_pct"] is None:
                    rec["price_5m_pct"] = pct
                if elapsed >= 900.0 and rec["price_15m_pct"] is None:
                    rec["price_15m_pct"] = pct
                if elapsed >= FINALIZE_AFTER_SECONDS and rec["price_30m_pct"] is None:
                    rec["price_30m_pct"] = pct

        if elapsed >= FINALIZE_AFTER_SECONDS:
            rec["finalized"] = True
            finalized_ids.append(oid)
        to_flush.append(rec)

    _flush_outcomes_to_db(db, to_flush)
    for oid in finalized_ids:
        _active_outcomes.pop(oid, None)

    return {"polled": len(unique_mints), "updated": len(to_flush), "finalized": len(finalized_ids)}


def get_recent_outcomes(db: Any, limit: int = 2000) -> list[dict[str, Any]]:
    """Query the latest N rows from candidate_outcomes newest-first."""
    cols_sql = ", ".join(OUTCOME_COLUMNS)
    with db._lock:
        ensure_outcomes_table(db._conn)
        rows = db._conn.execute(
            f"SELECT {cols_sql} FROM candidate_outcomes ORDER BY discovered_at DESC LIMIT ?",
            [int(limit)],
        ).fetchall()
    return [dict(zip(OUTCOME_COLUMNS, r)) for r in rows]


def render_outcomes_csv(db: Any, limit: int = 2000) -> str:
    """Serialize candidate_outcomes rows into CSV text."""
    rows = get_recent_outcomes(db, limit=limit)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=OUTCOME_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in OUTCOME_COLUMNS})
    return buf.getvalue()


async def run_outcome_tracker_loop(
    config: AgentConfig,
    db: Any,
    *,
    http_client: Optional[httpx.AsyncClient] = None,
) -> None:
    """Lightweight 60s background loop polling DexScreener in batches of 30 for shadow outcomes."""
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(follow_redirects=True, timeout=15.0)
    try:
        while True:
            await asyncio.sleep(OUTCOME_POLL_INTERVAL_SECONDS)
            try:
                await poll_active_outcomes_once(db, config, client)
            except Exception as exc:
                logger.warning("[OutcomeTracker] Poll cycle error: %s", exc)
    finally:
        if owns_client:
            await client.aclose()
