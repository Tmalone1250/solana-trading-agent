"""Pure Decision 1 Strategist (alpha_node.py): Zero generative LLM calls."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from config import LAMPORTS_PER_SOL, AgentConfig
from db import AgentDatabase, utc_now
from decision_client import GeminiDecisionClient
from leg2_predictor import Leg2Result, compute_leg2_score
from ohlcv_client import fetch_ohlcv_batch
from scalper_entry_engine import ScalperEntryResult, evaluate_scalper_entry
from state import (
    AgentGraphState,
    AlphaDecision,
    FocusState,
    PendingOrder,
    ReentryDecision,
)

logger = logging.getLogger(__name__)

NARRATIVE_RUBRIC = [
    "0: No discernible meme, dead or gibberish branding.",
    "1: Weak or stale derivative with negligible cultural hook.",
    "2: Generic memecoin theme with mediocre differentiation.",
    "3: Solid relatable meme/narrative with modest viral potential.",
    "4: Strong, timely cultural narrative with high organic attention.",
    "5: Breakout flagship narrative with viral social momentum.",
]

MOMENTUM_RUBRIC = [
    "0: Collapsing volume and heavy sell pressure.",
    "1: Weak, choppy tape with stalling buyers.",
    "2: Flat or inconsistent momentum lacking follow-through.",
    "3: Constructive uptrend with healthy buyer participation.",
    "4: Strong accelerating breakout with sustained buy-side flow.",
    "5: Explosive high-conviction momentum runner with dominant buy flow.",
]

LEG2_MOMENTUM_RUBRIC = [
    "0: Complete breakdown with no buyer defense.",
    "1: Weak bounce on fading volume.",
    "2: Tentative stabilization without clear buyer absorption.",
    "3: Developing higher low with improving 5m buy ratio.",
    "4: Strong curl off retrace low with decisive buyer reclaim.",
    "5: High-velocity Leg 2 breakout reclaiming trend with heavy buy flow.",
]


def _resolve_leg2(cand: dict[str, Any]) -> Leg2Result:
    """Return existing Leg2Result from candidate breakdown or compute on the fly."""
    bd = cand.get("leg2_breakdown")
    if isinstance(bd, dict) and "score" in bd:
        return Leg2Result.model_validate(bd)
    if cand.get("leg2_score") is not None and not any(
        k in cand for k in ("buys_5m", "txns", "volume_1h_usd", "volume")
    ):
        score = float(cand["leg2_score"])
        return Leg2Result(
            score=score,
            buy_absorption=0.0,
            volume_acceleration=0.0,
            bounce_structure=0.0,
            liquidity_stability=0.0,
            summary=f"leg2={score:.1f}/100",
        )
    return compute_leg2_score(
        cand,
        tracked_peak_mc=float(cand.get("peak_mc") or cand.get("market_cap_usd") or 0.0),
        tracked_low_mc=float(cand.get("low_mc") or cand.get("market_cap_usd") or 0.0),
        entry_liquidity_usd=float(cand.get("entry_liquidity_usd") or cand.get("liquidity_usd") or 0.0) or None,
    )


def _build_batch_entry_prompt(
    candidates: list[dict[str, Any]],
    leg2_results: list[Leg2Result],
    scalper_results: list[ScalperEntryResult],
) -> str:
    lines = ["Evaluate the following Solana memecoin candidates that passed safety checks:"]
    for idx, (c, leg2, sc) in enumerate(zip(candidates, leg2_results, scalper_results)):
        buys_5m = int(c.get("buys_5m") or 0)
        sells_5m = int(c.get("sells_5m") or 0)
        total_5m = buys_5m + sells_5m
        buy_ratio_5m = (buys_5m / total_5m) if total_5m > 0 else 0.0
        lines.append(
            f"[TOKEN_{idx}] symbol={c.get('symbol')} name={c.get('name')} mint={c.get('mint')} | "
            f"mc=${float(c.get('market_cap_usd') or 0):,.0f} | "
            f"liq=${float(c.get('liquidity_usd') or 0):,.0f} | "
            f"vol_1h=${float(c.get('volume_1h_usd') or 0):,.0f} | "
            f"vol_5m=${float(c.get('volume_5m_usd') or 0):,.0f} | "
            f"5m_change={float(c.get('price_change_5m_pct') or 0):+.1f}% | "
            f"1h_change={float(c.get('price_change_1h_pct') or 0):+.1f}% | "
            f"5m_buys={buys_5m} 5m_sells={sells_5m} (buy_ratio={buy_ratio_5m:.2f}) | "
            f"age_min={float(c.get('age_seconds') or 0) / 60.0:.1f} | "
            f"boost={float(c.get('boost_amount') or 0):.0f} | "
            f"leg2_predictor={leg2.summary} | "
            f"scalper_setup={sc.setup_type} rsi_5m={sc.rsi_5m:.1f} "
            f"dist_from_1h_high={sc.dist_from_1h_high_pct * 100:.1f}% "
            f"scalper_eligible={sc.eligible}"
        )
    return "\n".join(lines)


def _build_batch_entry_questions(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    questions: dict[str, dict[str, Any]] = {}
    for idx, c in enumerate(candidates):
        tag = f"TOKEN_{idx} ({c.get('symbol')})"
        prefix = f"t{idx}"
        questions[f"{prefix}_narrative_strength"] = {
            "type": "score",
            "instructions": f"Rate the narrative strength of {tag} from 0 to 5.",
            "criteria": NARRATIVE_RUBRIC,
        }
        questions[f"{prefix}_momentum_quality"] = {
            "type": "score",
            "instructions": f"Rate the momentum quality of {tag} from 0 to 5.",
            "criteria": MOMENTUM_RUBRIC,
        }
        questions[f"{prefix}_is_copycat_vamp"] = {
            "type": "noul",
            "instructions": (
                f"Is {tag} a low-effort copycat or vampire derivative trying to mimic "
                "a larger existing token?"
            ),
        }
        questions[f"{prefix}_action"] = {
            "type": "choice",
            "instructions": f"Select the trading action for {tag}.",
            "criteria": {
                "BUY": "High-conviction momentum & narrative setup ready for immediate entry.",
                "WATCH": "Interesting setup but needs stronger confirmation before entering.",
                "REJECT": "Unattractive, risky, or copycat token to skip.",
            },
        }
    return questions


def format_entry_rationale(decision: AlphaDecision) -> str:
    """Deterministic Python rationale formatter — zero generative LLM calls."""
    verdict = "APPROVED -> ENTER IN_POSITION" if decision.approved else "SKIPPED"
    return (
        f"[DECISION-1 ENTRY] {decision.symbol} ({decision.mint[:6]}..): "
        f"action={decision.action} (conf={decision.action_confidence:.2f}) | "
        f"narrative={decision.narrative_strength:.2f}/5.0 | "
        f"momentum={decision.momentum_quality:.2f}/5.0 | "
        f"copycat_vamp={decision.is_copycat_vamp:.2f} | {verdict}"
    )


def format_reentry_rationale(decision: ReentryDecision) -> str:
    """Deterministic Python rationale formatter for Leg 2 re-entry."""
    verdict = "APPROVED -> RE-ENTER IN_POSITION" if decision.approved else "HOLD WATCHING_REENTRY"
    return (
        f"[DECISION-1 LEG-2] {decision.symbol} ({decision.mint[:6]}..): "
        f"leg_2_momentum={decision.leg_2_momentum:.2f}/5.0 | "
        f"dead_cat_bounce={decision.is_dead_cat_bounce:.2f} | {verdict}"
    )


async def evaluate_leg2_reentry(
    *,
    mint: str,
    symbol: str,
    curl_pct: float,
    buy_ratio_5m: float,
    retrace_from_peak_pct: float,
    current_sol_value: float,
    liquidity_usd: float,
    config: AgentConfig,
    decision_client: GeminiDecisionClient,
    db: AgentDatabase,
) -> ReentryDecision:
    """Single gemini-decision-1 call confirming Leg 2 re-entry quality."""
    input_text = (
        f"Evaluating Leg 2 re-entry for Solana memecoin {symbol} ({mint}): "
        f"curl_off_retrace_low={curl_pct * 100:.2f}% | "
        f"5m_buy_ratio={buy_ratio_5m * 100:.1f}% | "
        f"drawdown_from_run_peak={retrace_from_peak_pct * 100:.2f}% | "
        f"quoted_sol_value={current_sol_value:.4f} SOL | "
        f"liquidity=${liquidity_usd:,.0f}."
    )
    questions = {
        "leg_2_momentum": {
            "type": "score",
            "instructions": f"Rate Leg 2 breakout momentum quality for {symbol} from 0 to 5.",
            "criteria": LEG2_MOMENTUM_RUBRIC,
        },
        "is_dead_cat_bounce": {
            "type": "noul",
            "instructions": f"Is this bounce on {symbol} merely a weak dead-cat bounce likely to fail?",
        },
    }
    answers = await decision_client.decide(input_text=input_text, questions=questions)

    leg2_score = float((answers.get("leg_2_momentum") or {}).get("score", 0.0))
    dead_cat = float((answers.get("is_dead_cat_bounce") or {}).get("noul", 1.0))
    approved = (
        leg2_score >= config.REENTRY_MIN_LEG2_SCORE
        and dead_cat <= config.REENTRY_MAX_DEAD_CAT
    )

    decision = ReentryDecision(
        mint=mint,
        symbol=symbol,
        leg_2_momentum=leg2_score,
        is_dead_cat_bounce=dead_cat,
        approved=approved,
        rationale="",
    )
    decision.rationale = format_reentry_rationale(decision)

    db.log_decision(
        mint=mint,
        symbol=symbol,
        phase="REENTRY",
        action="REBUY" if approved else "HOLD",
        confidence=float((answers.get("leg_2_momentum") or {}).get("confidence", 0.0)),
        scores={
            "leg_2_momentum": leg2_score,
            "is_dead_cat_bounce": dead_cat,
            "curl_pct": curl_pct,
            "buy_ratio_5m": buy_ratio_5m,
        },
        rationale=decision.rationale,
    )
    return decision


async def alpha_node(
    state: AgentGraphState,
    *,
    config: AgentConfig,
    db: AgentDatabase,
    decision_client: GeminiDecisionClient,
    http_client: Optional[httpx.AsyncClient] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """LangGraph node: single batched gemini-decision-1 call gated by Scalper Entry Engine."""
    if state.get("focus_state") != FocusState.SCANNING.value:
        return {"alpha_decisions": [], "pending_order": None}

    safe_candidates = state.get("safe_candidates") or []
    if not safe_candidates:
        return {"alpha_decisions": [], "pending_order": None}

    sol_balance = float(state.get("sol_balance") or 0.0)
    if sol_balance < config.TRADE_SIZE_SOL:
        rationale = (
            f"[RISK GUARD] Insufficient SOL balance ({sol_balance:.4f} < "
            f"{config.TRADE_SIZE_SOL:.4f} SOL) to open position."
        )
        return {
            "alpha_decisions": [],
            "pending_order": None,
            "last_decision_rationale": rationale,
        }

    now_dt = now or utc_now()
    now_iso = now_dt.isoformat()
    watchlist_by_mint = {w["mint"]: w for w in db.get_watchlist()}
    leg2_results = [_resolve_leg2(cand) for cand in safe_candidates]

    # Fetch 5m OHLCV candles for candidates lacking pre-attached candles_5m
    pairs_to_fetch: list[str] = []
    for cand in safe_candidates:
        if cand.get("candles_5m") is None:
            p_addr = str(
                cand.get("pair_address")
                or (watchlist_by_mint.get(cand["mint"]) or {}).get("pair_address")
                or ""
            )
            if p_addr:
                pairs_to_fetch.append(p_addr)

    ohlcv_by_pair: dict[str, list[dict[str, float]]] = {}
    if pairs_to_fetch:
        owns_client = http_client is None
        client = http_client or httpx.AsyncClient()
        try:
            ohlcv_by_pair = await fetch_ohlcv_batch(
                pairs_to_fetch, config, client, now_ts=now_dt.timestamp()
            )
        finally:
            if owns_client:
                await client.aclose()

    scalper_results: list[ScalperEntryResult] = []
    for cand in safe_candidates:
        added_at_str = cand.get("added_at") or (watchlist_by_mint.get(cand["mint"]) or {}).get("added_at")
        if added_at_str:
            added_dt = datetime.fromisoformat(added_at_str)
            if added_dt.tzinfo is None:
                added_dt = added_dt.replace(tzinfo=timezone.utc)
            time_in_slot_min = max(0.0, (now_dt - added_dt).total_seconds() / 60.0)
        else:
            time_in_slot_min = 0.0

        p_addr = str(
            cand.get("pair_address")
            or (watchlist_by_mint.get(cand["mint"]) or {}).get("pair_address")
            or ""
        )
        candles_5m = cand.get("candles_5m")
        if candles_5m is None:
            candles_5m = ohlcv_by_pair.get(p_addr, [])

        scalper_results.append(
            evaluate_scalper_entry(
                candles_5m,
                cand,
                time_in_slot_min,
                config=config,
            )
        )

    input_text = _build_batch_entry_prompt(safe_candidates, leg2_results, scalper_results)
    questions = _build_batch_entry_questions(safe_candidates)
    answers = await decision_client.decide(input_text=input_text, questions=questions)

    decisions: list[AlphaDecision] = []
    approved_pairs: list[tuple[AlphaDecision, dict[str, Any], bool, float, str]] = []

    for idx, (cand, leg2, scalper) in enumerate(zip(safe_candidates, leg2_results, scalper_results)):
        prefix = f"t{idx}"
        narr = float((answers.get(f"{prefix}_narrative_strength") or {}).get("score", 0.0))
        mom = float((answers.get(f"{prefix}_momentum_quality") or {}).get("score", 0.0))
        vamp = float((answers.get(f"{prefix}_is_copycat_vamp") or {}).get("noul", 1.0))
        act_ans = answers.get(f"{prefix}_action") or {}
        action = str(act_ans.get("choice", "REJECT")).upper()
        conf = float(act_ans.get("confidence", 0.0))
        leg2_score = leg2.score

        standard_approved = (
            action == "BUY"
            and conf >= config.MIN_BUY_CONFIDENCE
            and vamp <= config.MAX_VAMP_ENTRY
            and narr >= config.MIN_NARRATIVE_SCORE
            and mom >= config.MIN_MOMENTUM_SCORE
        )
        leg2_fast_track = (
            leg2_score >= config.LEG2_FAST_TRACK_MIN_SCORE
            and vamp <= config.MAX_VAMP_ENTRY
            and mom >= config.LEG2_FAST_TRACK_MIN_MOM
            and (
                (action == "BUY" and conf >= config.LEG2_FAST_TRACK_MIN_CONF)
                or (action == "WATCH" and leg2_score >= config.LEG2_FAST_TRACK_WATCH_SCORE)
            )
        )
        decision1_wants_buy = standard_approved or leg2_fast_track
        approved = decision1_wants_buy and scalper.eligible

        dec = AlphaDecision(
            mint=cand["mint"],
            symbol=cand.get("symbol", "UNKNOWN"),
            narrative_strength=narr,
            momentum_quality=mom,
            is_copycat_vamp=vamp,
            action=action,
            action_confidence=conf,
            approved=approved,
            rationale="",
        )
        base_rationale = format_entry_rationale(dec)
        if approved and leg2_fast_track and not standard_approved:
            dec.rationale = (
                f"{base_rationale} [LEG2_ENTRY leg2={leg2_score:.0f}/100 "
                f"setup={scalper.setup_type} rsi={scalper.rsi_5m:.1f}]"
            )
        elif decision1_wants_buy and not scalper.eligible:
            dec.rationale = (
                f"{base_rationale} [BLOCKED: {scalper.rejection_reason}]"
            )
        else:
            dec.rationale = (
                f"{base_rationale} [leg2={leg2_score:.0f}/100 "
                f"setup={scalper.setup_type} rsi={scalper.rsi_5m:.1f}]"
            )
        decisions.append(dec)

        scores_payload = {
            "narrative_strength": dec.narrative_strength,
            "momentum_quality": dec.momentum_quality,
            "is_copycat_vamp": dec.is_copycat_vamp,
            "action": dec.action,
            "action_confidence": dec.action_confidence,
            "leg2_score": leg2_score,
            "leg2_fast_track": leg2_fast_track,
            "rsi_5m": scalper.rsi_5m,
            "setup_type": scalper.setup_type,
            "scalper_eligible": scalper.eligible,
            "dist_from_1h_high_pct": scalper.dist_from_1h_high_pct,
            "approved": dec.approved,
        }

        db.log_decision(
            mint=dec.mint,
            symbol=dec.symbol,
            phase="ENTRY",
            action=dec.action,
            confidence=dec.action_confidence,
            scores=scores_payload,
            rationale=dec.rationale,
        )

        if approved:
            if leg2_fast_track and not standard_approved:
                logger.info(
                    "[Alpha] $%s | LEG2_ENTRY (leg2=%.0f/100, setup=%s, RSI=%.1f) | action=%s (%.2f) | narr=%.1f mom=%.1f vamp=%.2f -> APPROVED (ENTER IN_POSITION)",
                    dec.symbol,
                    leg2_score,
                    scalper.setup_type,
                    scalper.rsi_5m,
                    dec.action,
                    dec.action_confidence,
                    dec.narrative_strength,
                    dec.momentum_quality,
                    dec.is_copycat_vamp,
                )
            else:
                logger.info(
                    "[Alpha] $%s | action=%s (%.2f) | narr=%.1f mom=%.1f vamp=%.2f leg2=%.0f/100 setup=%s RSI=%.1f -> APPROVED (ENTER IN_POSITION)",
                    dec.symbol,
                    dec.action,
                    dec.action_confidence,
                    dec.narrative_strength,
                    dec.momentum_quality,
                    dec.is_copycat_vamp,
                    leg2_score,
                    scalper.setup_type,
                    scalper.rsi_5m,
                )
            db.update_watchlist_scores(
                mint=dec.mint,
                scores=scores_payload,
                last_evaluated_at=now_iso,
                vamp_strikes=0,
                rsi_5m=scalper.rsi_5m,
                setup_type=scalper.setup_type,
                scalper_eligible=scalper.eligible,
                dist_from_1h_high_pct=scalper.dist_from_1h_high_pct,
            )
            approved_pairs.append(
                (dec, cand, leg2_fast_track and not standard_approved, leg2_score, scalper.setup_type)
            )
            continue

        # Blocked by Scalper Expert Entry Engine (retain in watchlist)
        if decision1_wants_buy and not scalper.eligible:
            db.update_watchlist_scores(
                mint=dec.mint,
                scores=scores_payload,
                last_evaluated_at=now_iso,
                vamp_strikes=0,
                rsi_5m=scalper.rsi_5m,
                setup_type=scalper.setup_type,
                scalper_eligible=False,
                dist_from_1h_high_pct=scalper.dist_from_1h_high_pct,
            )
            logger.info(
                "[Alpha] $%s | BLOCKED (%s) | action=%s (%.2f) | narr=%.1f mom=%.1f vamp=%.2f leg2=%.0f/100",
                dec.symbol,
                scalper.rejection_reason,
                dec.action,
                dec.action_confidence,
                dec.narrative_strength,
                dec.momentum_quality,
                dec.is_copycat_vamp,
                leg2_score,
            )
            continue

        # Eviction Rule 1: High Vamp Score (jitter-resistant)
        # Immediate eviction at >= 0.85, OR 2 consecutive cycles at >= 0.75 -> 20m cooldown
        prev_vamp_strikes = int((watchlist_by_mint.get(dec.mint) or {}).get("vamp_strikes") or 0)
        if dec.is_copycat_vamp >= config.VAMP_EVICT_THRESHOLD:
            evict_reason = f"HIGH_VAMP ({dec.is_copycat_vamp:.2f} >= {config.VAMP_EVICT_THRESHOLD:.2f})"
            db.remove_from_watchlist(dec.mint)
            db.set_cooldown_minutes(
                dec.mint,
                minutes=config.VAMP_DROP_COOLDOWN_MINUTES,
                reason=evict_reason,
                now=now_dt,
            )
            db.log_action(
                mode=config.EXECUTION_MODE,
                action="EVICT",
                symbol=dec.symbol,
                asset_id=dec.mint,
                strategy=scalper.setup_type,
                reason=evict_reason,
                timestamp_utc=now_iso,
            )
            logger.info(
                "[Alpha] $%s | action=%s (%.2f) | narr=%.1f mom=%.1f vamp=%.2f leg2=%.0f/100 -> EVICTED (vamp >= %.2f, %dm cooldown)",
                dec.symbol,
                dec.action,
                dec.action_confidence,
                dec.narrative_strength,
                dec.momentum_quality,
                dec.is_copycat_vamp,
                leg2_score,
                config.VAMP_EVICT_THRESHOLD,
                config.VAMP_DROP_COOLDOWN_MINUTES,
            )
            continue

        new_vamp_strikes = 0
        if dec.is_copycat_vamp >= config.VAMP_WARN_THRESHOLD:
            new_vamp_strikes = prev_vamp_strikes + 1
            if new_vamp_strikes >= config.VAMP_CONSECUTIVE_STRIKES:
                evict_reason = (
                    f"CONSECUTIVE_VAMP ({dec.is_copycat_vamp:.2f} >= {config.VAMP_WARN_THRESHOLD:.2f}, "
                    f"{new_vamp_strikes}/{config.VAMP_CONSECUTIVE_STRIKES} strikes)"
                )
                db.remove_from_watchlist(dec.mint)
                db.set_cooldown_minutes(
                    dec.mint,
                    minutes=config.VAMP_DROP_COOLDOWN_MINUTES,
                    reason=evict_reason,
                    now=now_dt,
                )
                db.log_action(
                    mode=config.EXECUTION_MODE,
                    action="EVICT",
                    symbol=dec.symbol,
                    asset_id=dec.mint,
                    strategy=scalper.setup_type,
                    reason=evict_reason,
                    timestamp_utc=now_iso,
                )
                logger.info(
                    "[Alpha] $%s | action=%s (%.2f) | narr=%.1f mom=%.1f vamp=%.2f leg2=%.0f/100 -> EVICTED (vamp >= %.2f x%d strikes, %dm cooldown)",
                    dec.symbol,
                    dec.action,
                    dec.action_confidence,
                    dec.narrative_strength,
                    dec.momentum_quality,
                    dec.is_copycat_vamp,
                    leg2_score,
                    config.VAMP_WARN_THRESHOLD,
                    new_vamp_strikes,
                    config.VAMP_DROP_COOLDOWN_MINUTES,
                )
                continue

        # Eviction Rule 2: Weak Narrative & Momentum (< 2.0 AND < 2.0 -> 15m cooldown)
        if (
            dec.narrative_strength < config.LOW_SCORE_EVICT_THRESHOLD
            and dec.momentum_quality < config.LOW_SCORE_EVICT_THRESHOLD
        ):
            evict_reason = (
                f"LOW_SCORE (narr={dec.narrative_strength:.2f}, "
                f"mom={dec.momentum_quality:.2f} < {config.LOW_SCORE_EVICT_THRESHOLD:.1f})"
            )
            db.remove_from_watchlist(dec.mint)
            db.set_cooldown_minutes(
                dec.mint,
                minutes=config.LOW_SCORE_COOLDOWN_MINUTES,
                reason=evict_reason,
                now=now_dt,
            )
            db.log_action(
                mode=config.EXECUTION_MODE,
                action="EVICT",
                symbol=dec.symbol,
                asset_id=dec.mint,
                strategy=scalper.setup_type,
                reason=evict_reason,
                timestamp_utc=now_iso,
            )
            logger.info(
                "[Alpha] $%s | action=%s (%.2f) | narr=%.1f mom=%.1f vamp=%.2f leg2=%.0f/100 -> EVICTED (narr < %.1f & mom < %.1f, %dm cooldown)",
                dec.symbol,
                dec.action,
                dec.action_confidence,
                dec.narrative_strength,
                dec.momentum_quality,
                dec.is_copycat_vamp,
                leg2_score,
                config.LOW_SCORE_EVICT_THRESHOLD,
                config.LOW_SCORE_EVICT_THRESHOLD,
                config.LOW_SCORE_COOLDOWN_MINUTES,
            )
            continue

        # Eviction Rule 3: Stale Watch Timeout (> 15m in slot without BUY -> 10m cooldown)
        added_at_str = cand.get("added_at") or (watchlist_by_mint.get(dec.mint) or {}).get("added_at")
        if added_at_str:
            added_dt = datetime.fromisoformat(added_at_str)
            if added_dt.tzinfo is None:
                added_dt = added_dt.replace(tzinfo=timezone.utc)
            elapsed_min = (now_dt - added_dt).total_seconds() / 60.0
            if elapsed_min > config.STALE_WATCH_TIMEOUT_MINUTES:
                evict_reason = f"STALE_WATCH ({elapsed_min:.1f}m > {config.STALE_WATCH_TIMEOUT_MINUTES}m)"
                db.remove_from_watchlist(dec.mint)
                db.set_cooldown_minutes(
                    dec.mint,
                    minutes=config.STALE_WATCH_COOLDOWN_MINUTES,
                    reason=evict_reason,
                    now=now_dt,
                )
                db.log_action(
                    mode=config.EXECUTION_MODE,
                    action="EVICT",
                    symbol=dec.symbol,
                    asset_id=dec.mint,
                    strategy=scalper.setup_type,
                    reason=evict_reason,
                    timestamp_utc=now_iso,
                )
                logger.info(
                    "[Alpha] $%s | action=%s (%.2f) | narr=%.1f mom=%.1f vamp=%.2f leg2=%.0f/100 -> EVICTED (stale %.1fm > %dm, %dm cooldown)",
                    dec.symbol,
                    dec.action,
                    dec.action_confidence,
                    dec.narrative_strength,
                    dec.momentum_quality,
                    dec.is_copycat_vamp,
                    leg2_score,
                    elapsed_min,
                    config.STALE_WATCH_TIMEOUT_MINUTES,
                    config.STALE_WATCH_COOLDOWN_MINUTES,
                )
                continue

        # Retained in rotating watchlist with updated latest scores, scalper metrics & vamp strike count
        db.update_watchlist_scores(
            mint=dec.mint,
            scores=scores_payload,
            last_evaluated_at=now_iso,
            vamp_strikes=new_vamp_strikes,
            rsi_5m=scalper.rsi_5m,
            setup_type=scalper.setup_type,
            scalper_eligible=scalper.eligible,
            dist_from_1h_high_pct=scalper.dist_from_1h_high_pct,
        )
        strike_note = (
            f" (vamp strike {new_vamp_strikes}/{config.VAMP_CONSECUTIVE_STRIKES})"
            if new_vamp_strikes > 0
            else ""
        )
        logger.info(
            "[Alpha] $%s | action=%s (%.2f) | narr=%.1f mom=%.1f vamp=%.2f leg2=%.0f/100 setup=%s RSI=%.1f -> RETAINED in watchlist%s",
            dec.symbol,
            dec.action,
            dec.action_confidence,
            dec.narrative_strength,
            dec.momentum_quality,
            dec.is_copycat_vamp,
            leg2_score,
            scalper.setup_type,
            scalper.rsi_5m,
            strike_note,
        )

    if not approved_pairs:
        latest_rationale = decisions[0].rationale if decisions else ""
        return {
            "alpha_decisions": [d.model_dump() for d in decisions],
            "active_watchlist": db.get_watchlist(),
            "pending_order": None,
            "last_decision_rationale": latest_rationale,
        }

    # Focus on ONE best coin at a time: rank by combined score, leg2_score, & confidence
    approved_pairs.sort(
        key=lambda item: (
            item[0].narrative_strength + item[0].momentum_quality + (item[3] / 25.0),
            item[0].action_confidence,
            -item[0].is_copycat_vamp,
        ),
        reverse=True,
    )
    winner, winner_cand, is_leg2_only, _, winner_setup = approved_pairs[0]

    order = PendingOrder(
        side="BUY",
        mint=winner.mint,
        symbol=winner.symbol,
        amount_atomic=int(round(config.TRADE_SIZE_SOL * LAMPORTS_PER_SOL)),
        reason="LEG2_ENTRY" if is_leg2_only else "DECISION_1_ENTRY",
        target_state_after_fill=FocusState.IN_POSITION,
        apply_cooldown=False,
        entry_liquidity_usd=float(winner_cand.get("liquidity_usd") or 0.0),
        strategy="LEG2_ENTRY" if is_leg2_only else winner_setup,
        rationale=winner.rationale,
    )
    return {
        "alpha_decisions": [d.model_dump() for d in decisions],
        "active_watchlist": db.get_watchlist(),
        "pending_order": order.model_dump(),
        "last_decision_rationale": winner.rationale,
    }
