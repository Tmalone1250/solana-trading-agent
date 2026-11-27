"""BREAKOUT_IGNITION Recorded Decision Audit & Deterministic Gate Replay Engine.

Implements two strictly separated concepts:
1. `RECORDED_DECISION_AUDIT`: Reconstructs what historical telemetry records say happened,
   separating recorded hard-veto diagnostics from score-only deductions.
2. `DETERMINISTIC_GATE_REPLAY`: Reevaluates ONLY individual BREAKOUT_IGNITION gates whose
   inputs are completely available at `decision_timestamp`. Any gate with missing inputs
   returns `NOT_REPLAYABLE` (never an assumed pass).

Operational guarantees:
- Limited strictly to `BREAKOUT_IGNITION`.
- Never imports `main.py`, `executor_node`, `alpha_node`, `ohlcv_client`, or external clients.
- Never accesses future outcome fields (`DecisionSnapshot` enforces `LookAheadViolationError`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from simulator import (
    EXACT_HISTORICAL,
    INFERRED,
    MISSING,
    MODE_DETERMINISTIC_GATE_REPLAY,
    MODE_RECORDED_DECISION_AUDIT,
    NOT_REPLAYABLE,
    RECONSTRUCTABLE,
    RUNTIME_CODE_PROVENANCE,
    SUPPORTED_STRATEGY,
)
from simulator.config_profiles import ConfigProfile
from simulator.snapshot_builder import DecisionSnapshot

# Hard-veto vs score-only prefixes in `breakout_ignition.py:227-297, 354-360`
HARD_VETO_DIAGNOSTIC_PREFIXES: tuple[str, ...] = (
    "BARS(",
    "RES(",
    "RES_BODY(",
    "VOL_SPIKE(",
    "EXT(",
    "RSI(",
)
SCORE_DEDUCTION_DIAGNOSTIC_PREFIXES: tuple[str, ...] = (
    "COIL(",
    "BUY_RATIO(",
    "BUY_ACCEL(",
    "EMA(",
    "VWAP(",
    "HTF_15M(",
)

BREAKOUT_MAX_EXTENSION_PCT = 12.0  # `breakout_ignition.py:129` (`max_extension_pct = 0.12`)
BREAKOUT_MAX_RSI = 78.0  # `breakout_ignition.py:213` & `scalper_entry_engine.py:197`


@dataclass(frozen=True)
class GateEvaluation:
    """Result of evaluating a single BREAKOUT_IGNITION gate without look-ahead or fabrication."""

    gate_id: str
    stage: str
    description: str
    is_hard_entry_gate: bool
    status: str  # "PASS", "FAIL", "NOT_REPLAYABLE", "EVICT_TRIGGERED", "EVICT_NOT_TRIGGERED"
    availability: str  # EXACT_HISTORICAL, RECONSTRUCTABLE, MISSING, NOT_REPLAYABLE, etc.
    observed_value: Any
    threshold_value: Any
    threshold_provenance: str
    explanation: str


@dataclass(frozen=True)
class RecordedDecisionAuditResult:
    """Concept A: Structured reconstruction of what the historical telemetry recorded."""

    replay_mode: str
    strategy_scope: str
    runtime_code_provenance: str
    config_profile_name: str
    config_profile_provenance: str
    source_table: str
    source_action_log_id: int
    source_terminal_action_log_id: Optional[int]
    source_decisions_log_id: Optional[int]
    episode_id: Optional[str]
    mint: str
    symbol: str
    decision_timestamp: str

    recorded_strategy: str
    recorded_decision_class: str  # "APPROVED", "BLOCKED", "WATCH", "REJECT", "UNKNOWN"
    recorded_alpha_action: str  # "BUY", "WATCH", "REJECT", "AMBIGUOUS_BLOCKED"
    recorded_alpha_action_provenance: str
    recorded_reason: str
    recorded_evicted_same_tick: bool
    recorded_eviction_reason: Optional[str]

    recorded_metrics: dict[str, Any]
    recorded_metric_provenance: dict[str, str]
    recorded_hard_veto_diagnostics: tuple[str, ...]
    recorded_score_deduction_diagnostics: tuple[str, ...]
    unclassified_diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class DeterministicGateReplayResult:
    """Concept B: Independent reevaluation of individual gates with complete historical inputs."""

    replay_mode: str
    strategy_scope: str
    runtime_code_provenance: str
    config_profile_name: str
    config_profile_provenance: str
    source_table: str
    source_action_log_id: int
    episode_id: Optional[str]
    mint: str
    symbol: str
    decision_timestamp: str

    gates: tuple[GateEvaluation, ...]
    eviction_gates: tuple[GateEvaluation, ...]
    replayable_hard_gate_count: int
    non_replayable_hard_gate_count: int
    failed_hard_gate_ids: tuple[str, ...]
    passed_hard_gate_ids: tuple[str, ...]
    not_replayable_gate_ids: tuple[str, ...]

    overall_entry_verdict: str
    # Either:
    # - "DETERMINISTICALLY_REJECTED_OR_BLOCKED" (at least 1 replayable hard gate provably FAILED)
    # - "NOT_REPLAYABLE_FOR_FULL_APPROVAL" (0 replayable hard gates failed, but candle/txn gates are NOT_REPLAYABLE)
    eviction_replay_verdict: str
    consistent_with_recorded_outcome: Optional[bool]
    consistency_explanation: str


def _classify_recorded_outcome(
    recorded_reason: str,
    decisions_log_action: Optional[str],
) -> tuple[str, str, str]:
    """Return `(decision_class, alpha_action, alpha_action_provenance)` from recorded strings."""
    r = (recorded_reason or "").strip()
    if "-> APPROVE" in r:
        prefix = r.split("->", 1)[0].strip().upper()
        act = decisions_log_action.upper() if decisions_log_action else (prefix or "BUY")
        prov = EXACT_HISTORICAL
        return "APPROVED", act, prov
    if r.startswith("BLOCKED:"):
        if decisions_log_action:
            return "BLOCKED", decisions_log_action.upper(), EXACT_HISTORICAL
        # Without `decisions_log`, `BLOCKED:` is emitted when `dec.action == "BUY" or decision1_wants_buy`
        return "BLOCKED", "BUY_OR_FAST_TRACK", INFERRED
    if r == "WATCH -> WATCH" or r.startswith("WATCH ->"):
        return "WATCH", (decisions_log_action or "WATCH").upper(), EXACT_HISTORICAL
    if r == "REJECT -> REJECT" or r.startswith("REJECT ->"):
        return "REJECT", (decisions_log_action or "REJECT").upper(), EXACT_HISTORICAL
    return "UNKNOWN", (decisions_log_action or "UNKNOWN").upper(), INFERRED


def split_breakout_diagnostics(
    failed_gates: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Split `breakout_failed_gates` into `(hard_vetoes, score_deductions, unclassified)`."""
    hard: list[str] = []
    soft: list[str] = []
    other: list[str] = []
    for item in failed_gates:
        if any(item.startswith(p) for p in HARD_VETO_DIAGNOSTIC_PREFIXES):
            hard.append(item)
        elif any(item.startswith(p) for p in SCORE_DEDUCTION_DIAGNOSTIC_PREFIXES):
            soft.append(item)
        else:
            other.append(item)
    return tuple(hard), tuple(soft), tuple(other)


