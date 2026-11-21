"""Slot 2 Concurrent Whale Lane (whale_lane.py): independent WHALE_SCALP entry & 6-layer exit."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from batched_price_monitor import (
    estimate_position_sol_value,
    fetch_batched_sol_prices,
    resolve_position_sol_value,
)
from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase, utc_now
from executor_node import executor_node
from jupiter_token_client import enrich_mints
from ohlcv_client import fetch_ohlcv_1m_batch, fetch_ohlcv_batch
from safety_gate_node import check_token_safety
from scalper_entry_engine import evaluate_scalper_entry
from state import ActivePosition, FocusState, PendingOrder

logger = logging.getLogger(__name__)
EPS = 1e-9


def _fmt_usd(v: float) -> str:
    return f"${v / 1_000_000:.0f}M" if v >= 1_000_000 else f"${v / 1_000:.0f}K"


def evaluate_whale_exit_order(
    pos: ActivePosition,
    *,
    current_sol_value: float,
    config: AgentConfig,
    db: AgentDatabase,
    now_dt: datetime,
) -> tuple[Optional[PendingOrder], str]:
    """Evaluate the 6-layer WHALE_SCALP exit engine (Shadow, TP, 2-Tier Trail, BE Shield, Stop, Time)."""
    if current_sol_value > pos.peak_sol_value:
        pos.peak_sol_value = current_sol_value

    pnl_pct = (current_sol_value - pos.entry_sol) / pos.entry_sol if pos.entry_sol > 0 else 0.0
    peak_pnl_pct = (pos.peak_sol_value - pos.entry_sol) / pos.entry_sol if pos.entry_sol > 0 else 0.0
    pnl_pct_100 = pnl_pct * 100.0
    peak_pnl_pct_100 = peak_pnl_pct * 100.0

    if peak_pnl_pct_100 + EPS >= config.WHALE_BREAKEVEN_ARM_PCT:
        pos.breakeven_armed = True
    if peak_pnl_pct_100 + EPS >= config.WHALE_TRAIL_ARM_PCT:
        pos.trail_armed = True

    def _sell(reason: str, rationale: str) -> tuple[PendingOrder, str]:
        return (
            PendingOrder(
                side="SELL",
                mint=pos.mint,
                symbol=pos.symbol,
                amount_atomic=pos.token_amount,
                reason=reason,
                target_state_after_fill=FocusState.SCANNING,
                apply_cooldown=True,
                strategy="WHALE_SCALP",
                triggering_wallets=list(pos.triggering_wallets),
                entry_mc_usd=pos.entry_mc_usd,
                whale_sol_flow=pos.whale_sol_flow,
                rationale=rationale,
            ),
            reason,
        )

    # 1. Whale Shadow Exit: triggering whale sold this mint
    db_shadow_flag = bool(db.get_state().get("whale_shadow_exit_requested", False))
    if pos.whale_shadow_exit_flag or db_shadow_flag:
        return _sell(
            "WHALE_SHADOW_EXIT",
            f"[WHALE_SCALP EXIT] {pos.symbol}: WHALE_SHADOW_EXIT (triggering whale sold)",
        )

    # 2. Front-Run Hard TP (+15%)
    if pnl_pct_100 + EPS >= config.WHALE_TP_PCT:
        return _sell(
            "WHALE_FRONT_RUN_TP",
            f"[WHALE_SCALP TP] {pos.symbol}: WHALE_FRONT_RUN_TP ({pnl_pct_100:+.2f}% >= +{config.WHALE_TP_PCT:.0f}%)",
        )

    # 3. 2-Tier Peak Drawdown Trail (+8%/-3.5% -> +12%/-2.5%)
    if pos.trail_armed and pos.peak_sol_value > 0:
        active_drop_limit = (
            config.WHALE_TIGHT_TRAIL_DROP_PCT
            if peak_pnl_pct_100 + EPS >= config.WHALE_TIGHT_TRAIL_ARM_PCT
            else config.WHALE_TRAIL_DROP_PCT
        )
        drop_from_peak_100 = ((pos.peak_sol_value - current_sol_value) / pos.peak_sol_value) * 100.0
        if drop_from_peak_100 + EPS >= active_drop_limit:
            return _sell(
                "WHALE_PEAK_TRAIL",
                f"[WHALE_SCALP TRAIL] {pos.symbol}: WHALE_PEAK_TRAIL "
                f"(peak {peak_pnl_pct_100:+.2f}%, drop -{drop_from_peak_100:.2f}% >= -{active_drop_limit:.1f}%)",
            )

    # 4. Whale Breakeven Shield (+5% arm -> +1% floor)
    if pos.breakeven_armed and pnl_pct_100 <= config.WHALE_BREAKEVEN_FLOOR_PCT + EPS:
        return _sell(
            "WHALE_BREAKEVEN_SHIELD",
            f"[WHALE_SCALP BE] {pos.symbol}: WHALE_BREAKEVEN_SHIELD "
            f"(armed >= +{config.WHALE_BREAKEVEN_ARM_PCT:.0f}%, hit floor {pnl_pct_100:+.2f}% <= +{config.WHALE_BREAKEVEN_FLOOR_PCT:.0f}%)",
        )

    # 5. Hard Stop (-10%)
    if pnl_pct_100 <= config.WHALE_HARD_STOP_PCT + EPS:
        return _sell(
            "WHALE_HARD_STOP",
            f"[WHALE_SCALP STOP] {pos.symbol}: WHALE_HARD_STOP ({pnl_pct_100:+.2f}% <= {config.WHALE_HARD_STOP_PCT:.0f}%)",
        )

    # 6. Configurable Time Stop (.env WHALE_TIME_STOP_SECONDS, default 300s)
    if pos.opened_at:
        opened_dt = datetime.fromisoformat(pos.opened_at)
        if opened_dt.tzinfo is None:
            opened_dt = opened_dt.replace(tzinfo=timezone.utc)
        held_sec = (now_dt - opened_dt).total_seconds()
        if (
            held_sec + EPS >= config.WHALE_TIME_STOP_SECONDS
            and pnl_pct_100 + EPS < config.WHALE_TIME_STOP_MIN_PNL_PCT
        ):
            return _sell(
                "WHALE_TIME_STOP",
                f"[WHALE_SCALP TIME] {pos.symbol}: WHALE_TIME_STOP "
                f"(held {held_sec:.0f}s >= {config.WHALE_TIME_STOP_SECONDS}s, "
                f"PnL {pnl_pct_100:+.2f}% < +{config.WHALE_TIME_STOP_MIN_PNL_PCT:.0f}%)",
            )

    return None, "HOLD_WHALE_SCALP"


async def _fetch_best_pair_for_mints(
    mints: list[str],
    config: AgentConfig,
    client: httpx.AsyncClient,
) -> dict[str, dict[str, Any]]:
    """Fetch highest-liquidity Solana DexScreener pair per mint."""
    best: dict[str, dict[str, Any]] = {}
    for idx in range(0, len(mints), 30):
        chunk = mints[idx : idx + 30]
        url = f"{config.DEXSCREENER_BASE_URL}/tokens/v1/solana/{','.join(chunk)}"
        try:
            resp = await client.get(url, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            pairs = data if isinstance(data, list) else (data.get("pairs") or [])
        except Exception as exc:
            logger.warning("[WhaleLane] DexScreener lookup failed: %s", exc)
            continue
        for p in pairs:
            if not isinstance(p, dict) or str(p.get("chainId") or "").lower() != "solana":
                continue
            mint = str((p.get("baseToken") or {}).get("address") or "").strip()
            if not mint:
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0.0)
            prev_liq = float((best.get(mint, {}).get("liquidity") or {}).get("usd") or 0.0)
            if mint not in best or liq > prev_liq:
                best[mint] = p
    return best


async def run_whale_lane_tick(
    *,
    config: AgentConfig,
    db: AgentDatabase,
    http_client: Optional[httpx.AsyncClient] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Execute one tick of the Whale Lane (up to MAX_WHALE_POSITIONS=3): batched monitor + breakout-gated entry."""
    if not config.WHALE_STRATEGY_ENABLED:
        return {"slot": "whale_slot", "event": "DISABLED"}

    now_dt = now or utc_now()
    st = db.get_state()
    sol_balance = float(st["sol_balance"])
    daily_start = float(st["daily_start_sol"])
    daily_pnl = float(st["daily_pnl_sol"])
    whale_positions: list[dict[str, Any]] = list(st.get("whale_positions") or [])
    chart_positions: list[dict[str, Any]] = list(st.get("chart_positions") or [])
    chart_pos_raw = st.get("active_position")

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(follow_redirects=True)
    try:
        # Step 1: Monitor ALL open whale positions via 1 batched price fetch + on-demand quote near thresholds
        if whale_positions:
            open_mints = [str(p["mint"]) for p in whale_positions if isinstance(p, dict) and p.get("mint")]
            sol_prices, _ = await fetch_batched_sol_prices(open_mints, config, client, now_ts=now_dt.timestamp())
            surviving: list[dict[str, Any]] = []
            for raw_p in whale_positions:
                pos = ActivePosition.model_validate(raw_p)
                est_val = estimate_position_sol_value(pos, sol_prices.get(pos.mint))
                curr_val, _used_q = await resolve_position_sol_value(
                    pos, estimated_sol_value=est_val, config=config, client=client, now_dt=now_dt
                )
                pos.current_sol_value = curr_val
                exit_order, _ev = evaluate_whale_exit_order(
                    pos, current_sol_value=curr_val, config=config, db=db, now_dt=now_dt
                )
                if exit_order is None:
                    surviving.append(pos.model_dump())
                else:
                    ex = await executor_node(
                        {
                            "focus_state": st["focus_state"],
                            "sol_balance": sol_balance,
                            "daily_pnl_sol": daily_pnl,
                            "active_position": chart_pos_raw,
                            "chart_positions": chart_positions,
                            "whale_position": pos.model_dump(),
                            "whale_positions": surviving + [pos.model_dump()],
                            "pending_order": exit_order.model_dump(),
                            "slot": "whale_slot",
                        },
                        config=config,
                        db=db,
                        http_client=client,
                    )
                    sol_balance = float(ex.get("sol_balance", sol_balance))
                    daily_pnl = float(ex.get("daily_pnl_sol", daily_pnl))
            whale_positions = surviving
            db.save_lane_positions(
                lane="whale",
                positions=whale_positions,
                sol_balance=sol_balance,
                daily_pnl_sol=daily_pnl,
            )

        # Step 2: Check capacity (MAX_WHALE_POSITIONS=3, MAX_OPEN_POSITIONS=6), circuit breaker & free SOL
        total_open = len(chart_positions) + len(whale_positions)
        if len(whale_positions) >= config.MAX_WHALE_POSITIONS or total_open >= config.MAX_OPEN_POSITIONS:
            return {"slot": "whale_slot", "event": "WHALE_LANE_FULL", "whale_positions": whale_positions}
        if daily_start > 0 and (-daily_pnl / daily_start) >= config.DAILY_MAX_LOSS_PCT:
            return {"slot": "whale_slot", "event": "CIRCUIT_BREAKER_HALTED", "whale_positions": whale_positions}
        if sol_balance < config.WHALE_POSITION_SOL:
            return {"slot": "whale_slot", "event": "LOW_SOL_BALANCE", "whale_positions": whale_positions}

        min_unique = config.whale_min_unique_wallets()
        min_liq = config.whale_min_liquidity_usd()
        triggers = db.get_whale_fast_track_triggers(
            confluence_window_minutes=5,
            max_age_seconds=config.WHALE_ENTRY_MAX_AGE_SECONDS,
            min_confluence_wallets=min_unique,
            min_solo_sol=config.WHALE_MIN_SOLO_BUY_SOL,
            now=now_dt,
        )
        if not triggers:
            return {"slot": "whale_slot", "event": "NO_QUALIFYING_CONFLUENCE", "whale_positions": whale_positions}

        mints = [t["mint"] for t in triggers]
        pairs_by_mint = await _fetch_best_pair_for_mints(mints, config, client)
        jup_meta = (
            await enrich_mints(mints, config, client, now_ts=now_dt.timestamp())
            if config.ENABLE_JUPITER_DISCOVERY
            else {}
        )
        # Fetch 5m OHLCV (+ 1m fallback when < 12 5m bars) for Breakout Meter & Whale Breakout Gate
        pair_addrs = [str(p.get("pairAddress") or "") for p in pairs_by_mint.values() if p.get("pairAddress")]
        ohlcv_5m = await fetch_ohlcv_batch(pair_addrs, config, client, now_ts=now_dt.timestamp()) if pair_addrs else {}
        short_addrs = [a for a in pair_addrs if len(ohlcv_5m.get(a) or []) < 12]
        ohlcv_1m = await fetch_ohlcv_1m_batch(short_addrs, config, client, now_ts=now_dt.timestamp()) if short_addrs else {}

        open_mints = db.get_all_open_mints()
        for trig in triggers:
            mint = trig["mint"]
            if db.is_on_cooldown(mint, now=now_dt):
                db.upsert_whale_decision(mint, "SKIPPED", "COOLDOWN", updated_at=now_dt.isoformat())
                continue
            if mint in open_mints:
                db.upsert_whale_decision(mint, "SKIPPED", "DUPLICATE_MINT_IN_CHART_SLOT", updated_at=now_dt.isoformat())
                continue

            pair = pairs_by_mint.get(mint)
            if not pair:
                continue
            symbol = str((pair.get("baseToken") or {}).get("symbol") or f"{mint[:6]}..{mint[-4:]}")
            liq_usd = float((pair.get("liquidity") or {}).get("usd") or 0.0)
            mc_usd = float(pair.get("marketCap") or pair.get("fdv") or 0.0)
            p_addr = str(pair.get("pairAddress") or "")

            # Compute shared breakout_telemetry (5m or 1m_fallback) for every Whale Radar candidate
            c5 = pair.get("candles_5m") if isinstance(pair.get("candles_5m"), list) else ohlcv_5m.get(p_addr, [])
            c1 = pair.get("candles_1m") if isinstance(pair.get("candles_1m"), list) else ohlcv_1m.get(p_addr, [])
            sc_res = evaluate_scalper_entry(c5, pair, time_in_slot_min=5.0, config=config, candles_1m=c1)
            bt = sc_res.breakout_telemetry or {}
            bt_score = int(bt.get("score") or 0)
            bt_state = str(bt.get("state") or "FORMING")
            bt_failed_str = "; ".join(bt.get("failed_gates") or []) if bt else ""
            bt_log_kwargs: dict[str, Any] = {
                "breakout_score": bt.get("score"),
                "breakout_state": bt.get("state"),
                "breakout_timeframe": bt.get("timeframe"),
                "breakout_vol_ratio": bt.get("vol_ratio"),
                "breakout_ema_ext_pct": bt.get("ema_ext_pct"),
                "breakout_failed_gates": bt_failed_str or None,
            }

            if (jup_meta.get(mint) or {}).get("is_sus") is True:
                db.upsert_whale_decision(mint, "SKIPPED", "AUDIT_SUS", updated_at=now_dt.isoformat(), breakout_telemetry=bt)
                continue
            if liq_usd < min_liq:
                db.upsert_whale_decision(mint, "SKIPPED", f"LIQ < {_fmt_usd(min_liq)}", updated_at=now_dt.isoformat(), breakout_telemetry=bt)
                continue
            if not (config.WHALE_MIN_MCAP_USD <= mc_usd <= config.WHALE_MAX_MCAP_USD):
                mc_reason = f"MCAP < {_fmt_usd(config.WHALE_MIN_MCAP_USD)}" if mc_usd < config.WHALE_MIN_MCAP_USD else f"MCAP > {_fmt_usd(config.WHALE_MAX_MCAP_USD)}"
                db.upsert_whale_decision(mint, "SKIPPED", mc_reason, updated_at=now_dt.isoformat(), breakout_telemetry=bt)
                continue

            # Whale Breakout Gate (applied when OHLCV bars were evaluated; rejects EXTENDED or low score)
            has_ohlcv = bool(c5 or c1)
            if has_ohlcv and config.WHALE_BLOCK_EXTENDED and bt_state == "EXTENDED":
                rej = f"BREAKOUT_EXTENDED ({bt_failed_str})" if bt_failed_str else "BREAKOUT_EXTENDED"
                db.upsert_whale_decision(mint, "SKIPPED", rej, updated_at=now_dt.isoformat(), breakout_telemetry=bt)
                db.log_action(
                    mode=config.EXECUTION_MODE, action="WHALE_CONFLUENCE", symbol=symbol, asset_id=mint,
                    strategy="WHALE_SCALP", reason=f"SKIPPED: {rej}", timestamp_utc=now_dt.isoformat(),
                    mc_usd=round(mc_usd, 2) if mc_usd else None, liq_usd=round(liq_usd, 2) if liq_usd else None,
                    whale_sol_flow=round(float(trig["total_sol"]), 6), **bt_log_kwargs,
                )
                continue
            if has_ohlcv and bt_score < config.WHALE_MIN_BREAKOUT_SCORE:
                rej = f"BREAKOUT_SCORE({bt_score}<{config.WHALE_MIN_BREAKOUT_SCORE}): {bt_failed_str}"
                db.upsert_whale_decision(mint, "SKIPPED", rej, updated_at=now_dt.isoformat(), breakout_telemetry=bt)
                db.log_action(
                    mode=config.EXECUTION_MODE, action="WHALE_CONFLUENCE", symbol=symbol, asset_id=mint,
                    strategy="WHALE_SCALP", reason=f"SKIPPED: {rej}", timestamp_utc=now_dt.isoformat(),
                    mc_usd=round(mc_usd, 2) if mc_usd else None, liq_usd=round(liq_usd, 2) if liq_usd else None,
                    whale_sol_flow=round(float(trig["total_sol"]), 6), **bt_log_kwargs,
                )
                continue

            report = await check_token_safety(mint, config, client)
            if not report.passed:
                reason_str = "; ".join(report.reasons)
                db.upsert_whale_decision(mint, "SKIPPED", "SAFETY_REJECT", updated_at=now_dt.isoformat(), breakout_telemetry=bt)
                db.set_cooldown_minutes(mint, minutes=config.SAFETY_REJECT_COOLDOWN_MINUTES, reason=f"WHALE_SAFETY_REJECT: {reason_str}", now=now_dt)
                continue

            trig_wallets = list(trig["triggering_wallets"])
            trig_type = str(trig["trigger_type"])
            total_sol = float(trig["total_sol"])
            entry_reason = "WHALE_BREAKOUT_CONFLUENCE" if bt_state == "IGNITION" else f"WHALE_SCALP_{trig_type}"
            db.upsert_whale_decision(mint, "ENTERED", entry_reason, updated_at=now_dt.isoformat(), breakout_telemetry=bt)
            db.log_action(
                mode=config.EXECUTION_MODE,
                action="WHALE_CONFLUENCE",
                symbol=symbol,
                asset_id=mint,
                notional=round(config.WHALE_POSITION_SOL, 6),
                strategy="WHALE_SCALP",
                reason=entry_reason,
                timestamp_utc=now_dt.isoformat(),
                mc_usd=round(mc_usd, 2) if mc_usd else None,
                liq_usd=round(liq_usd, 2) if liq_usd else None,
                triggering_whales=",".join(config.resolve_wallet_label(w) for w in trig_wallets),
                whale_sol_flow=round(total_sol, 6),
                **bt_log_kwargs,
            )
            order = PendingOrder(
                side="BUY",
                mint=mint,
                symbol=symbol,
                amount_atomic=int(round(config.WHALE_POSITION_SOL * LAMPORTS_PER_SOL)),
                reason=entry_reason,
                target_state_after_fill=FocusState.IN_POSITION,
                apply_cooldown=False,
                entry_liquidity_usd=liq_usd,
                entry_mc_usd=round(mc_usd, 2) if mc_usd else None,
                whale_sol_flow=round(total_sol, 6),
                strategy="WHALE_SCALP",
                triggering_wallets=trig_wallets,
                breakout_telemetry=bt or None,
                rationale=f"[{entry_reason}] {symbol} ({mint[:6]}..): {len(trig_wallets)} whales, {total_sol:.2f} SOL, breakout={bt_score}/100 ({bt_state})",
            )
            exec_out = await executor_node(
                {
                    "focus_state": st["focus_state"],
                    "sol_balance": sol_balance,
                    "daily_pnl_sol": daily_pnl,
                    "active_position": chart_pos_raw,
                    "chart_positions": chart_positions,
                    "whale_position": whale_positions[0] if whale_positions else None,
                    "whale_positions": whale_positions,
                    "pending_order": order.model_dump(),
                    "slot": "whale_slot",
                },
                config=config,
                db=db,
                http_client=client,
            )
            return {
                "slot": "whale_slot",
                "event": f"{entry_reason}_ENTERED",
                "whale_position": exec_out.get("whale_position"),
                "whale_positions": exec_out.get("whale_positions", whale_positions),
                "last_execution": exec_out.get("last_execution"),
            }

        return {"slot": "whale_slot", "event": "ALL_CANDIDATES_FILTERED", "whale_positions": whale_positions}
    finally:
        if owns_client:
            await client.aclose()
