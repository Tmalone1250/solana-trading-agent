"""Report builders for the offline Sprint 1 outcome audit (audit_reports.py)."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Optional

STRESS_LOSS_LEVELS: tuple[float, ...] = (-50.0, -80.0, -95.0)
LOW_SAMPLE_THRESHOLD = 30
FAST_EXIT_FARMER_SECONDS = 45.0


def fmt_pct(val: Optional[float]) -> str:
    return f"{val:+.2f}%" if val is not None else "n/a"


def safe_median(vals: list[float]) -> Optional[float]:
    return round(statistics.median(vals), 2) if vals else None


def safe_quantile(vals: list[float], q: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    idx = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return round(s[idx], 2)


def compute_delayed_proxy_pct(r_start_pct: Optional[float], r_end_pct: Optional[float]) -> Optional[float]:
    """Milestone-based delayed-entry proxy: ((1 + r_end) / (1 + r_start) - 1) * 100."""
    if r_start_pct is None or r_end_pct is None:
        return None
    denom = 1.0 + (r_start_pct / 100.0)
    if denom <= 1e-6:
        return None
    return round((((1.0 + (r_end_pct / 100.0)) / denom) - 1.0) * 100.0, 2)


def utc_session_block(dt: Optional[datetime]) -> str:
    if dt is None:
        return "UNKNOWN"
    h = dt.hour
    if h < 8:
        return "ASIA(00-08)"
    if h < 16:
        return "EU(08-16)"
    return "US(16-24)"


def compute_bucket_expectancy(
    rows: list[dict[str, Any]],
    *,
    friction_pct: float = 3.0,
    return_col: str = "price_30m_pct",
) -> dict[str, Any]:
    """Compute sample counts, [LOW_SAMPLE] flag, observed-only and stress-tested expectancy."""
    counts = Counter(r["_state"] for r in rows)
    n_total = len(rows)
    n_complete = counts.get("COMPLETE_30M", 0)
    n_vanished = counts.get("VANISHED_OR_DEAD", 0)
    n_partial = counts.get("PARTIAL_OR_UNRESOLVED", 0)
    n_interrupted = counts.get("INTERRUPTED_BY_RESTART", 0)
    n_in_progress = counts.get("IN_PROGRESS", 0)

    complete_vals = [
        float(r[return_col]) - friction_pct
        for r in rows
        if r["_state"] == "COMPLETE_30M" and r.get(return_col) is not None
    ]
    obs_exp = round(sum(complete_vals) / len(complete_vals), 2) if complete_vals else None

    stress_exp: dict[float, Optional[float]] = {}
    denom = len(complete_vals) + n_vanished
    for loss_pct in STRESS_LOSS_LEVELS:
        if denom > 0:
            total_ret = sum(complete_vals) + (n_vanished * (loss_pct - friction_pct))
            stress_exp[loss_pct] = round(total_ret / denom, 2)
        else:
            stress_exp[loss_pct] = None

    return {
        "n_total": n_total,
        "n_complete": n_complete,
        "n_vanished": n_vanished,
        "n_partial": n_partial,
        "n_interrupted": n_interrupted,
        "n_in_progress": n_in_progress,
        "low_sample": n_complete < LOW_SAMPLE_THRESHOLD,
        "observed_expectancy_pct": obs_exp,
        "stress_expectancy_pct": stress_exp,
    }


def build_loss_attribution(
    outcomes: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    *,
    friction_pct: float = 3.0,
) -> list[dict[str, Any]]:
    """Report 1: Join closed SELL trades with outcomes and assign primary + secondary causes."""
    outcomes_by_mint: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for o in outcomes:
        outcomes_by_mint[str(o.get("mint") or "")].append(o)

    buys_by_mint: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sells: list[dict[str, Any]] = []
    for a in actions:
        act = str(a.get("action") or "").upper()
        if act in ("BUY", "REENTRY_BUY"):
            buys_by_mint[str(a.get("asset_id") or "")].append(a)
        elif act == "SELL":
            sells.append(a)

    attributed: list[dict[str, Any]] = []
    for sell in sells:
        mint = str(sell.get("asset_id") or "")
        sell_dt: Optional[datetime] = sell.get("_dt")
        pnl_pct = sell.get("pnl_pct")
        peak_pnl = sell.get("peak_pnl_pct")
        reason = str(sell.get("reason") or "")
        liq_usd = sell.get("liq_usd")

        # Match nearest prior outcome snapshot within 2h
        best_o: Optional[dict[str, Any]] = None
        best_gap = float("inf")
        for o in outcomes_by_mint.get(mint, []):
            o_dt = o.get("_dt")
            if sell_dt and o_dt:
                gap = (sell_dt - o_dt).total_seconds()
                if -60.0 <= gap <= 7200.0 and abs(gap) < best_gap:
                    best_gap = abs(gap)
                    best_o = o
            elif best_o is None:
                best_o = o

        # Match nearest prior BUY action for entry price-impact / breakout telemetry
        best_buy: Optional[dict[str, Any]] = None
        best_buy_gap = float("inf")
        for b in buys_by_mint.get(mint, []):
            b_dt = b.get("_dt")
            if sell_dt and b_dt:
                gap = (sell_dt - b_dt).total_seconds()
                if 0.0 <= gap <= 7200.0 and gap < best_buy_gap:
                    best_buy_gap = gap
                    best_buy = b

        pos_mfe = sell.get("position_mfe_pct")
        pos_mae = sell.get("position_mae_pct")
        has_pos_metrics = pos_mfe is not None or pos_mae is not None
        metrics_source = "position_anchored" if has_pos_metrics else "candidate_joined_provisional"
        mfe = pos_mfe if pos_mfe is not None else (best_o.get("mfe_pct") if best_o else None)
        mae = pos_mae if pos_mae is not None else (best_o.get("mae_pct") if best_o else None)
        p5 = best_o.get("price_5m_pct") if best_o else None
        p15 = best_o.get("price_15m_pct") if best_o else None
        p30 = best_o.get("price_30m_pct") if best_o else None
        is_loss = pnl_pct is not None and float(pnl_pct) < 0.0

        # Primary suspected cause (only for losing or non-winning trades; winners labeled WINNER)
        if not is_loss:
            primary = "WINNER"
        else:
            had_strong_upside = (mfe is not None and mfe >= 10.0) or (peak_pnl is not None and peak_pnl >= 8.0)
            recovered_post_stop = (
                "STOP" in reason.upper()
                and ((p15 is not None and p15 >= 5.0) or (p30 is not None and p30 >= 5.0))
            )
            never_worked = (
                (mfe is None or mfe < 5.0)
                and (p5 is None or p5 <= 0.0)
                and (p15 is None or p15 <= 0.0)
            )
            if had_strong_upside or recovered_post_stop:
                primary = "MANAGEMENT"
            elif never_worked:
                primary = "SELECTION"
            else:
                primary = "TIMING"

        secondary: list[str] = []
        eff_liq = liq_usd if liq_usd is not None else (best_o.get("entry_liq_usd") if best_o else None)
        if eff_liq is not None and float(eff_liq) < 40_000.0:
            secondary.append("weak_liquidity")
        b_state = (best_buy or {}).get("breakout_state") or (best_o or {}).get("breakout_state") or ""
        ema_ext = (best_buy or {}).get("breakout_ema_ext_pct")
        if str(b_state).upper() == "EXTENDED" or (ema_ext is not None and float(ema_ext) > 6.0):
            secondary.append("late_breakout_confirmation")
        if "STOP" in reason.upper() and ((p15 is not None and p15 > 0.0) or (p30 is not None and p30 > 0.0)):
            secondary.append("stop_triggered_during_recovery")

        # Trigger high_entry_friction ONLY from observed price impact (>=2.0%) or monitor-vs-fill slippage (>=2.0%)
        max_obs_impact = 0.0
        for raw_imp in ((best_buy or {}).get("jupiter_price_impact_raw"), sell.get("jupiter_price_impact_raw")):
            try:
                if raw_imp not in (None, ""):
                    max_obs_impact = max(max_obs_impact, abs(float(raw_imp)))
            except (TypeError, ValueError):
                pass
        mon_val = sell.get("monitor_sol_value")
        fill_val = sell.get("notional")
        obs_slippage_pct = (
            ((float(mon_val) - float(fill_val)) / float(mon_val)) * 100.0
            if mon_val is not None and fill_val is not None and float(mon_val) > 0
            else 0.0
        )
        if max_obs_impact >= 2.0 or obs_slippage_pct >= 2.0:
            secondary.append("high_entry_friction")

        sim_net_pnl = round(float(pnl_pct) - friction_pct, 2) if pnl_pct is not None else None
        attributed.append(
            {
                "symbol": sell.get("symbol") or "",
                "mint": mint,
                "strategy": sell.get("strategy") or "",
                "exit_reason": reason,
                "observed_pnl_pct": pnl_pct,
                "simulated_net_pnl_pct": sim_net_pnl,
                "mfe_pct": mfe,
                "mae_pct": mae,
                "metrics_source": metrics_source,
                "price_15m_pct": p15,
                "price_30m_pct": p30,
                "primary_cause": primary,
                "secondary_factors": secondary,
                "matched_outcome": best_o is not None,
            }
        )
    return attributed


def build_rejection_counterfactuals(
    outcomes: list[dict[str, Any]],
    *,
    friction_pct: float = 3.0,
) -> list[dict[str, Any]]:
    """Report 2: Group REJECTED/BLOCKED/WATCH by decision_reason vs ENTERED with t+1m delayed proxies."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for o in outcomes:
        dec = str(o.get("decision") or "").upper()
        if dec == "ENTERED":
            groups["[BASELINE] ENTERED"].append(o)
        elif dec in ("REJECTED", "BLOCKED", "WATCH"):
            reason = str(o.get("decision_reason") or dec).strip() or dec
            groups[f"{dec}: {reason[:60]}"].append(o)

    results: list[dict[str, Any]] = []
    for label, rows in sorted(groups.items(), key=lambda kv: (kv[0] != "[BASELINE] ENTERED", -len(kv[1]))):
        stats = compute_bucket_expectancy(rows, friction_pct=friction_pct)
        r_1_5 = [
            v for r in rows
            if (v := compute_delayed_proxy_pct(r.get("price_1m_pct"), r.get("price_5m_pct"))) is not None
        ]
        r_1_15 = [
            v for r in rows
            if (v := compute_delayed_proxy_pct(r.get("price_1m_pct"), r.get("price_15m_pct"))) is not None
        ]
        sessions = Counter(utc_session_block(r.get("_dt")) for r in rows)
        results.append(
            {
                "bucket": label,
                **stats,
                "median_r_1_to_5_pct": safe_median(r_1_5),
                "median_r_1_to_15_pct": safe_median(r_1_15),
                "sessions": dict(sessions),
            }
        )
    return results


