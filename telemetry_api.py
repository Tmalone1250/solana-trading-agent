"""FastAPI Telemetry Server (telemetry_api.py) serving GET /api/telemetry on port 8001."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from config import AgentConfig, load_config
from db import ACTION_LOG_COLUMNS, AgentDatabase, utc_now
from helius_webhook import (
    fetch_helius_tracked_wallets,
    process_helius_webhook_payload,
    verify_helius_auth,
)


def create_telemetry_app(
    config: Optional[AgentConfig] = None,
    db: Optional[AgentDatabase] = None,
) -> FastAPI:
    """Create the FastAPI telemetry application."""
    cfg = config or load_config()
    database = db or AgentDatabase(
        db_path=cfg.DUCKDB_PATH,
        starting_sol=cfg.PAPER_STARTING_SOL,
    )

    app = FastAPI(
        title="solana-meme-agent Telemetry API",
        version="1.0.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/telemetry")
    async def get_telemetry() -> dict[str, Any]:
        st = database.get_state()
        pos = st.get("active_position")

        live_pnl: Optional[dict[str, Any]] = None
        active_mint: Optional[str] = None
        active_symbol: Optional[str] = None

        if isinstance(pos, dict):
            active_mint = pos.get("mint")
            active_symbol = pos.get("symbol")
            entry_sol = float(pos.get("entry_sol") or 0.0)
            current_sol = float(pos.get("current_sol_value") or 0.0)
            peak_sol = float(pos.get("peak_sol_value") or 0.0)
            pnl_sol = current_sol - entry_sol if entry_sol > 0 else 0.0
            pnl_pct = (pnl_sol / entry_sol) * 100.0 if entry_sol > 0 else 0.0
            live_pnl = {
                "strategy": str(pos.get("strategy") or st.get("strategy") or "SCALPER"),
                "triggering_wallets": list(pos.get("triggering_wallets") or st.get("triggering_wallets") or []),
                "entry_sol": round(entry_sol, 6),
                "current_sol_value": round(current_sol, 6),
                "peak_sol_value": round(peak_sol, 6),
                "pnl_sol": round(pnl_sol, 6),
                "pnl_pct": round(pnl_pct, 2),
                "breakeven_armed": bool(pos.get("breakeven_armed", False)),
                "trail_armed": bool(pos.get("trail_armed", False)),
                "retrace_low_sol": pos.get("retrace_low_sol"),
                "run_peak_sol": pos.get("run_peak_sol"),
                "watch_started_at": pos.get("watch_started_at"),
                "last_5m_buy_ratio": pos.get("last_5m_buy_ratio"),
                "last_5m_sell_ratio": pos.get("last_5m_sell_ratio"),
                "last_liquidity_usd": pos.get("last_liquidity_usd"),
            }

        daily_start = float(st.get("daily_start_sol") or cfg.PAPER_STARTING_SOL)
        daily_pnl = float(st.get("daily_pnl_sol") or 0.0)
        daily_loss_hit = daily_start > 0 and (-daily_pnl / daily_start) >= cfg.DAILY_MAX_LOSS_PCT

        recent_decisions = database.get_recent_decisions(limit=15)

        now_dt = utc_now()
        active_social = database.get_active_social_signals(
            ttl_minutes=cfg.X_SIGNAL_TTL_MINUTES,
            now=now_dt,
        )
        active_whales = database.get_active_whale_signals(
            ttl_minutes=cfg.WHALE_SIGNAL_TTL_MINUTES,
            min_buys=1,
            now=now_dt,
        )
        active_watchlist_payload = []
        for entry in database.get_watchlist():
            added_dt = datetime.fromisoformat(entry["added_at"])
            if added_dt.tzinfo is None:
                added_dt = added_dt.replace(tzinfo=timezone.utc)
            in_slot_sec = max(0.0, (now_dt - added_dt).total_seconds())
            cand = entry.get("candidate") or {}
            scores = entry.get("latest_scores") or {}
            scalper_ok = bool(entry.get("scalper_eligible", False))
            if scores.get("approved"):
                slot_status = "APPROVED"
            elif scores.get("action") == "BUY" and not scalper_ok:
                slot_status = "BLOCKED"
            elif not scalper_ok and (in_slot_sec / 60.0) < cfg.MIN_WATCHLIST_SOAK_MINUTES:
                slot_status = "SOAKING"
            elif not scalper_ok:
                slot_status = "BLOCKED"
            else:
                slot_status = "READY"
            sig = active_social.get(entry["mint"])
            wh = active_whales.get(entry["mint"])
            active_watchlist_payload.append(
                {
                    "mint": entry["mint"],
                    "symbol": entry["symbol"],
                    "pair_address": entry.get("pair_address") or "",
                    "added_at": entry["added_at"],
                    "safety_passed": bool(entry.get("safety_passed", False)),
                    "market_cap_usd": float(cand.get("market_cap_usd") or 0.0),
                    "liquidity_usd": float(cand.get("liquidity_usd") or 0.0),
                    "peak_mc": float(entry.get("peak_mc") or 0.0),
                    "low_mc": float(entry.get("low_mc") or 0.0),
                    "leg2_score": float(entry.get("leg2_score") or 0.0),
                    "leg2_breakdown": entry.get("leg2_breakdown"),
                    "vamp_strikes": int(entry.get("vamp_strikes") or 0),
                    "rsi_5m": entry.get("rsi_5m"),
                    "setup_type": entry.get("setup_type") or "NONE",
                    "scalper_eligible": scalper_ok,
                    "dist_from_1h_high_pct": entry.get("dist_from_1h_high_pct"),
                    "discovery_source": entry.get("discovery_source") or "DEXSCREENER",
                    "jup_organic_score": entry.get("jup_organic_score"),
                    "x_mentions": int(sig["mention_count"]) if sig else 0,
                    "whale_buys": int(wh["buy_count"]) if wh else 0,
                    "status": slot_status,
                    "time_in_slot_seconds": round(in_slot_sec, 1),
                    "time_in_slot_minutes": round(in_slot_sec / 60.0, 2),
                    "last_evaluated_at": entry["last_evaluated_at"],
                    "scores": entry["latest_scores"],
                }
            )

        tracked_wallets = await fetch_helius_tracked_wallets(cfg)
        recent_whale_events = [
            {**ev, "wallet_label": cfg.resolve_wallet_label(ev["wallet"])}
            for ev in database.get_recent_whale_events(limit=25)
        ]
        whale_roster = [
            {**row, "wallet_label": cfg.resolve_wallet_label(row["wallet"])}
            for row in database.get_whale_roster_stats(
                tracked_wallets, window_hours=24, now=now_dt
            )
        ]

        return {
            "focus_state": st["focus_state"],
            "execution_mode": cfg.EXECUTION_MODE,
            "hunting_lane": cfg.HUNTING_LANE,
            "active_mint": active_mint,
            "active_symbol": active_symbol,
            "active_watchlist": active_watchlist_payload,
            "x_discovery": {
                "enabled": cfg.ENABLE_X_DISCOVERY,
                "tweets_today": database.count_x_tweets_today(now=now_dt),
                "daily_cap": cfg.X_DAILY_TWEET_CAP,
                "active_signal_mints": len(active_social),
                "last_poll_utc": database.get_x_last_poll_utc(),
            },
            "whale_discovery": {
                "enabled": cfg.ENABLE_HELIUS_WHALE_WEBHOOK,
                "tracked_wallet_count": len(tracked_wallets),
                "active_whale_mints": len(active_whales),
                "events_today": database.count_whale_events_today(now=now_dt),
                "total_sol_volume_today": database.get_whale_sol_volume_today(now=now_dt),
                "active_signals": list(active_whales.values()),
                "recent_events": recent_whale_events,
                "roster": whale_roster,
            },
            "live_jupiter_pnl": live_pnl,
            "sol_balance": round(float(st["sol_balance"]), 6),
            "daily_pnl_sol": round(daily_pnl, 6),
            "daily_max_loss_hit": daily_loss_hit,
            "latest_decision_1_rationale": st.get("last_decision_rationale", ""),
            "decision_1_rationales": [
                {
                    "timestamp": d["timestamp"],
                    "mint": d["mint"],
                    "symbol": d["symbol"],
                    "phase": d["phase"],
                    "action": d["action"],
                    "confidence": d["confidence"],
                    "rationale": d["rationale"],
                }
                for d in recent_decisions
            ],
            "recent_trades": database.get_recent_closed_trades(limit=15),
            "active_cooldowns": database.get_active_cooldowns(),
            "updated_at": st["updated_at"],
        }

    @app.get("/api/export/actions.csv")
    def export_actions_csv(
        limit: int = Query(default=1000, ge=1, le=50_000),
        actions: Optional[str] = Query(default=None),
    ) -> Response:
        action_filter = (
            [a.strip().upper() for a in actions.split(",") if a.strip()]
            if actions
            else None
        )
        rows = database.get_action_log(limit=limit, actions=action_filter)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=ACTION_LOG_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in ACTION_LOG_COLUMNS})
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="solana_actions.csv"',
            },
        )

    @app.post("/api/webhooks/helius")
    async def helius_webhook(
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, Any]:
        if not verify_helius_auth(authorization, cfg.HELIUS_AUTH_HEADER):
            raise HTTPException(status_code=401, detail="Invalid Authorization header")
        try:
            payload = await request.json()
        except Exception:
            return {"status": "ok", "received": 0, "recorded": 0, "duplicates": 0, "ignored": 0}
        return process_helius_webhook_payload(payload, config=cfg, db=database)

    @app.post("/api/webhooks/helius/sync")
    async def helius_webhook_sync() -> dict[str, Any]:
        wallets = await fetch_helius_tracked_wallets(cfg, force_refresh=True)
        return {"synced_count": len(wallets), "wallets": wallets}

    return app


app = create_telemetry_app()
