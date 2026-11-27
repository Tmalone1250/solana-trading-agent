"""Markdown & JSON Report Renderer for the BREAKOUT_IGNITION Historical Replay Simulator.

Clearly separates:
- Recorded historical telemetry (`EXACT_HISTORICAL`)
- Reconstructed portfolio state (`RECONSTRUCTABLE`)
- Inferred values (`INFERRED`, `INFERRED_PRICE_FROM_MARKET_CAP`)
- Non-replayable / missing inputs (`MISSING`, `NOT_REPLAYABLE`, `UNAVAILABLE`)
- Execution quote evidence (`OBSERVED_QUOTE` vs `PRICE_ONLY` vs `UNAVAILABLE`)
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from typing import Any, Optional

from simulator import (
    EXACT_HISTORICAL,
    INFERRED,
    INFERRED_PRICE_FROM_MARKET_CAP,
    MISSING,
    MODE_DETERMINISTIC_GATE_REPLAY,
    MODE_RECORDED_DECISION_AUDIT,
    NOT_REPLAYABLE,
    OBSERVED_QUOTE,
    PARTIALLY_AVAILABLE,
    PRICE_ONLY,
    RECONSTRUCTABLE,
    RUNTIME_CODE_PROVENANCE,
    SUPPORTED_STRATEGY,
    UNAVAILABLE,
)
from simulator.config_profiles import ConfigProfile
from simulator.data_loader import HistoricalDataset
from simulator.execution_analyzer import (
    AggregateExecutionReport,
    MintExecutionSummary,
)
from simulator.outcome_analyzer import (
    AggregateOutcomeReport,
    ObservationOutcomeReport,
)
from simulator.replay_engine import (
    DeterministicGateReplayResult,
    RecordedDecisionAuditResult,
    run_deterministic_gate_replay,
    run_recorded_decision_audit,
)
from simulator.snapshot_builder import DecisionSnapshot, build_decision_snapshot


def _fmt(val: Any, decimals: int = 2, signed: bool = False, pct: bool = False) -> str:
    if val is None:
        return "—"
    if isinstance(val, float) or isinstance(val, int):
        fmt_spec = f"{'+' if signed else ''}.{decimals}f"
        out = format(float(val), fmt_spec)
        return f"{out}%" if pct else out
    return str(val)


def build_token_observation_payload(
    *,
    dataset: HistoricalDataset,
    snapshot: DecisionSnapshot,
    audit: RecordedDecisionAuditResult,
    gate_replay: DeterministicGateReplayResult,
    execution: MintExecutionSummary,
    outcome: ObservationOutcomeReport,
) -> dict[str, Any]:
    """Build deterministic JSON-serializable dict for a single token observation report."""
    mint_episodes = dataset.episodes_by_mint.get(snapshot.mint, [])
    return {
        "strategy_scope": SUPPORTED_STRATEGY,
        "runtime_code_provenance": RUNTIME_CODE_PROVENANCE,
        "snapshot_provenance": {
            "source_db_path": dataset.provenance.source_db_path,
            "wal_included": dataset.provenance.wal_included,
            "snapshot_method": dataset.provenance.snapshot_method,
            "db_sha256": dataset.provenance.db_sha256,
            "wal_sha256": dataset.provenance.wal_sha256,
        },
        "target": {
            "mint": snapshot.mint,
            "symbol": snapshot.symbol,
            "decision_timestamp": snapshot.decision_timestamp,
            "episode_id": snapshot.episode_id,
            "mint_total_episodes_in_window": len(mint_episodes),
            "mint_total_d1_observations_in_window": sum(e.observation_count for e in mint_episodes),
        },
        "recorded_decision_audit": asdict(audit),
        "deterministic_gate_replay": asdict(gate_replay),
        "execution_analysis": asdict(execution),
        "post_decision_outcome_analysis": asdict(outcome),
    }


def render_token_observation_report(
    *,
    dataset: HistoricalDataset,
    profile: ConfigProfile,
    snapshot: DecisionSnapshot,
    audit: RecordedDecisionAuditResult,
    gate_replay: DeterministicGateReplayResult,
    execution: MintExecutionSummary,
    outcome: ObservationOutcomeReport,
) -> str:
    """Render deterministic Markdown report for a single `(mint, decision_timestamp)` observation."""
    mint_episodes = dataset.episodes_by_mint.get(snapshot.mint, [])
    total_mint_d1 = sum(e.observation_count for e in mint_episodes)
    m = audit.recorded_metrics

    lines: list[str] = [
        f"# BREAKOUT_IGNITION Historical Replay Report — {snapshot.symbol} (`{snapshot.mint}`)",
        "",
        "## 1. Mandatory Provenance & Isolation Envelope",
        "",
        "| Field | Value |",
        "| :--- | :--- |",
        f"| **Strategy Scope** | `{SUPPORTED_STRATEGY}` |",
        f"| **Replay Modes Executed** | `{MODE_RECORDED_DECISION_AUDIT}`, `{MODE_DETERMINISTIC_GATE_REPLAY}` |",
        f"| **Runtime Code Provenance** | `{RUNTIME_CODE_PROVENANCE}` |",
        f"| **Configuration Profile** | `{profile.name}` (`{profile.profile_provenance}`) |",
        f"| **Snapshot Acquisition** | `{dataset.provenance.snapshot_method}` (`wal_included={dataset.provenance.wal_included}`) |",
        f"| **Source DB SHA-256** | `{dataset.provenance.db_sha256}` |",
        f"| **Source WAL SHA-256** | `{dataset.provenance.wal_sha256 or 'NONE'}` |",
        f"| **Source Record IDs** | `action_log.id={audit.source_action_log_id}`, `terminal_action_log.id={audit.source_terminal_action_log_id or 'NONE'}`, `decisions_log.id={audit.source_decisions_log_id or 'PRUNED'}` |",
        f"| **Target Decision Timestamp** | `{snapshot.decision_timestamp}` |",
        f"| **Episode Context** | `{snapshot.episode_id or 'UNGROUPED'}` (observation `#{snapshot.observation_index_in_episode}`; mint has `{len(mint_episodes)}` episodes / `{total_mint_d1}` `DECISION_1` rows in window) |",
        "",
        "---",
        "",
        f"## 2. Mode A — `{MODE_RECORDED_DECISION_AUDIT}`",
        "",
        "*Reconstructs what the original telemetry records state occurred at the decision timestamp (not an independent recreation of `detect_breakout_ignition()`).*",
        "",
        f"- **Recorded Decision Class:** `{audit.recorded_decision_class}`",
        f"- **Recorded Alpha Action:** `{audit.recorded_alpha_action}` (`{audit.recorded_alpha_action_provenance}`)",
        f"- **Recorded Strategy Label:** `{audit.recorded_strategy}`",
        f"- **Recorded Reason:** `{audit.recorded_reason}`",
        f"- **Same-Tick Watchlist Eviction:** `{audit.recorded_evicted_same_tick}`"
        + (f" — `{audit.recorded_eviction_reason}`" if audit.recorded_eviction_reason else ""),
        "",
        "### Recorded Input & Detector Telemetry (`action_log`)",
        "",
        "| Metric | Recorded Value | Provenance Classification |",
        "| :--- | :--- | :--- |",
        f"| Market Cap (USD) | \\${_fmt(m['mc_usd'], 2)} | `{audit.recorded_metric_provenance['mc_usd']}` |",
        f"| Liquidity (USD) | \\${_fmt(m['liq_usd'], 2)} | `{audit.recorded_metric_provenance['liq_usd']}` |",
        f"| Watchlist Soak (min) | `{_fmt(m['soak_minutes'], 2)}` | `{audit.recorded_metric_provenance['soak_minutes']}` |",
        f"| RSI | `{_fmt(m['rsi'], 2)}` | `{audit.recorded_metric_provenance['rsi']}` |",
        f"| Breakout Score | `{m['breakout_score']}` | `{audit.recorded_metric_provenance['breakout_score']}` |",
        f"| Breakout State | `{m['breakout_state']}` | `{audit.recorded_metric_provenance['breakout_state']}` |",
        f"| Breakout Timeframe | `{m['breakout_timeframe']}` | `{audit.recorded_metric_provenance['breakout_timeframe']}` |",
        f"| Breakout Vol Ratio | `{_fmt(m['breakout_vol_ratio'], 2)}x` | `{audit.recorded_metric_provenance['breakout_vol_ratio']}` |",
        f"| Breakout EMA9 Extension | `{_fmt(m['breakout_ema_ext_pct'], 2, signed=True, pct=True)}` | `{audit.recorded_metric_provenance['breakout_ema_ext_pct']}` |",
        f"| Narrative Score | `{_fmt(m['narrative_score'], 2)}` | `{audit.recorded_metric_provenance['narrative_score']}` |",
        f"| Momentum Score | `{_fmt(m['momentum_score'], 2)}` | `{audit.recorded_metric_provenance['momentum_score']}` |",
        f"| Copycat / Vamp Score | `{_fmt(m['vamp_score'], 3)}` | `{audit.recorded_metric_provenance['vamp_score']}` |",
        f"| Action Confidence (`buy_confidence` col) | `{_fmt(m['buy_confidence'], 3)}` | `{audit.recorded_metric_provenance['buy_confidence']}` |",
        f"| Leg2 Predictor Score | `{_fmt(m['leg2_score'], 2)}` | `{audit.recorded_metric_provenance['leg2_score']}` |",
        "",
        "### Recorded Breakout Diagnostics Separation (`D1_failed_gates`)",
        "",
        f"- **Recorded Hard-Veto Diagnostics ({len(audit.recorded_hard_veto_diagnostics)}):** "
        + (", ".join(f"`{g}`" for g in audit.recorded_hard_veto_diagnostics) or "`None`"),
        f"- **Recorded Score-Deduction Diagnostics ({len(audit.recorded_score_deduction_diagnostics)}):** "
        + (", ".join(f"`{g}`" for g in audit.recorded_score_deduction_diagnostics) or "`None`"),
        "",
        "---",
        "",
        f"## 3. Mode B — `{MODE_DETERMINISTIC_GATE_REPLAY}`",
        "",
        "*Independently reevaluates only gates whose required historical inputs exist at `<= decision_timestamp`. Missing inputs produce `NOT_REPLAYABLE` (never an assumed pass).*",
        "",
        f"- **Overall Entry Verdict:** `{gate_replay.overall_entry_verdict}`",
        f"- **Eviction Replay Verdict:** `{gate_replay.eviction_replay_verdict}`",
        f"- **Consistent With Recorded Outcome:** `{gate_replay.consistent_with_recorded_outcome}` — {gate_replay.consistency_explanation}",
        f"- **Hard Gate Replayability:** `{gate_replay.replayable_hard_gate_count}` replayable (`{len(gate_replay.passed_hard_gate_ids)}` PASS, `{len(gate_replay.failed_hard_gate_ids)}` FAIL) / `{gate_replay.non_replayable_hard_gate_count}` `NOT_REPLAYABLE`",
        "",
        "### Gate-by-Gate Entry Trace",
        "",
        "| Gate ID | Hard Gate? | Status | Input Availability | Observed | Profile Threshold | Explanation |",
        "| :--- | :---: | :--- | :--- | :--- | :--- | :--- |",
    ]

    for g in gate_replay.gates:
        lines.append(
            f"| `{g.gate_id}` | `{'YES' if g.is_hard_entry_gate else 'NO'}` | **`{g.status}`** | `{g.availability}` | `{g.observed_value}` | `{g.threshold_value}` | {g.explanation} |"
        )

    lines.extend(
        [
            "",
            "### Watchlist Eviction Rule Trace",
            "",
            "| Rule ID | Status | Input Availability | Observed | Threshold | Explanation |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
    )
    for eg in gate_replay.eviction_gates:
        lines.append(
            f"| `{eg.gate_id}` | **`{eg.status}`** | `{eg.availability}` | `{eg.observed_value}` | `{eg.threshold_value}` | {eg.explanation} |"
        )

    lines.extend(
        [
            "",
            "---",
            "",
            "## 4. Historical Execution Evidence (`execution_analyzer`)",
            "",
            f"- **Traded in `{SUPPORTED_STRATEGY}`:** `{execution.traded}`",
            f"- **Executable Quote Availability:** `{execution.executable_quote_availability}`",
            f"- **Assessment:** {execution.note}",
            "",
            "---",
            "",
            "## 5. Post-Decision Subsequent Outcome Analysis (`outcome_analyzer`)",
            "",
            f"> **Anti-Look-Ahead & Execution Disclaimer:** {outcome.executable_fill_disclaimer}",
            "",
        ]
    )

    out_rec = outcome.matched_outcome_record
    if out_rec is None:
        lines.append(
            "- **Matched `candidate_outcomes` Record:** `NONE` (suppressed by 15-minute per-mint deduplication window)."
        )
    else:
        lines.extend(
            [
                f"- **Matched `candidate_outcomes.id`:** `{out_rec.outcome_id}` (`discovered_at={out_rec.discovered_at}`, `finalized={out_rec.finalized}`)",
                f"- **Outcome Evidence Classification:** `{out_rec.evidence_classification}` (60-second DexScreener spot polling, NOT an executable Jupiter fill)",
                f"- **Baseline Snapshot (`PRICE_ONLY`):** Price `\\${_fmt(out_rec.entry_price_usd, 7)}`, MC `\\${_fmt(out_rec.entry_mc_usd, 2)}`, Liquidity `\\${_fmt(out_rec.entry_liq_usd, 2)}`",
                "",
                "| Horizon / Excursion Metric | Recorded Value | Evidence Class |",
                "| :--- | ---: | :--- |",
                f"| `+1m` Price Change (`price_1m_pct`) | `{_fmt(out_rec.price_1m_pct, 2, signed=True, pct=True)}` | `{out_rec.evidence_classification}` |",
                f"| `+5m` Price Change (`price_5m_pct`) | `{_fmt(out_rec.price_5m_pct, 2, signed=True, pct=True)}` | `{out_rec.evidence_classification}` |",
                f"| `+15m` Price Change (`price_15m_pct`) | `{_fmt(out_rec.price_15m_pct, 2, signed=True, pct=True)}` | `{out_rec.evidence_classification}` |",
                f"| `+30m` Price Change (`price_30m_pct`) | `{_fmt(out_rec.price_30m_pct, 2, signed=True, pct=True)}` | `{out_rec.evidence_classification}` |",
                f"| `30m` Maximum Favorable Excursion (`mfe_pct`) | `{_fmt(out_rec.mfe_pct, 2, signed=True, pct=True)}` | `{out_rec.evidence_classification}` |",
                f"| `30m` Maximum Adverse Excursion (`mae_pct`) | `{_fmt(out_rec.mae_pct, 2, signed=True, pct=True)}` | `{out_rec.evidence_classification}` |",
            ]
        )

    lines.extend(
        [
            "",
            "### Subsequent Watchlist Trajectory (`action_log` Post-Decision Observations)",
            "",
            f"- **Blind Gap After Decision (Cooldown / Off-Watchlist):** `{_fmt(outcome.blind_gap_minutes_after_decision, 2)} min`",
            f"- **Subsequent `DECISION_1` Ticks Within 30m:** `{outcome.subsequent_observation_count_30m}`",
            f"- **Peak Recorded Market Cap After Decision:** \\${_fmt(outcome.peak_mc_usd_after_decision, 2)}",
            f"- **Subsequent Liquidity Range:** \\${_fmt(outcome.min_liq_usd_after_decision, 2)} – \\${_fmt(outcome.peak_liq_usd_after_decision, 2)}",
            "",
        ]
    )

    if outcome.sample_trajectory_30m:
        lines.extend(
            [
                "| Timestamp (UTC) | +Elapsed (m) | MC (USD) | MC Δ % | Liquidity (USD) | Liq Δ % | Implied USD Price | Price Provenance |",
                "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |",
            ]
        )
        for pt in outcome.sample_trajectory_30m:
            p_str = f"\\${pt.inferred_price_usd:.7f}" if pt.inferred_price_usd is not None else "—"
            lines.append(
                f"| `{pt.timestamp_utc}` | `{pt.elapsed_minutes:.2f}` | \\${_fmt(pt.mc_usd, 0)} | `{_fmt(pt.mc_change_pct, 2, signed=True, pct=True)}` | \\${_fmt(pt.liq_usd, 2)} | `{_fmt(pt.liq_change_pct, 2, signed=True, pct=True)}` | {p_str} | `{pt.price_provenance}` |"
            )

    if mint_episodes:
        lines.extend(
            [
                "",
                f"### All Watchlist Decision Episodes for {snapshot.symbol} ({len(mint_episodes)} Episodes)",
                "",
                "| Episode ID | Started At (UTC) | Ended At (UTC) | `DECISION_1` Rows | Peak Breakout Score | Terminal Action | Terminal Reason |",
                "| :--- | :--- | :--- | ---: | ---: | :--- | :--- |",
            ]
        )
        for ep in mint_episodes:
            bscores = [int(r["breakout_score"]) for r in ep.decision1_rows if r.get("breakout_score") is not None]
            pk_bs = max(bscores) if bscores else None
            lines.append(
                f"| `{ep.episode_id}` | `{ep.started_at}` | `{ep.ended_at}` | {ep.observation_count} | {pk_bs if pk_bs is not None else '—'} | `{ep.terminal_action or 'NONE'}` | `{ep.terminal_reason or '—'}` |"
            )

    lines.append("")
    return "\n".join(lines)


def summarize_dataset_replay(
    dataset: HistoricalDataset,
    profile: ConfigProfile,
) -> dict[str, Any]:
    """Run Mode A and Mode B across every `DECISION_1` observation in `dataset.episodes`."""
    rec_class_counts: Counter[str] = Counter()
    replay_verdict_counts: Counter[str] = Counter()
    gate_status_counts: dict[str, Counter[str]] = {}
    consistent_count = 0
    mismatch_count = 0
    unknown_consistency_count = 0

    for ep in dataset.episodes:
        for idx, row in enumerate(ep.decision1_rows):
            snap = build_decision_snapshot(
                dataset,
                action_row=row,
                episode_id=ep.episode_id,
                prior_episode_d1_rows=ep.decision1_rows[:idx],
                vamp_warn_threshold=float(profile.config.VAMP_WARN_THRESHOLD),
                low_score_threshold=float(profile.config.LOW_SCORE_EVICT_THRESHOLD),
            )
            aud = run_recorded_decision_audit(snap, profile)
            rep = run_deterministic_gate_replay(snap, profile)
            rec_class_counts[aud.recorded_decision_class] += 1
            replay_verdict_counts[rep.overall_entry_verdict] += 1
            if rep.consistent_with_recorded_outcome is True:
                consistent_count += 1
            elif rep.consistent_with_recorded_outcome is False:
                mismatch_count += 1
            else:
                unknown_consistency_count += 1
            for g in list(rep.gates) + list(rep.eviction_gates):
                gate_status_counts.setdefault(g.gate_id, Counter())[g.status] += 1

    return {
        "profile_name": profile.name,
        "profile_provenance": profile.profile_provenance,
        "total_d1_replayed": sum(rec_class_counts.values()),
        "recorded_decision_class_counts": dict(sorted(rec_class_counts.items())),
        "replay_verdict_counts": dict(sorted(replay_verdict_counts.items())),
        "consistent_count": consistent_count,
        "mismatch_count": mismatch_count,
        "unknown_consistency_count": unknown_consistency_count,
        "gate_status_counts": {k: dict(sorted(v.items())) for k, v in sorted(gate_status_counts.items())},
    }


def render_replayability_report(
    *,
    dataset: HistoricalDataset,
    profile: ConfigProfile,
    replay_summary: dict[str, Any],
) -> str:
    """Render standalone Replayability & Missing-Data Classification Report."""
    total_d1 = replay_summary["total_d1_replayed"]
    unpruned_dec_cnt = len(dataset.decisions_log)

    lines: list[str] = [
        "# BREAKOUT_IGNITION Replayability & Historical Input Classification Report",
        "",
        "## 1. Provenance Summary",
        "",
        f"- **Strategy Scope:** `{SUPPORTED_STRATEGY}`",
        f"- **Runtime Code Provenance:** `{RUNTIME_CODE_PROVENANCE}`",
        f"- **Configuration Profile:** `{profile.name}` (`{profile.profile_provenance}`)",
        f"- **Snapshot Method:** `{dataset.provenance.snapshot_method}` (`db_sha256={dataset.provenance.db_sha256}`)",
        f"- **Active-Soak `DECISION_1` Rows Audited:** `{total_d1}` across `{len(dataset.episodes)}` episodes (`{len(dataset.episodes_by_mint)}` unique mints)",
        f"- **Unpruned `decisions_log` Rows Remaining:** `{unpruned_dec_cnt}` (1,000-row rolling cap)",
        "",
        "## 2. Historical Input & Execution Evidence Classification Matrix",
        "",
        "| Component / Field | Classification | Source Table / Artifact | Replayability Impact |",
        "| :--- | :--- | :--- | :--- |",
        f"| Live-forming 5m & 1m OHLCV candles | `{MISSING}` | Not persisted in DuckDB | `detect_breakout_ignition()` candle-level detection is `{NOT_REPLAYABLE}`. |",
        f"| Raw `pair_data` (`txns.m5`, `txns.h1`, `volume`) | `{MISSING}` | Not persisted in DuckDB | `SCALPER_MIN_TXNS_5M`, `res_passed`, `BUY_RATIO`, and `BUY_ACCEL` are `{NOT_REPLAYABLE}`. |",
        f"| Recorded detector scalars (`breakout_score`, `state`, `timeframe`, `vol_ratio`, `ema_ext_pct`, `failed_gates`) | `{EXACT_HISTORICAL}` | `action_log` (`100%` of `{total_d1}` `DECISION_1` rows) | Supports `{MODE_RECORDED_DECISION_AUDIT}` and scalar threshold checks (`score`, `vol_ratio`, `ema_ext > 12%`). |",
        f"| VWAP extension (`ext_above_vwap_pct`) & `dist_from_1h_high_pct` | `{PARTIALLY_AVAILABLE}` | `decisions_log.scores_json` (`{unpruned_dec_cnt}` unpruned rows only) | When `ema_ext <= 12%` and `decisions_log` is pruned, anti-chase pass is `{NOT_REPLAYABLE}`. |",
        f"| `mc_usd`, `liq_usd`, `soak_minutes`, `rsi`, `narrative_score`, `momentum_score`, `vamp_score`, `buy_confidence`, `leg2_score` | `{EXACT_HISTORICAL}` | `action_log` (`100%` of `{total_d1}` `DECISION_1` rows) | Full deterministic threshold replay of soak, RSI<=78, narrative, momentum, vamp, and low-score eviction. |",
        f"| Open Chart/Whale positions & slot occupancy at `T` | `{RECONSTRUCTABLE}` | `trades` (`timestamp <= T`) | Deterministic duplicate-mint and slot-cap verification. |",
        f"| Historical Pi configuration (`supplied_phase1_pi_snapshot`) | `{PARTIALLY_AVAILABLE}` / `{INFERRED}` | User-supplied Pi `.env` snapshot + `config.py` defaults for omitted keys | `50` supplied keys + omitted defaults (`PAPER_TX_FEE_SOL=0.001`); PID 8315 runtime remains `{RUNTIME_CODE_PROVENANCE}`. |",
        f"| Executed trade entry & `jupiter_exact_quote` exit fills | `{OBSERVED_QUOTE}` | `trades` + `action_log` | Exact fee and slippage verification on executed trades. |",
        f"| `EXODUS_SELL_FLOW` trigger valuations (`jupiter_v3`) | `{PRICE_ONLY}` | `action_log.monitor_sol_value` | Captures trigger-to-fill slippage against Jupiter `/swap/v2/order` settlement (`{OBSERVED_QUOTE}`). |",
        f"| Untraded candidate executable quotes (e.g., PAIR) | `{UNAVAILABLE}` | Never requested | Untraded candidates have `{UNAVAILABLE}` executable quotes; never fabricated. |",
        f"| 5-second position monitor `HOLD` sell-ratio progression | `{UNAVAILABLE}` | Not persisted in DuckDB | Intermediate 5s sell-flow trajectory prior to Exodus exit is `{UNAVAILABLE}`. |",
        f"| `candidate_outcomes` forward horizons (`1m/5m/15m/30m`, `MFE/MAE`) | `{PRICE_ONLY}` | `candidate_outcomes` (60s DexScreener polling, 15m per-mint dedup) | Post-decision outcome analysis only (strictly blocked from `DecisionSnapshot`). |",
        f"| Post-decision trajectory prices derived from `mc_usd` | `{INFERRED_PRICE_FROM_MARKET_CAP}` | `action_log.mc_usd` anchored to `entry_price_usd` | Diagnostic trajectory only; never presented as exact or executable prices. |",
        "",
        "## 3. Per-Gate Replayability & Status Distribution Across Active Soak",
        "",
        "| Gate / Rule ID | `PASS` | `FAIL` | `EVICT_TRIGGERED` | `EVICT_NOT_TRIGGERED` | `NOT_REPLAYABLE` |",
        "| :--- | ---: | ---: | ---: | ---: | ---: |",
    ]

    for gid, counts in replay_summary["gate_status_counts"].items():
        lines.append(
            f"| `{gid}` | {counts.get('PASS', 0)} | {counts.get('FAIL', 0)} | "
            f"{counts.get('EVICT_TRIGGERED', 0)} | {counts.get('EVICT_NOT_TRIGGERED', 0)} | "
            f"{counts.get(NOT_REPLAYABLE, 0)} |"
        )

    lines.append("")
    return "\n".join(lines)


def build_aggregate_soak_payload(
    *,
    dataset: HistoricalDataset,
    profile: ConfigProfile,
    replay_summary: dict[str, Any],
    execution_report: AggregateExecutionReport,
    outcome_report: AggregateOutcomeReport,
) -> dict[str, Any]:
    return {
        "strategy_scope": SUPPORTED_STRATEGY,
        "runtime_code_provenance": RUNTIME_CODE_PROVENANCE,
        "config_profile": profile.to_metadata_dict(),
        "snapshot_provenance": asdict(dataset.provenance),
        "decision_replay_summary": replay_summary,
        "execution_analysis": asdict(execution_report),
        "outcome_analysis": {
            k: v for k, v in asdict(outcome_report).items() if k != "episode_summaries"
        },
    }


def render_aggregate_soak_report(
    *,
    dataset: HistoricalDataset,
    profile: ConfigProfile,
    replay_summary: dict[str, Any],
    execution_report: AggregateExecutionReport,
    outcome_report: AggregateOutcomeReport,
) -> str:
    """Render deterministic Aggregate Phase 1 BREAKOUT_IGNITION Replay & Audit Report."""
    rc = replay_summary["recorded_decision_class_counts"]
    rv = replay_summary["replay_verdict_counts"]

    lines: list[str] = [
        "# Phase 1 BREAKOUT_IGNITION — Aggregate Historical Replay & Outcome Report",
        "",
        "## 1. Provenance & Configuration Envelope",
        "",
        "| Attribute | Value |",
        "| :--- | :--- |",
        f"| **Strategy Scope** | `{SUPPORTED_STRATEGY}` ONLY |",
        f"| **Runtime Code Provenance** | `{RUNTIME_CODE_PROVENANCE}` |",
        f"| **Configuration Profile** | `{profile.name}` (`{profile.profile_provenance}`) |",
        f"| **Active Soak Cutoff (`--since`)** | `{execution_report.since_utc or 'ALL'}` |",
        f"| **Snapshot Method** | `{dataset.provenance.snapshot_method}` (`wal_included={dataset.provenance.wal_included}`) |",
        f"| **Source DB SHA-256** | `{dataset.provenance.db_sha256}` |",
        f"| **Source WAL SHA-256** | `{dataset.provenance.wal_sha256 or 'NONE'}` |",
        "",
        "---",
        "",
        "## 2. Episode-Grouped Decision Replay Summary",
        "",
        "| Metric | Count / Value |",
        "| :--- | ---: |",
        f"| Total `DECISION_1` Observations Replayed | `{replay_summary['total_d1_replayed']}` |",
        f"| Distinct Mint Watchlist Episodes (`DecisionEpisode`) | `{outcome_report.total_episodes}` |",
        f"| Unique Mints Evaluated | `{outcome_report.unique_mints}` |",
        f"| Recorded `APPROVED` / `BLOCKED` / `WATCH` / `REJECT` | `{rc.get('APPROVED', 0)}` / `{rc.get('BLOCKED', 0)}` / `{rc.get('WATCH', 0)}` / `{rc.get('REJECT', 0)}` |",
        f"| Replay Verdict: `DETERMINISTICALLY_REJECTED_OR_BLOCKED` | `{rv.get('DETERMINISTICALLY_REJECTED_OR_BLOCKED', 0)}` |",
        f"| Replay Verdict: `NOT_REPLAYABLE_FOR_FULL_APPROVAL` (0 scalar fails; candle/txn gates `{NOT_REPLAYABLE}`) | `{rv.get('NOT_REPLAYABLE_FOR_FULL_APPROVAL', 0)}` |",
        f"| Consistent With Recorded Outcome (`{profile.name}`) | `{replay_summary['consistent_count']} / {replay_summary['total_d1_replayed']}` (`mismatches={replay_summary['mismatch_count']}`) |",
        "",
        "---",
        "",
        "## 3. Historical Execution & Fee Accounting Analysis (`trades` + `action_log`)",
        "",
        "| Execution Metric | Value |",
        "| :--- | ---: |",
        f"| `BREAKOUT_IGNITION` Entries (`BUY`) | `{execution_report.total_breakout_entries}` |",
        f"| Closed Lifecycles / Open at Snapshot | `{execution_report.closed_lifecycles}` / `{execution_report.open_lifecycles}` |",
        f"| `PARTIAL_SELL` Legs / Final `SELL` Legs | `{execution_report.partial_sell_count}` / `{execution_report.full_sell_count}` |",
        f"| Configured Paper Fee Per Leg (`PAPER_TX_FEE_SOL`) | `{execution_report.fee_per_leg_sol:.4f} SOL` (`{profile.field_provenance('PAPER_TX_FEE_SOL')}`) |",
        f"| All Trade Fee Deductions Verified Exact | `{execution_report.all_fees_verified}` (`VERIFIED={execution_report.fee_verified_count}`, `FAILED={execution_report.fee_failed_count}`, `UNKNOWN={execution_report.fee_unknown_count}`, coverage=`{execution_report.fee_verification_coverage_pct:.2f}%`, `{execution_report.total_fees_paid_sol:.4f} SOL` expected fees) |",
        f"| Gross PnL (Before Fees) | `{execution_report.gross_pnl_sol:+.6f} SOL` |",
        f"| Net Realized PnL (After Fees) | `{execution_report.net_realized_pnl_sol:+.6f} SOL` |",
        f"| Win Rate (Closed Lifecycles) | `{execution_report.win_rate_pct:.2f}%` (`{execution_report.win_count}W / {execution_report.loss_count}L`) |",
        f"| Instant `EXODUS_SELL_FLOW` Exits (`<= 45s` hold) | `{execution_report.instant_exodus_count_under_45s}` (5s monitor trajectory: `{UNAVAILABLE}`) |",
        "",
        "### Trigger-to-Settlement Slippage by Exit Category",
        "",
        "| Exit Category | Count | Trigger Evidence | Settlement Evidence | Min Slippage % | Median Slippage % | Mean Slippage % | Max Slippage % |",
        "| :--- | ---: | :--- | :--- | ---: | ---: | ---: | ---: |",
    ]

    for cat, stats in execution_report.slippage_by_exit_category.items():
        sample_leg = next(
            (lg for lc in execution_report.lifecycles for lg in lc.exit_legs if lg.exit_category == cat),
            None,
        )
        t_ev = sample_leg.trigger_evidence_class if sample_leg else UNAVAILABLE
        s_ev = sample_leg.settlement_evidence_class if sample_leg else UNAVAILABLE
        lines.append(
            f"| `{cat}` | {stats['count']} | `{t_ev}` | `{s_ev}` | `{stats['min_pct']:+.4f}%` | `{stats['median_pct']:+.4f}%` | `{stats['mean_pct']:+.4f}%` | `{stats['max_pct']:+.4f}%` |"
        )

    lines.extend(
        [
            "",
            "### Reconstructed Historical Trade Lifecycles",
            "",
            "| Buy ID | Symbol | Entry UTC | Hold (s) | Exit Category | Trigger Ev. | Monitor SOL | Settled SOL | Slippage % | Net PnL SOL | MFE % | MAE % |",
            "| ---: | :--- | :--- | ---: | :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for lc in execution_report.lifecycles:
        final_leg = lc.exit_legs[-1] if lc.exit_legs else None
        if final_leg is None:
            lines.append(
                f"| {lc.buy_trade_id} | **{lc.symbol}** | `{lc.entry_timestamp}` | — | `OPEN` | — | — | — | — | — | — | — |"
            )
        else:
            lines.append(
                f"| {lc.buy_trade_id} | **{lc.symbol}** | `{lc.entry_timestamp}` | `{_fmt(final_leg.hold_seconds, 1)}` | `{final_leg.exit_category}` | `{final_leg.trigger_evidence_class}` | `{_fmt(final_leg.monitor_sol_value, 4)}` | `{final_leg.received_sol:.4f}` | `{_fmt(final_leg.trigger_to_fill_slippage_pct, 3, signed=True, pct=True)}` | `{_fmt(lc.net_realized_pnl_sol, 4, signed=True)}` | `{_fmt(final_leg.position_mfe_pct, 2, signed=True, pct=True)}` | `{_fmt(final_leg.position_mae_pct, 2, signed=True, pct=True)}` |"
            )

    lines.extend(
        [
            "",
            "---",
            "",
            "## 4. Episode-Grouped Outcome & Repeated-Observation Analysis (`PRICE_ONLY`)",
            "",
            "> **Important:** `candidate_outcomes` forward metrics reflect 60-second DexScreener spot polling (`PRICE_ONLY`), NOT executable Jupiter quotes.",
            "",
            "| Outcome / Deduplication Metric | Value |",
            "| :--- | ---: |",
            f"| Total `candidate_outcomes` Rows in Window | `{outcome_report.total_outcome_rows}` (`{outcome_report.finalized_outcome_count}` finalized) |",
            f"| Unique Mints vs Repeated Outcome Rows | `{outcome_report.unique_mints}` unique / `{outcome_report.repeated_outcome_rows}` repeats |",
            f"| `DECISION_1` Ticks Suppressed by 15m Dedup | `{outcome_report.dedup_suppressed_d1_observations}` |",
            f"| Approved Episodes With `ENTERED` Outcome Suppressed by Prior `WATCH`/`REJECT` | `{outcome_report.approved_episodes_missing_entered_outcome} / {outcome_report.approved_episodes_count}` |",
            f"| Episodes With Recorded `MFE >= +50%` (`PRICE_ONLY`) | `{outcome_report.episodes_with_mfe_ge_50_pct}` episodes across `{outcome_report.unique_mints_with_mfe_ge_50_pct}` unique mints |",
            f"| Median Episode Best MFE % / Worst MAE % | `{_fmt(outcome_report.median_episode_best_mfe_pct, 2, signed=True, pct=True)}` / `{_fmt(outcome_report.median_episode_worst_mae_pct, 2, signed=True, pct=True)}` |",
            "",
            "### Top 10 Most Repeated Mints (Grouped by Episode)",
            "",
            "| Symbol | Mint | Watchlist Episodes | `DECISION_1` Rows | `candidate_outcomes` Rows |",
            "| :--- | :--- | ---: | ---: | ---: |",
        ]
    )
    for item in outcome_report.top_repeated_mints:
        lines.append(
            f"| **{item['symbol']}** | `{item['mint']}` | {item['episode_count']} | {item['d1_observation_count']} | {item['outcome_row_count']} |"
        )

    lines.append("")
    return "\n".join(lines)
