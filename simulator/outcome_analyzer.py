"""Post-Decision Subsequent Outcome & Episode Analyzer (BREAKOUT_IGNITION ONLY).

Strict isolation rules:
- Consumed ONLY after `DecisionSnapshot` construction and decision replay are complete.
- Groups repeated observations by `(mint, decision_episode)` rather than treating repeated
  `action_log` or `candidate_outcomes` rows as independent trading opportunities.
- Labels all market-cap-derived prices `INFERRED_PRICE_FROM_MARKET_CAP` and all
  `candidate_outcomes` poll metrics `PRICE_ONLY` (never `OBSERVED_QUOTE` or executable fills).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Optional

from simulator import (
    EXACT_HISTORICAL,
    INFERRED_PRICE_FROM_MARKET_CAP,
    MISSING,
    PRICE_ONLY,
    RUNTIME_CODE_PROVENANCE,
    SUPPORTED_STRATEGY,
    UNAVAILABLE,
)
from simulator.data_loader import DecisionEpisode, HistoricalDataset, parse_iso_utc


@dataclass(frozen=True)
class OutcomeSnapshotRecord:
    """Single recorded `candidate_outcomes` 60s-polled forward outcome row."""

    outcome_id: str
    mint: str
    symbol: str
    discovered_at: str
    decision: str
    decision_reason: str
    strategy: str
    entry_price_usd: Optional[float]
    entry_mc_usd: Optional[float]
    entry_liq_usd: Optional[float]
    price_1m_pct: Optional[float]
    price_5m_pct: Optional[float]
    price_15m_pct: Optional[float]
    price_30m_pct: Optional[float]
    mfe_pct: Optional[float]
    mae_pct: Optional[float]
    finalized: bool
    evidence_classification: str  # Always PRICE_ONLY (60s DexScreener polling, NOT executable fill)


@dataclass(frozen=True)
class TrajectoryPoint:
    """Post-decision market-cap, liquidity & inferred-price observation from subsequent `action_log` rows."""

    timestamp_utc: str
    elapsed_minutes: float
    mc_usd: Optional[float]
    liq_usd: Optional[float]
    mc_change_pct: Optional[float]
    liq_change_pct: Optional[float]
    inferred_price_usd: Optional[float]
    price_provenance: str  # INFERRED_PRICE_FROM_MARKET_CAP or MISSING


@dataclass(frozen=True)
class ObservationOutcomeReport:
    """Post-decision outcome report for a specific `(mint, decision_timestamp)` observation."""

    mint: str
    symbol: str
    decision_timestamp: str
    episode_id: Optional[str]
    runtime_code_provenance: str
    matched_outcome_record: Optional[OutcomeSnapshotRecord]
    outcome_deduplicated_away: bool
    subsequent_observation_count_30m: int
    blind_gap_minutes_after_decision: Optional[float]
    peak_mc_usd_after_decision: Optional[float]
    peak_liq_usd_after_decision: Optional[float]
    min_liq_usd_after_decision: Optional[float]
    inferred_price_provenance_label: str
    sample_trajectory_30m: tuple[TrajectoryPoint, ...]
    executable_fill_disclaimer: str


@dataclass(frozen=True)
class EpisodeOutcomeSummary:
    episode_id: str
    mint: str
    symbol: str
    episode_index: int
    started_at: str
    ended_at: str
    d1_observation_count: int
    terminal_action: Optional[str]
    terminal_reason: Optional[str]
    start_mc_usd: Optional[float]
    peak_mc_usd: Optional[float]
    start_liq_usd: Optional[float]
    end_liq_usd: Optional[float]
    linked_outcome_count: int
    best_recorded_mfe_pct: Optional[float]
    worst_recorded_mae_pct: Optional[float]
    entered_trade: bool
    entered_outcome_suppressed_by_dedup: bool


@dataclass(frozen=True)
class AggregateOutcomeReport:
    strategy_scope: str
    runtime_code_provenance: str
    since_utc: Optional[str]
    total_d1_observations: int
    total_episodes: int
    unique_mints: int
    total_outcome_rows: int
    repeated_outcome_rows: int
    dedup_suppressed_d1_observations: int
    approved_episodes_count: int
    approved_episodes_missing_entered_outcome: int
    finalized_outcome_count: int
    unique_mints_with_mfe_ge_50_pct: int
    episodes_with_mfe_ge_50_pct: int
    median_episode_best_mfe_pct: Optional[float]
    median_episode_worst_mae_pct: Optional[float]
    top_repeated_mints: tuple[dict[str, Any], ...]
    episode_summaries: tuple[EpisodeOutcomeSummary, ...]


def _to_outcome_record(row: dict[str, Any]) -> OutcomeSnapshotRecord:
    return OutcomeSnapshotRecord(
        outcome_id=str(row["id"]),
        mint=str(row["mint"]),
        symbol=str(row.get("symbol") or row["mint"][:8]),
        discovered_at=str(row["discovered_at"]),
        decision=str(row.get("decision") or ""),
        decision_reason=str(row.get("decision_reason") or ""),
        strategy=str(row.get("strategy") or ""),
        entry_price_usd=float(row["entry_price_usd"]) if row.get("entry_price_usd") is not None else None,
        entry_mc_usd=float(row["entry_mc_usd"]) if row.get("entry_mc_usd") is not None else None,
        entry_liq_usd=float(row["entry_liq_usd"]) if row.get("entry_liq_usd") is not None else None,
        price_1m_pct=float(row["price_1m_pct"]) if row.get("price_1m_pct") is not None else None,
        price_5m_pct=float(row["price_5m_pct"]) if row.get("price_5m_pct") is not None else None,
        price_15m_pct=float(row["price_15m_pct"]) if row.get("price_15m_pct") is not None else None,
        price_30m_pct=float(row["price_30m_pct"]) if row.get("price_30m_pct") is not None else None,
        mfe_pct=float(row["mfe_pct"]) if row.get("mfe_pct") is not None else None,
        mae_pct=float(row["mae_pct"]) if row.get("mae_pct") is not None else None,
        finalized=bool(row.get("finalized", False)),
        evidence_classification=PRICE_ONLY,
    )


def analyze_observation_outcome(
    dataset: HistoricalDataset,
    *,
    mint: str,
    symbol: str,
    decision_timestamp: str,
    base_mc_usd: Optional[float],
    base_liq_usd: Optional[float],
    episode_id: Optional[str] = None,
) -> ObservationOutcomeReport:
    """Analyze post-decision forward outcomes and market-cap/liquidity trajectory for one observation."""
    dec_dt = parse_iso_utc(decision_timestamp)
    if dec_dt is None:
        raise ValueError(f"Invalid decision_timestamp: {decision_timestamp!r}")

    matched_out: Optional[OutcomeSnapshotRecord] = None
    for o in dataset.candidate_outcomes:
        if str(o.get("mint") or "").strip() != mint:
            continue
        o_dt = parse_iso_utc(o.get("discovered_at"))
        if o_dt is not None and abs((o_dt - dec_dt).total_seconds()) <= 2.0:
            matched_out = _to_outcome_record(o)
            break

    anchor_price = matched_out.entry_price_usd if matched_out is not None else None
    anchor_mc = (
        matched_out.entry_mc_usd
        if (matched_out is not None and matched_out.entry_mc_usd)
        else base_mc_usd
    )

    future_d1: list[tuple[float, dict[str, Any]]] = []
    all_future_mc: list[float] = []
    all_future_liq: list[float] = []

    for r in dataset.action_log:
        if str(r.get("asset_id") or "").strip() != mint:
            continue
        if str(r.get("action") or "").upper() != "DECISION_1":
            continue
        r_dt = parse_iso_utc(r.get("timestamp_utc"))
        if r_dt is None or r_dt <= dec_dt:
            continue
        elapsed_min = (r_dt - dec_dt).total_seconds() / 60.0
        if r.get("mc_usd") is not None:
            all_future_mc.append(float(r["mc_usd"]))
        if r.get("liq_usd") is not None:
            all_future_liq.append(float(r["liq_usd"]))
        if elapsed_min <= 30.0:
            future_d1.append((elapsed_min, r))

    blind_gap: Optional[float] = None
    if future_d1:
        blind_gap = round(future_d1[0][0], 2)
    else:
        # Check if there is any later observation beyond 30m
        for r in dataset.action_log:
            if str(r.get("asset_id") or "").strip() == mint and str(r.get("action") or "").upper() == "DECISION_1":
                r_dt = parse_iso_utc(r.get("timestamp_utc"))
                if r_dt is not None and r_dt > dec_dt:
                    blind_gap = round((r_dt - dec_dt).total_seconds() / 60.0, 2)
                    break

    traj: list[TrajectoryPoint] = []
    # Downsample to at most 12 representative points across the 30m window
    step = max(1, len(future_d1) // 12) if len(future_d1) > 12 else 1
    sampled = future_d1[::step]
    if future_d1 and sampled[-1] != future_d1[-1]:
        sampled.append(future_d1[-1])

    for elapsed_min, r in sampled:
        mc_v = float(r["mc_usd"]) if r.get("mc_usd") is not None else None
        liq_v = float(r["liq_usd"]) if r.get("liq_usd") is not None else None
        mc_chg = (
            round(((mc_v - base_mc_usd) / base_mc_usd) * 100.0, 2)
            if (mc_v is not None and base_mc_usd and base_mc_usd > 0)
            else None
        )
        liq_chg = (
            round(((liq_v - base_liq_usd) / base_liq_usd) * 100.0, 2)
            if (liq_v is not None and base_liq_usd and base_liq_usd > 0)
            else None
        )
        if anchor_price is not None and anchor_price > 0 and anchor_mc and anchor_mc > 0 and mc_v is not None:
            inf_p: Optional[float] = round(anchor_price * (mc_v / anchor_mc), 10)
            p_prov = INFERRED_PRICE_FROM_MARKET_CAP
        else:
            inf_p = None
            p_prov = MISSING
        traj.append(
            TrajectoryPoint(
                timestamp_utc=str(r["timestamp_utc"]),
                elapsed_minutes=round(elapsed_min, 2),
                mc_usd=mc_v,
                liq_usd=liq_v,
                mc_change_pct=mc_chg,
                liq_change_pct=liq_chg,
                inferred_price_usd=inf_p,
                price_provenance=p_prov,
            )
        )

    return ObservationOutcomeReport(
        mint=mint,
        symbol=symbol,
        decision_timestamp=decision_timestamp,
        episode_id=episode_id,
        runtime_code_provenance=RUNTIME_CODE_PROVENANCE,
        matched_outcome_record=matched_out,
        outcome_deduplicated_away=(matched_out is None),
        subsequent_observation_count_30m=len(future_d1),
        blind_gap_minutes_after_decision=blind_gap,
        peak_mc_usd_after_decision=max(all_future_mc) if all_future_mc else None,
        peak_liq_usd_after_decision=max(all_future_liq) if all_future_liq else None,
        min_liq_usd_after_decision=min(all_future_liq) if all_future_liq else None,
        inferred_price_provenance_label=INFERRED_PRICE_FROM_MARKET_CAP,
        sample_trajectory_30m=tuple(traj),
        executable_fill_disclaimer=(
            "Outcome metrics are 60-second DexScreener spot polls (PRICE_ONLY), and trajectory "
            "USD prices between snapshots are INFERRED_PRICE_FROM_MARKET_CAP. Neither proves "
            "executable Jupiter fill prices or liquidity depth."
        ),
    )


def analyze_aggregate_outcomes(
    dataset: HistoricalDataset,
    *,
    since_utc: Optional[str] = None,
) -> AggregateOutcomeReport:
    """Compute episode-grouped and deduplicated outcome analytics across the dataset."""
    since_dt = parse_iso_utc(since_utc) if since_utc else None

    outcomes_by_id: dict[str, OutcomeSnapshotRecord] = {}
    soak_outcomes: list[OutcomeSnapshotRecord] = []
    for o in dataset.candidate_outcomes:
        if str(o.get("source") or "").upper() == "WHALE_SCALP":
            continue
        o_dt = parse_iso_utc(o.get("discovered_at"))
        rec = _to_outcome_record(o)
        outcomes_by_id[rec.outcome_id] = rec
        if since_dt is None or (o_dt is not None and o_dt >= since_dt):
            soak_outcomes.append(rec)

    seen_out_mints: set[str] = set()
    repeated_out_cnt = 0
    for rec in soak_outcomes:
        if rec.mint in seen_out_mints:
            repeated_out_cnt += 1
        else:
            seen_out_mints.add(rec.mint)

    ep_summaries: list[EpisodeOutcomeSummary] = []
    total_d1 = 0
    approved_ep_cnt = 0
    approved_missing_entered = 0
    mints_ge_50: set[str] = set()
    eps_ge_50 = 0
    best_mfes: list[float] = []
    worst_maes: list[float] = []

    for ep in dataset.episodes:
        total_d1 += ep.observation_count
        mcs = [float(r["mc_usd"]) for r in ep.decision1_rows if r.get("mc_usd") is not None]
        liqs = [float(r["liq_usd"]) for r in ep.decision1_rows if r.get("liq_usd") is not None]
        linked_recs = [outcomes_by_id[oid] for oid in ep.linked_outcome_ids if oid in outcomes_by_id]
        mfes = [r.mfe_pct for r in linked_recs if r.mfe_pct is not None]
        maes = [r.mae_pct for r in linked_recs if r.mae_pct is not None]
        best_mfe = max(mfes) if mfes else None
        worst_mae = min(maes) if maes else None

        if best_mfe is not None:
            best_mfes.append(best_mfe)
            if best_mfe >= 50.0:
                eps_ge_50 += 1
                mints_ge_50.add(ep.mint)
        if worst_mae is not None:
            worst_maes.append(worst_mae)

        had_approve = any("-> APPROVE" in str(r.get("reason") or "") for r in ep.decision1_rows) or (
            ep.terminal_action in ("BUY", "REENTRY_BUY")
        )
        has_entered_out = any(r.decision == "ENTERED" for r in linked_recs)
        suppressed_entered = had_approve and not has_entered_out
        if had_approve:
            approved_ep_cnt += 1
            if suppressed_entered:
                approved_missing_entered += 1

        ep_summaries.append(
            EpisodeOutcomeSummary(
                episode_id=ep.episode_id,
                mint=ep.mint,
                symbol=ep.symbol,
                episode_index=ep.episode_index,
                started_at=ep.started_at,
                ended_at=ep.ended_at,
                d1_observation_count=ep.observation_count,
                terminal_action=ep.terminal_action,
                terminal_reason=ep.terminal_reason,
                start_mc_usd=mcs[0] if mcs else None,
                peak_mc_usd=max(mcs) if mcs else None,
                start_liq_usd=liqs[0] if liqs else None,
                end_liq_usd=liqs[-1] if liqs else None,
                linked_outcome_count=len(linked_recs),
                best_recorded_mfe_pct=best_mfe,
                worst_recorded_mae_pct=worst_mae,
                entered_trade=had_approve,
                entered_outcome_suppressed_by_dedup=suppressed_entered,
            )
        )

    # Also check soak_outcomes for any mint >= 50% MFE whose outcome wasn't linked within episode bounds
    for rec in soak_outcomes:
        if rec.mfe_pct is not None and rec.mfe_pct >= 50.0:
            mints_ge_50.add(rec.mint)

    mint_rollup: list[dict[str, Any]] = []
    for m, eps in dataset.episodes_by_mint.items():
        d1_sum = sum(e.observation_count for e in eps)
        out_cnt = sum(1 for r in soak_outcomes if r.mint == m)
        mint_rollup.append(
            {
                "mint": m,
                "symbol": eps[-1].symbol,
                "episode_count": len(eps),
                "d1_observation_count": d1_sum,
                "outcome_row_count": out_cnt,
            }
        )
    mint_rollup.sort(key=lambda x: (-x["d1_observation_count"], -x["episode_count"], x["mint"]))

    return AggregateOutcomeReport(
        strategy_scope=SUPPORTED_STRATEGY,
        runtime_code_provenance=RUNTIME_CODE_PROVENANCE,
        since_utc=since_utc,
        total_d1_observations=total_d1,
        total_episodes=len(dataset.episodes),
        unique_mints=len(dataset.episodes_by_mint),
        total_outcome_rows=len(soak_outcomes),
        repeated_outcome_rows=repeated_out_cnt,
        dedup_suppressed_d1_observations=max(0, total_d1 - len(soak_outcomes)),
        approved_episodes_count=approved_ep_cnt,
        approved_episodes_missing_entered_outcome=approved_missing_entered,
        finalized_outcome_count=sum(1 for r in soak_outcomes if r.finalized),
        unique_mints_with_mfe_ge_50_pct=len(mints_ge_50),
        episodes_with_mfe_ge_50_pct=eps_ge_50,
        median_episode_best_mfe_pct=round(statistics.median(best_mfes), 2) if best_mfes else None,
        median_episode_worst_mae_pct=round(statistics.median(worst_maes), 2) if worst_maes else None,
        top_repeated_mints=tuple(mint_rollup[:10]),
        episode_summaries=tuple(ep_summaries),
    )


__all__ = [
    "AggregateOutcomeReport",
    "EpisodeOutcomeSummary",
    "ObservationOutcomeReport",
    "OutcomeSnapshotRecord",
    "TrajectoryPoint",
    "analyze_aggregate_outcomes",
    "analyze_observation_outcome",
    "EXACT_HISTORICAL",
    "UNAVAILABLE",
]
