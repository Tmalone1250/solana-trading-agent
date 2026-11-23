"""Offline Outcome Audit CLI (audit_outcomes.py) — Sprint 1 Baseline Recovery & Instrumentation.

Standalone laptop audit tool (never imported by the Pi runtime).
Ingests candidate_outcomes.csv + solana_actions.csv (or a local DuckDB copy) and emits:
  A. 5-State Completeness Ledger (zero silent drops)
  B. Dual-Bound + Multi-Level Stress-Test Sensitivity (-50%, -80%, -95% assumed losses)
  C. Four required reports:
     1. Primary + Secondary Loss Attribution (Closed Trades)
     2. Rejection-Reason Counterfactuals (with t+1m delayed-entry proxies)
     3. Time-Anchored Per-Wallet Attribution (rank, forward returns, hold seconds, farmer flag)
     4. Chart Lane vs. Whale Lane Side-by-Side Comparison
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from audit_reports import (
    STRESS_LOSS_LEVELS,
    build_lane_comparison,
    build_loss_attribution,
    build_rejection_counterfactuals,
    build_wallet_attribution,
    compute_bucket_expectancy,
    fmt_pct,
)

IN_PROGRESS_WINDOW_SECONDS = 35 * 60.0
RESTART_CLUSTER_WINDOW_SECONDS = 120.0
RESTART_MIN_OTHER_CANDIDATES = 3
VANISHED_SEVERE_MAE_PCT = -70.0


def _parse_iso(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    s = str(ts).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _to_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _to_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    return str(val or "").strip().lower() in ("true", "1", "yes", "t")


def _normalize_outcome_row(raw: dict[str, Any]) -> dict[str, Any]:
    dt = _parse_iso(raw.get("discovered_at"))
    row = dict(raw)
    row["_dt"] = dt
    row["_ts"] = dt.timestamp() if dt else None
    row["finalized"] = _to_bool(raw.get("finalized"))
    for col in (
        "entry_price_usd", "entry_mc_usd", "entry_liq_usd", "breakout_score",
        "leg2_score", "vamp_score", "rsi", "whale_sol_flow",
        "price_1m_pct", "price_5m_pct", "price_15m_pct", "price_30m_pct",
        "mfe_pct", "mae_pct",
    ):
        row[col] = _to_float(raw.get(col))
    return row


def _normalize_action_row(raw: dict[str, Any]) -> dict[str, Any]:
    dt = _parse_iso(raw.get("timestamp_utc"))
    row = dict(raw)
    row["_dt"] = dt
    row["_ts"] = dt.timestamp() if dt else None
    for col in (
        "qty", "notional", "price_or_value", "pnl_amount", "pnl_pct",
        "mc_usd", "liq_usd", "peak_pnl_pct", "hold_seconds",
        "whale_sol_flow", "breakout_ema_ext_pct",
    ):
        row[col] = _to_float(raw.get(col))
    return row


def load_csv_rows(path: Optional[str], normalizer: Any) -> list[dict[str, Any]]:
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"CSV file not found: {path}")
    with p.open("r", encoding="utf-8", newline="") as fh:
        return [normalizer(r) for r in csv.DictReader(fh)]


def load_from_duckdb(db_path: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Read candidate_outcomes, action_log, and whale_events from a local DuckDB copy (read-only)."""
    import duckdb

    conn = duckdb.connect(db_path, read_only=True)
    try:
        tables = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
        outcomes: list[dict[str, Any]] = []
        actions: list[dict[str, Any]] = []
        whale_events: list[dict[str, Any]] = []

        if "candidate_outcomes" in tables:
            cur = conn.execute("SELECT * FROM candidate_outcomes")
            cols = [d[0] for d in cur.description]
            outcomes = [_normalize_outcome_row(dict(zip(cols, r))) for r in cur.fetchall()]
        if "action_log" in tables:
            cur = conn.execute("SELECT * FROM action_log")
            cols = [d[0] for d in cur.description]
            actions = [_normalize_action_row(dict(zip(cols, r))) for r in cur.fetchall()]
        if "whale_events" in tables:
            cur = conn.execute("SELECT signature, wallet, mint, side, sol_amount, token_amount, timestamp_utc FROM whale_events")
            for sig, wallet, mint, side, sol_amt, tok_amt, ts_str in cur.fetchall():
                dt = _parse_iso(ts_str)
                whale_events.append(
                    {
                        "signature": sig, "wallet": wallet, "mint": mint, "side": side,
                        "sol_amount": _to_float(sol_amt), "token_amount": _to_float(tok_amt),
                        "timestamp_utc": ts_str, "_dt": dt, "_ts": dt.timestamp() if dt else None,
                    }
                )
        return outcomes, actions, whale_events
    finally:
        conn.close()