def run_recorded_decision_audit(
    snapshot: DecisionSnapshot,
    profile: ConfigProfile,
) -> RecordedDecisionAuditResult:
    """Concept A: Reconstruct what historical telemetry says happened at `decision_timestamp`."""
    dec_class, alpha_act, act_prov = _classify_recorded_outcome(
        snapshot.recorded_reason, snapshot.decisions_log_action
    )
    hard_diag, soft_diag, other_diag = split_breakout_diagnostics(snapshot.breakout_failed_gates)

    metrics = {
        "mc_usd": snapshot.mc_usd,
        "liq_usd": snapshot.liq_usd,
        "soak_minutes": snapshot.soak_minutes,
        "rsi": snapshot.rsi,
        "leg2_score": snapshot.leg2_score,
        "narrative_score": snapshot.narrative_score,
        "momentum_score": snapshot.momentum_score,
        "vamp_score": snapshot.vamp_score,
        "buy_confidence": snapshot.buy_confidence,
        "breakout_score": snapshot.breakout_score,
        "breakout_state": snapshot.breakout_state,
        "breakout_timeframe": snapshot.breakout_timeframe,
        "breakout_vol_ratio": snapshot.breakout_vol_ratio,
        "breakout_ema_ext_pct": snapshot.breakout_ema_ext_pct,
        "breakout_failed_gates": list(snapshot.breakout_failed_gates),
    }
    prov_map = {
        k: snapshot.input_availability.get(k, MISSING)
        for k in metrics.keys()
    }

    return RecordedDecisionAuditResult(
        replay_mode=MODE_RECORDED_DECISION_AUDIT,
        strategy_scope=SUPPORTED_STRATEGY,
        runtime_code_provenance=RUNTIME_CODE_PROVENANCE,
        config_profile_name=profile.name,
        config_profile_provenance=profile.profile_provenance,
        source_table="action_log",
        source_action_log_id=snapshot.action_log_id,
        source_terminal_action_log_id=snapshot.same_tick_terminal_id,
        source_decisions_log_id=snapshot.decisions_log_id,
        episode_id=snapshot.episode_id,
        mint=snapshot.mint,
        symbol=snapshot.symbol,
        decision_timestamp=snapshot.decision_timestamp,
        recorded_strategy=snapshot.recorded_strategy,
        recorded_decision_class=dec_class,
        recorded_alpha_action=alpha_act,
        recorded_alpha_action_provenance=act_prov,
        recorded_reason=snapshot.recorded_reason,
        recorded_evicted_same_tick=(snapshot.same_tick_terminal_action == "EVICT"),
        recorded_eviction_reason=snapshot.same_tick_terminal_reason,
        recorded_metrics=metrics,
        recorded_metric_provenance=prov_map,
        recorded_hard_veto_diagnostics=hard_diag,
        recorded_score_deduction_diagnostics=soft_diag,
        unclassified_diagnostics=other_diag,
    )


