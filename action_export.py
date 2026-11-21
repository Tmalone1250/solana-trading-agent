"""Enriched full-board CSV export schema, type filters, and CSV formatter."""

from __future__ import annotations

import csv
import io
from typing import Any, Optional

ACTION_LOG_COLUMNS: list[str] = [
    "timestamp_utc",
    "bot",
    "mode",
    "action",
    "symbol",
    "asset_id",
    "qty",
    "notional",
    "unit",
    "price_or_value",
    "pnl_amount",
    "pnl_pct",
    "strategy",
    "reason",
    # Enriched full-board telemetry columns
    "sol_equity",
    "daily_realized_sol",
    "circuit_breaker",
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
    "triggering_whales",
    "whale_sol_flow",
    # Breakout Ignition telemetry columns
    "breakout_score",
    "breakout_state",
    "breakout_timeframe",
    "breakout_vol_ratio",
    "breakout_ema_ext_pct",
    "breakout_failed_gates",
]

ACTION_LOG_MIGRATIONS: list[tuple[str, str]] = [
    ("sol_equity", "DOUBLE"),
    ("daily_realized_sol", "DOUBLE"),
    ("circuit_breaker", "BOOLEAN"),
    ("mc_usd", "DOUBLE"),
    ("liq_usd", "DOUBLE"),
    ("soak_minutes", "DOUBLE"),
    ("rsi", "DOUBLE"),
    ("leg2_score", "DOUBLE"),
    ("narrative_score", "DOUBLE"),
    ("momentum_score", "DOUBLE"),
    ("vamp_score", "DOUBLE"),
    ("buy_confidence", "DOUBLE"),
    ("peak_pnl_pct", "DOUBLE"),
    ("hold_seconds", "DOUBLE"),
    ("triggering_whales", "VARCHAR"),
    ("whale_sol_flow", "DOUBLE"),
    ("breakout_score", "INTEGER"),
    ("breakout_state", "VARCHAR"),
    ("breakout_timeframe", "VARCHAR"),
    ("breakout_vol_ratio", "DOUBLE"),
    ("breakout_ema_ext_pct", "DOUBLE"),
    ("breakout_failed_gates", "VARCHAR"),
]

EXPORT_TYPE_ACTIONS: dict[str, Optional[list[str]]] = {
    "all": None,
    "trades": ["BUY", "SELL", "REENTRY_BUY"],
    "whales": ["WHALE_WEBHOOK", "WHALE_CONFLUENCE", "WHALE_DISCOVERY"],
    "watchlist": ["DECISION_1", "EVICT", "CIRCUIT_BREAKER"],
}


def resolve_export_action_filter(
    export_type: Optional[str] = None,
    actions_param: Optional[str] = None,
) -> Optional[list[str]]:
    """Resolve `?type=trades|whales|watchlist|all` and/or `?actions=...` into an action filter list."""
    if actions_param:
        parsed = [a.strip().upper() for a in actions_param.split(",") if a.strip()]
        if parsed:
            return parsed
    key = (export_type or "all").strip().lower()
    return EXPORT_TYPE_ACTIONS.get(key, None)


def render_actions_csv(rows: list[dict[str, Any]]) -> str:
    """Serialize action_log rows into CSV with all 30 enriched columns."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=ACTION_LOG_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {k: ("" if row.get(k) is None else row.get(k)) for k in ACTION_LOG_COLUMNS}
        )
    return buf.getvalue()
