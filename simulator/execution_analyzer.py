"""Historical Trade Execution & Accounting Analyzer (BREAKOUT_IGNITION ONLY).

Operational rules:
- Analyzes actual historical trades from `trades` and `action_log` ONLY.
- Strictly separates `OBSERVED_QUOTE`, `PRICE_ONLY`, and `UNAVAILABLE`.
- Never manufactures a Jupiter quote or synthetic fill for an untraded candidate (e.g., PAIR).
- Never treats DexScreener or market-cap-derived prices as executable fills.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Optional

from simulator import (
    EXACT_HISTORICAL,
    OBSERVED_QUOTE,
    PRICE_ONLY,
    RUNTIME_CODE_PROVENANCE,
    SUPPORTED_STRATEGY,
    UNAVAILABLE,
)
from simulator.config_profiles import ConfigProfile
from simulator.data_loader import HistoricalDataset, parse_iso_utc


def classify_exit_reason(reason: Optional[str]) -> str:
    r = str(reason or "").upper()
    for prefix in (
        "HARD_STOP_LOSS",
        "EXODUS_SELL_FLOW",
        "EXODUS_LIQ_DROP",
        "BREAKEVEN_SHIELD",
        "TRAILING_TP",
        "PARTIAL_TP",
    ):
        if prefix in r:
            return prefix
    return "OTHER" if r else "UNKNOWN"


def classify_price_source_evidence(price_source: Optional[str]) -> str:
    """Map `action_log.price_source` to mandatory execution evidence classification."""
    ps = str(price_source or "").strip().lower()
    if ps == "jupiter_exact_quote":
        return OBSERVED_QUOTE
    if ps in ("jupiter_v3", "dexscreener_native", "dexscreener_usd"):
        return PRICE_ONLY
    return UNAVAILABLE


@dataclass(frozen=True)
class ExitLegRecord:
    trade_id: int
    action_log_id: Optional[int]
    side: str  # "PARTIAL_SELL" or "SELL"
    timestamp: str
    in_token_amount: int
    out_lamports: int
    received_sol: float
    recorded_pnl_sol: Optional[float]
    recorded_pnl_pct: Optional[float]
    expected_fee_sol: float
    recomputed_pnl_sol: Optional[float]
    fee_accounting_verified: bool
    exit_category: str
    reason: str
    jupiter_price_impact_raw: Optional[str]
    trigger_price_source: Optional[str]
    trigger_evidence_class: str
    settlement_evidence_class: str
    monitor_sol_value: Optional[float]
    trigger_to_fill_slippage_sol: Optional[float]
    trigger_to_fill_slippage_pct: Optional[float]
    position_mfe_pct: Optional[float]
    position_mae_pct: Optional[float]
    hold_seconds: Optional[float]


@dataclass(frozen=True)
class TradeLifecycleRecord:
    buy_trade_id: int
    buy_action_log_id: Optional[int]
    mint: str
    symbol: str
    strategy: str
    mode: str
    entry_timestamp: str
    opened_at_tag: Optional[str]
    entry_sol: float
    entry_tokens: int
    entry_jupiter_price_impact_raw: Optional[str]
    entry_quote_evidence_class: str
    entry_fee_sol: float
    entry_daily_pnl_delta_verified: Optional[bool]
    exit_legs: tuple[ExitLegRecord, ...]
    status: str  # "CLOSED" or "OPEN_AT_SNAPSHOT"
    gross_exit_sol: float
    total_fees_sol: float
    gross_pnl_sol: Optional[float]
    net_realized_pnl_sol: Optional[float]
    net_realized_pnl_pct: Optional[float]
    five_second_monitor_trajectory_status: str


@dataclass(frozen=True)
class MintExecutionSummary:
    mint: str
    symbol: str
    runtime_code_provenance: str
    config_profile_name: str
    traded: bool
    executable_quote_availability: str  # OBSERVED_QUOTE or UNAVAILABLE
    lifecycles: tuple[TradeLifecycleRecord, ...]
    note: str


@dataclass(frozen=True)
class AggregateExecutionReport:
    strategy_scope: str
    runtime_code_provenance: str
    config_profile_name: str
    config_profile_provenance: str
    since_utc: Optional[str]
    excluded_non_breakout_trade_rows: int
    total_breakout_entries: int
    closed_lifecycles: int
    open_lifecycles: int
    partial_sell_count: int
    full_sell_count: int
    fee_per_leg_sol: float
    all_fees_verified: bool
    total_fees_paid_sol: float
    gross_pnl_sol: float
    net_realized_pnl_sol: float
    win_count: int
    loss_count: int
    win_rate_pct: float
    exit_category_counts: dict[str, int]
    trigger_evidence_counts: dict[str, int]
    slippage_by_exit_category: dict[str, dict[str, Any]]
    instant_exodus_count_under_45s: int
    lifecycles: tuple[TradeLifecycleRecord, ...]


def _match_action_log_for_trade(
    trade_row: dict[str, Any],
    exec_action_rows: list[dict[str, Any]],
    used_action_ids: set[int],
) -> Optional[dict[str, Any]]:
    t_dt = parse_iso_utc(trade_row.get("timestamp"))
    t_mint = str(trade_row.get("mint") or "").strip()
    t_side = str(trade_row.get("side") or "").upper()
    if t_dt is None or not t_mint:
        return None

    allowed_actions = {"BUY", "REENTRY_BUY"} if t_side == "BUY" else {t_side}
    best: Optional[tuple[float, dict[str, Any]]] = None
    for a in exec_action_rows:
        aid = int(a.get("id") or 0)
        if aid in used_action_ids:
            continue
        if str(a.get("asset_id") or "").strip() != t_mint:
            continue
        if str(a.get("action") or "").upper() not in allowed_actions:
            continue
        a_dt = parse_iso_utc(a.get("timestamp_utc"))
        if a_dt is None:
            continue
        diff = abs((a_dt - t_dt).total_seconds())
        if diff <= 5.0 and (best is None or diff < best[0]):
            best = (diff, a)
    if best is not None:
        used_action_ids.add(int(best[1]["id"]))
        return best[1]
    return None


def analyze_breakout_executions(
    dataset: HistoricalDataset,
    profile: ConfigProfile,
    *,
    since_utc: Optional[str] = None,
    mint_filter: Optional[str] = None,
) -> AggregateExecutionReport:
    """Reconstruct and audit all actual historical BREAKOUT_IGNITION trades."""
    since_dt = parse_iso_utc(since_utc) if since_utc else None
    cfg = profile.config

    exec_actions = [
        r
        for r in dataset.action_log
        if str(r.get("action") or "").upper() in ("BUY", "REENTRY_BUY", "PARTIAL_SELL", "SELL")
    ]
    # Map action_log id -> previous action_log row with non-null daily_realized_sol (to verify BUY fee delta)
    prev_daily_pnl_by_action_id: dict[int, float] = {}
    last_daily_pnl: Optional[float] = None
    for r in dataset.action_log:
        rid = int(r.get("id") or 0)
        if last_daily_pnl is not None:
            prev_daily_pnl_by_action_id[rid] = last_daily_pnl
        if r.get("daily_realized_sol") is not None:
            last_daily_pnl = float(r["daily_realized_sol"])

    used_action_ids: set[int] = set()
    excluded_other_strat = 0
    filtered_trades: list[dict[str, Any]] = []

    for t in dataset.trades:
        t_dt = parse_iso_utc(t.get("timestamp"))
        if since_dt is not None and (t_dt is None or t_dt < since_dt):
            continue
        if mint_filter and str(t.get("mint") or "").strip() != mint_filter:
            continue
        strat = str(t.get("strategy") or "").upper()
        if strat != SUPPORTED_STRATEGY:
            excluded_other_strat += 1
            continue
        filtered_trades.append(t)

    open_lots: list[dict[str, Any]] = []
    completed_lots: list[dict[str, Any]] = []

    for t in filtered_trades:
        side = str(t.get("side") or "").upper()
        mint = str(t.get("mint") or "").strip()
        mode = str(t.get("mode") or cfg.EXECUTION_MODE).lower()
        fee_sol = float(cfg.PAPER_TX_FEE_SOL) if mode == "paper" else 0.0
        matched_act = _match_action_log_for_trade(t, exec_actions, used_action_ids)

        if side == "BUY":
            entry_sol = float(t.get("sol_value") or 0.0)
            impact = str(t["jupiter_price_impact_raw"]) if t.get("jupiter_price_impact_raw") is not None else None
            buy_fee_ok: Optional[bool] = None
            if matched_act is not None and matched_act.get("daily_realized_sol") is not None:
                aid = int(matched_act["id"])
                if aid in prev_daily_pnl_by_action_id:
                    obs_delta = round(float(matched_act["daily_realized_sol"]) - prev_daily_pnl_by_action_id[aid], 6)
                    buy_fee_ok = abs(obs_delta - (-fee_sol)) <= 2e-5

            lot = {
                "buy_trade": t,
                "buy_action": matched_act,
                "mint": mint,
                "symbol": str(t.get("symbol") or mint[:8]),
                "strategy": str(t.get("strategy") or SUPPORTED_STRATEGY),
                "mode": mode,
                "entry_timestamp": str(t.get("timestamp") or ""),
                "opened_at_tag": None,
                "entry_sol": entry_sol,
                "remaining_basis_sol": entry_sol,
                "entry_tokens": int(t.get("out_amount") or 0),
                "entry_impact": impact,
                "entry_evidence": OBSERVED_QUOTE if impact is not None or int(t.get("out_amount") or 0) > 0 else UNAVAILABLE,
                "entry_fee_sol": fee_sol,
                "buy_fee_ok": buy_fee_ok,
                "exit_legs": [],
                "closed": False,
            }
            open_lots.append(lot)

        elif side in ("PARTIAL_SELL", "SELL"):
            opened_at = str(t.get("opened_at") or "").strip()
            target_lot: Optional[dict[str, Any]] = None
            # Match by mint + opened_at first, else earliest open lot for mint
            for lot in open_lots:
                if not lot["closed"] and lot["mint"] == mint:
                    if opened_at and lot["opened_at_tag"] and lot["opened_at_tag"] == opened_at:
                        target_lot = lot
                        break
            if target_lot is None:
                for lot in open_lots:
                    if not lot["closed"] and lot["mint"] == mint:
                        target_lot = lot
                        break

            received_sol = float(t.get("sol_value") or 0.0)
            rec_pnl = float(t["pnl_sol"]) if t.get("pnl_sol") is not None else None
            rec_pnl_pct = float(t["pnl_pct"]) * 100.0 if t.get("pnl_pct") is not None else None
            impact = str(t["jupiter_price_impact_raw"]) if t.get("jupiter_price_impact_raw") is not None else None

            if target_lot is not None:
                if opened_at and not target_lot["opened_at_tag"]:
                    target_lot["opened_at_tag"] = opened_at
                if side == "PARTIAL_SELL":
                    frac = float(cfg.PARTIAL_TP_SELL_FRACTION)
                    cost_leg = round(target_lot["entry_sol"] * frac, 6)
                    target_lot["remaining_basis_sol"] = round(
                        max(0.0, target_lot["remaining_basis_sol"] - cost_leg), 6
                    )
                else:
                    cost_leg = float(target_lot["remaining_basis_sol"])
                recomp_pnl: Optional[float] = round(received_sol - cost_leg - fee_sol, 6)
                fee_ok = rec_pnl is not None and abs(rec_pnl - recomp_pnl) <= 2e-5
            else:
                recomp_pnl = None
                fee_ok = False

            mon_sol = (
                float(matched_act["monitor_sol_value"])
                if matched_act is not None and matched_act.get("monitor_sol_value") is not None
                else None
            )
            p_src = (
                str(matched_act["price_source"])
                if matched_act is not None and matched_act.get("price_source") is not None
                else None
            )
            trig_ev = classify_price_source_evidence(p_src)
            settle_ev = OBSERVED_QUOTE if impact is not None else PRICE_ONLY

            slip_sol: Optional[float] = None
            slip_pct: Optional[float] = None
            if mon_sol is not None and mon_sol > 0:
                slip_sol = round(received_sol - mon_sol, 6)
                slip_pct = round(((received_sol - mon_sol) / mon_sol) * 100.0, 4)

            leg = ExitLegRecord(
                trade_id=int(t["id"]),
                action_log_id=int(matched_act["id"]) if matched_act is not None else None,
                side=side,
                timestamp=str(t.get("timestamp") or ""),
                in_token_amount=int(t.get("in_amount") or 0),
                out_lamports=int(t.get("out_amount") or 0),
                received_sol=received_sol,
                recorded_pnl_sol=rec_pnl,
                recorded_pnl_pct=round(rec_pnl_pct, 4) if rec_pnl_pct is not None else None,
                expected_fee_sol=fee_sol,
                recomputed_pnl_sol=recomp_pnl,
                fee_accounting_verified=fee_ok,
                exit_category=classify_exit_reason(t.get("reason")),
                reason=str(t.get("reason") or ""),
                jupiter_price_impact_raw=impact,
                trigger_price_source=p_src,
                trigger_evidence_class=trig_ev,
                settlement_evidence_class=settle_ev,
                monitor_sol_value=mon_sol,
                trigger_to_fill_slippage_sol=slip_sol,
                trigger_to_fill_slippage_pct=slip_pct,
                position_mfe_pct=(
                    float(matched_act["position_mfe_pct"])
                    if matched_act is not None and matched_act.get("position_mfe_pct") is not None
                    else None
                ),
                position_mae_pct=(
                    float(matched_act["position_mae_pct"])
                    if matched_act is not None and matched_act.get("position_mae_pct") is not None
                    else None
                ),
                hold_seconds=(
                    float(matched_act["hold_seconds"])
                    if matched_act is not None and matched_act.get("hold_seconds") is not None
                    else None
                ),
            )

            if target_lot is not None:
                target_lot["exit_legs"].append(leg)
                if side == "SELL":
                    target_lot["closed"] = True
                    completed_lots.append(target_lot)

    all_lots = sorted(open_lots, key=lambda item: item["entry_timestamp"])
    lifecycles: list[TradeLifecycleRecord] = []
    exit_cat_counts: dict[str, int] = {}
    trig_ev_counts: dict[str, int] = {}
    slip_by_cat: dict[str, list[float]] = {}
    partial_cnt = 0
    sell_cnt = 0
    instant_exodus_cnt = 0
    all_fees_ok = True
    total_fees = 0.0
    total_gross_pnl = 0.0
    total_net_pnl = 0.0
    win_cnt = 0
    loss_cnt = 0

    for lot in all_lots:
        legs: tuple[ExitLegRecord, ...] = tuple(lot["exit_legs"])
        gross_out = round(sum(lg.received_sol for lg in legs), 6)
        fees = round(float(lot["entry_fee_sol"]) + sum(lg.expected_fee_sol for lg in legs), 6)
        total_fees = round(total_fees + fees, 6)

        if lot["buy_fee_ok"] is False:
            all_fees_ok = False
        for lg in legs:
            if not lg.fee_accounting_verified:
                all_fees_ok = False
            if lg.side == "PARTIAL_SELL":
                partial_cnt += 1
            else:
                sell_cnt += 1
            exit_cat_counts[lg.exit_category] = exit_cat_counts.get(lg.exit_category, 0) + 1
            trig_ev_counts[lg.trigger_evidence_class] = trig_ev_counts.get(lg.trigger_evidence_class, 0) + 1
            if lg.trigger_to_fill_slippage_pct is not None:
                slip_by_cat.setdefault(lg.exit_category, []).append(lg.trigger_to_fill_slippage_pct)
            if lg.exit_category == "EXODUS_SELL_FLOW" and lg.hold_seconds is not None and lg.hold_seconds <= 45.0:
                instant_exodus_cnt += 1

        if lot["closed"]:
            gross_pnl = round(gross_out - float(lot["entry_sol"]), 6)
            net_pnl = round(gross_out - float(lot["entry_sol"]) - fees, 6)
            net_pct = round((net_pnl / float(lot["entry_sol"])) * 100.0, 4) if float(lot["entry_sol"]) > 0 else 0.0
            total_gross_pnl = round(total_gross_pnl + gross_pnl, 6)
            total_net_pnl = round(total_net_pnl + net_pnl, 6)
            if net_pnl > 0:
                win_cnt += 1
            else:
                loss_cnt += 1
            status = "CLOSED"
        else:
            gross_pnl = None
            net_pnl = None
            net_pct = None
            status = "OPEN_AT_SNAPSHOT"

        lifecycles.append(
            TradeLifecycleRecord(
                buy_trade_id=int(lot["buy_trade"]["id"]),
                buy_action_log_id=int(lot["buy_action"]["id"]) if lot["buy_action"] is not None else None,
                mint=lot["mint"],
                symbol=lot["symbol"],
                strategy=lot["strategy"],
                mode=lot["mode"],
                entry_timestamp=lot["entry_timestamp"],
                opened_at_tag=lot["opened_at_tag"],
                entry_sol=float(lot["entry_sol"]),
                entry_tokens=int(lot["entry_tokens"]),
                entry_jupiter_price_impact_raw=lot["entry_impact"],
                entry_quote_evidence_class=lot["entry_evidence"],
                entry_fee_sol=float(lot["entry_fee_sol"]),
                entry_daily_pnl_delta_verified=lot["buy_fee_ok"],
                exit_legs=legs,
                status=status,
                gross_exit_sol=gross_out,
                total_fees_sol=fees,
                gross_pnl_sol=gross_pnl,
                net_realized_pnl_sol=net_pnl,
                net_realized_pnl_pct=net_pct,
                five_second_monitor_trajectory_status=UNAVAILABLE,
            )
        )

    slip_summary: dict[str, dict[str, Any]] = {}
    for cat, vals in sorted(slip_by_cat.items()):
        slip_summary[cat] = {
            "count": len(vals),
            "min_pct": round(min(vals), 4),
            "median_pct": round(statistics.median(vals), 4),
            "mean_pct": round(statistics.mean(vals), 4),
            "max_pct": round(max(vals), 4),
        }

    closed_cnt = sum(1 for lc in lifecycles if lc.status == "CLOSED")
    open_cnt = len(lifecycles) - closed_cnt
    fee_per_leg = float(cfg.PAPER_TX_FEE_SOL) if cfg.EXECUTION_MODE == "paper" else 0.0

    return AggregateExecutionReport(
        strategy_scope=SUPPORTED_STRATEGY,
        runtime_code_provenance=RUNTIME_CODE_PROVENANCE,
        config_profile_name=profile.name,
        config_profile_provenance=profile.profile_provenance,
        since_utc=since_utc,
        excluded_non_breakout_trade_rows=excluded_other_strat,
        total_breakout_entries=len(lifecycles),
        closed_lifecycles=closed_cnt,
        open_lifecycles=open_cnt,
        partial_sell_count=partial_cnt,
        full_sell_count=sell_cnt,
        fee_per_leg_sol=fee_per_leg,
        all_fees_verified=all_fees_ok,
        total_fees_paid_sol=total_fees,
        gross_pnl_sol=total_gross_pnl,
        net_realized_pnl_sol=total_net_pnl,
        win_count=win_cnt,
        loss_count=loss_cnt,
        win_rate_pct=round((win_cnt / closed_cnt) * 100.0, 2) if closed_cnt > 0 else 0.0,
        exit_category_counts=exit_cat_counts,
        trigger_evidence_counts=trig_ev_counts,
        slippage_by_exit_category=slip_summary,
        instant_exodus_count_under_45s=instant_exodus_cnt,
        lifecycles=tuple(lifecycles),
    )


def analyze_mint_execution(
    dataset: HistoricalDataset,
    *,
    mint: str,
    symbol: str,
    profile: ConfigProfile,
    since_utc: Optional[str] = None,
) -> MintExecutionSummary:
    """Return execution evidence for a single mint, never fabricating quotes for untraded tokens."""
    agg = analyze_breakout_executions(
        dataset, profile, since_utc=since_utc, mint_filter=mint
    )
    if not agg.lifecycles:
        return MintExecutionSummary(
            mint=mint,
            symbol=symbol,
            runtime_code_provenance=RUNTIME_CODE_PROVENANCE,
            config_profile_name=profile.name,
            traded=False,
            executable_quote_availability=UNAVAILABLE,
            lifecycles=(),
            note=(
                "Candidate was NOT traded in BREAKOUT_IGNITION. Historical Jupiter order quotes, "
                "route price impact, and executable slippage are UNAVAILABLE; chart/DexScreener "
                "prices are never treated as executable fills."
            ),
        )
    return MintExecutionSummary(
        mint=mint,
        symbol=symbol,
        runtime_code_provenance=RUNTIME_CODE_PROVENANCE,
        config_profile_name=profile.name,
        traded=True,
        executable_quote_availability=OBSERVED_QUOTE,
        lifecycles=agg.lifecycles,
        note=f"{len(agg.lifecycles)} historical BREAKOUT_IGNITION trade lifecycle(s) reconstructed from observed records.",
    )


__all__ = [
    "AggregateExecutionReport",
    "ExitLegRecord",
    "MintExecutionSummary",
    "TradeLifecycleRecord",
    "analyze_breakout_executions",
    "analyze_mint_execution",
    "classify_exit_reason",
    "classify_price_source_evidence",
    "EXACT_HISTORICAL",
]