def run_deterministic_gate_replay(
    snapshot: DecisionSnapshot,
    profile: ConfigProfile,
) -> DeterministicGateReplayResult:
    """Concept B: Recompute only individual BREAKOUT_IGNITION gates with complete historical inputs."""
    cfg = profile.config
    gates: list[GateEvaluation] = []

    # 1. Raw candle & order-flow detector gates — NEVER persisted in Phase 1 DuckDB -> NOT_REPLAYABLE
    gates.append(
        GateEvaluation(
            gate_id="G01_RAW_BREAKOUT_DETECTOR",
            stage="breakout_ignition.detect_breakout_ignition",
            description="Full candle-level BREAKOUT_IGNITION detection from live-forming 5m/1m OHLCV & pair_data",
            is_hard_entry_gate=True,
            status=NOT_REPLAYABLE,
            availability=MISSING,
            observed_value=None,
            threshold_value=">=12 5m or >=15 1m live candles + raw pair_data",
            threshold_provenance="HARDCODED_CODE_INVARIANT",
            explanation=(
                "Raw live-forming 5m/1m OHLCV candles and raw DexScreener pair_data were not persisted "
                "in DuckDB; independent detector recreation is NOT_REPLAYABLE."
            ),
        )
    )

    gates.append(
        GateEvaluation(
            gate_id="G02_BREAKOUT_RESISTANCE_AND_BODY",
            stage="breakout_ignition.detect_breakout_ignition (L188-195, L356)",
            description="Close > 15m-30m swing high, green candle, and body_pos_pct >= 65% (res_passed)",
            is_hard_entry_gate=True,
            status=NOT_REPLAYABLE,
            availability=MISSING,
            observed_value=None,
            threshold_value="close > swing_high AND body_pos_pct >= 0.65",
            threshold_provenance="HARDCODED_CODE_INVARIANT",
            explanation=(
                "Prior candle highs and breakout candle open/high/low/close are MISSING; "
                "cannot independently recompute res_passed without trusting recorded diagnostic strings."
            ),
        )
    )

    gates.append(
        GateEvaluation(
            gate_id="G03_BREAKOUT_FLOW_AND_ACCEL_SCORE_PILLARS",
            stage="breakout_ignition.detect_breakout_ignition (L252-266)",
            description="5m buy ratio >= 62% (8 pts) & 5m buy acceleration >= 1.5x (7 pts) [score-only pillars]",
            is_hard_entry_gate=False,
            status=NOT_REPLAYABLE,
            availability=MISSING,
            observed_value=None,
            threshold_value="buy_ratio_5m >= 0.62; buys_accel >= 1.5x (score deductions only)",
            threshold_provenance="HARDCODED_CODE_INVARIANT",
            explanation=(
                "Raw 5m/1h buy and sell transaction counts are MISSING. Absence of BUY_RATIO or "
                "BUY_ACCEL in breakout_failed_gates is never treated as proof of passing."
            ),
        )
    )

    # 2. Recorded detector scalar threshold gates (`vol_ratio`, `breakout_score`, `extension`, `rsi`)
    vol_thresh = float(cfg.BREAKOUT_MIN_VOL_RATIO)
    if snapshot.breakout_vol_ratio is None:
        vol_status = NOT_REPLAYABLE
        vol_avail = MISSING
        vol_exp = "breakout_vol_ratio is NULL in action_log."
    else:
        vol_ok = snapshot.breakout_vol_ratio >= vol_thresh
        vol_status = "PASS" if vol_ok else "FAIL"
        vol_avail = EXACT_HISTORICAL
        vol_exp = (
            f"Recorded breakout_vol_ratio={snapshot.breakout_vol_ratio:.2f}x "
            f"{'>=' if vol_ok else '<'} {vol_thresh:.2f}x (note: uses recorded detector scalar; raw candle volumes MISSING)."
        )
    gates.append(
        GateEvaluation(
            gate_id="G04_BREAKOUT_VOL_RATIO_THRESHOLD",
            stage="breakout_ignition.detect_breakout_ignition (L201, L357)",
            description="Recorded breakout volume spike ratio >= BREAKOUT_MIN_VOL_RATIO",
            is_hard_entry_gate=True,
            status=vol_status,
            availability=vol_avail,
            observed_value=snapshot.breakout_vol_ratio,
            threshold_value=vol_thresh,
            threshold_provenance=profile.field_provenance("BREAKOUT_MIN_VOL_RATIO"),
            explanation=vol_exp,
        )
    )

    score_thresh = int(cfg.BREAKOUT_MIN_SCORE)
    if snapshot.breakout_score is None:
        sc_status = NOT_REPLAYABLE
        sc_avail = MISSING
        sc_exp = "breakout_score is NULL in action_log."
    else:
        sc_ok = snapshot.breakout_score >= score_thresh
        sc_status = "PASS" if sc_ok else "FAIL"
        sc_avail = EXACT_HISTORICAL
        sc_exp = (
            f"Recorded breakout_score={snapshot.breakout_score} "
            f"{'>=' if sc_ok else '<'} {score_thresh} (uses recorded detector score; raw candles MISSING)."
        )
    gates.append(
        GateEvaluation(
            gate_id="G05_BREAKOUT_MIN_SCORE_THRESHOLD",
            stage="breakout_ignition.detect_breakout_ignition (L355)",
            description="Recorded breakout score >= BREAKOUT_MIN_SCORE",
            is_hard_entry_gate=True,
            status=sc_status,
            availability=sc_avail,
            observed_value=snapshot.breakout_score,
            threshold_value=score_thresh,
            threshold_provenance=profile.field_provenance("BREAKOUT_MIN_SCORE"),
            explanation=sc_exp,
        )
    )

    # Anti-chase extension requires BOTH `ext_ema9 <= 12%` AND `ext_vwap <= 12%`.
    # `action_log` only stores `breakout_ema_ext_pct`. `ext_above_vwap_pct` exists only if `decisions_log` survived pruning.
    vwap_ext_pct: Optional[float] = None
    if snapshot.decisions_log_scores is not None:
        bm = snapshot.decisions_log_scores.get("breakout_metrics")
        if isinstance(bm, dict) and bm.get("ext_above_vwap_pct") is not None:
            vwap_ext_pct = float(bm["ext_above_vwap_pct"]) * 100.0

    if snapshot.breakout_ema_ext_pct is not None and snapshot.breakout_ema_ext_pct > BREAKOUT_MAX_EXTENSION_PCT:
        ext_status = "FAIL"
        ext_avail = EXACT_HISTORICAL
        ext_obs: Any = {"ema_ext_pct": snapshot.breakout_ema_ext_pct, "vwap_ext_pct": vwap_ext_pct}
        ext_exp = (
            f"breakout_ema_ext_pct={snapshot.breakout_ema_ext_pct:+.2f}% > +{BREAKOUT_MAX_EXTENSION_PCT:.1f}% "
            "(provably over-extended regardless of missing VWAP extension)."
        )
    elif snapshot.breakout_ema_ext_pct is not None and vwap_ext_pct is not None:
        both_ok = (
            snapshot.breakout_ema_ext_pct <= BREAKOUT_MAX_EXTENSION_PCT
            and vwap_ext_pct <= BREAKOUT_MAX_EXTENSION_PCT
        )
        ext_status = "PASS" if both_ok else "FAIL"
        ext_avail = EXACT_HISTORICAL
        ext_obs = {"ema_ext_pct": snapshot.breakout_ema_ext_pct, "vwap_ext_pct": round(vwap_ext_pct, 2)}
        ext_exp = (
            f"ema_ext_pct={snapshot.breakout_ema_ext_pct:+.2f}%, vwap_ext_pct={vwap_ext_pct:+.2f}% "
            f"vs +{BREAKOUT_MAX_EXTENSION_PCT:.1f}% cap."
        )
    else:
        ext_status = NOT_REPLAYABLE
        ext_avail = MISSING
        ext_obs = {"ema_ext_pct": snapshot.breakout_ema_ext_pct, "vwap_ext_pct": None}
        ext_exp = (
            f"ema_ext_pct={snapshot.breakout_ema_ext_pct} <= +12.0%, but ext_above_vwap_pct is MISSING "
            "(decisions_log pruned); refusing to assume VWAP extension passed."
        )
    gates.append(
        GateEvaluation(
            gate_id="G06_BREAKOUT_ANTI_CHASE_EXTENSION",
            stage="breakout_ignition.detect_breakout_ignition (L218-220, L358)",
            description="Anti-chase extension <= +12.0% above both EMA9 and VWAP",
            is_hard_entry_gate=True,
            status=ext_status,
            availability=ext_avail,
            observed_value=ext_obs,
            threshold_value=BREAKOUT_MAX_EXTENSION_PCT,
            threshold_provenance="HARDCODED_CODE_INVARIANT",
            explanation=ext_exp,
        )
    )

    if snapshot.rsi is None:
        rsi_status = NOT_REPLAYABLE
        rsi_avail = MISSING
        rsi_exp = "rsi is NULL in action_log."
    else:
        rsi_ok = snapshot.rsi <= BREAKOUT_MAX_RSI
        rsi_status = "PASS" if rsi_ok else "FAIL"
        rsi_avail = EXACT_HISTORICAL
        rsi_exp = f"Recorded RSI={snapshot.rsi:.2f} {'<=' if rsi_ok else '>'} {BREAKOUT_MAX_RSI:.1f}."
    gates.append(
        GateEvaluation(
            gate_id="G07_BREAKOUT_RSI_CEILING",
            stage="breakout_ignition (L213, L359) & scalper_entry_engine (L286)",
            description="Breakout RSI ceiling <= 78.0",
            is_hard_entry_gate=True,
            status=rsi_status,
            availability=rsi_avail,
            observed_value=snapshot.rsi,
            threshold_value=BREAKOUT_MAX_RSI,
            threshold_provenance="HARDCODED_CODE_INVARIANT",
            explanation=rsi_exp,
        )
    )

    # 3. Scalper Entry Engine gates (`soak`, `SCALPER_MIN_TXNS_5M`, `CHART_ENABLED_STRATEGIES`)
    min_soak = float(cfg.MIN_WATCHLIST_SOAK_MINUTES)
    if snapshot.soak_minutes is None:
        soak_status = NOT_REPLAYABLE
        soak_avail = MISSING
        soak_exp = "soak_minutes is NULL in action_log."
    else:
        soak_ok = snapshot.soak_minutes >= min_soak
        soak_status = "PASS" if soak_ok else "FAIL"
        soak_avail = EXACT_HISTORICAL
        soak_exp = f"Recorded soak_minutes={snapshot.soak_minutes:.2f}m {'>=' if soak_ok else '<'} {min_soak:.2f}m."
    gates.append(
        GateEvaluation(
            gate_id="G08_WATCHLIST_SOAK_GATE",
            stage="scalper_entry_engine.evaluate_scalper_entry (L220, L280)",
            description="Watchlist soak duration >= MIN_WATCHLIST_SOAK_MINUTES",
            is_hard_entry_gate=True,
            status=soak_status,
            availability=soak_avail,
            observed_value=snapshot.soak_minutes,
            threshold_value=min_soak,
            threshold_provenance=profile.field_provenance("MIN_WATCHLIST_SOAK_MINUTES"),
            explanation=soak_exp,
        )
    )

    gates.append(
        GateEvaluation(
            gate_id="G09_SCALPER_MIN_TXNS_5M",
            stage="scalper_entry_engine.evaluate_scalper_entry (L222, L305)",
            description="5m total transactions >= SCALPER_MIN_TXNS_5M (note: SCALPER_MIN_BUY_RATIO_5M bypassed for BREAKOUT_IGNITION)",
            is_hard_entry_gate=True,
            status=NOT_REPLAYABLE,
            availability=MISSING,
            observed_value=None,
            threshold_value=int(cfg.SCALPER_MIN_TXNS_5M),
            threshold_provenance=profile.field_provenance("SCALPER_MIN_TXNS_5M"),
            explanation=(
                "Historical 5m transaction counts (txns.m5.buys + sells) were not persisted in DuckDB; "
                "SCALPER_MIN_TXNS_5M cannot be independently evaluated."
            ),
        )
    )

    bo_enabled = SUPPORTED_STRATEGY in cfg.chart_enabled_strategies()
    gates.append(
        GateEvaluation(
            gate_id="G10_CHART_STRATEGY_ALLOWLIST",
            stage="scalper_entry_engine (L274), alpha_node (L229, L439), executor_node (L134)",
            description="BREAKOUT_IGNITION permitted by CHART_ENABLED_STRATEGIES",
            is_hard_entry_gate=True,
            status="PASS" if bo_enabled else "FAIL",
            availability=EXACT_HISTORICAL,
            observed_value=sorted(cfg.chart_enabled_strategies()),
            threshold_value=SUPPORTED_STRATEGY,
            threshold_provenance=profile.field_provenance("CHART_ENABLED_STRATEGIES"),
            explanation=(
                f"BREAKOUT_IGNITION is {'enabled' if bo_enabled else 'disabled'} in profile '{profile.name}'."
            ),
        )
    )

    # 4. Alpha standard approval gates (`action == "BUY"`, `conf`, `vamp`, `narr`, `mom`)
    _, alpha_act, act_prov = _classify_recorded_outcome(
        snapshot.recorded_reason, snapshot.decisions_log_action
    )
    if alpha_act in ("BUY", "WATCH", "REJECT"):
        act_status = "PASS" if alpha_act == "BUY" else "FAIL"
        act_avail = act_prov
        act_exp = f"Recorded Alpha action='{alpha_act}' {'==' if alpha_act == 'BUY' else '!='} 'BUY'."
    else:
        act_status = NOT_REPLAYABLE
        act_avail = MISSING
        act_exp = (
            f"Recorded reason '{snapshot.recorded_reason[:60]}' does not disambiguate BUY vs LEG2_FAST_TRACK "
            "and decisions_log row was pruned."
        )
    gates.append(
        GateEvaluation(
            gate_id="G11_ALPHA_ACTION_CHOICE",
            stage="alpha_node.alpha_node (L213)",
            description="Alpha action choice == 'BUY' (standard BREAKOUT_IGNITION path)",
            is_hard_entry_gate=True,
            status=act_status,
            availability=act_avail,
            observed_value=alpha_act,
            threshold_value="BUY",
            threshold_provenance="HARDCODED_CODE_INVARIANT",
            explanation=act_exp,
        )
    )

    min_conf = float(cfg.MIN_BUY_CONFIDENCE)
    if snapshot.buy_confidence is None:
        conf_status = NOT_REPLAYABLE
        conf_avail = MISSING
        conf_exp = "buy_confidence is NULL in action_log."
    elif alpha_act in ("WATCH", "REJECT"):
        # `action_log.buy_confidence` stores `dec.action_confidence` for whichever action was chosen!
        conf_status = NOT_REPLAYABLE
        conf_avail = EXACT_HISTORICAL
        conf_exp = (
            f"action_log.buy_confidence={snapshot.buy_confidence:.3f} records confidence in '{alpha_act}', "
            "not BUY confidence (Alpha chose non-BUY action)."
        )
    elif alpha_act == "BUY":
        c_ok = snapshot.buy_confidence >= min_conf
        conf_status = "PASS" if c_ok else "FAIL"
        conf_avail = EXACT_HISTORICAL
        conf_exp = f"BUY action_confidence={snapshot.buy_confidence:.3f} {'>=' if c_ok else '<'} {min_conf:.2f}."
    else:
        # `BUY_OR_FAST_TRACK`: if `< min_conf`, even if action=='BUY' it provably fails standard confidence!
        if snapshot.buy_confidence < min_conf:
            conf_status = "FAIL"
            conf_avail = EXACT_HISTORICAL
            conf_exp = f"action_confidence={snapshot.buy_confidence:.3f} < {min_conf:.2f}."
        else:
            conf_status = NOT_REPLAYABLE
            conf_avail = INFERRED
            conf_exp = (
                f"action_confidence={snapshot.buy_confidence:.3f} >= {min_conf:.2f}, "
                "but exact BUY vs WATCH action choice is unverified (decisions_log pruned)."
            )
    gates.append(
        GateEvaluation(
            gate_id="G12_ALPHA_MIN_BUY_CONFIDENCE",
            stage="alpha_node.alpha_node (L214)",
            description="BUY action confidence >= MIN_BUY_CONFIDENCE",
            is_hard_entry_gate=True,
            status=conf_status,
            availability=conf_avail,
            observed_value=snapshot.buy_confidence,
            threshold_value=min_conf,
            threshold_provenance=profile.field_provenance("MIN_BUY_CONFIDENCE"),
            explanation=conf_exp,
        )
    )

    max_vamp = float(cfg.MAX_VAMP_ENTRY)
    if snapshot.vamp_score is None:
        vamp_status = NOT_REPLAYABLE
        vamp_avail = MISSING
        vamp_exp = "vamp_score is NULL in action_log."
    else:
        v_ok = snapshot.vamp_score <= max_vamp
        vamp_status = "PASS" if v_ok else "FAIL"
        vamp_avail = EXACT_HISTORICAL
        vamp_exp = f"Recorded vamp_score={snapshot.vamp_score:.3f} {'<=' if v_ok else '>'} {max_vamp:.2f}."
    gates.append(
        GateEvaluation(
            gate_id="G13_ALPHA_MAX_VAMP_ENTRY",
            stage="alpha_node.alpha_node (L215)",
            description="Copycat/vamp probability <= MAX_VAMP_ENTRY",
            is_hard_entry_gate=True,
            status=vamp_status,
            availability=vamp_avail,
            observed_value=snapshot.vamp_score,
            threshold_value=max_vamp,
            threshold_provenance=profile.field_provenance("MAX_VAMP_ENTRY"),
            explanation=vamp_exp,
        )
    )

    min_narr = float(cfg.MIN_NARRATIVE_SCORE)
    if snapshot.narrative_score is None:
        narr_status = NOT_REPLAYABLE
        narr_avail = MISSING
        narr_exp = "narrative_score is NULL in action_log."
    else:
        n_ok = snapshot.narrative_score >= min_narr
        narr_status = "PASS" if n_ok else "FAIL"
        narr_avail = EXACT_HISTORICAL
        narr_exp = f"Recorded narrative_score={snapshot.narrative_score:.2f} {'>=' if n_ok else '<'} {min_narr:.2f}."
    gates.append(
        GateEvaluation(
            gate_id="G14_ALPHA_MIN_NARRATIVE_SCORE",
            stage="alpha_node.alpha_node (L216)",
            description="Narrative strength >= MIN_NARRATIVE_SCORE",
            is_hard_entry_gate=True,
            status=narr_status,
            availability=narr_avail,
            observed_value=snapshot.narrative_score,
            threshold_value=min_narr,
            threshold_provenance=profile.field_provenance("MIN_NARRATIVE_SCORE"),
            explanation=narr_exp,
        )
    )

    min_mom = float(cfg.MIN_MOMENTUM_SCORE)
    if snapshot.momentum_score is None:
        mom_status = NOT_REPLAYABLE
        mom_avail = MISSING
        mom_exp = "momentum_score is NULL in action_log."
    else:
        m_ok = snapshot.momentum_score >= min_mom
        mom_status = "PASS" if m_ok else "FAIL"
        mom_avail = EXACT_HISTORICAL
        mom_exp = f"Recorded momentum_score={snapshot.momentum_score:.2f} {'>=' if m_ok else '<'} {min_mom:.2f}."
    gates.append(
        GateEvaluation(
            gate_id="G15_ALPHA_MIN_MOMENTUM_SCORE",
            stage="alpha_node.alpha_node (L217)",
            description="Momentum quality >= MIN_MOMENTUM_SCORE",
            is_hard_entry_gate=True,
            status=mom_status,
            availability=mom_avail,
            observed_value=snapshot.momentum_score,
            threshold_value=min_mom,
            threshold_provenance=profile.field_provenance("MIN_MOMENTUM_SCORE"),
            explanation=mom_exp,
        )
    )

    # 5. Reconstructed portfolio duplicate-mint & slot capacity gate (`trades <= decision_dt`)
    open_mints = set(snapshot.open_chart_mints_at_decision) | set(snapshot.open_whale_mints_at_decision)
    dup_open = snapshot.mint in open_mints
    slot_full = (
        snapshot.open_chart_count >= int(cfg.MAX_CHART_POSITIONS)
        or snapshot.open_total_count >= int(cfg.MAX_OPEN_POSITIONS)
    )
    port_ok = (not dup_open) and (not slot_full)
    gates.append(
        GateEvaluation(
            gate_id="G16_PORTFOLIO_SLOT_AND_DUPLICATE_MINT",
            stage="alpha_node (L66-69, L432-438) & executor_node (L151-161)",
            description="Mint not already open and Chart/global position slots available",
            is_hard_entry_gate=True,
            status="PASS" if port_ok else "FAIL",
            availability=RECONSTRUCTABLE,
            observed_value={
                "mint_already_open": dup_open,
                "open_chart_count": snapshot.open_chart_count,
                "open_total_count": snapshot.open_total_count,
            },
            threshold_value={
                "MAX_CHART_POSITIONS": int(cfg.MAX_CHART_POSITIONS),
                "MAX_OPEN_POSITIONS": int(cfg.MAX_OPEN_POSITIONS),
            },
            threshold_provenance=profile.field_provenance("MAX_CHART_POSITIONS"),
            explanation=(
                f"Reconstructed from trades <= decision_timestamp: open_chart={snapshot.open_chart_count}/"
                f"{cfg.MAX_CHART_POSITIONS}, total={snapshot.open_total_count}/{cfg.MAX_OPEN_POSITIONS}, "
                f"dup_mint={dup_open}."
            ),
        )
    )

    # 6. Watchlist Eviction Rules (evaluated when candidate is NOT approved and NOT blocked-wants-buy)
    eviction_gates: list[GateEvaluation] = []
    vamp_evict = float(cfg.VAMP_EVICT_THRESHOLD)
    if snapshot.vamp_score is None:
        ev_vamp_status = NOT_REPLAYABLE
        ev_vamp_exp = "vamp_score is NULL."
    elif snapshot.vamp_score >= vamp_evict:
        ev_vamp_status = "EVICT_TRIGGERED"
        ev_vamp_exp = f"Immediate HIGH_VAMP eviction: {snapshot.vamp_score:.3f} >= {vamp_evict:.2f}."
    elif (
        snapshot.vamp_score >= float(cfg.VAMP_WARN_THRESHOLD)
        and (snapshot.consecutive_prior_vamp_warn_strikes + 1) >= int(cfg.VAMP_CONSECUTIVE_STRIKES)
    ):
        ev_vamp_status = "EVICT_TRIGGERED"
        ev_vamp_exp = (
            f"CONSECUTIVE_VAMP eviction: {snapshot.vamp_score:.3f} >= {cfg.VAMP_WARN_THRESHOLD:.2f} "
            f"for {snapshot.consecutive_prior_vamp_warn_strikes + 1} consecutive ticks."
        )
    else:
        ev_vamp_status = "EVICT_NOT_TRIGGERED"
        ev_vamp_exp = f"vamp_score={snapshot.vamp_score:.3f} below eviction thresholds."
    eviction_gates.append(
        GateEvaluation(
            gate_id="E01_VAMP_EVICTION_RULE",
            stage="alpha_node.alpha_node (L363-380)",
            description="Immediate vamp >= VAMP_EVICT_THRESHOLD or consecutive >= VAMP_WARN_THRESHOLD",
            is_hard_entry_gate=False,
            status=ev_vamp_status,
            availability=EXACT_HISTORICAL if snapshot.vamp_score is not None else MISSING,
            observed_value=snapshot.vamp_score,
            threshold_value=vamp_evict,
            threshold_provenance=profile.field_provenance("VAMP_EVICT_THRESHOLD"),
            explanation=ev_vamp_exp,
        )
    )

    low_thresh = float(cfg.LOW_SCORE_EVICT_THRESHOLD)
    dist_1h: Optional[float] = None
    if snapshot.decisions_log_scores is not None and snapshot.decisions_log_scores.get("dist_from_1h_high_pct") is not None:
        dist_1h = float(snapshot.decisions_log_scores["dist_from_1h_high_pct"])

    if snapshot.narrative_score is None or snapshot.momentum_score is None:
        ev_low_status = NOT_REPLAYABLE
        ev_low_avail = MISSING
        ev_low_exp = "narrative_score or momentum_score is NULL."
    elif snapshot.narrative_score < low_thresh and snapshot.momentum_score < low_thresh:
        # Check coiling-base 2-strike grace (`liq >= 40_000 and vamp < 0.65 and dist_from_1h_high_pct <= 0.05`)
        can_be_coiling = (
            snapshot.liq_usd is not None
            and snapshot.liq_usd >= 40_000.0
            and snapshot.vamp_score is not None
            and snapshot.vamp_score < 0.65
        )
        new_strikes = snapshot.consecutive_prior_low_score_strikes + 1
        if not can_be_coiling or new_strikes >= 2:
            ev_low_status = "EVICT_TRIGGERED"
            ev_low_avail = EXACT_HISTORICAL
            ev_low_exp = (
                f"LOW_SCORE eviction triggered: narr={snapshot.narrative_score:.2f} < {low_thresh:.1f} "
                f"AND mom={snapshot.momentum_score:.2f} < {low_thresh:.1f} "
                f"(coiling_base_eligible=False or strikes={new_strikes} >= 2)."
            )
        elif dist_1h is not None:
            is_coiling = dist_1h <= 0.05
            ev_low_status = "EVICT_NOT_TRIGGERED" if is_coiling else "EVICT_TRIGGERED"
            ev_low_avail = EXACT_HISTORICAL
            ev_low_exp = (
                f"narr & mom < {low_thresh:.1f} on strike 1 with dist_from_1h_high_pct={dist_1h:.4f} "
                f"(is_coiling_base={is_coiling})."
            )
        else:
            ev_low_status = NOT_REPLAYABLE
            ev_low_avail = MISSING
            ev_low_exp = (
                f"narr & mom < {low_thresh:.1f} on strike 1 with liq=${snapshot.liq_usd:,.2f} >= $40k and "
                f"vamp={snapshot.vamp_score:.3f} < 0.65, but dist_from_1h_high_pct is MISSING (decisions_log pruned)."
            )
    else:
        ev_low_status = "EVICT_NOT_TRIGGERED"
        ev_low_avail = EXACT_HISTORICAL
        ev_low_exp = (
            f"Not both below {low_thresh:.1f} (narr={snapshot.narrative_score:.2f}, mom={snapshot.momentum_score:.2f})."
        )
    eviction_gates.append(
        GateEvaluation(
            gate_id="E02_LOW_SCORE_EVICTION_RULE",
            stage="alpha_node.alpha_node (L382-406)",
            description="Weak narrative & momentum (< LOW_SCORE_EVICT_THRESHOLD)",
            is_hard_entry_gate=False,
            status=ev_low_status,
            availability=ev_low_avail,
            observed_value={"narrative_score": snapshot.narrative_score, "momentum_score": snapshot.momentum_score},
            threshold_value=low_thresh,
            threshold_provenance=profile.field_provenance("LOW_SCORE_EVICT_THRESHOLD"),
            explanation=ev_low_exp,
        )
    )

    stale_base = int(cfg.STALE_WATCH_TIMEOUT_MINUTES)
    if snapshot.soak_minutes is None:
        ev_stale_status = NOT_REPLAYABLE
        ev_stale_avail = MISSING
        ev_stale_exp = "soak_minutes is NULL."
    elif snapshot.soak_minutes <= stale_base:
        ev_stale_status = "EVICT_NOT_TRIGGERED"
        ev_stale_avail = EXACT_HISTORICAL
        ev_stale_exp = f"soak_minutes={snapshot.soak_minutes:.2f}m <= {stale_base}m."
    elif snapshot.soak_minutes > max(25, stale_base):
        ev_stale_status = "EVICT_TRIGGERED"
        ev_stale_avail = EXACT_HISTORICAL
        ev_stale_exp = f"soak_minutes={snapshot.soak_minutes:.2f}m > {max(25, stale_base)}m (exceeds coiling cap)."
    else:
        # Between `stale_base` (15m) and 25m -> depends on `is_coiling_base`
        can_be_coiling = (
            snapshot.liq_usd is not None
            and snapshot.liq_usd >= 40_000.0
            and snapshot.vamp_score is not None
            and snapshot.vamp_score < 0.65
        )
        if not can_be_coiling:
            ev_stale_status = "EVICT_TRIGGERED"
            ev_stale_avail = EXACT_HISTORICAL
            ev_stale_exp = f"soak_minutes={snapshot.soak_minutes:.2f}m > {stale_base}m and coiling_base=False."
        elif dist_1h is not None:
            ev_stale_status = "EVICT_NOT_TRIGGERED" if dist_1h <= 0.05 else "EVICT_TRIGGERED"
            ev_stale_avail = EXACT_HISTORICAL
            ev_stale_exp = f"soak_minutes={snapshot.soak_minutes:.2f}m in (15m, 25m] with dist_1h={dist_1h:.4f}."
        else:
            ev_stale_status = NOT_REPLAYABLE
            ev_stale_avail = MISSING
            ev_stale_exp = "soak_minutes in (15m, 25m] and dist_from_1h_high_pct is MISSING."
    eviction_gates.append(
        GateEvaluation(
            gate_id="E03_STALE_WATCH_EVICTION_RULE",
            stage="alpha_node.alpha_node (L410-424)",
            description="Watchlist elapsed time > STALE_WATCH_TIMEOUT_MINUTES (or 25m if coiling base)",
            is_hard_entry_gate=False,
            status=ev_stale_status,
            availability=ev_stale_avail,
            observed_value=snapshot.soak_minutes,
            threshold_value=stale_base,
            threshold_provenance=profile.field_provenance("STALE_WATCH_TIMEOUT_MINUTES"),
            explanation=ev_stale_exp,
        )
    )

    hard_gates = [g for g in gates if g.is_hard_entry_gate]
    failed_hard = tuple(g.gate_id for g in hard_gates if g.status == "FAIL")
    passed_hard = tuple(g.gate_id for g in hard_gates if g.status == "PASS")
    nr_all = tuple(g.gate_id for g in list(gates) + eviction_gates if g.status == NOT_REPLAYABLE)
    replayable_hard_cnt = sum(1 for g in hard_gates if g.status in ("PASS", "FAIL"))
    nr_hard_cnt = sum(1 for g in hard_gates if g.status == NOT_REPLAYABLE)

    if failed_hard:
        overall_verdict = "DETERMINISTICALLY_REJECTED_OR_BLOCKED"
    else:
        # Even if all replayable hard gates pass, raw candle/txn gates remain NOT_REPLAYABLE
        overall_verdict = "NOT_REPLAYABLE_FOR_FULL_APPROVAL"

    triggered_evicts = [e.gate_id for e in eviction_gates if e.status == "EVICT_TRIGGERED"]
    nr_evicts = [e.gate_id for e in eviction_gates if e.status == NOT_REPLAYABLE]
    dec_class, _, _ = _classify_recorded_outcome(snapshot.recorded_reason, snapshot.decisions_log_action)

    # Note: in `alpha_node.py:352`, if `decision1_wants_buy and not scalper.eligible`, eviction rules are skipped!
    wants_buy_std = (
        conf_status == "PASS"
        and vamp_status == "PASS"
        and narr_status == "PASS"
        and mom_status == "PASS"
        and act_status == "PASS"
    )
    if overall_verdict != "DETERMINISTICALLY_REJECTED_OR_BLOCKED" or wants_buy_std:
        evict_verdict = "SKIPPED_APPROVED_OR_BLOCKED_BUY_INTENT"
    elif triggered_evicts:
        evict_verdict = f"DETERMINISTIC_EVICT ({', '.join(triggered_evicts)})"
    elif nr_evicts:
        evict_verdict = NOT_REPLAYABLE
    else:
        evict_verdict = "RETAINED_ON_WATCHLIST"

    # Compare deterministic replay against recorded outcome
    if dec_class == "APPROVED":
        if failed_hard:
            consistent: Optional[bool] = False
            cons_exp = (
                f"MISMATCH: Recorded APPROVED, but profile '{profile.name}' failed hard gate(s): "
                f"{', '.join(failed_hard)}."
            )
        else:
            consistent = True
            cons_exp = (
                f"CONSISTENT (partial): All {len(passed_hard)} replayable hard gates PASSED under "
                f"'{profile.name}'; {nr_hard_cnt} candle/txn gate(s) remain NOT_REPLAYABLE."
            )
    elif dec_class in ("REJECT", "WATCH", "BLOCKED"):
        if failed_hard:
            consistent = True
            cons_exp = (
                f"CONSISTENT: Recorded {dec_class} aligns with {len(failed_hard)} failed hard gate(s) "
                f"({', '.join(failed_hard)}) under '{profile.name}'."
            )
        else:
            # Recorded non-approval, and 0 replayable scalar gates failed -> rejection came from a NOT_REPLAYABLE candle/txn gate!
            consistent = True
            cons_exp = (
                f"CONSISTENT (non-replayable blocker): Recorded {dec_class}; all {len(passed_hard)} "
                f"scalar gates passed, indicating rejection occurred in one of the {nr_hard_cnt} "
                "NOT_REPLAYABLE candle/txn gates."
            )
    else:
        consistent = None
        cons_exp = "Recorded decision class could not be classified."

    return DeterministicGateReplayResult(
        replay_mode=MODE_DETERMINISTIC_GATE_REPLAY,
        strategy_scope=SUPPORTED_STRATEGY,
        runtime_code_provenance=RUNTIME_CODE_PROVENANCE,
        config_profile_name=profile.name,
        config_profile_provenance=profile.profile_provenance,
        source_table="action_log",
        source_action_log_id=snapshot.action_log_id,
        episode_id=snapshot.episode_id,
        mint=snapshot.mint,
        symbol=snapshot.symbol,
        decision_timestamp=snapshot.decision_timestamp,
        gates=tuple(gates),
        eviction_gates=tuple(eviction_gates),
        replayable_hard_gate_count=replayable_hard_cnt,
        non_replayable_hard_gate_count=nr_hard_cnt,
        failed_hard_gate_ids=failed_hard,
        passed_hard_gate_ids=passed_hard,
        not_replayable_gate_ids=nr_all,
        overall_entry_verdict=overall_verdict,
        eviction_replay_verdict=evict_verdict,
        consistent_with_recorded_outcome=consistent,
        consistency_explanation=cons_exp,
    )
