"""FastAPI Telemetry Server (telemetry_api.py) serving GET /api/telemetry on port 8001."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from action_export import render_actions_csv, resolve_export_action_filter
from config import AgentConfig, load_config
from db import AgentDatabase, utc_now
from helius_webhook import (
    fetch_helius_tracked_wallets,
    process_helius_webhook_payload,
    verify_helius_auth,
)


def create_telemetry_app(
    config: Optional[AgentConfig] = None,
    db: Optional[AgentDatabase] = None,
    http_client: Optional[httpx.AsyncClient] = None,
) -> FastAPI:
    """Create the FastAPI telemetry application reusing a shared DB & HTTP client."""
    cfg = config or load_config()
    database = db or AgentDatabase(
        db_path=cfg.DUCKDB_PATH,
        starting_sol=cfg.PAPER_STARTING_SOL,
    )
    shared_http: dict[str, Optional[httpx.AsyncClient]] = {"client": http_client}

    def _get_http_client() -> httpx.AsyncClient:
        if shared_http["client"] is None or shared_http["client"].is_closed:
            shared_http["client"] = httpx.AsyncClient(
                follow_redirects=True,
                timeout=10.0,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return shared_http["client"]

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
        whale_pos = st.get("whale_position")
        raw_chart_list = list(st.get("chart_positions") or [])
        raw_whale_list = list(st.get("whale_positions") or [])
        if not raw_chart_list and isinstance(pos, dict) and st["focus_state"] != "SCANNING":
            raw_chart_list = [pos]
        if not raw_whale_list and isinstance(whale_pos, dict):
            raw_whale_list = [whale_pos]

        def _build_slot_pnl(p: dict[str, Any], slot_name: str) -> dict[str, Any]:
            e_sol = float(p.get("entry_sol") or 0.0)
            partial_taken = bool(p.get("partial_tp_taken", False))
            init_sol = float(p.get("initial_entry_sol") or e_sol)
            rem_basis = float(p.get("remaining_cost_basis_sol") or e_sol)
            basis = rem_basis if (partial_taken and rem_basis > 0) else e_sol
            c_sol = float(p.get("current_sol_value") or 0.0)
            pk_sol = float(p.get("peak_sol_value") or 0.0)
            p_sol = c_sol - basis if basis > 0 else 0.0
            p_pct = (p_sol / basis) * 100.0 if basis > 0 else 0.0
            return {
                "slot": slot_name,
                "mint": p.get("mint"),
                "symbol": p.get("symbol"),
                "strategy": str(p.get("strategy") or ("WHALE_SCALP" if slot_name == "whale_slot" else st.get("strategy") or "SCALPER")),
                "triggering_wallets": list(p.get("triggering_wallets") or []),
                "entry_sol": round(e_sol, 6),
                "initial_entry_sol": round(init_sol, 6),
                "remaining_cost_basis_sol": round(rem_basis, 6),
                "partial_tp_taken": partial_taken,
                "realized_partial_pnl_sol": round(float(p.get("realized_partial_pnl_sol") or 0.0), 6),
                "current_sol_value": round(c_sol, 6),
                "peak_sol_value": round(pk_sol, 6),
                "pnl_sol": round(p_sol, 6),
                "pnl_pct": round(p_pct, 2),
                "breakeven_armed": bool(p.get("breakeven_armed", False)),
                "trail_armed": bool(p.get("trail_armed", False)),
                "retrace_low_sol": p.get("retrace_low_sol"),
                "run_peak_sol": p.get("run_peak_sol"),
                "watch_started_at": p.get("watch_started_at"),
                "last_5m_buy_ratio": p.get("last_5m_buy_ratio"),
                "last_5m_sell_ratio": p.get("last_5m_sell_ratio"),
                "last_liquidity_usd": p.get("last_liquidity_usd"),
            }

        chart_live_positions = [_build_slot_pnl(p, "chart_slot") for p in raw_chart_list if isinstance(p, dict)]
        whale_live_positions = [_build_slot_pnl(p, "whale_slot") for p in raw_whale_list if isinstance(p, dict)]
        chart_live_pnl = chart_live_positions[0] if chart_live_positions else None
        whale_live_pnl = whale_live_positions[0] if whale_live_positions else None
        active_positions = chart_live_positions + whale_live_positions

        # Backward-compatible primary focus fields (Slot 1 primary; fallback to Slot 2 if Slot 1 idle)
        primary_pos = raw_chart_list[0] if raw_chart_list else (raw_whale_list[0] if raw_whale_list else None)
        live_pnl = chart_live_pnl or whale_live_pnl
        active_mint = primary_pos.get("mint") if isinstance(primary_pos, dict) else None
        active_symbol = primary_pos.get("symbol") if isinstance(primary_pos, dict) else None

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
                    "breakout_telemetry": scores.get("breakout_telemetry"),
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
        whale_decisions = database.get_whale_decisions()

        # Enrich active_signals (Confluence) with agent_status & agent_reason
        open_whale_pos = (
            whale_pos
            if isinstance(whale_pos, dict)
            else (
                pos
                if isinstance(pos, dict)
                and st["focus_state"] == "IN_POSITION"
                and str(pos.get("strategy") or st.get("strategy") or "").upper() == "WHALE_SCALP"
                else None
            )
        )
        open_whale_mint = open_whale_pos.get("mint") if isinstance(open_whale_pos, dict) else None
        open_whale_pnl = whale_live_pnl or live_pnl
        enriched_signals: list[dict[str, Any]] = []
        for sig in active_whales.values():
            dec_info = whale_decisions.get(sig["mint"])
            if open_whale_mint and sig["mint"] == open_whale_mint:
                a_status = "ENTERED"
                a_reason = "OPEN"
            elif dec_info:
                a_status = dec_info["status"]
                a_reason = dec_info["reason"]
            else:
                a_status = None
                a_reason = None
            enriched_signals.append(
                {
                    **sig,
                    "agent_status": a_status,
                    "agent_reason": a_reason,
                    "breakout_telemetry": dec_info.get("breakout_telemetry") if dec_info else None,
                }
            )

        # Build whale_trades (prepend currently open WHALE_SCALP position if live) + whale_stats
        closed_whale_trades = [
            {
                **t,
                "triggering_wallets": [
                    {"wallet": w, "label": cfg.resolve_wallet_label(w)}
                    for w in (t.get("triggering_wallets") or [])
                ],
            }
            for t in database.get_whale_trades(limit=25)
        ]
        total_closed = len(closed_whale_trades)
        wins = sum(1 for t in closed_whale_trades if float(t.get("pnl_sol") or 0.0) > 0)
        losses = total_closed - wins
        win_rate_pct = round((wins / total_closed) * 100.0, 1) if total_closed > 0 else 0.0
        total_whale_pnl = round(sum(float(t.get("pnl_sol") or 0.0) for t in closed_whale_trades), 6)
        holds = [float(t["hold_seconds"]) for t in closed_whale_trades if t.get("hold_seconds") is not None]
        avg_hold_sec = round(sum(holds) / len(holds), 1) if holds else 0.0

        whale_trades_payload: list[dict[str, Any]] = []
        for ow_pos, ow_pnl in zip(raw_whale_list, whale_live_positions):
            if not isinstance(ow_pos, dict):
                continue
            opened_str = ow_pos.get("opened_at")
            open_hold: Optional[float] = None
            if opened_str:
                try:
                    o_dt = datetime.fromisoformat(opened_str)
                    if o_dt.tzinfo is None:
                        o_dt = o_dt.replace(tzinfo=timezone.utc)
                    open_hold = round(max(0.0, (now_dt - o_dt).total_seconds()), 1)
                except ValueError:
                    open_hold = None
            whale_trades_payload.append(
                {
                    "status": "OPEN",
                    "symbol": ow_pos.get("symbol") or "UNKNOWN",
                    "mint": ow_pos.get("mint") or "",
                    "entry_sol": ow_pnl["entry_sol"],
                    "exit_sol": ow_pnl["current_sol_value"],
                    "pnl_sol": ow_pnl["pnl_sol"],
                    "pnl_pct": ow_pnl["pnl_pct"],
                    "partial_tp_taken": ow_pnl["partial_tp_taken"],
                    "realized_partial_pnl_sol": ow_pnl["realized_partial_pnl_sol"],
                    "exit_reason": "OPEN",
                    "opened_at": opened_str,
                    "closed_at": None,
                    "hold_seconds": open_hold,
                    "triggering_wallets": [
                        {"wallet": w, "label": cfg.resolve_wallet_label(w)}
                        for w in (ow_pnl.get("triggering_wallets") or [])
                    ],
                }
            )
        if not whale_trades_payload and open_whale_mint and isinstance(open_whale_pos, dict) and open_whale_pnl is not None:
            opened_str = open_whale_pos.get("opened_at")
            open_hold = None
            if opened_str:
                try:
                    o_dt = datetime.fromisoformat(opened_str)
                    if o_dt.tzinfo is None:
                        o_dt = o_dt.replace(tzinfo=timezone.utc)
                    open_hold = round(max(0.0, (now_dt - o_dt).total_seconds()), 1)
                except ValueError:
                    open_hold = None
            whale_trades_payload.append(
                {
                    "status": "OPEN",
                    "symbol": open_whale_pos.get("symbol") or "UNKNOWN",
                    "mint": open_whale_mint,
                    "entry_sol": open_whale_pnl["entry_sol"],
                    "exit_sol": open_whale_pnl["current_sol_value"],
                    "pnl_sol": open_whale_pnl["pnl_sol"],
                    "pnl_pct": open_whale_pnl["pnl_pct"],
                    "partial_tp_taken": open_whale_pnl.get("partial_tp_taken", False),
                    "realized_partial_pnl_sol": open_whale_pnl.get("realized_partial_pnl_sol", 0.0),
                    "exit_reason": "OPEN",
                    "opened_at": opened_str,
                    "closed_at": None,
                    "hold_seconds": open_hold,
                    "triggering_wallets": [
                        {"wallet": w, "label": cfg.resolve_wallet_label(w)}
                        for w in (open_whale_pnl.get("triggering_wallets") or [])
                    ],
                }
            )
        whale_trades_payload.extend(closed_whale_trades)

        return {
            "focus_state": st["focus_state"],
            "execution_mode": cfg.EXECUTION_MODE,
            "hunting_lane": cfg.HUNTING_LANE,
            "active_mint": active_mint,
            "active_symbol": active_symbol,
            "active_position": pos,
            "whale_position": whale_live_pnl,
            "chart_positions": chart_live_positions,
            "whale_positions": whale_live_positions,
            "active_positions": active_positions,
            "max_chart_positions": cfg.MAX_CHART_POSITIONS,
            "max_whale_positions": cfg.MAX_WHALE_POSITIONS,
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
                "active_signals": enriched_signals,
                "recent_events": recent_whale_events,
                "roster": whale_roster,
                "whale_trades": whale_trades_payload,
                "whale_stats": {
                    "total_trades": total_closed,
                    "wins": wins,
                    "losses": losses,
                    "win_rate_pct": win_rate_pct,
                    "total_pnl_sol": total_whale_pnl,
                    "avg_hold_seconds": avg_hold_sec,
                },
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
            "recent_trades": database.get_recent_closed_trades(limit=100),
            "closed_trades_summary": database.get_closed_trades_summary(),
            "active_cooldowns": database.get_active_cooldowns(),
            "updated_at": st["updated_at"],
        }

    def _build_csv_export(
        limit: int,
        export_type: Optional[str],
        actions: Optional[str],
    ) -> Response:
        action_filter = resolve_export_action_filter(export_type=export_type, actions_param=actions)
        rows = database.get_action_log(limit=limit, actions=action_filter)
        return Response(
            content=render_actions_csv(rows),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="solana_actions.csv"'},
        )

    @app.get("/api/export/csv")
    @app.get("/api/export/actions.csv")
    def export_actions_csv(
        limit: int = Query(default=1000, ge=1, le=50_000),
        export_type: Optional[str] = Query(default="all", alias="type"),
        actions: Optional[str] = Query(default=None),
    ) -> Response:
        return _build_csv_export(limit=limit, export_type=export_type, actions=actions)

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
        tracked_wallets = await fetch_helius_tracked_wallets(cfg)
        return process_helius_webhook_payload(
            payload,
            config=cfg,
            db=database,
            active_roster=set(tracked_wallets),
            strict_roster=True,
        )

    @app.post("/api/webhooks/helius/sync")
    async def helius_webhook_sync() -> dict[str, Any]:
        wallets = await fetch_helius_tracked_wallets(cfg, force_refresh=True)
        database.log_action(
            mode=cfg.EXECUTION_MODE,
            action="WHALE_DISCOVERY",
            symbol="ROSTER",
            asset_id="HELIUS_WEBHOOK",
            qty=float(len(wallets)),
            strategy="WHALE_DISCOVERY",
            reason=f"ROSTER_SYNC ({len(wallets)} wallets)",
            triggering_whales=",".join(cfg.resolve_wallet_label(w) for w in wallets[:10]),
        )
        return {"synced_count": len(wallets), "wallets": wallets}

    return app


_lazy_app: Optional[FastAPI] = None


def __getattr__(name: str) -> Any:
    """Lazily instantiate `app` only if accessed directly (prevents leaked DB on import)."""
    global _lazy_app
    if name == "app":
        if _lazy_app is None:
            _lazy_app = create_telemetry_app()
        return _lazy_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
