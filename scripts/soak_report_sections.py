"""Markdown section builders (Sections 3–8) for scripts/export_soak_report.py."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Optional


def safe_float(val: Any) -> Optional[float]:
    if val is None or val == "" or isinstance(val, bool):
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def safe_json(val: Any) -> dict[str, Any]:
    if isinstance(val, dict):
        return val
    if not val or not isinstance(val, str):
        return {}
    try:
        parsed = json.loads(val)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def parse_iso_utc(raw: Any) -> Optional[datetime]:
    if not raw or not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).astimezone(timezone.utc)
    except ValueError:
        return None


def percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    f, c = math.floor(k), math.ceil(k)
    return sorted_vals[int(k)] if f == c else (sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f))


def dist_stats(vals: list[float]) -> dict[str, float]:
    clean = sorted(v for v in vals if v is not None and math.isfinite(v))
    if not clean:
        return {"count": 0, "min": 0.0, "median": 0.0, "mean": 0.0, "p90": 0.0, "max": 0.0}
    return {
        "count": len(clean), "min": clean[0], "median": percentile(clean, 50.0),
        "mean": sum(clean) / len(clean), "p90": percentile(clean, 90.0), "max": clean[-1],
    }


def fmt_num(val: Optional[float], digits: int = 2, signed: bool = False) -> str:
    if val is None or not math.isfinite(val):
        return "—"
    return format(val, f"{'+' if signed else ''}.{digits}f")


def short_mint(m: Any) -> str:
    s = str(m or "").strip()
    return f"{s[:6]}..{s[-4:]}" if len(s) > 12 else (s or "—")


def _enrich_trades_with_action_mfe_mae(trades_rows: list[dict[str, Any]], action_rows: list[dict[str, Any]]) -> None:
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for a in action_rows:
        act = str(a.get("action") or "").upper()
        if act in {"SELL", "PARTIAL_SELL"}:
            by_key[(str(a.get("asset_id") or ""), act)].append(a)
    for t in trades_rows:
        side = str(t.get("side") or "").upper()
        if side not in {"SELL", "PARTIAL_SELL"}:
            continue
        t_dt = parse_iso_utc(t.get("timestamp"))
        best, best_diff = None, 999999.0
        for c in by_key.get((str(t.get("mint") or ""), side), []):
            c_dt = parse_iso_utc(c.get("timestamp_utc"))
            diff = abs((t_dt - c_dt).total_seconds()) if (t_dt and c_dt) else 0.0
            if diff < best_diff:
                best_diff, best = diff, c
        if best is not None and best_diff <= 15.0:
            mfe = safe_float(best.get("position_mfe_pct"))
            t["_mfe_pct"] = mfe if mfe is not None else safe_float(best.get("peak_pnl_pct"))
            t["_mae_pct"] = safe_float(best.get("position_mae_pct"))


def _compute_trade_window_metrics(rows: list[dict[str, Any]], open_mints_now: set[str]) -> dict[str, Any]:
    buys = [r for r in rows if str(r.get("side") or "").upper() == "BUY"]
    exits = [r for r in rows if str(r.get("side") or "").upper() in {"SELL", "PARTIAL_SELL"}]
    full_sells = [r for r in rows if str(r.get("side") or "").upper() == "SELL"]
    sold_keys = {(str(r.get("mint") or ""), str(r.get("opened_at") or "")) for r in full_sells}
    sold_mints = {str(r.get("mint") or "") for r in full_sells}
    open_cnt = sum(
        1 for b in buys
        if str(b.get("mint") or "") in open_mints_now
        or ((str(b.get("mint") or ""), str(b.get("opened_at") or b.get("timestamp") or "")) not in sold_keys and str(b.get("mint") or "") not in sold_mints)
    )
    pnl_sols, pnl_pcts, mfes, maes = [], [], [], []
    wins = 0
    by_setup: dict[str, list[float]] = defaultdict(list)
    by_reason: Counter[str] = Counter()
    for r in exits:
        ps, pp = safe_float(r.get("pnl_sol")), safe_float(r.get("pnl_pct"))
        if pp is not None and abs(pp) <= 3.0:
            pp *= 100.0
        if ps is not None:
            pnl_sols.append(ps)
            if ps > 0:
                wins += 1
        if pp is not None:
            pnl_pcts.append(pp)
        if (mfe := safe_float(r.get("_mfe_pct"))) is not None:
            mfes.append(mfe)
        if (mae := safe_float(r.get("_mae_pct"))) is not None:
            maes.append(mae)
        by_setup[str(r.get("strategy") or "SCALPER")].append(ps or 0.0)
        by_reason[str(r.get("reason") or "UNKNOWN").split("(")[0].strip()] += 1
    closed_cnt = len(exits)
    return {
        "total_rows": len(rows), "buys": len(buys), "closed": closed_cnt, "open": open_cnt,
        "wins": wins, "win_rate": (wins / closed_cnt * 100.0) if closed_cnt > 0 else 0.0,
        "total_pnl_sol": sum(pnl_sols),
        "avg_pnl_sol": (sum(pnl_sols) / len(pnl_sols)) if pnl_sols else 0.0,
        "avg_pnl_pct": (sum(pnl_pcts) / len(pnl_pcts)) if pnl_pcts else 0.0,
        "pnl_sol_dist": dist_stats(pnl_sols), "pnl_pct_dist": dist_stats(pnl_pcts),
        "mfe_dist": dist_stats(mfes), "mae_dist": dist_stats(maes),
        "by_setup": dict(by_setup), "by_reason": dict(by_reason),
    }


def render_trades_section(
    pre_trades: list[dict[str, Any]],
    soak_trades: list[dict[str, Any]],
    all_actions: list[dict[str, Any]],
    open_mints_now: set[str],
) -> tuple[str, dict[str, Any]]:
    _enrich_trades_with_action_mfe_mae(pre_trades, all_actions)
    _enrich_trades_with_action_mfe_mae(soak_trades, all_actions)
    pre_m = _compute_trade_window_metrics(pre_trades, set())
    soak_m = _compute_trade_window_metrics(soak_trades, open_mints_now)
    lines = [
        "## 3. Trades Performance (`trades`)", "", "### Side-by-Side Window Summary",
        "| Metric | Pre-Soak Baseline | Active Soak Window |", "| :--- | ---: | ---: |",
        f"| Total Trade Events (Rows) | {pre_m['total_rows']} | {soak_m['total_rows']} |",
        f"| Entries (`BUY`) | {pre_m['buys']} | {soak_m['buys']} |",
        f"| Exits (`PARTIAL_SELL` + `SELL`) | {pre_m['closed']} | {soak_m['closed']} |",
        f"| Currently Open Positions | {pre_m['open']} | {soak_m['open']} |",
        f"| Winning Exits / Win Rate | {pre_m['wins']} ({pre_m['win_rate']:.1f}%) | {soak_m['wins']} ({soak_m['win_rate']:.1f}%) |",
        f"| Net Realized PnL (SOL) | {fmt_num(pre_m['total_pnl_sol'], 4, True)} SOL | {fmt_num(soak_m['total_pnl_sol'], 4, True)} SOL |",
        f"| Average Exit PnL (SOL / %) | {fmt_num(pre_m['avg_pnl_sol'], 4, True)} SOL ({fmt_num(pre_m['avg_pnl_pct'], 2, True)}%) | {fmt_num(soak_m['avg_pnl_sol'], 4, True)} SOL ({fmt_num(soak_m['avg_pnl_pct'], 2, True)}%) |",
        f"| Exit PnL % (Min / Median / P90 / Max) | {pre_m['pnl_pct_dist']['min']:+.2f}% / {pre_m['pnl_pct_dist']['median']:+.2f}% / {pre_m['pnl_pct_dist']['p90']:+.2f}% / {pre_m['pnl_pct_dist']['max']:+.2f}% | {soak_m['pnl_pct_dist']['min']:+.2f}% / {soak_m['pnl_pct_dist']['median']:+.2f}% / {soak_m['pnl_pct_dist']['p90']:+.2f}% / {soak_m['pnl_pct_dist']['max']:+.2f}% |",
        f"| Position MFE % (Min / Median / Mean / P90 / Max) | {pre_m['mfe_dist']['min']:+.2f}% / {pre_m['mfe_dist']['median']:+.2f}% / {pre_m['mfe_dist']['mean']:+.2f}% / {pre_m['mfe_dist']['p90']:+.2f}% / {pre_m['mfe_dist']['max']:+.2f}% | {soak_m['mfe_dist']['min']:+.2f}% / {soak_m['mfe_dist']['median']:+.2f}% / {soak_m['mfe_dist']['mean']:+.2f}% / {soak_m['mfe_dist']['p90']:+.2f}% / {soak_m['mfe_dist']['max']:+.2f}% |",
        f"| Position MAE % (Min / Median / Mean / P90 / Max) | {pre_m['mae_dist']['min']:+.2f}% / {pre_m['mae_dist']['median']:+.2f}% / {pre_m['mae_dist']['mean']:+.2f}% / {pre_m['mae_dist']['p90']:+.2f}% / {pre_m['mae_dist']['max']:+.2f}% | {soak_m['mae_dist']['min']:+.2f}% / {soak_m['mae_dist']['median']:+.2f}% / {soak_m['mae_dist']['mean']:+.2f}% / {soak_m['mae_dist']['p90']:+.2f}% / {soak_m['mae_dist']['max']:+.2f}% |",
        "", "### Breakdown by Strategy / Setup Type",
        "| Strategy / Setup | Pre-Soak Exits | Pre-Soak Net SOL | Active Soak Exits | Active Soak Net SOL |", "| :--- | ---: | ---: | ---: | ---: |",
    ]
    all_setups = sorted(set(pre_m["by_setup"].keys()) | set(soak_m["by_setup"].keys()))
    if not all_setups:
        lines.append("| *(none)* | 0 | 0.0000 | 0 | 0.0000 |")
    for st in all_setups:
        p_list, s_list = pre_m["by_setup"].get(st, []), soak_m["by_setup"].get(st, [])
        lines.append(f"| `{st}` | {len(p_list)} | {sum(p_list):+.4f} | {len(s_list)} | {sum(s_list):+.4f} |")
    lines.extend(["", "### Breakdown by Exit Reason", "| Exit Reason | Pre-Soak Count | Active Soak Count |", "| :--- | ---: | ---: |"])
    all_reasons = sorted(set(pre_m["by_reason"].keys()) | set(soak_m["by_reason"].keys()))
    if not all_reasons:
        lines.append("| *(none)* | 0 | 0 |")
    for rsn in all_reasons:
        lines.append(f"| `{rsn}` | {pre_m['by_reason'].get(rsn, 0)} | {soak_m['by_reason'].get(rsn, 0)} |")
    lines.extend([
        "", f"### Active Soak Window Trade Ledger ({len(soak_trades)} rows)",
        "| Timestamp (UTC) | Side | Symbol | Mint | Setup | SOL Value | PnL % | PnL SOL | MFE % | MAE % | Status | Reason |",
        "| :--- | :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | :--- | :--- |",
    ])
    if not soak_trades:
        lines.append("| *(no trades in soak window)* | — | — | — | — | — | — | — | — | — | — | — |")
    for r in soak_trades:
        side, mint = str(r.get("side") or "").upper(), str(r.get("mint") or "")
        pp = safe_float(r.get("pnl_pct"))
        if pp is not None and abs(pp) <= 3.0:
            pp *= 100.0
        status = "OPEN" if (side == "BUY" and mint in open_mints_now) else ("ENTRY" if side == "BUY" else ("PARTIAL" if side == "PARTIAL_SELL" else "CLOSED"))
        lines.append(
            f"| `{r.get('timestamp', '')}` | `{side}` | **{r.get('symbol', '')}** | `{short_mint(mint)}` | `{r.get('strategy') or 'SCALPER'}` | {fmt_num(safe_float(r.get('sol_value')), 4)} | {fmt_num(pp, 2, True)} | {fmt_num(safe_float(r.get('pnl_sol')), 4, True)} | {fmt_num(safe_float(r.get('_mfe_pct')), 2, True)} | {fmt_num(safe_float(r.get('_mae_pct')), 2, True)} | `{status}` | `{r.get('reason', '')}` |"
        )
    lines.append("")
    return "\n".join(lines), soak_m


def render_decisions_section(pre_rows: list[dict[str, Any]], soak_rows: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    def _tally(rows: list[dict[str, Any]]) -> dict[str, Any]:
        actions, setups = Counter(), Counter()
        metrics: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            actions[str(r.get("action") or "UNKNOWN").upper()] += 1
            sc = safe_json(r.get("scores_json"))
            setups[str(sc.get("setup_type") or "NONE").upper()] += 1
            for k, col in [("narrative", "narrative_strength"), ("momentum", "momentum_quality"), ("vamp", "is_copycat_vamp"), ("leg2", "leg2_score"), ("rsi", "rsi_5m")]:
                if (v := safe_float(sc.get(col))) is not None:
                    metrics[k].append(v)
            if (c := safe_float(r.get("confidence")) or safe_float(sc.get("action_confidence"))) is not None:
                metrics["confidence"].append(c)
        return {"actions": actions, "setups": setups, "metrics": {k: dist_stats(v) for k, v in metrics.items()}}

    pre_t, soak_t = _tally(pre_rows), _tally(soak_rows)
    lines = ["## 4. Alpha Decisions & Stage 3 Verification (`decisions_log`)", "", "### Action Breakdown (Pre-Soak vs. Active Soak)", "| Action | Pre-Soak Count | Active Soak Count |", "| :--- | ---: | ---: |"]
    for act in sorted(set(pre_t["actions"].keys()) | set(soak_t["actions"].keys())):
        lines.append(f"| `{act}` | {pre_t['actions'].get(act, 0)} | {soak_t['actions'].get(act, 0)} |")
    if not (pre_t["actions"] or soak_t["actions"]):
        lines.append("| *(none)* | 0 | 0 |")
    lines.extend(["", "### Setup Type Breakdown (Pre-Soak vs. Active Soak)", "| Setup Type | Pre-Soak Count | Active Soak Count |", "| :--- | ---: | ---: |"])
    for st in sorted(set(pre_t["setups"].keys()) | set(soak_t["setups"].keys())):
        lines.append(f"| `{st}` | {pre_t['setups'].get(st, 0)} | {soak_t['setups'].get(st, 0)} |")
    if not (pre_t["setups"] or soak_t["setups"]):
        lines.append("| *(none)* | 0 | 0 |")
    lines.extend(["", "### Score Distributions (Pre-Soak vs. Active Soak)", "| Metric | Window | Count | Min | Median | Mean | P90 | Max |", "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for m_key in ("narrative", "momentum", "vamp", "leg2", "rsi", "confidence"):
        for label, d_map in (("Pre-Soak", pre_t["metrics"]), ("Active Soak", soak_t["metrics"])):
            st = d_map.get(m_key, dist_stats([]))
            lines.append(f"| `{m_key}` | {label} | {int(st['count'])} | {st['min']:.2f} | {st['median']:.2f} | {st['mean']:.2f} | {st['p90']:.2f} | {st['max']:.2f} |")

    by_mint: dict[str, dict[str, Any]] = {}
    breakout_rows: list[dict[str, Any]] = []
    for r in soak_rows:
        mint, sym = str(r.get("mint") or ""), str(r.get("symbol") or "UNKNOWN")
        sc = safe_json(r.get("scores_json"))
        setup, act = str(sc.get("setup_type") or "NONE").upper(), str(r.get("action") or sc.get("action") or "UNKNOWN").upper()
        mom, narr = safe_float(sc.get("momentum_quality")), safe_float(sc.get("narrative_strength"))
        entry = by_mint.setdefault(mint, {"symbol": sym, "count": 0, "moms": [], "narrs": [], "actions": Counter(), "setups": Counter(), "cache_hits": 0})
        entry["count"] += 1
        if mom is not None:
            entry["moms"].append(round(mom, 3))
        if narr is not None:
            entry["narrs"].append(round(narr, 3))
        entry["actions"][act] += 1
        if setup != "NONE":
            entry["setups"][setup] += 1
            bt = sc.get("breakout_telemetry") or {}
            breakout_rows.append({
                "timestamp": r.get("timestamp", ""), "symbol": sym, "mint": mint, "setup": setup,
                "eligible": bool(sc.get("scalper_eligible", False)), "action": act, "approved": bool(sc.get("approved", False)),
                "mom": mom, "narr": narr, "rsi": safe_float(sc.get("rsi_5m")), "leg2": safe_float(sc.get("leg2_score")),
                "bt_score": bt.get("score"), "bt_state": bt.get("state"),
            })
        if sc.get("narrative_cache_hit"):
            entry["cache_hits"] += 1

    top25 = sorted(by_mint.items(), key=lambda kv: (-kv[1]["count"], kv[0]))[:25]
    lines.extend(["", "### Stage 3 Unfrozen-Momentum Verification Table (Top 25 Soak Tokens)", "| Symbol | Mint | Evals | Cache Hits | Distinct Momentum | Momentum Min..Max | Narrative Min..Max | Actions | Non-NONE Setups |", "| :--- | :--- | ---: | ---: | ---: | :--- | :--- | :--- | :--- |"])
    if not top25:
        lines.append("| *(no soak decisions)* | — | 0 | 0 | 0 | — | — | — | — |")
    for mint, info in top25:
        moms, narrs = info["moms"], info["narrs"]
        m_range = f"{min(moms):.2f}..{max(moms):.2f}" if moms else "—"
        n_range = f"{min(narrs):.2f}..{max(narrs):.2f}" if narrs else "—"
        act_str = ", ".join(f"{k}:{v}" for k, v in info["actions"].most_common()) or "—"
        set_str = ", ".join(f"{k}:{v}" for k, v in info["setups"].most_common()) or "NONE"
        lines.append(f"| **{info['symbol']}** | `{short_mint(mint)}` | {info['count']} | {info['cache_hits']} | {len(set(moms))} | `{m_range}` | `{n_range}` | `{act_str}` | `{set_str}` |")

    total_bo, cap_bo = len(breakout_rows), breakout_rows[:150]
    lines.extend(["", f"### Complete Soak Breakout & Setup Log ({total_bo} non-NONE evaluations; showing {len(cap_bo)})", "| Timestamp (UTC) | Symbol | Mint | Setup | Eligible | Action | Approved | Mom | Narr | RSI | Leg2 | BO Score/State |", "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | :--- |"])
    if not cap_bo:
        lines.append("| *(no non-NONE setups in soak window)* | — | — | — | — | — | — | — | — | — | — | — |")
    for b in cap_bo:
        lines.append(f"| `{b['timestamp']}` | **{b['symbol']}** | `{short_mint(b['mint'])}` | `{b['setup']}` | `{b['eligible']}` | `{b['action']}` | `{b['approved']}` | {fmt_num(b['mom'])} | {fmt_num(b['narr'])} | {fmt_num(b['rsi'], 1)} | {fmt_num(b['leg2'], 1)} | `{b['bt_score'] or '—'}/{b['bt_state'] or '—'}` |")
    if total_bo > 150:
        lines.append(f"\n> *Note: Showing first 150 of {total_bo} non-NONE setup evaluations in the Active Soak window.*")
    lines.append("")
    return "\n".join(lines), {"breakout_count": total_bo, "unique_tokens": len(by_mint)}


def render_outcomes_and_pipeline_sections(
    pre_outcomes: list[dict[str, Any]], soak_outcomes: list[dict[str, Any]],
    pre_actions: list[dict[str, Any]], soak_actions: list[dict[str, Any]],
    soak_whale_events: list[dict[str, Any]], all_whale_events: list[dict[str, Any]],
    whale_signals: list[dict[str, Any]], whale_decisions: list[dict[str, Any]],
    state_tables: dict[str, list[dict[str, Any]]], since_dt: datetime,
) -> str:
    lines: list[str] = [
        "## 5. Candidate Outcomes & Missed-Opportunity Telemetry (`candidate_outcomes`)", "",
        "### Forward Return & MFE/MAE Distributions (Pre-Soak vs. Active Soak)",
        "| Metric | Window | Count | Min % | Median % | Mean % | P90 % | Max % |", "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for col in ("mfe_pct", "mae_pct", "price_1m_pct", "price_5m_pct", "price_15m_pct", "price_30m_pct"):
        for label, subset in (("Pre-Soak", pre_outcomes), ("Active Soak", soak_outcomes)):
            st = dist_stats([v for r in subset if (v := safe_float(r.get(col))) is not None])
            lines.append(f"| `{col}` | {label} | {int(st['count'])} | {st['min']:+.2f}% | {st['median']:+.2f}% | {st['mean']:+.2f}% | {st['p90']:+.2f}% | {st['max']:+.2f}% |")

    ranked_soak = sorted(soak_outcomes, key=lambda r: (safe_float(r.get("mfe_pct")) if safe_float(r.get("mfe_pct")) is not None else -9999.0), reverse=True)[:25]
    lines.extend(["", "### Top 25 Active Soak Candidates Ranked by MFE % (Highlighting Missed Runners)", "| Rank | Discovered At (UTC) | Symbol | Mint | Source | Decision | Status Tag | MFE % | MAE % | 5m % | 15m % | 30m % | Reason |", "| ---: | :--- | :--- | :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | :--- |"])
    if not ranked_soak:
        lines.append("| — | *(no soak candidate outcomes)* | — | — | — | — | — | — | — | — | — | — | — |")
    for idx, r in enumerate(ranked_soak, 1):
        dec, mfe = str(r.get("decision") or "UNKNOWN").upper(), safe_float(r.get("mfe_pct"))
        tag = "TRADED" if dec in {"ENTERED", "BUY", "APPROVED"} else ("MISSED_RUNNER" if (mfe or 0.0) >= 20.0 else "WATCHED/SKIPPED")
        lines.append(f"| {idx} | `{r.get('discovered_at', '')}` | **{r.get('symbol', '')}** | `{short_mint(r.get('mint'))}` | `{r.get('source', '')}` | `{dec}` | **{tag}** | {fmt_num(mfe, 2, True)}% | {fmt_num(safe_float(r.get('mae_pct')), 2, True)}% | {fmt_num(safe_float(r.get('price_5m_pct')), 2, True)}% | {fmt_num(safe_float(r.get('price_15m_pct')), 2, True)}% | {fmt_num(safe_float(r.get('price_30m_pct')), 2, True)}% | `{str(r.get('decision_reason') or '')[:60]}` |")

    def _classify_actions(rows: list[dict[str, Any]]) -> tuple[Counter[str], Counter[str]]:
        act_c, evict_c = Counter(), Counter()
        for r in rows:
            a, rsn = str(r.get("action") or "UNKNOWN").upper(), str(r.get("reason") or "")
            act_c[a] += 1
            if a == "EVICT" or "BLOCKED" in rsn or "REJECT" in rsn or "SUS" in rsn.upper():
                if "STALE_WATCH" in rsn:
                    evict_c["STALE_WATCH_15M"] += 1
                elif "VAMP" in rsn:
                    evict_c["VAMP_EVICT_OR_STRIKE"] += 1
                elif "LOW_SCORE" in rsn:
                    evict_c["LOW_SCORE_EVICT"] += 1
                elif "SUS" in rsn.upper():
                    evict_c["IS_SUS_BLOCK"] += 1
                elif "SAFETY" in rsn.upper():
                    evict_c["SAFETY_GATE_REJECT"] += 1
                elif a == "EVICT":
                    evict_c[f"EVICT:{rsn.split('(')[0].strip() or 'OTHER'}"] += 1
                elif "BLOCKED" in rsn:
                    evict_c["DECISION_1_BLOCKED"] += 1
        return act_c, evict_c

    pre_ac, pre_ev = _classify_actions(pre_actions)
    soak_ac, soak_ev = _classify_actions(soak_actions)
    lines.extend(["", "## 6. Pipeline Activity, Slot Churn & Phase 0 Baseline (`action_log`)", "", "### Action Type Counts (Pre-Soak vs. Active Soak)", "| Action | Pre-Soak Count | Active Soak Count |", "| :--- | ---: | ---: |"])
    for k in sorted(set(pre_ac.keys()) | set(soak_ac.keys())):
        lines.append(f"| `{k}` | {pre_ac.get(k, 0)} | {soak_ac.get(k, 0)} |")
    if not (pre_ac or soak_ac):
        lines.append("| *(none)* | 0 | 0 |")
    lines.extend(["", "### Eviction, Gate Rejection & Block Breakdown", "| Category | Pre-Soak Count | Active Soak Count |", "| :--- | ---: | ---: |"])
    for k in sorted(set(pre_ev.keys()) | set(soak_ev.keys())):
        lines.append(f"| `{k}` | {pre_ev.get(k, 0)} | {soak_ev.get(k, 0)} |")
    if not (pre_ev or soak_ev):
        lines.append("| *(none)* | 0 | 0 |")

    tok_counts: Counter[tuple[str, str]] = Counter()
    soak_vals: list[float] = []
    for r in soak_actions:
        tok_counts[(str(r.get("symbol") or "UNKNOWN"), str(r.get("asset_id") or ""))] += 1
        if (sm := safe_float(r.get("soak_minutes"))) is not None:
            soak_vals.append(sm)
    soak_dist = dist_stats(soak_vals)
    lines.extend([
        "", f"- **Active Soak Slot Occupancy (`soak_minutes` across evaluations):** count={int(soak_dist['count'])}, median={soak_dist['median']:.2f}m, mean={soak_dist['mean']:.2f}m, p90={soak_dist['p90']:.2f}m, max={soak_dist['max']:.2f}m",
        "", "### Top 20 Most Active Tokens in Active Soak (`action_log`)", "| Symbol | Mint | Action Log Events |", "| :--- | :--- | ---: |",
    ])
    for (sym, mint), cnt in tok_counts.most_common(20):
        lines.append(f"| **{sym}** | `{short_mint(mint)}` | {cnt} |")
    if not tok_counts:
        lines.append("| *(none)* | — | 0 |")

    soak_wallets = {str(r.get("wallet") or "") for r in soak_whale_events if r.get("wallet")}
    all_wallets = {str(r.get("wallet") or "") for r in all_whale_events if r.get("wallet")}
    w_mints = Counter(str(r.get("mint") or "") for r in soak_whale_events if r.get("mint"))
    wd_soak = [r for r in whale_decisions if (dt := parse_iso_utc(r.get("updated_at"))) and dt >= since_dt]
    wd_status = Counter(f"{r.get('status')}:{r.get('reason')}" for r in wd_soak)
    lines.extend([
        "", "## 7. Whale Pipeline (`whale_events`, `whale_signals`, `whale_decisions`)", "",
        f"- **Whale Events:** All-Time={len(all_whale_events)} (Unique Wallets={len(all_wallets)}) | Active Soak={len(soak_whale_events)} (Unique Wallets={len(soak_wallets)})",
        f"- **Whale Signals Table Rows:** {len(whale_signals)} | **Whale Decisions Table Rows:** All-Time={len(whale_decisions)}, Active Soak={len(wd_soak)}",
        "- **Top Active Soak Whale Mints:** " + (", ".join(f"`{short_mint(m)}` ({c})" for m, c in w_mints.most_common(10)) or "*(none)*"),
        "", "### Active Soak Whale Decisions Breakdown", "| Status : Reason | Count |", "| :--- | ---: |",
    ])
    for k, c in wd_status.most_common(20):
        lines.append(f"| `{k}` | {c} |")
    if not wd_status:
        lines.append("| *(none)* | 0 |")
    lines.extend(["", "### Latest 20 Active Soak Whale Events", "| Timestamp (UTC) | Side | Wallet | Mint | SOL Amount | Token Amount |", "| :--- | :--- | :--- | :--- | ---: | ---: |"])
    for r in sorted(soak_whale_events, key=lambda x: str(x.get("timestamp_utc") or ""), reverse=True)[:20]:
        lines.append(f"| `{r.get('timestamp_utc', '')}` | `{r.get('side', '')}` | `{short_mint(r.get('wallet'))}` | `{short_mint(r.get('mint'))}` | {fmt_num(safe_float(r.get('sol_amount')), 4)} | {fmt_num(safe_float(r.get('token_amount')), 2)} |")
    if not soak_whale_events:
        lines.append("| *(no soak whale events)* | — | — | — | — | — |")

    st_rows, wl_rows, cd_rows, xw_rows, ss_rows = state_tables.get("agent_state", []), state_tables.get("active_watchlist", []), state_tables.get("cooldowns", []), state_tables.get("x_worker_state", []), state_tables.get("social_signals", [])
    ss_soak = [r for r in ss_rows if (dt := parse_iso_utc(r.get("last_seen_utc"))) and dt >= since_dt]
    lines.extend(["", "## 8. Live State & Social Signals (`agent_state`, `active_watchlist`, `cooldowns`, `social_signals`, `x_worker_state`)", ""])
    if st_rows:
        s0 = st_rows[0]
        lines.append(f"- **`agent_state` Snapshot:** focus=`{s0.get('focus_state')}` | sol_balance=`{s0.get('sol_balance')}` | daily_start_sol=`{s0.get('daily_start_sol')}` | daily_pnl_sol=`{s0.get('daily_pnl_sol')}` | updated_at=`{s0.get('updated_at')}`")
    else:
        lines.append("- **`agent_state` Snapshot:** *(empty)*")
    if xw_rows:
        lines.append(f"- **`x_worker_state` Snapshot:** last_poll_utc=`{xw_rows[0].get('last_poll_utc')}`")
    lines.extend([
        f"- **`active_watchlist` ({len(wl_rows)} slots):** " + (", ".join(f"**{r.get('symbol')}** (`{short_mint(r.get('mint'))}`, setup=`{r.get('setup_type')}`)" for r in wl_rows) or "*(empty)*"),
        f"- **`cooldowns` ({len(cd_rows)} active):** " + (", ".join(f"`{short_mint(r.get('mint'))}` ({r.get('reason')})" for r in cd_rows[:15]) or "*(empty)*"),
        f"- **`social_signals`:** Total={len(ss_rows)} (Pre-Soak={len(ss_rows) - len(ss_soak)}, Active Soak={len(ss_soak)})",
        "", "### Latest 10 Social Signals", "| Last Seen (UTC) | Mint | Mentions | Unique Authors | Total Likes |", "| :--- | :--- | ---: | ---: | ---: |",
    ])
    for r in sorted(ss_rows, key=lambda x: str(x.get("last_seen_utc") or ""), reverse=True)[:10]:
        lines.append(f"| `{r.get('last_seen_utc', '')}` | `{short_mint(r.get('mint'))}` | {r.get('mention_count', 0)} | {r.get('unique_authors', 0)} | {r.get('total_likes', 0)} |")
    if not ss_rows:
        lines.append("| *(none)* | — | 0 | 0 | 0 |")
    lines.append("")
    return "\n".join(lines)
