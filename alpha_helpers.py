"""Decision-1 rubrics, prompt builders, Leg-2 reentry, and live momentum evaluator (alpha_helpers.py)."""

from __future__ import annotations

from typing import Any, Optional

from config import AgentConfig
from db import AgentDatabase
from decision_client import GeminiDecisionClient
from leg2_predictor import Leg2Result, compute_leg2_score
from scalper_entry_engine import ScalperEntryResult
from state import AlphaDecision, ReentryDecision

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


def resolve_leg2(cand: dict[str, Any]) -> Leg2Result:
    """Return existing Leg2Result from candidate breakdown or compute on the fly."""
    bd = cand.get("leg2_breakdown")
    if isinstance(bd, dict) and "score" in bd:
        return Leg2Result.model_validate(bd)
    if cand.get("leg2_score") is not None and not any(
        k in cand for k in ("buys_5m", "txns", "volume_1h_usd", "volume")
    ):
        score = float(cand["leg2_score"])
        return Leg2Result(
            score=score, buy_absorption=0.0, volume_acceleration=0.0,
            bounce_structure=0.0, liquidity_stability=0.0, summary=f"leg2={score:.1f}/100",
        )
    return compute_leg2_score(
        cand,
        tracked_peak_mc=float(cand.get("peak_mc") or cand.get("market_cap_usd") or 0.0),
        tracked_low_mc=float(cand.get("low_mc") or cand.get("market_cap_usd") or 0.0),
        entry_liquidity_usd=float(cand.get("entry_liquidity_usd") or cand.get("liquidity_usd") or 0.0) or None,
    )