def _estimated_stop_ts(row: dict[str, Any]) -> Optional[float]:
    base_ts = row.get("_ts")
    if base_ts is None:
        return None
    if row.get("price_15m_pct") is not None:
        return float(base_ts) + 15 * 60.0
    if row.get("price_5m_pct") is not None:
        return float(base_ts) + 5 * 60.0
    if row.get("price_1m_pct") is not None:
        return float(base_ts) + 1 * 60.0
    return float(base_ts)


def classify_observation_states(
    outcomes: list[dict[str, Any]],
    actions: Optional[list[dict[str, Any]]] = None,
    *,
    reference_ts: Optional[float] = None,
) -> dict[str, int]:
    """Classify every candidate_outcomes row into 5 mutually exclusive states (zero silent drops)."""
    if not outcomes:
        return {
            "COMPLETE_30M": 0,
            "IN_PROGRESS": 0,
            "INTERRUPTED_BY_RESTART": 0,
            "VANISHED_OR_DEAD": 0,
            "PARTIAL_OR_UNRESOLVED": 0,
        }

    ts_values = [float(r["_ts"]) for r in outcomes if r.get("_ts") is not None]
    latest_ts = reference_ts if reference_ts is not None else (max(ts_values) if ts_values else 0.0)

    # Corroborated dead-pool / liquidity-collapse mints from actual action_log rows (if provided)
    dead_pool_mints: set[str] = set()
    for a in actions or []:
        mint = str(a.get("asset_id") or "")
        liq = a.get("liq_usd")
        reason_up = str(a.get("reason") or "").upper()
        if mint and (
            (liq is not None and float(liq) <= 1000.0)
            or any(k in reason_up for k in ("RUG", "LIQ_COLLAPSE", "DEAD_POOL"))
        ):
            dead_pool_mints.add(mint)

    # Precompute stop timestamps for unfinalized rows past 35m to detect Pi restart clusters
    restart_candidates: list[tuple[int, float]] = []
    for idx, r in enumerate(outcomes):
        if r.get("finalized") and r.get("price_30m_pct") is not None:
            continue
        ts = r.get("_ts")
        if ts is None:
            continue
        elapsed = latest_ts - float(ts)
        if not r.get("finalized") and elapsed > IN_PROGRESS_WINDOW_SECONDS:
            stop_ts = _estimated_stop_ts(r)
            if stop_ts is not None:
                restart_candidates.append((idx, stop_ts))

    restart_indices: set[int] = set()
    for idx, stop_ts in restart_candidates:
        others = sum(
            1
            for o_idx, o_stop in restart_candidates
            if o_idx != idx and abs(o_stop - stop_ts) <= RESTART_CLUSTER_WINDOW_SECONDS
        )
        if others >= RESTART_MIN_OTHER_CANDIDATES:
            restart_indices.add(idx)

    counts = {
        "COMPLETE_30M": 0,
        "IN_PROGRESS": 0,
        "INTERRUPTED_BY_RESTART": 0,
        "VANISHED_OR_DEAD": 0,
        "PARTIAL_OR_UNRESOLVED": 0,
    }

    for idx, r in enumerate(outcomes):
        ts = r.get("_ts")
        elapsed = (latest_ts - float(ts)) if ts is not None else float("inf")
        if r.get("finalized") and r.get("price_30m_pct") is not None:
            state = "COMPLETE_30M"
        elif elapsed < IN_PROGRESS_WINDOW_SECONDS:
            state = "IN_PROGRESS"
        elif idx in restart_indices:
            state = "INTERRUPTED_BY_RESTART"
        else:
            missing_late = (r.get("price_15m_pct") is None) or (r.get("price_30m_pct") is None)
            mae = r.get("mae_pct")
            entry_liq = r.get("entry_liq_usd")
            corroborated_dead = (
                (mae is not None and float(mae) <= VANISHED_SEVERE_MAE_PCT)
                or (entry_liq is not None and float(entry_liq) <= 0.0)
                or (str(r.get("mint") or "") in dead_pool_mints)
            )
            if elapsed > IN_PROGRESS_WINDOW_SECONDS and missing_late and corroborated_dead:
                state = "VANISHED_OR_DEAD"
            else:
                state = "PARTIAL_OR_UNRESOLVED"
        r["_state"] = state
        counts[state] += 1

    return counts


