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
from ohlcv_client import fetch_ohlcv_1m_batch, fetch_ohlcv_batch
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
    social_by_mint: Optional[dict[str, dict[str, Any]]] = None,
    whale_by_mint: Optional[dict[str, dict[str, Any]]] = None,
) -> str:
    lines = ["Evaluate the following Solana memecoin candidates that passed safety checks:"]
    social_map = social_by_mint or {}
    whale_map = whale_by_mint or {}
    for idx, (c, leg2, sc) in enumerate(zip(candidates, leg2_results, scalper_results)):
        buys_5m = int(c.get("buys_5m") or 0)
        sells_5m = int(c.get("sells_5m") or 0)
        total_5m = buys_5m + sells_5m
        buy_ratio_5m = (buys_5m / total_5m) if total_5m > 0 else 0.0
        line = (
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
        j_meta = c.get("jup_meta") if isinstance(c.get("jup_meta"), dict) else {}
        jup_org = c.get("jup_organic_score")
        if jup_org is None:
            jup_org = j_meta.get("organic_score")
        if jup_org is not None:
            jup_ver = c.get("jup_is_verified") if c.get("jup_is_verified") is not None else j_meta.get("is_verified", False)
            jup_holders = c.get("jup_holder_count") if c.get("jup_holder_count") is not None else j_meta.get("holder_count", 0)
            jup_top = c.get("jup_top_holders_pct") if c.get("jup_top_holders_pct") is not None else j_meta.get("top_holders_pct")
            top_str = f"{float(jup_top):.1f}%" if jup_top is not None else "n/a"
            line += (
                f" | jup_organic_score={float(jup_org):.1f}/100"
                f" jup_is_verified={bool(jup_ver)}"
                f" jup_holder_count={int(jup_holders or 0)}"
                f" jup_top_holders_pct={top_str}"
            )
        sig = social_map.get(c.get("mint", ""))
        if sig:
            snippets = sig.get("top_snippets") or []
            formatted_snips = " || ".join(
                f"@{s.get('author')} ({s.get('followers', 0)}f, {s.get('likes', 0)}L): {str(s.get('text') or '').replace(chr(10), ' ')}"
                for s in snippets[:5]
            )
            line += (
                f" | x_mentions={sig.get('mention_count', 0)}"
                f" x_unique_authors={sig.get('unique_authors', 0)}"
                f" x_total_likes={sig.get('total_likes', 0)}"
                f" x_snippets=[{formatted_snips}]"
            )
        wh = whale_map.get(c.get("mint", ""))
        if wh:
            line += (
                f" | whale_buy_count={wh.get('buy_count', 0)}"
                f" whale_unique_wallets={wh.get('unique_whales', 0)}"
                f" whale_total_sol_spent={float(wh.get('total_sol_spent') or 0.0):.3f}"
                f" whale_sell_count={wh.get('sell_count', 0)}"
            )
        lines.append(line)
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
    ohlcv_1m_by_pair: dict[str, list[dict[str, float]]] = {}
    if pairs_to_fetch:
        owns_client = http_client is None
        client = http_client or httpx.AsyncClient()
        try:
            ohlcv_by_pair = await fetch_ohlcv_batch(
                pairs_to_fetch, config, client, now_ts=now_dt.timestamp()
            )
            short_pairs = [p for p in pairs_to_fetch if len(ohlcv_by_pair.get(p) or []) < 12]
            if short_pairs:
                ohlcv_1m_by_pair = await fetch_ohlcv_1m_batch(
                    short_pairs, config, client, now_ts=now_dt.timestamp()
                )
        finally:
            if owns_client:
                await client.aclose()

    scalper_results: list[ScalperEntryResult] = []
    soak_minutes_list: list[float] = []
    for cand in safe_candidates:
        added_at_str = cand.get("added_at") or (watchlist_by_mint.get(cand["mint"]) or {}).get("added_at")
        if added_at_str:
            added_dt = datetime.fromisoformat(added_at_str)
            if added_dt.tzinfo is None:
                added_dt = added_dt.replace(tzinfo=timezone.utc)
            time_in_slot_min = max(0.0, (now_dt - added_dt).total_seconds() / 60.0)
        else:
            time_in_slot_min = 0.0
        soak_minutes_list.append(round(time_in_slot_min, 2))

        p_addr = str(
            cand.get("pair_address")
            or (watchlist_by_mint.get(cand["mint"]) or {}).get("pair_address")
            or ""
        )
        candles_5m = cand.get("candles_5m")
        if candles_5m is None:
            candles_5m = ohlcv_by_pair.get(p_addr, [])
        candles_1m = cand.get("candles_1m")
        if candles_1m is None and len(candles_5m) < 12:
            candles_1m = ohlcv_1m_by_pair.get(p_addr, [])

        scalper_results.append(
            evaluate_scalper_entry(
                candles_5m,
                cand,
                time_in_slot_min,
                config=config,
                candles_1m=candles_1m,
            )
        )

    active_social = db.get_active_social_signals(
        ttl_minutes=config.X_SIGNAL_TTL_MINUTES,
        now=now_dt,
    )
    active_whales = db.get_active_whale_signals(
        ttl_minutes=config.WHALE_SIGNAL_TTL_MINUTES,
        min_buys=0,
        now=now_dt,
    )
    input_text = _build_batch_entry_prompt(
        safe_candidates,
        leg2_results,
        scalper_results,
        social_by_mint=active_social,
        whale_by_mint=active_whales,
    )
    questions = _build_batch_entry_questions(safe_candidates)
    answers = await decision_client.decide(input_text=input_text, questions=questions)

    decisions: list[AlphaDecision] = []
    approved_pairs: list[tuple[AlphaDecision, dict[str, Any], bool, float, str, Optional[dict[str, Any]]]] = []

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
            "breakout_metrics": scalper.breakout_metrics,
            "breakout_telemetry": scalper.breakout_telemetry,
            "approved": dec.approved,
        }

        bt = scalper.breakout_telemetry or {}
        bt_log_kwargs: dict[str, Any] = {
            "breakout_score": bt.get("score"),
            "breakout_state": bt.get("state"),
            "breakout_timeframe": bt.get("timeframe"),
            "breakout_vol_ratio": bt.get("vol_ratio"),
            "breakout_ema_ext_pct": bt.get("ema_ext_pct"),
            "breakout_failed_gates": "; ".join(bt.get("failed_gates") or []) if bt else None,
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
        cand_mc = float(cand.get("market_cap_usd") or 0.0) or None
        cand_liq = float(cand.get("liquidity_usd") or 0.0) or None
        cand_soak = soak_minutes_list[idx]
        bi_suffix = ""
        if scalper.breakout_metrics and scalper.setup_type == "BREAKOUT_IGNITION":
            bm = scalper.breakout_metrics
            bi_suffix = (
                f" [tf={bm.get('timeframe')} vol={bm.get('vol_ratio')}x "
                f"ext_ema9={float(bm.get('ext_above_ema9_pct') or 0)*100:+.1f}% "
                f"ext_vwap={float(bm.get('ext_above_vwap_pct') or 0)*100:+.1f}%]"
            )
        if approved:
            dec_reason = f"{dec.action} -> APPROVE{bi_suffix}"
        elif dec.action == "BUY" or decision1_wants_buy:
            block_reasons: list[str] = []
            if not scalper.eligible and scalper.rejection_reason:
                block_reasons.append(scalper.rejection_reason)
            elif scalper.setup_type == "NONE":
                block_reasons.append("NO_VALID_SETUP")
            if conf < config.MIN_BUY_CONFIDENCE:
                block_reasons.append(f"conf={conf:.2f} < {config.MIN_BUY_CONFIDENCE:.2f}")
            if vamp > config.MAX_VAMP_ENTRY:
                block_reasons.append(f"vamp={vamp:.2f} > {config.MAX_VAMP_ENTRY:.2f}")
            if narr < config.MIN_NARRATIVE_SCORE:
                block_reasons.append(f"narr={narr:.1f} < {config.MIN_NARRATIVE_SCORE:.1f}")
            if mom < config.MIN_MOMENTUM_SCORE:
                block_reasons.append(f"mom={mom:.1f} < {config.MIN_MOMENTUM_SCORE:.1f}")
            dec_reason = f"BLOCKED: {'; '.join(block_reasons) or 'GATE_REJECT'}"
        else:
            dec_reason = f"{dec.action} -> {dec.action}"
        db.log_action(
            mode=config.EXECUTION_MODE,
            action="DECISION_1",
            symbol=dec.symbol,
            asset_id=dec.mint,
            strategy="LEG2_ENTRY" if (approved and leg2_fast_track and not standard_approved) else scalper.setup_type,
            reason=dec_reason,
            timestamp_utc=now_iso,
            mc_usd=cand_mc,
            liq_usd=cand_liq,
            soak_minutes=cand_soak,
            rsi=round(scalper.rsi_5m, 2),
            leg2_score=round(leg2_score, 2),
            narrative_score=round(dec.narrative_strength, 2),
            momentum_score=round(dec.momentum_quality, 2),
            vamp_score=round(dec.is_copycat_vamp, 3),
            buy_confidence=round(dec.action_confidence, 3),
            **bt_log_kwargs,
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
                (dec, cand, leg2_fast_track and not standard_approved, leg2_score, scalper.setup_type, scalper.breakout_telemetry)
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
                **bt_log_kwargs,
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

        # Coiling base check: liq >= $40K, vamp < 0.65, within 5% of 1h high
        is_coiling_base = (
            float(cand.get("liquidity_usd") or 0.0) >= 40_000.0
            and dec.is_copycat_vamp < 0.65
            and scalper.dist_from_1h_high_pct <= 0.05
        )
        prev_scores = (watchlist_by_mint.get(dec.mint) or {}).get("latest_scores") or {}
        prev_low_strikes = int(prev_scores.get("low_score_strikes") or 0)

        # Eviction Rule 2: Weak Narrative & Momentum (< 2.0 AND < 2.0 -> 15m cooldown)
        # Coiling bases require 2 consecutive LOW_SCORE ticks so quiet coils aren't evicted prematurely
        if (
            dec.narrative_strength < config.LOW_SCORE_EVICT_THRESHOLD
            and dec.momentum_quality < config.LOW_SCORE_EVICT_THRESHOLD
        ):
            new_low_strikes = prev_low_strikes + 1
            if is_coiling_base and new_low_strikes < 2:
                scores_payload["low_score_strikes"] = new_low_strikes
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
                logger.info(
                    "[Alpha] $%s | COILING BASE (liq=$%.0f, vamp=%.2f, dist=%.1f%%) -> retained on 1st LOW_SCORE strike (1/2)",
                    dec.symbol,
                    float(cand.get("liquidity_usd") or 0.0),
                    dec.is_copycat_vamp,
                    scalper.dist_from_1h_high_pct * 100.0,
                )
                continue

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
                **bt_log_kwargs,
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

        scores_payload["low_score_strikes"] = 0

        # Eviction Rule 3: Stale Watch Timeout (> 15m, or > 25m for coiling bases -> 10m cooldown)
        stale_limit_min = max(25, config.STALE_WATCH_TIMEOUT_MINUTES) if is_coiling_base else config.STALE_WATCH_TIMEOUT_MINUTES
        added_at_str = cand.get("added_at") or (watchlist_by_mint.get(dec.mint) or {}).get("added_at")
        if added_at_str:
            added_dt = datetime.fromisoformat(added_at_str)
            if added_dt.tzinfo is None:
                added_dt = added_dt.replace(tzinfo=timezone.utc)
            elapsed_min = (now_dt - added_dt).total_seconds() / 60.0
            if elapsed_min > stale_limit_min:
                evict_reason = f"STALE_WATCH ({elapsed_min:.1f}m > {stale_limit_min}m)"
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

    # Shared Guard: exclude any mint already open in Slot 2 (whale_slot)
    whale_pos = state.get("whale_position") or db.get_state().get("whale_position")
    whale_mint = str(whale_pos.get("mint") or "") if isinstance(whale_pos, dict) else ""
    if whale_mint:
        approved_pairs = [item for item in approved_pairs if item[0].mint != whale_mint]

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
    winner, winner_cand, is_leg2_only, _, winner_setup, winner_bt = approved_pairs[0]

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
        breakout_telemetry=winner_bt,
        rationale=winner.rationale,
    )
    return {
        "alpha_decisions": [d.model_dump() for d in decisions],
        "active_watchlist": db.get_watchlist(),
        "pending_order": order.model_dump(),
        "last_decision_rationale": winner.rationale,
    }
