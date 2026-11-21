"""3-State Focus Monitor (position_monitor_node.py): IN_POSITION & WATCHING_REENTRY."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from alpha_node import evaluate_leg2_reentry
from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase, utc_now
from decision_client import GeminiDecisionClient
from executor_node import fetch_jupiter_order
from leg2_predictor import compute_leg2_score
from state import ActivePosition, AgentGraphState, FocusState, PendingOrder

logger = logging.getLogger(__name__)
EPS = 1e-9


async def fetch_dexscreener_5m_flow(
    mint: str,
    config: AgentConfig,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Fetch latest 5m buys/sells and liquidity USD for a single mint from DexScreener."""
    url = f"{config.DEXSCREENER_BASE_URL}/tokens/v1/solana/{mint}"
    resp = await client.get(url, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    pairs = data if isinstance(data, list) else (data.get("pairs") or [])
    if not pairs:
        return {"buys_5m": 0, "sells_5m": 0, "liquidity_usd": 0.0, "pair": {}}

    # Select highest-liquidity Solana pair
    sol_pairs = [p for p in pairs if str(p.get("chainId", "")).lower() == "solana"] or pairs
    best = max(sol_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0))
    m5 = (best.get("txns") or {}).get("m5") or {}
    return {
        "buys_5m": int(m5.get("buys") or 0),
        "sells_5m": int(m5.get("sells") or 0),
        "liquidity_usd": float((best.get("liquidity") or {}).get("usd") or 0.0),
        "pair": best,
    }


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
            True,
            sell_ratio,
            liq_drop_pct,
            f"EXODUS_SELL_FLOW (5m sells={sell_ratio * 100:.1f}% >= {config.EXODUS_SELL_RATIO * 100:.0f}%)",
        )
    if entry_liquidity_usd > 0 and current_liquidity_usd > 0 and liq_drop_pct >= config.EXODUS_LIQ_DROP_PCT:
        return (
            True,
            sell_ratio,
            liq_drop_pct,
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

    raw_pos = state.get("active_position")
    if not raw_pos:
        return {
            "focus_state": FocusState.SCANNING.value,
            "active_position": None,
            "pending_order": None,
        }

    pos = ActivePosition.model_validate(raw_pos)
    quote_amount = pos.token_amount if focus_state == FocusState.IN_POSITION.value else pos.reference_token_amount
    if quote_amount <= 0:
        quote_amount = 1_000_000

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient()
    try:
        quote_resp = await fetch_jupiter_order(
            input_mint=pos.mint,
            output_mint=SOL_MINT,
            amount_atomic=quote_amount,
            config=config,
            client=client,
            taker=None,
        )
        current_sol_value = int(quote_resp["outAmount"]) / LAMPORTS_PER_SOL
        flow = await fetch_dexscreener_5m_flow(pos.mint, config, client)
    finally:
        if owns_client:
            await client.aclose()

    buys_5m = int(flow["buys_5m"])
    sells_5m = int(flow["sells_5m"])
    total_5m = buys_5m + sells_5m
    buy_ratio_5m = (buys_5m / total_5m) if total_5m > 0 else 0.0
    current_liq = float(flow["liquidity_usd"])

    is_exodus, sell_ratio_5m, _, exodus_reason = _compute_exodus(
        buys_5m=buys_5m,
        sells_5m=sells_5m,
        entry_liquidity_usd=pos.entry_liquidity_usd,
        current_liquidity_usd=current_liq,
        config=config,
    )

    pos.current_sol_value = current_sol_value
    pos.last_5m_buy_ratio = buy_ratio_5m
    pos.last_5m_sell_ratio = sell_ratio_5m
    pos.last_liquidity_usd = current_liq

    sol_balance = float(state.get("sol_balance") or 0.0)
    daily_pnl_sol = float(state.get("daily_pnl_sol") or 0.0)

    # =========================================================================
    # STATE 2: IN_POSITION
    # =========================================================================
    if focus_state == FocusState.IN_POSITION.value:
        if current_sol_value > pos.peak_sol_value:
            pos.peak_sol_value = current_sol_value

        pnl_pct = (current_sol_value - pos.entry_sol) / pos.entry_sol if pos.entry_sol > 0 else 0.0
        peak_pnl_pct = (pos.peak_sol_value - pos.entry_sol) / pos.entry_sol if pos.entry_sol > 0 else 0.0

        # ---------------------------------------------------------------------
        # Dedicated WHALE_SCALP 6-Layer Exit Engine (delegated to whale_lane.py)
        # ---------------------------------------------------------------------
        if str(pos.strategy or "").upper() == "WHALE_SCALP":
            from whale_lane import evaluate_whale_exit_order

            now_dt = now or utc_now()
            exit_order, monitor_event = evaluate_whale_exit_order(
                pos,
                current_sol_value=current_sol_value,
                config=config,
                db=db,
                now_dt=now_dt,
            )
            if exit_order is not None:
                return {
                    "active_position": pos.model_dump(),
                    "pending_order": exit_order.model_dump(),
                    "monitor_event": monitor_event,
                }
            pos_dict = pos.model_dump()
            db.save_state(
                focus_state=FocusState.IN_POSITION.value,
                sol_balance=sol_balance,
                daily_pnl_sol=daily_pnl_sol,
                active_position=pos_dict,
            )
            return {
                "active_position": pos_dict,
                "pending_order": None,
                "monitor_event": monitor_event,
            }

        if peak_pnl_pct + EPS >= config.BREAKEVEN_ARM_PCT:
            pos.breakeven_armed = True
        if peak_pnl_pct + EPS >= config.TRAIL_ARM_PCT:
            pos.trail_armed = True

        # 1. Hard Stop (-10%) -> 100% sell -> SCANNING + 2h cooldown
        if pnl_pct <= -config.HARD_STOP_LOSS_PCT + EPS:
            reason = f"HARD_STOP_LOSS ({pnl_pct * 100:+.2f}% <= -{config.HARD_STOP_LOSS_PCT * 100:.0f}%)"
            order = PendingOrder(
                side="SELL",
                mint=pos.mint,
                symbol=pos.symbol,
                amount_atomic=pos.token_amount,
                reason=reason,
                target_state_after_fill=FocusState.SCANNING,
                apply_cooldown=True,
                strategy=pos.strategy or "SCALPER",
                rationale=f"[MONITOR EXIT] {pos.symbol}: {reason}",
            )
            return {
                "active_position": pos.model_dump(),
                "pending_order": order.model_dump(),
                "monitor_event": reason,
            }

        # 2. Exodus (5m sells >= 65% or liquidity drop >= 10%) -> 100% sell -> SCANNING + 2h cooldown
        if is_exodus:
            order = PendingOrder(
                side="SELL",
                mint=pos.mint,
                symbol=pos.symbol,
                amount_atomic=pos.token_amount,
                reason=exodus_reason,
                target_state_after_fill=FocusState.SCANNING,
                apply_cooldown=True,
                strategy=pos.strategy or "SCALPER",
                rationale=f"[MONITOR EXIT] {pos.symbol}: {exodus_reason}",
            )
            return {
                "active_position": pos.model_dump(),
                "pending_order": order.model_dump(),
                "monitor_event": exodus_reason,
            }

        # 3. Trailing TP (+12% armed, -6% pullback from peak) -> WATCHING_REENTRY (or SCANNING + cooldown if max re-entries hit)
        if pos.trail_armed and pos.peak_sol_value > 0:
            pullback_pct = (pos.peak_sol_value - current_sol_value) / pos.peak_sol_value
            if pullback_pct + EPS >= config.TRAIL_PULLBACK_PCT:
                at_reentry_cap = pos.reentry_count >= config.MAX_REENTRIES_PER_MINT
                target_state = FocusState.SCANNING if at_reentry_cap else FocusState.WATCHING_REENTRY
                cap_suffix = (
                    f" [MAX_REENTRIES={pos.reentry_count}/{config.MAX_REENTRIES_PER_MINT} -> COOLDOWN]"
                    if at_reentry_cap
                    else ""
                )
                reason = (
                    f"TRAILING_TP (peak {peak_pnl_pct * 100:+.2f}%, "
                    f"pullback -{pullback_pct * 100:.2f}% >= -{config.TRAIL_PULLBACK_PCT * 100:.0f}%){cap_suffix}"
                )
                order = PendingOrder(
                    side="SELL",
                    mint=pos.mint,
                    symbol=pos.symbol,
                    amount_atomic=pos.token_amount,
                    reason=reason,
                    target_state_after_fill=target_state,
                    apply_cooldown=at_reentry_cap,
                    run_peak_sol=pos.peak_sol_value,
                    strategy=pos.strategy or "SCALPER",
                    rationale=f"[MONITOR TP] {pos.symbol}: {reason} -> {target_state.value}",
                )
                return {
                    "active_position": pos.model_dump(),
                    "pending_order": order.model_dump(),
                    "monitor_event": reason,
                }

        # 4. Breakeven Shield (+2% floor once +8% reached) -> 100% sell -> SCANNING + 2h cooldown
        if pos.breakeven_armed and pnl_pct <= config.BREAKEVEN_FLOOR_PCT + EPS:
            reason = (
                f"BREAKEVEN_SHIELD (armed >= +{config.BREAKEVEN_ARM_PCT * 100:.0f}%, "
                f"hit floor {pnl_pct * 100:+.2f}% <= +{config.BREAKEVEN_FLOOR_PCT * 100:.0f}%)"
            )
            order = PendingOrder(
                side="SELL",
                mint=pos.mint,
                symbol=pos.symbol,
                amount_atomic=pos.token_amount,
                reason=reason,
                target_state_after_fill=FocusState.SCANNING,
                apply_cooldown=True,
                strategy=pos.strategy or "SCALPER",
                rationale=f"[MONITOR EXIT] {pos.symbol}: {reason}",
            )
            return {
                "active_position": pos.model_dump(),
                "pending_order": order.model_dump(),
                "monitor_event": reason,
            }

        # Hold position and persist updated peak / PnL
        pos_dict = pos.model_dump()
        db.save_state(
            focus_state=FocusState.IN_POSITION.value,
            sol_balance=sol_balance,
            daily_pnl_sol=daily_pnl_sol,
            active_position=pos_dict,
        )
        return {
            "active_position": pos_dict,
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

    # Abandonment 0: Max re-entries per mint reached -> cooldown + SCANNING
    if pos.reentry_count >= config.MAX_REENTRIES_PER_MINT:
        reason = f"MAX_REENTRIES_PER_MINT ({pos.reentry_count}/{config.MAX_REENTRIES_PER_MINT})"
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

    # Abandonment 1: Exodus
    if is_exodus:
        db.set_cooldown(pos.mint, hours=config.COOLDOWN_HOURS, reason=exodus_reason)
        rationale = f"[WATCH ABANDON] {pos.symbol}: {exodus_reason} -> SCANNING"
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
            "monitor_event": exodus_reason,
            "last_decision_rationale": rationale,
        }

    # Abandonment 2: -25% bleed from run peak
    bleed_pct = (run_peak - current_sol_value) / run_peak if run_peak > 0 else 0.0
    if bleed_pct >= config.WATCH_MAX_BLEED_PCT:
        reason = f"WATCH_BLEED (-{bleed_pct * 100:.1f}% from run peak >= -{config.WATCH_MAX_BLEED_PCT * 100:.0f}%)"
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

    # Abandonment 3: 20-minute watch timeout
    if pos.watch_started_at:
        started_dt = datetime.fromisoformat(pos.watch_started_at)
        if started_dt.tzinfo is None:
            started_dt = started_dt.replace(tzinfo=timezone.utc)
        elapsed_min = (now_dt - started_dt).total_seconds() / 60.0
        if elapsed_min >= config.WATCH_TIMEOUT_MINUTES:
            reason = f"WATCH_TIMEOUT ({elapsed_min:.1f}m >= {config.WATCH_TIMEOUT_MINUTES}m)"
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
