"""Multi-slot Chart lane exit evaluator & priority ordering (chart_exit_engine.py)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel

from batched_price_monitor import active_cost_basis_sol, compute_net_lifecycle_pnl, is_hard_stop_hit
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


class ExodusEvaluation(BaseModel):
    """Structured Exodus evaluation result with observable suppression/confirmation telemetry."""

    triggered: bool = False
    sell_ratio_5m: float = 0.0
    liq_drop_pct: float = 0.0
    detail: str = ""
    status: str = "EXODUS_CLEAR"
    liq_status: str = "EXODUS_LIQ_OK"
    confirm_count: int = 0
    last_confirmed_at: Optional[str] = None


def _parse_iso_utc(raw: Optional[str]) -> Optional[datetime]:
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def pos_matches(p_dict: dict[str, Any], mint: str, opened_at: Optional[str]) -> bool:
    """Match an open position dictionary by stable (mint, opened_at) identity."""
    if str(p_dict.get("mint") or "") != str(mint or ""):
        return False
    p_open = str(p_dict.get("opened_at") or "")
    target_open = str(opened_at or "")
    if p_open and target_open:
        return p_open == target_open
    return True


def evaluate_exodus_signal(
    *,
    buys_5m: Optional[int],
    sells_5m: Optional[int],
    entry_liquidity_usd: float,
    current_liquidity_usd: Optional[float],
    config: AgentConfig,
    flow_available: bool = True,
    liq_available: bool = True,
    opened_at: Optional[str] = None,
    now_dt: Optional[datetime] = None,
    enforce_warmup: bool = False,
    confirm_count: int = 0,
    last_confirmed_at: Optional[str] = None,
    enforce_confirmation: bool = False,
    min_confirm_interval_seconds: Optional[float] = None,
) -> ExodusEvaluation:
    """Evaluate liquidity-drop and sell-flow Exodus with sample-size, warm-up, debounce, and availability guards."""
    ref_now = now_dt or datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)

    # 1. Compute flow metrics (without fabricating zero-flow from missing data)
    has_flow = bool(flow_available) and buys_5m is not None and sells_5m is not None
    total_5m = (int(buys_5m) + int(sells_5m)) if has_flow else 0
    sell_ratio = (int(sells_5m) / total_5m) if (has_flow and total_5m > 0) else 0.0

    # 2. Liquidity-drop evaluation (independent of tx sample size and warm-up; handles confirmed $0 liquidity)
    has_liq = bool(liq_available) and current_liquidity_usd is not None
    liq_drop_pct = 0.0
    liq_status = "EXODUS_LIQ_OK"
    if not has_liq:
        liq_status = "EXODUS_LIQ_UNAVAILABLE"
    elif entry_liquidity_usd > 0:
        curr_liq_f = float(current_liquidity_usd)
        if curr_liq_f >= 0.0:
            liq_drop_pct = (entry_liquidity_usd - curr_liq_f) / entry_liquidity_usd
            if liq_drop_pct + EPS >= config.EXODUS_LIQ_DROP_PCT:
                return ExodusEvaluation(
                    triggered=True,
                    sell_ratio_5m=sell_ratio,
                    liq_drop_pct=liq_drop_pct,
                    detail=f"EXODUS_LIQ_DROP (liq drop={liq_drop_pct * 100:.1f}% >= {config.EXODUS_LIQ_DROP_PCT * 100:.0f}%)",
                    status="EXODUS_LIQ_DROP",
                    liq_status="EXODUS_LIQ_DROP",
                    confirm_count=confirm_count,
                    last_confirmed_at=last_confirmed_at,
                )

    # 3. Sell-flow availability check
    if not has_flow:
        return ExodusEvaluation(
            triggered=False,
            sell_ratio_5m=0.0,
            liq_drop_pct=liq_drop_pct,
            detail="EXODUS_FLOW_UNAVAILABLE",
            status="EXODUS_FLOW_UNAVAILABLE",
            liq_status=liq_status,
            confirm_count=0,
            last_confirmed_at=None,
        )

    # 4. Pre-entry rolling 5m warm-up guard (conservative on missing/invalid opened_at)
    if enforce_warmup and config.EXODUS_FLOW_WARMUP_SECONDS > 0:
        opened_dt = _parse_iso_utc(opened_at)
        if opened_dt is None:
            return ExodusEvaluation(
                triggered=False,
                sell_ratio_5m=sell_ratio,
                liq_drop_pct=liq_drop_pct,
                detail="EXODUS_FLOW_WARMUP (missing/invalid opened_at)",
                status="EXODUS_FLOW_WARMUP",
                liq_status=liq_status,
                confirm_count=0,
                last_confirmed_at=None,
            )
        elapsed_sec = (ref_now - opened_dt).total_seconds()
        if elapsed_sec + EPS < config.EXODUS_FLOW_WARMUP_SECONDS:
            return ExodusEvaluation(
                triggered=False,
                sell_ratio_5m=sell_ratio,
                liq_drop_pct=liq_drop_pct,
                detail=f"EXODUS_FLOW_WARMUP ({elapsed_sec:.0f}s < {config.EXODUS_FLOW_WARMUP_SECONDS:.0f}s)",
                status="EXODUS_FLOW_WARMUP",
                liq_status=liq_status,
                confirm_count=0,
                last_confirmed_at=None,
            )

    # 5. Minimum 5m transaction sample guard (prevents 1-sell / 0-buy or 0-tx triggers)
    min_txns = int(config.MIN_EXODUS_TXNS_5M)
    if total_5m < min_txns:
        return ExodusEvaluation(
            triggered=False,
            sell_ratio_5m=sell_ratio,
            liq_drop_pct=liq_drop_pct,
            detail=f"EXODUS_FLOW_INSUFFICIENT_SAMPLE (txns={total_5m} < {min_txns})",
            status="EXODUS_FLOW_INSUFFICIENT_SAMPLE",
            liq_status=liq_status,
            confirm_count=0,
            last_confirmed_at=None,
        )

    # 6. Sell-ratio threshold & confirmation/debounce
    if sell_ratio + EPS >= config.EXODUS_SELL_RATIO:
        required_ticks = int(config.EXODUS_CONFIRMATION_TICKS) if enforce_confirmation else 1
        min_interval = (
            float(min_confirm_interval_seconds)
            if min_confirm_interval_seconds is not None
            else float(config.MONITOR_POLL_SECONDS)
        )
        last_dt = _parse_iso_utc(last_confirmed_at)
        is_duplicate_tick = (
            enforce_confirmation
            and confirm_count > 0
            and last_dt is not None
            and (ref_now - last_dt).total_seconds() + EPS < min_interval
        )
        if is_duplicate_tick:
            new_count = int(confirm_count)
            new_last = last_confirmed_at
        else:
            new_count = int(confirm_count) + 1
            new_last = ref_now.isoformat()

        if new_count >= required_ticks:
            return ExodusEvaluation(
                triggered=True,
                sell_ratio_5m=sell_ratio,
                liq_drop_pct=liq_drop_pct,
                detail=(
                    f"EXODUS_SELL_FLOW (5m sells={sell_ratio * 100:.1f}% >= {config.EXODUS_SELL_RATIO * 100:.0f}%, "
                    f"txns={total_5m}, confirm={new_count}/{required_ticks})"
                ),
                status="EXODUS_FLOW_CONFIRMED",
                liq_status=liq_status,
                confirm_count=new_count,
                last_confirmed_at=new_last,
            )
        return ExodusEvaluation(
            triggered=False,
            sell_ratio_5m=sell_ratio,
            liq_drop_pct=liq_drop_pct,
            detail=(
                f"EXODUS_FLOW_CONFIRMING ({new_count}/{required_ticks}, "
                f"5m sells={sell_ratio * 100:.1f}%, txns={total_5m})"
            ),
            status="EXODUS_FLOW_CONFIRMING",
            liq_status=liq_status,
            confirm_count=new_count,
            last_confirmed_at=new_last,
        )

    # Signal cleared -> reset confirmation
    clear_status = "EXODUS_LIQ_UNAVAILABLE" if not has_liq else "EXODUS_CLEAR"
    return ExodusEvaluation(
        triggered=False,
        sell_ratio_5m=sell_ratio,
        liq_drop_pct=liq_drop_pct,
        detail="" if has_liq else "EXODUS_LIQ_UNAVAILABLE",
        status=clear_status,
        liq_status=liq_status,
        confirm_count=0,
        last_confirmed_at=None,
    )


def compute_exodus(
    *,
    buys_5m: Optional[int],
    sells_5m: Optional[int],
    entry_liquidity_usd: float,
    current_liquidity_usd: Optional[float],
    config: AgentConfig,
    flow_available: bool = True,
    liq_available: bool = True,
    opened_at: Optional[str] = None,
    now_dt: Optional[datetime] = None,
    enforce_warmup: bool = False,
    confirm_count: int = 0,
    last_confirmed_at: Optional[str] = None,
    enforce_confirmation: bool = False,
    min_confirm_interval_seconds: Optional[float] = None,
) -> tuple[bool, float, float, str]:
    """Return (is_exodus, sell_ratio_5m, liq_drop_pct, detail)."""
    ev = evaluate_exodus_signal(
        buys_5m=buys_5m,
        sells_5m=sells_5m,
        entry_liquidity_usd=entry_liquidity_usd,
        current_liquidity_usd=current_liquidity_usd,
        config=config,
        flow_available=flow_available,
        liq_available=liq_available,
        opened_at=opened_at,
        now_dt=now_dt,
        enforce_warmup=enforce_warmup,
        confirm_count=confirm_count,
        last_confirmed_at=last_confirmed_at,
        enforce_confirmation=enforce_confirmation,
        min_confirm_interval_seconds=min_confirm_interval_seconds,
    )
    return ev.triggered, ev.sell_ratio_5m, ev.liq_drop_pct, ev.detail


def evaluate_chart_position_exit(
    pos: ActivePosition,
    *,
    current_sol_value: float,
    buys_5m: Optional[int],
    sells_5m: Optional[int],
    current_liquidity_usd: Optional[float],
    config: AgentConfig,
    flow_available: bool = True,
    liq_available: bool = True,
    now_dt: Optional[datetime] = None,
) -> tuple[Optional[PendingOrder], str, int]:
    """Evaluate all 5 Chart exit layers in strict protective priority order:

    1. HARD_STOP_LOSS (PRIO=0)
    2. EXODUS (PRIO=1)
    3. PARTIAL_TP (PRIO=2)
    4. TRAILING_TP (PRIO=3)
    5. BREAKEVEN (PRIO=4)
    6. HOLD (PRIO=99)

    Mutates `pos` arming/peak/exodus confirmation fields in-place and returns `(order, monitor_event, priority)`.
    """
    if current_sol_value > pos.peak_sol_value:
        pos.peak_sol_value = current_sol_value

    ex_eval = evaluate_exodus_signal(
        buys_5m=buys_5m,
        sells_5m=sells_5m,
        entry_liquidity_usd=pos.entry_liquidity_usd,
        current_liquidity_usd=current_liquidity_usd,
        config=config,
        flow_available=flow_available,
        liq_available=liq_available,
        opened_at=pos.opened_at,
        now_dt=now_dt,
        enforce_warmup=True,
        confirm_count=pos.exodus_confirm_count,
        last_confirmed_at=pos.exodus_last_confirmed_at,
        enforce_confirmation=True,
    )
    if flow_available and buys_5m is not None and sells_5m is not None:
        pos.last_5m_sell_ratio = ex_eval.sell_ratio_5m
    pos.exodus_confirm_count = ex_eval.confirm_count
    pos.exodus_last_confirmed_at = ex_eval.last_confirmed_at
    pos.last_exodus_status = ex_eval.status

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
            pair_address=pos.pair_address,
            dex_id=pos.dex_id,
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

    # 1. Unconditional Hard Stop (-10% against remaining_cost_basis_sol) — PRIO_HARD_STOP=0
    hs_hit, hs_pnl_100, hs_limit_100 = is_hard_stop_hit(pos, current_sol_value, config)
    if hs_hit or pnl_pct <= -config.HARD_STOP_LOSS_PCT + EPS:
        hs_reason = f"HARD_STOP_LOSS ({hs_pnl_100:+.2f}% <= {hs_limit_100:.0f}%)"
        return _mk_order(hs_reason), hs_reason, PRIO_HARD_STOP

    fees_known, net_now_sol, net_now_pct, net_peak_pct, be_fee_status = compute_net_lifecycle_pnl(
        pos, current_sol_value, config
    )
    if fees_known and net_peak_pct is not None and net_peak_pct + EPS >= config.BREAKEVEN_ARM_PCT:
        pos.breakeven_armed = True
    if not fees_known:
        pos.last_breakeven_status = be_fee_status
    elif pos.breakeven_armed:
        pos.last_breakeven_status = "NET_BREAKEVEN_ARMED"
    else:
        pos.last_breakeven_status = "BREAKEVEN_UNARMED"

    if peak_pnl_pct + EPS >= config.TRAIL_ARM_PCT:
        pos.trail_armed = True

    # 2. Protective Exodus (confirmed sell flow or pinned liquidity drop) — PRIO_EXODUS=1
    if ex_eval.triggered:
        return _mk_order(ex_eval.detail), ex_eval.detail, PRIO_EXODUS

    # 3. 2-Step Partial Take-Profit (Scale-Out 50% at +15%) — PRIO_PARTIAL_TP=2
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

    # 4. Net Breakeven Shield (+2% net lifecycle floor once +8% net lifecycle reached; suppressed if fees unknown)
    if fees_known and pos.breakeven_armed and net_now_pct is not None and net_now_pct <= config.BREAKEVEN_FLOOR_PCT + EPS:
        pos.last_breakeven_status = "NET_BREAKEVEN_TRIGGERED"
        reason = (
            f"BREAKEVEN_SHIELD (armed >= +{config.BREAKEVEN_ARM_PCT * 100:.0f}%, "
            f"hit net floor {net_now_pct * 100:+.2f}% <= +{config.BREAKEVEN_FLOOR_PCT * 100:.0f}%)"
        )
        return _mk_order(reason), reason, PRIO_BREAKEVEN

    return None, "HOLD_IN_POSITION", PRIO_HOLD