def render_audit_report(
    outcomes: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    whale_events: Optional[list[dict[str, Any]]] = None,
    *,
    friction_pct: float = 3.0,
) -> str:
    """Run full Sprint 1 audit and render a structured Markdown report."""
    ledger = classify_observation_states(outcomes, actions)
    total = len(outcomes)
    lines: list[str] = [
        "# Sprint 1 — Offline Outcome Audit Report",
        f"- **Total `candidate_outcomes` rows**: {total}",
        f"- **Closed `SELL` rows in `action_log`**: {sum(1 for a in actions if str(a.get('action') or '').upper() == 'SELL')}",
        f"- **Simulated Round-Trip Friction Assumption**: `{friction_pct:.2f}%` (explicitly separated from observed mark-to-market)",
        "",
        "## A. 5-State Completeness Ledger (Zero Silent Drops)",
        "| Observation State | Count | Share |",
        "|---|---:|---:|",
    ]
    for st_name in ("COMPLETE_30M", "IN_PROGRESS", "INTERRUPTED_BY_RESTART", "VANISHED_OR_DEAD", "PARTIAL_OR_UNRESOLVED"):
        c = ledger[st_name]
        pct = (c / total * 100.0) if total else 0.0
        lines.append(f"| `{st_name}` | {c} | {pct:.1f}% |")

    # Strategy summary with dual-bound + stress-test sensitivity
    lines += [
        "",
        "## B. Strategy Dual-Bound & Stress-Test Expectancy",
        "> Stress-test columns include `VANISHED_OR_DEAD` rows at assumed loss levels (`-50%`, `-80%`, `-95%`) — **stress-test assumptions, not observed fills**.",
        "",
        "| Strategy | n_tot / n_comp / n_van / n_part / n_int | Tag | Observed Exp (30m - fric) | Stress -50% | Stress -80% | Stress -95% |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    by_strat: dict[str, list[dict[str, Any]]] = {}
    for o in outcomes:
        by_strat.setdefault(str(o.get("strategy") or "UNSPECIFIED"), []).append(o)
    for strat, rows in sorted(by_strat.items(), key=lambda kv: -len(kv[1])):
        b = compute_bucket_expectancy(rows, friction_pct=friction_pct)
        tag = "`[LOW_SAMPLE]`" if b["low_sample"] else "OK"
        counts_s = f"{b['n_total']}/{b['n_complete']}/{b['n_vanished']}/{b['n_partial']}/{b['n_interrupted']}"
        st = b["stress_expectancy_pct"]
        lines.append(
            f"| `{strat}` | {counts_s} | {tag} | {fmt_pct(b['observed_expectancy_pct'])} | "
            f"{fmt_pct(st[-50.0])} | {fmt_pct(st[-80.0])} | {fmt_pct(st[-95.0])} |"
        )

    # Report 1: Primary + Secondary Loss Attribution
    loss_rows = build_loss_attribution(outcomes, actions, friction_pct=friction_pct)
    cause_counts = Counter(r["primary_cause"] for r in loss_rows)
    sec_counts = Counter(f for r in loss_rows for f in r["secondary_factors"])
    lines += [
        "",
        "## C1. Primary + Secondary Loss Attribution (Closed Trades)",
        f"- **Primary Cause Breakdown**: {dict(cause_counts) if cause_counts else 'No closed SELL trades'}",
        f"- **Secondary Contributing Factors**: {dict(sec_counts) if sec_counts else 'None'}",
        "",
        "| Symbol | Strategy | Exit Reason | Observed PnL | Sim Net PnL (-fric) | MFE | MAE | Primary Cause | Secondary Factors |",
        "|---|---|---|---:|---:|---:|---:|---|---|",
    ]
    for r in loss_rows[:50]:
        sec_str = ", ".join(r["secondary_factors"]) or "-"
        lines.append(
            f"| {r['symbol']} | `{r['strategy']}` | {r['exit_reason']} | {fmt_pct(r['observed_pnl_pct'])} | "
            f"{fmt_pct(r['simulated_net_pnl_pct'])} | {fmt_pct(r['mfe_pct'])} | {fmt_pct(r['mae_pct'])} | "
            f"`{r['primary_cause']}` | {sec_str} |"
        )

    # Report 2: Rejection-Reason Counterfactuals
    rej_rows = build_rejection_counterfactuals(outcomes, friction_pct=friction_pct)
    lines += [
        "",
        "## C2. Rejection-Reason Counterfactuals",
        "> `r_1_to_5` and `r_1_to_15` are **milestone-based delayed-entry proxies** computed from observed `+1m` price, NOT exact trade replays.",
        "",
        "| Bucket | n_tot/comp/van/part/int | Tag | Obs Exp | Stress -80% | Med r_1->5 | Med r_1->15 | UTC Sessions |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for r in rej_rows[:25]:
        tag = "`[LOW_SAMPLE]`" if r["low_sample"] else "OK"
        c_str = f"{r['n_total']}/{r['n_complete']}/{r['n_vanished']}/{r['n_partial']}/{r['n_interrupted']}"
        lines.append(
            f"| {r['bucket']} | {c_str} | {tag} | {fmt_pct(r['observed_expectancy_pct'])} | "
            f"{fmt_pct(r['stress_expectancy_pct'][-80.0])} | {fmt_pct(r['median_r_1_to_5_pct'])} | "
            f"{fmt_pct(r['median_r_1_to_15_pct'])} | {r['sessions']} |"
        )

    # Report 3: Time-Anchored Per-Wallet Attribution
    w_rows = build_wallet_attribution(outcomes, actions, whale_events or [], friction_pct=friction_pct)
    lines += [
        "",
        "## C3. Time-Anchored Per-Wallet Attribution",
        "| Wallet | n_tot/comp/van | Tag | 1st-Mover % | Avg Rank | Med +5m | Med +15m | Med r_1->5 | Med Hold(s) | Early Exit | Farmer(<45s) |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in w_rows[:30]:
        tag = "`[LOW_SAMPLE]`" if r["low_sample"] else "OK"
        hold_s = f"{r['median_hold_seconds']:.1f}s" if r["median_hold_seconds"] is not None else "n/a"
        lines.append(
            f"| `{r['wallet'][:14]}` | {r['n_total']}/{r['n_complete']}/{r['n_vanished']} | {tag} | "
            f"{r['first_mover_pct']:.1f}% | {r['avg_entry_rank'] or 'n/a'} | {fmt_pct(r['median_fwd_5m_pct'])} | "
            f"{fmt_pct(r['median_fwd_15m_pct'])} | {fmt_pct(r['median_r_1_to_5_pct'])} | {hold_s} | "
            f"{r['wallet_exit_before_expansion_count']} | {'YES' if r['fast_exit_farmer'] else 'no'} |"
        )

    # Report 4: Chart Lane vs. Whale Lane Side-by-Side Comparison
    lanes = build_lane_comparison(outcomes, friction_pct=friction_pct)
    lines += [
        "",
        "## C4. Chart Lane vs. Whale Lane Side-by-Side Comparison",
        "| Metric | CHART_ALPHA | WHALE_WEBHOOK |",
        "|---|---:|---:|",
    ]
    ca, wh = lanes["CHART_ALPHA"], lanes["WHALE_WEBHOOK"]
    rows_cmp = [
        ("n_complete (tag)", f"{ca['n_complete']} {'[LOW_SAMPLE]' if ca['low_sample'] else ''}", f"{wh['n_complete']} {'[LOW_SAMPLE]' if wh['low_sample'] else ''}"),
        ("Median +5m / +15m / +30m", f"{fmt_pct(ca['median_5m_pct'])} / {fmt_pct(ca['median_15m_pct'])} / {fmt_pct(ca['median_30m_pct'])}", f"{fmt_pct(wh['median_5m_pct'])} / {fmt_pct(wh['median_15m_pct'])} / {fmt_pct(wh['median_30m_pct'])}"),
        ("MFE Win Rates (+5% / +10% / +15%)", f"{ca['win_rate_5_pct']}% / {ca['win_rate_10_pct']}% / {ca['win_rate_15_pct']}%", f"{wh['win_rate_5_pct']}% / {wh['win_rate_10_pct']}% / {wh['win_rate_15_pct']}%"),
        ("MFE (p25 / med / p75)", str(ca["mfe_p25_med_p75"]), str(wh["mfe_p25_med_p75"])),
        ("MAE (p25 / med / p75)", str(ca["mae_p25_med_p75"]), str(wh["mae_p25_med_p75"])),
        ("Observed Exp (-fric)", fmt_pct(ca["observed_expectancy_pct"]), fmt_pct(wh["observed_expectancy_pct"])),
        ("Stress Exp (-50% / -80% / -95%)", f"{fmt_pct(ca['stress_expectancy_pct'][-50.0])} / {fmt_pct(ca['stress_expectancy_pct'][-80.0])} / {fmt_pct(ca['stress_expectancy_pct'][-95.0])}", f"{fmt_pct(wh['stress_expectancy_pct'][-50.0])} / {fmt_pct(wh['stress_expectancy_pct'][-80.0])} / {fmt_pct(wh['stress_expectancy_pct'][-95.0])}"),
        ("UTC Blocks (ASIA / EU / US)", str(ca["time_block_expectancy"]), str(wh["time_block_expectancy"])),
    ]
    for metric, v1, v2 in rows_cmp:
        lines.append(f"| {metric} | {v1} | {v2} |")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sprint 1 Offline Outcome Audit CLI")
    parser.add_argument("--outcomes", type=str, default=None, help="Path to candidate_outcomes.csv")
    parser.add_argument("--actions", type=str, default=None, help="Path to solana_actions.csv")
    parser.add_argument("--duckdb", type=str, default=None, help="Path to local copy of solana_meme_agent.duckdb")
    parser.add_argument("--friction-pct", type=float, default=3.0, help="Simulated round-trip friction haircut %% (default: 3.0)")
    parser.add_argument("--output", type=str, default=None, help="Optional Markdown file path to save the report")
    args = parser.parse_args()

    if args.duckdb:
        outcomes, actions, whale_events = load_from_duckdb(args.duckdb)
    else:
        outcomes = load_csv_rows(args.outcomes, _normalize_outcome_row)
        actions = load_csv_rows(args.actions, _normalize_action_row)
        whale_events = []

    report = render_audit_report(outcomes, actions, whale_events, friction_pct=args.friction_pct)
    print(report)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