def build_wallet_attribution(
    outcomes: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    whale_events: list[dict[str, Any]],
    *,
    friction_pct: float = 3.0,
) -> list[dict[str, Any]]:
    """Report 3: Time-anchored per-wallet attribution with entry rank, +1m proxy, hold_seconds & farmer flag."""
    whale_rows = [
        o for o in outcomes
        if str(o.get("triggering_wallet") or "").strip()
        or str(o.get("source") or "").upper() == "WHALE_WEBHOOK"
    ]

    # Compute wallet_entry_rank per mint (within 30m signal clusters)
    by_mint: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in whale_rows:
        by_mint[str(r.get("mint") or "")].append(r)

    rank_by_id: dict[str, int] = {}
    for mint, m_rows in by_mint.items():
        m_rows_sorted = sorted(m_rows, key=lambda x: x.get("_ts") or 0.0)
        seen_wallets: list[tuple[str, float]] = []
        for r in m_rows_sorted:
            w = str(r.get("triggering_wallet") or "UNKNOWN")
            ts = float(r.get("_ts") or 0.0)
            # Keep only events within 30m window
            window_wallets = [ww for ww, wts in seen_wallets if (ts - wts) <= 1800.0]
            if w not in window_wallets:
                window_wallets.append(w)
            rank_by_id[str(r.get("id") or "")] = window_wallets.index(w) + 1
            seen_wallets.append((w, ts))

    # Build (wallet, mint) -> sorted BUY/SELL timestamps from whale_events or WHALE_WEBHOOK actions
    wh_buys: dict[tuple[str, str], list[float]] = defaultdict(list)
    wh_sells: dict[tuple[str, str], list[float]] = defaultdict(list)
    for ev in whale_events:
        key = (str(ev.get("wallet") or ""), str(ev.get("mint") or ""))
        ts = ev.get("_ts")
        if ts is None:
            continue
        side = str(ev.get("side") or "").upper()
        if side == "BUY":
            wh_buys[key].append(float(ts))
        elif side == "SELL":
            wh_sells[key].append(float(ts))

    for a in actions:
        if str(a.get("action") or "").upper() != "WHALE_WEBHOOK":
            continue
        ts = a.get("_ts")
        if ts is None:
            continue
        mint = str(a.get("asset_id") or "")
        reason_up = str(a.get("reason") or "").upper()
        wallets = [w.strip() for w in str(a.get("triggering_whales") or "").split(",") if w.strip()]
        for w in wallets:
            if "SELL" in reason_up:
                wh_sells[(w, mint)].append(float(ts))
            elif "BUY" in reason_up:
                wh_buys[(w, mint)].append(float(ts))

    by_wallet: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in whale_rows:
        w = str(r.get("triggering_wallet") or "").strip()
        if w:
            by_wallet[w].append(r)

    report: list[dict[str, Any]] = []
    for wallet, rows in sorted(by_wallet.items(), key=lambda kv: -len(kv[1])):
        stats = compute_bucket_expectancy(rows, friction_pct=friction_pct)
        ranks = [rank_by_id.get(str(r.get("id") or ""), 1) for r in rows]
        fwd5 = [float(r["price_5m_pct"]) for r in rows if r.get("price_5m_pct") is not None]
        fwd15 = [float(r["price_15m_pct"]) for r in rows if r.get("price_15m_pct") is not None]
        r_1_5 = [
            v for r in rows
            if (v := compute_delayed_proxy_pct(r.get("price_1m_pct"), r.get("price_5m_pct"))) is not None
        ]
        r_1_15 = [
            v for r in rows
            if (v := compute_delayed_proxy_pct(r.get("price_1m_pct"), r.get("price_15m_pct"))) is not None
        ]

        holds: list[float] = []
        exit_before_exp = 0
        for r in rows:
            mint = str(r.get("mint") or "")
            ts = float(r.get("_ts") or 0.0)
            sells_after = [s for s in sorted(wh_sells.get((wallet, mint), [])) if s >= ts]
            if sells_after:
                hold_s = round(sells_after[0] - ts, 1)
                holds.append(hold_s)
                mfe = r.get("mfe_pct")
                p15 = r.get("price_15m_pct")
                p1 = r.get("price_1m_pct")
                if hold_s < 900.0 and ((mfe is not None and mfe >= 10.0) or (p15 is not None and p1 is not None and p15 > p1)):
                    exit_before_exp += 1

        med_hold = safe_median(holds)
        hold_count = len(holds)
        fast_exit_count = sum(1 for h in holds if h < FAST_EXIT_FARMER_SECONDS)
        fast_exit_rate = round(fast_exit_count / hold_count, 3) if hold_count > 0 else 0.0
        fast_farmer = (med_hold is not None and med_hold < FAST_EXIT_FARMER_SECONDS) or (
            fast_exit_count >= 2 and fast_exit_rate >= 0.30
        )
        report.append(
            {
                "wallet": wallet,
                **stats,
                "first_mover_pct": round((sum(1 for rk in ranks if rk == 1) / len(ranks)) * 100.0, 1) if ranks else 0.0,
                "avg_entry_rank": round(sum(ranks) / len(ranks), 2) if ranks else None,
                "median_fwd_5m_pct": safe_median(fwd5),
                "median_fwd_15m_pct": safe_median(fwd15),
                "median_r_1_to_5_pct": safe_median(r_1_5),
                "median_r_1_to_15_pct": safe_median(r_1_15),
                "hold_count": hold_count,
                "fast_exit_count": fast_exit_count,
                "fast_exit_rate": fast_exit_rate,
                "median_hold_seconds": med_hold,
                "wallet_exit_before_expansion_count": exit_before_exp,
                "fast_exit_farmer": fast_farmer,
            }
        )
    return report


