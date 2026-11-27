"""3-State Focus Monitor (position_monitor_node.py): IN_POSITION & WATCHING_REENTRY."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from alpha_node import evaluate_leg2_reentry
from batched_price_monitor import (
    apply_dex_flow_and_pnl,
    estimate_position_sol_value,
    fetch_batched_sol_prices,
    resolve_position_sol_value,
)
from chart_exit_engine import (
    PRIO_HARD_STOP,
    PRIO_PARTIAL_TP,
    compute_exodus,
    evaluate_chart_position_exit,
    evaluate_exodus_signal,
    pos_matches,
)
from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase, utc_now
from decision_client import GeminiDecisionClient
from executor_node import executor_node, fetch_jupiter_order
from leg2_predictor import compute_leg2_score
from state import ActivePosition, AgentGraphState, FocusState, PendingOrder

logger = logging.getLogger(__name__)
EPS = 1e-9


def _resolve_exodus_observation_inputs(
    flow: Optional[dict[str, Any]],
    pos: ActivePosition,
    fallback_liq: float,
) -> tuple[bool, bool, Optional[float]]:
    """Return (flow_available, liq_available, current_liquidity_usd) from pinned pool telemetry."""
    status = str(pos.pool_observation_status or "")
    flow_ok = status in ("MATCHED", "ZERO_LIQUIDITY")
    if isinstance(flow, dict) and "flow_available" in flow:
        flow_ok = flow_ok and bool(flow.get("flow_available"))

    if status in ("MATCHED", "ZERO_LIQUIDITY"):
        raw_liq = flow.get("liquidity_usd") if isinstance(flow, dict) else None
        liq_val = float(raw_liq) if raw_liq is not None else float(fallback_liq)
        return flow_ok, True, liq_val

    if status == "INCOMPLETE_DATA" and isinstance(flow, dict) and flow.get("liquidity_usd") is not None:
        return False, True, float(flow["liquidity_usd"])

    return False, False, None


async def position_monitor_node(
    state: AgentGraphState,
    *,
    config: AgentConfig,
    db: AgentDatabase,
    decision_client: GeminiDecisionClient,
    http_client: Optional[httpx.AsyncClient] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """LangGraph node: monitors all open Chart slots (1–3) + WATCHING_REENTRY."""
    focus_state = state.get("focus_state")
    raw_chart_list = list(state.get("chart_positions") or [])
    if focus_state not in (FocusState.IN_POSITION.value, FocusState.WATCHING_REENTRY.value) and not raw_chart_list:
        return {"pending_order": None}

    raw_pos = state.get("active_position") or (raw_chart_list[0] if raw_chart_list else None)
    if not raw_pos:
        return {"focus_state": FocusState.SCANNING.value, "active_position": None, "chart_positions": [], "pending_order": None}

    primary_pos = ActivePosition.model_validate(raw_pos)
    primary_is_watch = (primary_pos.token_amount <= 0) and (focus_state == FocusState.WATCHING_REENTRY.value)
    leg2_enabled = config.is_chart_strategy_enabled("LEG2_ENTRY")

    # Deduplicate open token-holding Chart positions by (mint, opened_at) while preserving order
    open_chart_positions: list[ActivePosition] = []
    for p_item in ([primary_pos.model_dump()] if not primary_is_watch else []) + raw_chart_list:
        if not isinstance(p_item, dict) or not p_item.get("mint"):
            continue
        cp_cand = ActivePosition.model_validate(p_item)
        if cp_cand.token_amount <= 0 and not (not open_chart_positions and not primary_is_watch):
            continue
        if not any(pos_matches(existing.model_dump(), cp_cand.mint, cp_cand.opened_at) for existing in open_chart_positions):
            open_chart_positions.append(cp_cand)

    # Early abandonment when LEG2_ENTRY is disabled and no open token-holding Chart positions need monitoring
    if primary_is_watch and not leg2_enabled and not open_chart_positions:
        db.set_cooldown(primary_pos.mint, hours=config.COOLDOWN_HOURS, reason="LEG2_ENTRY_DISABLED")
        rationale = f"[WATCH ABANDON] {primary_pos.symbol}: LEG2_ENTRY_DISABLED -> SCANNING"
        kept_charts = db.update_lane_positions_only(
            lane="chart", positions=[], focus_state=FocusState.SCANNING.value,
            active_position=None, last_decision_rationale=rationale,
        )
        return {
            "focus_state": FocusState.SCANNING.value, "active_position": None, "chart_positions": kept_charts,
            "pending_order": None, "monitor_event": "LEG2_ENTRY_DISABLED", "last_decision_rationale": rationale,
        }

    # Only fetch market data for active positions (+ watcher ONLY if LEG2_ENTRY is enabled)
    all_mints = list(dict.fromkeys([cp.mint for cp in open_chart_positions] + ([primary_pos.mint] if (primary_is_watch and leg2_enabled) else [])))
    pinned_pools: dict[str, Optional[str]] = {cp.mint: cp.pair_address for cp in open_chart_positions}
    if primary_is_watch and leg2_enabled and primary_pos.mint not in pinned_pools:
        pinned_pools[primary_pos.mint] = primary_pos.pair_address
    now_dt = now or utc_now()
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient()
    st_latest = db.get_state()
    sol_balance = float(state.get("sol_balance") if "sol_balance" in state else st_latest.get("sol_balance") or 0.0)
    daily_pnl_sol = float(state.get("daily_pnl_sol") if "daily_pnl_sol" in state else st_latest.get("daily_pnl_sol") or 0.0)

    try:
        sol_prices, dex_flow = await fetch_batched_sol_prices(
            all_mints, config, client, now_ts=now_dt.timestamp(), pinned_pools=pinned_pools
        )

        # Evaluate EVERY open Chart slot (Slots 1, 2, and 3) independently with full 5-layer exit protection
        triggered_exits: list[tuple[int, int, ActivePosition, PendingOrder, str]] = []
        for slot_idx, cp in enumerate(open_chart_positions):
            ef = dex_flow.get(cp.mint) or {"buys_5m": 0, "sells_5m": 0, "liquidity_usd": 0.0, "pair": {}}
            if isinstance(ef, dict) and ef.get("decimals") is not None:
                cp.decimals = int(ef["decimals"])
            q_amt = cp.token_amount if cp.token_amount > 0 else max(1, cp.reference_token_amount or 1_000_000)
            est_val = estimate_position_sol_value(cp, sol_prices.get(cp.mint), token_decimals=cp.decimals)
            curr_val, used_q = await resolve_position_sol_value(
                cp, estimated_sol_value=est_val, config=config, client=client,
                now_dt=now_dt, has_batched_price=(cp.mint in sol_prices), quote_amount=q_amt,
            )
            b5, s5, _, c_liq = apply_dex_flow_and_pnl(
                cp, current_sol_value=curr_val, flow=ef, lane_label="chart_slot", used_exact_quote=used_q,
            )

            # Legacy Slot-1 WHALE_SCALP delegation (for backward-compatible single-slot whale exit tests)
            if str(cp.strategy or "").upper() == "WHALE_SCALP":
                from whale_lane import evaluate_whale_exit_order

                w_order, w_ev = evaluate_whale_exit_order(cp, current_sol_value=curr_val, config=config, db=db, now_dt=now_dt)
                if w_order is not None:
                    return {"active_position": cp.model_dump(), "pending_order": w_order.model_dump(), "monitor_event": w_ev}
                cp_d = cp.model_dump()
                db.update_lane_positions_only(lane="whale", positions=[cp_d], focus_state=FocusState.IN_POSITION.value, active_position=cp_d)
                return {"active_position": cp_d, "pending_order": None, "monitor_event": w_ev}

            flow_ok, liq_ok, obs_liq = _resolve_exodus_observation_inputs(ef, cp, c_liq)
            order, ev, prio = evaluate_chart_position_exit(
                cp,
                current_sol_value=curr_val,
                buys_5m=b5 if flow_ok else None,
                sells_5m=s5 if flow_ok else None,
                current_liquidity_usd=obs_liq,
                config=config,
                flow_available=flow_ok,
                liq_available=liq_ok,
                now_dt=now_dt,
            )
            if order is not None:
                triggered_exits.append((prio, slot_idx, cp, order, ev))

        keep_watch = primary_is_watch and leg2_enabled
        if primary_is_watch and not leg2_enabled:
            db.set_cooldown(primary_pos.mint, hours=config.COOLDOWN_HOURS, reason="LEG2_ENTRY_DISABLED")
        updated_open_dicts = [cp.model_dump() for cp in open_chart_positions]
        if open_chart_positions:
            db.update_lane_positions_only(
                lane="chart", positions=updated_open_dicts,
                focus_state=FocusState.WATCHING_REENTRY.value if keep_watch else FocusState.IN_POSITION.value,
                active_position=primary_pos.model_dump() if keep_watch else updated_open_dicts[0],
            )

        if triggered_exits:
            # Sort protective exits (HARD_STOP_LOSS=0, EXODUS=1) ahead of profit-taking exits
            triggered_exits.sort(key=lambda item: (item[0], item[1]))
            first_prio, _, first_cp, first_order, first_ev = triggered_exits[0]

            # Single-slot non-immediate exit returns PendingOrder for graph/caller execution compatibility
            if len(open_chart_positions) == 1 and not primary_is_watch and len(triggered_exits) == 1 and first_prio not in (PRIO_HARD_STOP, PRIO_PARTIAL_TP):
                return {"active_position": first_cp.model_dump(), "chart_positions": updated_open_dicts, "pending_order": first_order.model_dump(), "monitor_event": first_ev}

            # Multi-slot exits or immediate Hard Stop / Partial TP: execute SEQUENTIALLY with refreshed DB state
            last_out: dict[str, Any] = {}
            events: list[str] = []
            any_hs = False
            for prio, _, cp, order, ev in triggered_exits:
                if prio == PRIO_HARD_STOP:
                    any_hs = True
                st_step = db.get_state()
                live_charts = list(st_step.get("chart_positions") or updated_open_dicts)
                target_dict = next((p for p in live_charts if isinstance(p, dict) and pos_matches(p, cp.mint, cp.opened_at)), cp.model_dump())
                step_sol = sol_balance if len(events) == 0 else float(st_step["sol_balance"])
                step_pnl = daily_pnl_sol if len(events) == 0 else float(st_step["daily_pnl_sol"])
                last_out = await executor_node(
                    {
                        "focus_state": FocusState.IN_POSITION.value,
                        "sol_balance": step_sol,
                        "daily_pnl_sol": step_pnl,
                        "active_position": target_dict,
                        "chart_positions": live_charts,
                        "pending_order": order.model_dump(),
                    },
                    config=config, db=db, http_client=client,
                )
                events.append(ev)

            single_hs = len(triggered_exits) == 1 and first_prio == PRIO_HARD_STOP
            return {
                "focus_state": last_out["focus_state"],
                "sol_balance": last_out["sol_balance"],
                "daily_pnl_sol": last_out["daily_pnl_sol"],
                "active_position": last_out["active_position"],
                "chart_positions": last_out["chart_positions"],
                "pending_order": first_order.model_dump() if single_hs else None,
                "monitor_event": "; ".join(events),
                "last_execution": last_out.get("last_execution"),
                "_hard_stop_already_executed": any_hs,
            }

        if not primary_is_watch:
            return {
                "active_position": updated_open_dicts[0],
                "chart_positions": updated_open_dicts,
                "pending_order": None,
                "monitor_event": "HOLD_IN_POSITION",
            }

        # If LEG2_ENTRY is disabled while open Chart positions coexist, abandon watcher without market/Jupiter calls
        if not leg2_enabled:
            db.set_cooldown(primary_pos.mint, hours=config.COOLDOWN_HOURS, reason="LEG2_ENTRY_DISABLED")
            rationale = f"[WATCH ABANDON] {primary_pos.symbol}: LEG2_ENTRY_DISABLED -> IN_POSITION"
            next_act = updated_open_dicts[0] if updated_open_dicts else None
            next_fs = FocusState.IN_POSITION.value if next_act is not None else FocusState.SCANNING.value
            kept_charts = db.update_lane_positions_only(
                lane="chart", positions=list(updated_open_dicts),
                focus_state=next_fs, active_position=next_act, last_decision_rationale=rationale,
            )
            return {
                "focus_state": next_fs, "active_position": next_act, "chart_positions": kept_charts,
                "pending_order": None, "monitor_event": "LEG2_ENTRY_DISABLED", "last_decision_rationale": rationale,
            }

        # Refresh primary WATCHING_REENTRY quote while client is open
        pos = primary_pos
        updated_extra_charts = updated_open_dicts
        flow = dex_flow.get(pos.mint) or {"buys_5m": 0, "sells_5m": 0, "liquidity_usd": 0.0, "pair": {}}
        if isinstance(flow, dict) and flow.get("decimals") is not None:
            pos.decimals = int(flow["decimals"])
        quote_amount = pos.reference_token_amount if pos.reference_token_amount > 0 else 1_000_000
        est_sol = estimate_position_sol_value(pos, sol_prices.get(pos.mint), token_decimals=pos.decimals)
        current_sol_value, used_watch_q = await resolve_position_sol_value(
            pos, estimated_sol_value=est_sol, config=config, client=client,
            now_dt=now_dt, has_batched_price=(pos.mint in sol_prices), quote_amount=quote_amount,
        )
    finally:
        if owns_client:
            await client.aclose()

    buys_5m, sells_5m, buy_ratio_5m, current_liq = apply_dex_flow_and_pnl(
        pos, current_sol_value=current_sol_value, flow=flow, lane_label="watch_reentry", used_exact_quote=used_watch_q,
    )
    total_5m = buys_5m + sells_5m
    now_dt = now or utc_now()
    w_flow_ok, w_liq_ok, w_obs_liq = _resolve_exodus_observation_inputs(flow, pos, current_liq)
    w_ex_eval = evaluate_exodus_signal(
        buys_5m=buys_5m if w_flow_ok else None,
        sells_5m=sells_5m if w_flow_ok else None,
        entry_liquidity_usd=pos.entry_liquidity_usd,
        current_liquidity_usd=w_obs_liq,
        config=config,
        flow_available=w_flow_ok,
        liq_available=w_liq_ok,
        opened_at=pos.opened_at,
        now_dt=now_dt,
        enforce_warmup=True,
        confirm_count=pos.exodus_confirm_count,
        last_confirmed_at=pos.exodus_last_confirmed_at,
        enforce_confirmation=True,
    )
    is_exodus = w_ex_eval.triggered
    exodus_reason = w_ex_eval.detail
    if w_flow_ok:
        pos.last_5m_sell_ratio = w_ex_eval.sell_ratio_5m
    pos.exodus_confirm_count = w_ex_eval.confirm_count
    pos.exodus_last_confirmed_at = w_ex_eval.last_confirmed_at
    pos.last_exodus_status = w_ex_eval.status

    # =========================================================================
    # STATE 3: WATCHING_REENTRY
    # =========================================================================
    run_peak = pos.run_peak_sol or pos.peak_sol_value or current_sol_value
    retrace_low = pos.retrace_low_sol if pos.retrace_low_sol is not None else current_sol_value
    if current_sol_value < retrace_low:
        retrace_low = current_sol_value
        pos.retrace_low_sol = retrace_low

    def _abandon_watch(reason: str) -> dict[str, Any]:
        db.set_cooldown(pos.mint, hours=config.COOLDOWN_HOURS, reason=reason)
        rationale = f"[WATCH ABANDON] {pos.symbol}: {reason} -> SCANNING"
        next_act = updated_extra_charts[0] if updated_extra_charts else None
        next_fs = FocusState.IN_POSITION.value if next_act is not None else FocusState.SCANNING.value
        kept_charts = db.update_lane_positions_only(
            lane="chart", positions=list(updated_extra_charts),
            focus_state=next_fs, active_position=next_act, last_decision_rationale=rationale,
        )
        return {
            "focus_state": next_fs, "active_position": next_act, "chart_positions": kept_charts,
            "pending_order": None, "monitor_event": reason, "last_decision_rationale": rationale,
        }

    # Abandonment 0: LEG2_ENTRY disabled or max re-entries per mint reached -> cooldown + SCANNING
    if not config.is_chart_strategy_enabled("LEG2_ENTRY"):
        return _abandon_watch("LEG2_ENTRY_DISABLED")
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
                next_act = updated_extra_charts[0] if updated_extra_charts else None
                next_fs = FocusState.IN_POSITION.value if next_act is not None else FocusState.SCANNING.value
                kept_charts = db.update_lane_positions_only(
                    lane="chart", positions=list(updated_extra_charts),
                    focus_state=next_fs, active_position=next_act, last_decision_rationale=rationale,
                )
                return {
                    "focus_state": next_fs, "active_position": next_act, "chart_positions": kept_charts,
                    "pending_order": None, "monitor_event": "LEG_2_REENTRY_ABORT_CHASE",
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
                entry_mc_usd=pos.entry_mc_usd,
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
            db.update_lane_positions_only(
                lane="chart", positions=list(updated_extra_charts),
                focus_state=FocusState.WATCHING_REENTRY.value, active_position=pos_dict,
                last_decision_rationale=reentry_dec.rationale,
            )
            return {
                "active_position": pos_dict, "pending_order": None,
                "monitor_event": "LEG_2_REENTRY_REJECTED", "last_decision_rationale": reentry_dec.rationale,
            }

    # Continue watching without overwriting sol_balance or daily_pnl_sol
    pos_dict = pos.model_dump()
    db.update_lane_positions_only(
        lane="chart", positions=list(updated_extra_charts),
        focus_state=FocusState.WATCHING_REENTRY.value, active_position=pos_dict,
    )
    return {"active_position": pos_dict, "pending_order": None, "monitor_event": "WATCHING_REENTRY"}
