"""Multi-slot Chart lane exit evaluator & priority ordering (chart_exit_engine.py)."""

from __future__ import annotations

from typing import Any, Optional

from batched_price_monitor import active_cost_basis_sol, is_hard_stop_hit
from config import AgentConfig
from state import ActivePosition, FocusState, PendingOrder

EPS = 1e-9

# Protective exits always take priority when multiple slots trigger on the same tick
PRIO_HARD_STOP = 0
PRIO_EXODUS = 1
PRIO_PARTIAL_TP = 2
PRIO_TRAILING_TP = 3
PRIO_BREAKEVEN = 4
PRIO_HOLD = 99


def pos_matches(p_dict: dict[str, Any], mint: str, opened_at: Optional[str]) -> bool:
    """Match an open position dictionary by stable (mint, opened_at) identity."""
    if str(p_dict.get("mint") or "") != str(mint or ""):
        return False
    p_open = str(p_dict.get("opened_at") or "")
    target_open = str(opened_at or "")
    if p_open and target_open:
        return p_open == target_open
    return True


def compute_exodus(
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


def evaluate_chart_position_exit(
    pos: ActivePosition,
    *,
    current_sol_value: float,
    buys_5m: int,
    sells_5m: int,
    current_liquidity_usd: float,
    config: AgentConfig,
) -> tuple[Optional[PendingOrder], str, int]:
    """Evaluate all 5 Chart exit layers for a single open position regardless of slot index.

    Mutates `pos` arming/peak/sell-ratio fields in-place and returns `(order, monitor_event, priority)`.
    """
    if current_sol_value > pos.peak_sol_value:
        pos.peak_sol_value = current_sol_value

    is_exodus, sell_ratio_5m, _, exodus_reason = compute_exodus(
        buys_5m=buys_5m,
        sells_5m=sells_5m,
        entry_liquidity_usd=pos.entry_liquidity_usd,
        current_liquidity_usd=current_liquidity_usd,
        config=config,
    )
    pos.last_5m_sell_ratio = sell_ratio_5m

    cost_basis = active_cost_basis_sol(pos)
    pnl_pct = (current_sol_value - cost_basis) / cost_basis if cost_basis > 0 else 0.0
    peak_pnl_pct = (pos.peak_sol_value - cost_basis) / cost_basis if cost_basis > 0 else 0.0
    pnl_pct_100 = pnl_pct * 100.0

    def _mk_order(
        reason: str,
        *,
        side: str = "SELL",
        amount_atomic: Optional[int] = None,
        target_state: FocusState = FocusState.SCANNING,
        apply_cooldown: bool = True,
        run_peak_sol: Optional[float] = None,
        rationale: Optional[str] = None,
    ) -> PendingOrder:
        return PendingOrder(
            side=side,
            mint=pos.mint,
            symbol=pos.symbol,
            opened_at=pos.opened_at,
            amount_atomic=int(amount_atomic if amount_atomic is not None else pos.token_amount),
            reason=reason,
            target_state_after_fill=target_state,
            apply_cooldown=apply_cooldown,
            run_peak_sol=run_peak_sol,
            strategy=pos.strategy or "SCALPER",
            triggering_wallets=list(pos.triggering_wallets),
            entry_mc_usd=pos.entry_mc_usd,
            whale_sol_flow=pos.whale_sol_flow,
            breakout_telemetry=pos.breakout_telemetry,
            monitor_sol_value=round(current_sol_value, 6),
            price_source=pos.last_price_source,
            rationale=rationale or f"[MONITOR EXIT] {pos.symbol}: {reason}",
        )

    # 0. Unconditional Hard Stop (-10% against remaining_cost_basis_sol)
    hs_hit, hs_pnl_100, hs_limit_100 = is_hard_stop_hit(pos, current_sol_value, config)
    if hs_hit or pnl_pct <= -config.HARD_STOP_LOSS_PCT + EPS:
        hs_reason = f"HARD_STOP_LOSS ({hs_pnl_100:+.2f}% <= {hs_limit_100:.0f}%)"
        return _mk_order(hs_reason), hs_reason, PRIO_HARD_STOP

    if peak_pnl_pct + EPS >= config.BREAKEVEN_ARM_PCT:
        pos.breakeven_armed = True
    if peak_pnl_pct + EPS >= config.TRAIL_ARM_PCT:
        pos.trail_armed = True

    # 1. Step 1 — 2-Step Partial Take-Profit (Scale-Out 50% at +15%)
    if not pos.partial_tp_taken and pos.token_amount >= 2 and pnl_pct_100 + EPS >= config.PARTIAL_TP_TRIGGER_PCT:
        sell_qty = int(pos.token_amount * config.PARTIAL_TP_SELL_FRACTION)
        if sell_qty > 0:
            partial_reason = f"PARTIAL_TP_50PCT (+{config.PARTIAL_TP_TRIGGER_PCT:.0f}%)"
            order = _mk_order(
                partial_reason,
                side="PARTIAL_SELL",
                amount_atomic=sell_qty,
                target_state=FocusState.IN_POSITION,
                apply_cooldown=False,
                rationale=f"[MONITOR PARTIAL TP] {pos.symbol}: {partial_reason} ({pnl_pct_100:+.2f}%)",
            )
            return order, partial_reason, PRIO_PARTIAL_TP

    # 2. Exodus (5m sells >= 65% or liquidity drop >= 10%) — protective priority 1
    if is_exodus:
        return _mk_order(exodus_reason), exodus_reason, PRIO_EXODUS

    # 3. Trailing TP (+12% armed; -6% pullback pre-partial, or -10% RUNNER_TRAIL_PULLBACK_PCT on runner)
    if (pos.trail_armed or pos.partial_tp_taken) and pos.peak_sol_value > 0:
        raw_runner_pb = float(config.RUNNER_TRAIL_PULLBACK_PCT)
        runner_pb_frac = (raw_runner_pb / 100.0) if raw_runner_pb >= 1.0 else raw_runner_pb
        active_trail_pb = runner_pb_frac if pos.partial_tp_taken else config.TRAIL_PULLBACK_PCT
        pullback_pct = (pos.peak_sol_value - current_sol_value) / pos.peak_sol_value
        if pullback_pct + EPS >= active_trail_pb:
            leg2_enabled = config.is_chart_strategy_enabled("LEG2_ENTRY")
            at_reentry_cap = (not leg2_enabled) or (pos.reentry_count >= config.MAX_REENTRIES_PER_MINT)
            target_state = FocusState.SCANNING if at_reentry_cap else FocusState.WATCHING_REENTRY
            cap_suffix = (
                " [LEG2_ENTRY_DISABLED -> COOLDOWN]"
                if not leg2_enabled
                else (f" [MAX_REENTRIES={pos.reentry_count}/{config.MAX_REENTRIES_PER_MINT} -> COOLDOWN]" if at_reentry_cap else "")
            )
            reason = (
                f"TRAILING_TP (peak {peak_pnl_pct * 100:+.2f}%, "
                f"pullback -{pullback_pct * 100:.2f}% >= -{active_trail_pb * 100:.0f}%){cap_suffix}"
            )
            return (
                _mk_order(
                    reason,
                    target_state=target_state,
                    apply_cooldown=at_reentry_cap,
                    run_peak_sol=pos.peak_sol_value,
                    rationale=f"[MONITOR TP] {pos.symbol}: {reason} -> {target_state.value}",
                ),
                reason,
                PRIO_TRAILING_TP,
            )

    # 4. Breakeven Shield (+2% floor once +8% reached)
    if pos.breakeven_armed and pnl_pct <= config.BREAKEVEN_FLOOR_PCT + EPS:
        reason = (
            f"BREAKEVEN_SHIELD (armed >= +{config.BREAKEVEN_ARM_PCT * 100:.0f}%, "
            f"hit floor {pnl_pct * 100:+.2f}% <= +{config.BREAKEVEN_FLOOR_PCT * 100:.0f}%)"
        )
        return _mk_order(reason), reason, PRIO_BREAKEVEN

    return None, "HOLD_IN_POSITION", PRIO_HOLD