def build_lane_comparison(
    outcomes: list[dict[str, Any]],
    *,
    friction_pct: float = 3.0,
) -> dict[str, dict[str, Any]]:
    """Report 4: Chart Lane vs. Whale Lane side-by-side metrics and UTC time-block consistency."""
    lanes = {
        "CHART_ALPHA": [r for r in outcomes if str(r.get("source") or "").upper() == "CHART_ALPHA"],
        "WHALE_WEBHOOK": [r for r in outcomes if str(r.get("source") or "").upper() == "WHALE_WEBHOOK"],
    }
    out: dict[str, dict[str, Any]] = {}
    for lane_name, rows in lanes.items():
        stats = compute_bucket_expectancy(rows, friction_pct=friction_pct)
        comp = [r for r in rows if r["_state"] == "COMPLETE_30M"]
        p5 = [float(r["price_5m_pct"]) for r in comp if r.get("price_5m_pct") is not None]
        p15 = [float(r["price_15m_pct"]) for r in comp if r.get("price_15m_pct") is not None]
        p30 = [float(r["price_30m_pct"]) for r in comp if r.get("price_30m_pct") is not None]
        mfe = [float(r["mfe_pct"]) for r in comp if r.get("mfe_pct") is not None]
        mae = [float(r["mae_pct"]) for r in comp if r.get("mae_pct") is not None]

        def _wr(vals: list[float], thr: float) -> Optional[float]:
            return round((sum(1 for v in vals if v >= thr) / len(vals)) * 100.0, 1) if vals else None

        by_block: dict[str, Optional[float]] = {}
        for blk in ("ASIA(00-08)", "EU(08-16)", "US(16-24)"):
            blk_vals = [float(r["price_30m_pct"]) - friction_pct for r in comp if utc_session_block(r.get("_dt")) == blk and r.get("price_30m_pct") is not None]
            by_block[blk] = round(sum(blk_vals) / len(blk_vals), 2) if blk_vals else None

        out[lane_name] = {
            **stats,
            "median_5m_pct": safe_median(p5),
            "median_15m_pct": safe_median(p15),
            "median_30m_pct": safe_median(p30),
            "win_rate_5_pct": _wr(mfe, 5.0),
            "win_rate_10_pct": _wr(mfe, 10.0),
            "win_rate_15_pct": _wr(mfe, 15.0),
            "mfe_p25_med_p75": (safe_quantile(mfe, 0.25), safe_median(mfe), safe_quantile(mfe, 0.75)),
            "mae_p25_med_p75": (safe_quantile(mae, 0.25), safe_median(mae), safe_quantile(mae, 0.75)),
            "time_block_expectancy": by_block,
        }
    return out