def compute_live_momentum_and_action(
    cand: dict[str, Any],
    leg2: Leg2Result,
    scalper: ScalperEntryResult,
    *,
    narrative_score: float,
    vamp_score: float,
    config: AgentConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Evaluate `momentum_quality` (0–5) and `action` fresh from live price, volume, liquidity & breakout metrics.

    Used on narrative-cache hits so watchlist tokens transition dynamically between WATCH, BUY, and REJECT
    on live tape without waiting 30m for cache expiry and without extra Gemini LLM calls.
    """
    txns_m5 = (cand.get("txns") or {}).get("m5") or {}
    buys_5m = int(cand.get("buys_5m") if cand.get("buys_5m") is not None else (txns_m5.get("buys") or 0))
    sells_5m = int(cand.get("sells_5m") if cand.get("sells_5m") is not None else (txns_m5.get("sells") or 0))
    total_5m = buys_5m + sells_5m
    buy_ratio = (buys_5m / total_5m) if total_5m > 0 else 0.50

    vol_dict = cand.get("volume") or {}
    vol_5m = float(cand.get("volume_5m_usd") if cand.get("volume_5m_usd") is not None else (vol_dict.get("m5") or 0.0))
    vol_1h = float(cand.get("volume_1h_usd") if cand.get("volume_1h_usd") is not None else (vol_dict.get("h1") or 0.0))
    liq_dict = cand.get("liquidity")
    liq_usd = float(
        cand.get("liquidity_usd")
        if cand.get("liquidity_usd") is not None
        else (liq_dict.get("usd") if isinstance(liq_dict, dict) else 0.0) or 0.0
    )
    pc_dict = cand.get("priceChange") or {}
    chg_5m = float(cand.get("price_change_5m_pct") if cand.get("price_change_5m_pct") is not None else (pc_dict.get("m5") or 0.0))

    bt = scalper.breakout_telemetry or {}
    bt_score = int(bt.get("score") or 0)
    bt_state = str(bt.get("state") or "")
    bt_vol_ratio = float(bt.get("vol_ratio") or 0.0)
    leg2_score = float(leg2.score or 0.0)

    # 1. Order-flow component (0.0 .. 1.8)
    flow_pts = max(0.0, min(1.8, (buy_ratio - 0.42) * 5.0)) if total_5m > 0 else 0.40

    # 2. Structure & breakout component (0.45 .. 1.8)
    if (scalper.eligible and scalper.setup_type != "NONE") or bt_state == "IGNITION":
        struct_pts = 1.65 + min(0.15, bt_score / 600.0)
    elif leg2_score >= 65.0 or (bt_score >= 65 and bt_vol_ratio >= 1.3):
        struct_pts = 1.20 + min(0.40, max(float(bt_score), leg2_score) / 250.0)
    elif chg_5m >= 2.0:
        struct_pts = 0.85 + min(0.45, chg_5m / 25.0)
    else:
        struct_pts = 0.45

    # 3. Volume expansion & participation component (0.25 .. 1.4)
    vol_accel = ((vol_5m * 12.0) / vol_1h) if (vol_1h > 0.0 and vol_5m > 0.0) else bt_vol_ratio
    if vol_accel > 0.0:
        depth_bonus = 0.10 if (vol_accel >= 1.2 and liq_usd >= 40_000.0) else 0.0
        vel_pts = min(1.4, max(0.25, (vol_accel - 0.4) * 0.85 + depth_bonus))
    else:
        vel_pts = 0.45 if liq_usd >= 25_000.0 else 0.30

    raw_mom = flow_pts + struct_pts + vel_pts
    if (total_5m >= 6 and buy_ratio < 0.35) or chg_5m <= -12.0:
        raw_mom = min(raw_mom, 1.40)
    mom_score = round(max(0.0, min(5.0, raw_mom)), 2)

    enabled_strats = config.chart_enabled_strategies()
    setup_allowed = scalper.setup_type in enabled_strats
    leg2_allowed = "LEG2_ENTRY" in enabled_strats
    bo_allowed = "BREAKOUT_IGNITION" in enabled_strats

    # Live execution decision from cached narrative/vamp + fresh live momentum/setup
    if (
        vamp_score >= config.VAMP_EVICT_THRESHOLD
        or (narrative_score < config.LOW_SCORE_EVICT_THRESHOLD and mom_score < config.LOW_SCORE_EVICT_THRESHOLD)
        or (total_5m >= 10 and buy_ratio < 0.30 and chg_5m <= -10.0)
    ):
        choice = "REJECT"
        conf = 0.80
    elif vamp_score <= config.MAX_VAMP_ENTRY and (
        (scalper.eligible and setup_allowed and narrative_score >= config.MIN_NARRATIVE_SCORE and mom_score >= config.MIN_MOMENTUM_SCORE)
        or (bo_allowed and bt_state == "IGNITION" and narrative_score >= config.MIN_NARRATIVE_SCORE and mom_score >= config.MIN_MOMENTUM_SCORE)
        or (leg2_allowed and setup_allowed and leg2_score >= config.LEG2_FAST_TRACK_MIN_SCORE and mom_score >= config.LEG2_FAST_TRACK_MIN_MOM)
    ):
        choice = "BUY"
        conf = round(
            min(0.96, max(config.MIN_BUY_CONFIDENCE, 0.72 + max(0.0, mom_score - 3.0) * 0.08 + max(0.0, narrative_score - 3.0) * 0.05)),
            2,
        )
    else:
        choice = "WATCH"
        conf = round(min(0.85, max(0.55, 0.50 + mom_score * 0.07)), 2)

    mom_dict = {"type": "score", "score": mom_score, "confidence": conf, "source": "live_metrics"}
    act_dict = {"type": "choice", "choice": choice, "confidence": conf, "source": "live_metrics"}
    return mom_dict, act_dict


def build_batch_entry_prompt(
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
        jup_org = c.get("jup_organic_score") if c.get("jup_organic_score") is not None else j_meta.get("organic_score")
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


def build_batch_entry_questions(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
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
            "instructions": f"Is {tag} a low-effort copycat or vampire derivative trying to mimic a larger existing token?",
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
    approved = leg2_score >= config.REENTRY_MIN_LEG2_SCORE and dead_cat <= config.REENTRY_MAX_DEAD_CAT

    decision = ReentryDecision(
        mint=mint, symbol=symbol, leg_2_momentum=leg2_score,
        is_dead_cat_bounce=dead_cat, approved=approved, rationale="",
    )
    decision.rationale = format_reentry_rationale(decision)
    db.log_decision(
        mint=mint, symbol=symbol, phase="REENTRY",
        action="REBUY" if approved else "HOLD",
        confidence=float((answers.get("leg_2_momentum") or {}).get("confidence", 0.0)),
        scores={"leg_2_momentum": leg2_score, "is_dead_cat_bounce": dead_cat, "curl_pct": curl_pct, "buy_ratio_5m": buy_ratio_5m},
        rationale=decision.rationale,
    )
    return decision
