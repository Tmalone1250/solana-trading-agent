"""3-State Focus Monitor (position_monitor_node.py): IN_POSITION & WATCHING_REENTRY."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from alpha_node import evaluate_leg2_reentry
from batched_price_monitor import (
    active_cost_basis_sol,
    apply_dex_flow_and_pnl,
    estimate_position_sol_value,
    fetch_batched_sol_prices,
    resolve_position_sol_value,
)
from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase, utc_now
from decision_client import GeminiDecisionClient
from executor_node import executor_node, fetch_jupiter_order
from leg2_predictor import compute_leg2_score
from state import ActivePosition, AgentGraphState, FocusState, PendingOrder

logger = logging.getLogger(__name__)
EPS = 1e-9


def _compute_exodus(
    *,
    buys_5m: int,
    sells_5m: int,
    entry_liquidity_usd: float,
    current_liquidity_usd: float,
    config: AgentConfig,
) -> tuple[bool, float, float, str]:
    """Return (is_exodus, sell_ratio_5m, liq_drop_pct, detail)."""
    total_5m = buys_5m + sells_5m
    sell_ratio = (sells_5m / total_5m) if total_5m > 0 else 0.0
    liq_drop_pct = (
        (entry_liquidity_usd - current_liquidity_usd) / entry_liquidity_usd
        if entry_liquidity_usd > 0 and current_liquidity_usd > 0
        else 0.0
    )
    if total_5m > 0 and sell_ratio >= config.EXODUS_SELL_RATIO:
        return (
            True, sell_ratio, liq_drop_pct,
            f"EXODUS_SELL_FLOW (5m sells={sell_ratio * 100:.1f}% >= {config.EXODUS_SELL_RATIO * 100:.0f}%)",
        )
    if entry_liquidity_usd > 0 and current_liquidity_usd > 0 and liq_drop_pct >= config.EXODUS_LIQ_DROP_PCT:
        return (
            True, sell_ratio, liq_drop_pct,
            f"EXODUS_LIQ_DROP (liq drop={liq_drop_pct * 100:.1f}% >= {config.EXODUS_LIQ_DROP_PCT * 100:.0f}%)",
        )
    return (False, sell_ratio, liq_drop_pct, "")


async def position_monitor_node(
    state: AgentGraphState,
    *,
    config: AgentConfig,
    db: AgentDatabase,
    decision_client: GeminiDecisionClient,
    http_client: Optional[httpx.AsyncClient] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """LangGraph node: monitors single active mint in IN_POSITION or WATCHING_REENTRY."""
    focus_state = state.get("focus_state")
    if focus_state not in (FocusState.IN_POSITION.value, FocusState.WATCHING_REENTRY.value):
        return {"pending_order": None}

    raw_chart_list = list(state.get("chart_positions") or [])
    raw_pos = state.get("active_position") or (raw_chart_list[0] if raw_chart_list else None)
    if not raw_pos:
        return {"focus_state": FocusState.SCANNING.value, "active_position": None, "chart_positions": [], "pending_order": None}

    pos = ActivePosition.model_validate(raw_pos)
    quote_amount = pos.token_amount if focus_state == FocusState.IN_POSITION.value else pos.reference_token_amount
    if quote_amount <= 0:
        quote_amount = 1_000_000

    all_chart_dicts: list[dict[str, Any]] = []
    seen_mints: set[str] = set()
    for p_item in [pos.model_dump(), *raw_chart_list]:
        if isinstance(p_item, dict) and p_item.get("mint") and p_item["mint"] not in seen_mints:
            seen_mints.add(str(p_item["mint"]))
            all_chart_dicts.append(p_item)

    now_dt = now or utc_now()
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient()
    try:
        sol_prices, dex_flow = await fetch_batched_sol_prices(
            list(seen_mints), config, client, now_ts=now_dt.timestamp()
        )
        flow = dex_flow.get(pos.mint) or {"buys_5m": 0, "sells_5m": 0, "liquidity_usd": 0.0, "pair": {}}
        if isinstance(flow, dict) and flow.get("decimals") is not None:
            pos.decimals = int(flow["decimals"])
        est_sol = estimate_position_sol_value(pos, sol_prices.get(pos.mint), token_decimals=pos.decimals)
        current_sol_value, _ = await resolve_position_sol_value(
            pos, estimated_sol_value=est_sol, config=config, client=client,
            now_dt=now_dt, has_batched_price=(pos.mint in sol_prices), quote_amount=quote_amount,
        )
        # Also refresh any additional open chart positions (slots 2 & 3) in-place
        updated_extra_charts: list[dict[str, Any]] = []
        for extra_d in all_chart_dicts[1:]:
            ep = ActivePosition.model_validate(extra_d)
            ef = dex_flow.get(ep.mint)
            if isinstance(ef, dict) and ef.get("decimals") is not None:
                ep.decimals = int(ef["decimals"])
            e_est = estimate_position_sol_value(ep, sol_prices.get(ep.mint), token_decimals=ep.decimals)
            e_curr, _ = await resolve_position_sol_value(
                ep, estimated_sol_value=e_est, config=config, client=client,
                now_dt=now_dt, has_batched_price=(ep.mint in sol_prices),
            )
            apply_dex_flow_and_pnl(ep, current_sol_value=e_curr, flow=ef, lane_label="chart_slot")
            updated_extra_charts.append(ep.model_dump())
    finally:
        if owns_client:
            await client.aclose()

    buys_5m, sells_5m, buy_ratio_5m, current_liq = apply_dex_flow_and_pnl(
        pos, current_sol_value=current_sol_value, flow=flow, lane_label="chart_slot"
    )
    total_5m = buys_5m + sells_5m

    is_exodus, sell_ratio_5m, _, exodus_reason = _compute_exodus(
        buys_5m=buys_5m, sells_5m=sells_5m,
        entry_liquidity_usd=pos.entry_liquidity_usd, current_liquidity_usd=current_liq, config=config,
    )
    pos.last_5m_sell_ratio = sell_ratio_5m

    sol_balance = float(state.get("sol_balance") or 0.0)
    daily_pnl_sol = float(state.get("daily_pnl_sol") or 0.0)

    def _sync_chart_list(primary: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
        if primary is not None and focus_state == FocusState.IN_POSITION.value:
            return [primary, *updated_extra_charts]
        return list(updated_extra_charts)

    # =========================================================================
    # STATE 2: IN_POSITION
    # =========================================================================
    if focus_state == FocusState.IN_POSITION.value:
        if current_sol_value > pos.peak_sol_value:
            pos.peak_sol_value = current_sol_value

        cost_basis = active_cost_basis_sol(pos)
        pnl_pct = (current_sol_value - cost_basis) / cost_basis if cost_basis > 0 else 0.0
        peak_pnl_pct = (pos.peak_sol_value - cost_basis) / cost_basis if cost_basis > 0 else 0.0
        pnl_pct_100 = pnl_pct * 100.0

        # Dedicated WHALE_SCALP 6-Layer Exit Engine (delegated to whale_lane.py)
        if str(pos.strategy or "").upper() == "WHALE_SCALP":
            from whale_lane import evaluate_whale_exit_order

            now_dt = now or utc_now()
            exit_order, monitor_event = evaluate_whale_exit_order(
                pos, current_sol_value=current_sol_value, config=config, db=db, now_dt=now_dt,
            )
            if exit_order is not None:
                return {"active_position": pos.model_dump(), "pending_order": exit_order.model_dump(), "monitor_event": monitor_event}
            pos_dict = pos.model_dump()
            db.save_state(focus_state=FocusState.IN_POSITION.value, sol_balance=sol_balance, daily_pnl_sol=daily_pnl_sol, active_position=pos_dict)
            return {"active_position": pos_dict, "pending_order": None, "monitor_event": monitor_event}

        if peak_pnl_pct + EPS >= config.BREAKEVEN_ARM_PCT:
            pos.breakeven_armed = True
        if peak_pnl_pct + EPS >= config.TRAIL_ARM_PCT:
            pos.trail_armed = True

        # Step 1 — 2-Step Partial Take-Profit (Scale-Out 50% at +15%)
        if not pos.partial_tp_taken and pos.token_amount >= 2 and pnl_pct_100 + EPS >= config.PARTIAL_TP_TRIGGER_PCT:
            sell_qty = int(pos.token_amount * config.PARTIAL_TP_SELL_FRACTION)
            if sell_qty > 0:
                partial_reason = f"PARTIAL_TP_50PCT (+{config.PARTIAL_TP_TRIGGER_PCT:.0f}%)"
                partial_order = PendingOrder(
                    side="PARTIAL_SELL", mint=pos.mint, symbol=pos.symbol, amount_atomic=sell_qty,
                    reason=partial_reason, target_state_after_fill=FocusState.IN_POSITION,
                    apply_cooldown=False, strategy=pos.strategy or "SCALPER",
                    rationale=f"[MONITOR PARTIAL TP] {pos.symbol}: {partial_reason} ({pnl_pct_100:+.2f}%)",
                )
                p_client_owns = http_client is None
                p_client = http_client or httpx.AsyncClient()
                try:
                    p_out = await executor_node(
                        {
                            "focus_state": FocusState.IN_POSITION.value, "sol_balance": sol_balance,
                            "daily_pnl_sol": daily_pnl_sol, "active_position": pos.model_dump(),
                            "chart_positions": _sync_chart_list(pos.model_dump()),
                            "pending_order": partial_order.model_dump(),
                        },
                        config=config, db=db, http_client=p_client,
                    )
                finally:
                    if p_client_owns:
                        await p_client.aclose()
                return {
                    "focus_state": FocusState.IN_POSITION.value,
                    "sol_balance": p_out["sol_balance"], "daily_pnl_sol": p_out["daily_pnl_sol"],
                    "active_position": p_out["active_position"], "chart_positions": p_out["chart_positions"],
                    "pending_order": None, "monitor_event": partial_reason, "last_execution": p_out.get("last_execution"),
                }

        def _exit_out(
            reason: str, *, target_state: FocusState = FocusState.SCANNING,
            apply_cooldown: bool = True, run_peak_sol: Optional[float] = None, rationale: Optional[str] = None,
        ) -> dict[str, Any]:
            order = PendingOrder(
                side="SELL", mint=pos.mint, symbol=pos.symbol, amount_atomic=pos.token_amount,
                reason=reason, target_state_after_fill=target_state, apply_cooldown=apply_cooldown,
                run_peak_sol=run_peak_sol, strategy=pos.strategy or "SCALPER",
                rationale=rationale or f"[MONITOR EXIT] {pos.symbol}: {reason}",
            )
            return {"active_position": pos.model_dump(), "pending_order": order.model_dump(), "monitor_event": reason}

        # 1. Hard Stop (-10%) -> 100% sell -> SCANNING + 2h cooldown
        if pnl_pct <= -config.HARD_STOP_LOSS_PCT + EPS:
            return _exit_out(f"HARD_STOP_LOSS ({pnl_pct * 100:+.2f}% <= -{config.HARD_STOP_LOSS_PCT * 100:.0f}%)")

        # 2. Exodus (5m sells >= 65% or liquidity drop >= 10%) -> 100% sell -> SCANNING + 2h cooldown
        if is_exodus:
            return _exit_out(exodus_reason)

        # 3. Trailing TP (+12% armed; -6% pullback pre-partial, or -10% RUNNER_TRAIL_PULLBACK_PCT on remaining 50% runner)
        if (pos.trail_armed or pos.partial_tp_taken) and pos.peak_sol_value > 0:
            raw_runner_pb = float(config.RUNNER_TRAIL_PULLBACK_PCT)
            runner_pb_frac = (raw_runner_pb / 100.0) if raw_runner_pb >= 1.0 else raw_runner_pb
            active_trail_pb = runner_pb_frac if pos.partial_tp_taken else config.TRAIL_PULLBACK_PCT
            pullback_pct = (pos.peak_sol_value - current_sol_value) / pos.peak_sol_value
            if pullback_pct + EPS >= active_trail_pb:
                at_reentry_cap = pos.reentry_count >= config.MAX_REENTRIES_PER_MINT
                target_state = FocusState.SCANNING if at_reentry_cap else FocusState.WATCHING_REENTRY
                cap_suffix = f" [MAX_REENTRIES={pos.reentry_count}/{config.MAX_REENTRIES_PER_MINT} -> COOLDOWN]" if at_reentry_cap else ""
                reason = (
                    f"TRAILING_TP (peak {peak_pnl_pct * 100:+.2f}%, "
                    f"pullback -{pullback_pct * 100:.2f}% >= -{active_trail_pb * 100:.0f}%){cap_suffix}"
                )
                return _exit_out(
                    reason,
                    target_state=target_state,
                    apply_cooldown=at_reentry_cap,
                    run_peak_sol=pos.peak_sol_value,
                    rationale=f"[MONITOR TP] {pos.symbol}: {reason} -> {target_state.value}",
                )

        # 4. Breakeven Shield (+2% floor once +8% reached) -> 100% sell -> SCANNING + 2h cooldown
        if pos.breakeven_armed and pnl_pct <= config.BREAKEVEN_FLOOR_PCT + EPS:
            reason = (
                f"BREAKEVEN_SHIELD (armed >= +{config.BREAKEVEN_ARM_PCT * 100:.0f}%, "
                f"hit floor {pnl_pct * 100:+.2f}% <= +{config.BREAKEVEN_FLOOR_PCT * 100:.0f}%)"
            )
            return _exit_out(reason)

        # Hold position and persist updated peak / PnL to both active_position & chart_positions
        pos_dict = pos.model_dump()
        synced_charts = _sync_chart_list(pos_dict)
        db.save_lane_positions(
            lane="chart",
            positions=synced_charts,
            sol_balance=sol_balance,
            daily_pnl_sol=daily_pnl_sol,
            focus_state=FocusState.IN_POSITION.value,
            active_position=pos_dict,
        )
        return {
            "active_position": pos_dict,
            "chart_positions": synced_charts,
            "pending_order": None,
            "monitor_event": "HOLD_IN_POSITION",
        }

    # =========================================================================
    # STATE 3: WATCHING_REENTRY
    # =========================================================================
    now_dt = now or utc_now()
    run_peak = pos.run_peak_sol or pos.peak_sol_value or current_sol_value
    retrace_low = pos.retrace_low_sol if pos.retrace_low_sol is not None else current_sol_value
    if current_sol_value < retrace_low:
        retrace_low = current_sol_value
        pos.retrace_low_sol = retrace_low

    def _abandon_watch(reason: str) -> dict[str, Any]:
        db.set_cooldown(pos.mint, hours=config.COOLDOWN_HOURS, reason=reason)
        rationale = f"[WATCH ABANDON] {pos.symbol}: {reason} -> SCANNING"
        db.save_state(
            focus_state=FocusState.SCANNING.value,
            sol_balance=sol_balance,
            daily_pnl_sol=daily_pnl_sol,
            active_position=None,
            last_decision_rationale=rationale,
        )
        return {
            "focus_state": FocusState.SCANNING.value,
            "active_position": None,
            "pending_order": None,
            "monitor_event": reason,
            "last_decision_rationale": rationale,
        }

    # Abandonment 0: Max re-entries per mint reached -> cooldown + SCANNING
    if pos.reentry_count >= config.MAX_REENTRIES_PER_MINT:
        return _abandon_watch(f"MAX_REENTRIES_PER_MINT ({pos.reentry_count}/{config.MAX_REENTRIES_PER_MINT})")

    # Abandonment 1: Exodus
    if is_exodus:
        return _abandon_watch(exodus_reason)

    # Abandonment 2: -25% bleed from run peak
    bleed_pct = (run_peak - current_sol_value) / run_peak if run_peak > 0 else 0.0
    if bleed_pct >= config.WATCH_MAX_BLEED_PCT:
        return _abandon_watch(f"WATCH_BLEED (-{bleed_pct * 100:.1f}% from run peak >= -{config.WATCH_MAX_BLEED_PCT * 100:.0f}%)")

    # Abandonment 3: 20-minute watch timeout
    if pos.watch_started_at:
        started_dt = datetime.fromisoformat(pos.watch_started_at)
        if started_dt.tzinfo is None:
            started_dt = started_dt.replace(tzinfo=timezone.utc)
        elapsed_min = (now_dt - started_dt).total_seconds() / 60.0
        if elapsed_min >= config.WATCH_TIMEOUT_MINUTES:
            return _abandon_watch(f"WATCH_TIMEOUT ({elapsed_min:.1f}m >= {config.WATCH_TIMEOUT_MINUTES}m)")

    # Check Leg 2 Re-entry: +5% curl off retrace_low AND 5m buys >= 58% AND leg2_score >= 65
    curl_pct = (current_sol_value - retrace_low) / retrace_low if retrace_low > 0 else 0.0
    leg2 = compute_leg2_score(
        flow.get("pair") or {},
        entry_liquidity_usd=pos.entry_liquidity_usd,
    )
    if (
        curl_pct + EPS >= config.REENTRY_CURL_PCT
        and total_5m > 0
        and buy_ratio_5m >= config.REENTRY_MIN_BUY_RATIO
        and leg2.score >= config.REENTRY_MIN_LEG2_PREDICTOR_SCORE
        and sol_balance >= config.TRADE_SIZE_SOL
    ):
        reentry_dec = await evaluate_leg2_reentry(
            mint=pos.mint,
            symbol=pos.symbol,
            curl_pct=curl_pct,
            buy_ratio_5m=buy_ratio_5m,
            retrace_from_peak_pct=bleed_pct,
            current_sol_value=current_sol_value,
            liquidity_usd=current_liq,
            config=config,
            decision_client=decision_client,
            db=db,
        )
        if reentry_dec.approved:
            # Anti-Chase Guard: re-check live Jupiter quote after Decision-1 latency
            curl_trigger_sol = current_sol_value
            recheck_owns = http_client is None
            recheck_client = http_client or httpx.AsyncClient()
            try:
                recheck_resp = await fetch_jupiter_order(
                    input_mint=pos.mint,
                    output_mint=SOL_MINT,
                    amount_atomic=quote_amount,
                    config=config,
                    client=recheck_client,
                    taker=None,
                )
                live_sol_value = int(recheck_resp["outAmount"]) / LAMPORTS_PER_SOL
            finally:
                if recheck_owns:
                    await recheck_client.aclose()

            chase_pct = (
                (live_sol_value - curl_trigger_sol) / curl_trigger_sol
                if curl_trigger_sol > 0
                else 0.0
            )
            exceeded_chase = chase_pct > config.MAX_REENTRY_CHASE_PCT + EPS
            exceeded_peak = run_peak > 0 and live_sol_value > run_peak + EPS
            if exceeded_chase or exceeded_peak:
                abort_reason = (
                    f"REENTRY_ANTI_CHASE (live={live_sol_value:.4f} vs trigger={curl_trigger_sol:.4f} "
                    f"[{chase_pct * 100:+.1f}% > +{config.MAX_REENTRY_CHASE_PCT * 100:.0f}%], "
                    f"prior_peak={run_peak:.4f})"
                )
                rationale = f"[REENTRY ABORT] {pos.symbol}: {abort_reason} -> SCANNING"
                logger.warning(rationale)
                db.log_action(
                    mode=config.EXECUTION_MODE,
                    action="EVICT",
                    symbol=pos.symbol,
                    asset_id=pos.mint,
                    strategy="LEG2_ENTRY",
                    reason=abort_reason,
                )
                db.save_state(
                    focus_state=FocusState.SCANNING.value,
                    sol_balance=sol_balance,
                    daily_pnl_sol=daily_pnl_sol,
                    active_position=None,
                    last_decision_rationale=rationale,
                )
                return {
                    "focus_state": FocusState.SCANNING.value,
                    "active_position": None,
                    "pending_order": None,
                    "monitor_event": "LEG_2_REENTRY_ABORT_CHASE",
                    "last_decision_rationale": rationale,
                }

            order = PendingOrder(
                side="BUY",
                mint=pos.mint,
                symbol=pos.symbol,
                amount_atomic=int(round(config.TRADE_SIZE_SOL * LAMPORTS_PER_SOL)),
                reason="LEG_2_REENTRY",
                target_state_after_fill=FocusState.IN_POSITION,
                apply_cooldown=False,
                entry_liquidity_usd=current_liq or pos.entry_liquidity_usd,
                strategy="LEG2_ENTRY",
                rationale=reentry_dec.rationale,
            )
            return {
                "active_position": pos.model_dump(),
                "pending_order": order.model_dump(),
                "monitor_event": "LEG_2_REENTRY_APPROVED",
                "last_decision_rationale": reentry_dec.rationale,
            }
        else:
            pos_dict = pos.model_dump()
            db.save_state(
                focus_state=FocusState.WATCHING_REENTRY.value,
                sol_balance=sol_balance,
                daily_pnl_sol=daily_pnl_sol,
                active_position=pos_dict,
                last_decision_rationale=reentry_dec.rationale,
            )
            return {
                "active_position": pos_dict,
                "pending_order": None,
                "monitor_event": "LEG_2_REENTRY_REJECTED",
                "last_decision_rationale": reentry_dec.rationale,
            }

    # Continue watching
    pos_dict = pos.model_dump()
    db.save_state(
        focus_state=FocusState.WATCHING_REENTRY.value,
        sol_balance=sol_balance,
        daily_pnl_sol=daily_pnl_sol,
        active_position=pos_dict,
    )
    return {
        "active_position": pos_dict,
        "pending_order": None,
        "monitor_event": "WATCHING_REENTRY",
    }
