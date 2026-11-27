"""Pure Decision 1 Strategist (alpha_node.py): Cached Narrative + Live Momentum Engine."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from alpha_helpers import (
    LEG2_MOMENTUM_RUBRIC,
    MOMENTUM_RUBRIC,
    NARRATIVE_RUBRIC,
    build_batch_entry_prompt,
    build_batch_entry_questions,
    compute_live_momentum_and_action,
    evaluate_leg2_reentry,
    format_entry_rationale,
    format_reentry_rationale,
    resolve_leg2,
)
from config import LAMPORTS_PER_SOL, AgentConfig
from db import AgentDatabase, utc_now
from decision_client import GeminiDecisionClient
from narrative_cache import get_cached_narrative, put_cached_narrative
from ohlcv_client import (
    OhlcvFetchResult,
    cache_breakout_telemetry,
    fetch_ohlcv_1m_batch_with_provenance,
    fetch_ohlcv_batch_with_provenance,
    select_ohlcv_interval_provenance,
    should_fetch_1m_fallback,
)
from outcome_tracker import record_outcome_snapshot
from scalper_entry_engine import ScalperEntryResult, evaluate_scalper_entry
from state import (
    AgentGraphState,
    AlphaDecision,
    FocusState,
    PendingOrder,
    is_valid_pool_address,
)

__all__ = [
    "LEG2_MOMENTUM_RUBRIC",
    "MOMENTUM_RUBRIC",
    "NARRATIVE_RUBRIC",
    "alpha_node",
    "evaluate_leg2_reentry",
    "format_entry_rationale",
    "format_reentry_rationale",
]

logger = logging.getLogger(__name__)


def _resolve_pool_identity(
    cand: dict[str, Any],
    wl_entry: dict[str, Any],
    *,
    mint: str,
    symbol: str,
) -> tuple[Optional[str], Optional[str], bool]:
    """Resolve (pair_address, dex_id) as a single atomic pool identity tuple.

    When a valid pinned watchlist identity exists, preserve its (pair_address, dex_id)
    and never mix its pair_address with an unrelated candidate pool's dex_id.
    Returns (pair_address, dex_id, identity_disagreement).
    """
    wl_pair = str(wl_entry.get("pair_address") or "").strip()
    wl_dex = str(wl_entry.get("dex_id") or "").strip() or None
    cand_pair = str(cand.get("pair_address") or "").strip()
    cand_dex = str(cand.get("dex_id") or "").strip() or None

    if is_valid_pool_address(wl_pair, mint=mint):
        disagreement = bool(cand_pair and cand_pair != wl_pair)
        if disagreement:
            logger.warning(
                "[Alpha] $%s pool identity disagreement: preserving pinned watchlist pool %s (%s) over candidate pool %s (%s)",
                symbol, wl_pair[:8], wl_dex or "unknown", cand_pair[:8], cand_dex or "unknown",
            )
            return wl_pair, wl_dex, True
        resolved_dex = wl_dex if wl_dex is not None else (cand_dex if cand_pair == wl_pair else None)
        return wl_pair, resolved_dex, False

    return (cand_pair or None), cand_dex, False


async def alpha_node(
    state: AgentGraphState,
    *,
    config: AgentConfig,
    db: AgentDatabase,
    decision_client: GeminiDecisionClient,
    http_client: Optional[httpx.AsyncClient] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """LangGraph node: 30m narrative/vamp cache + fresh live momentum/breakout evaluation."""
    if "chart_positions" in state:
        chart_open = len(state.get("chart_positions") or [])
        whale_open = len(state.get("whale_positions") or [])
        if chart_open >= config.MAX_CHART_POSITIONS or (chart_open + whale_open) >= config.MAX_OPEN_POSITIONS:
            return {"alpha_decisions": [], "pending_order": None}
    elif state.get("focus_state") != FocusState.SCANNING.value:
        return {"alpha_decisions": [], "pending_order": None}

    safe_candidates = state.get("safe_candidates") or []
    if not safe_candidates:
        return {"alpha_decisions": [], "pending_order": None}

    sol_balance = float(state.get("sol_balance") or 0.0)
    if sol_balance < config.TRADE_SIZE_SOL:
        rationale = f"[RISK GUARD] Insufficient SOL balance ({sol_balance:.4f} < {config.TRADE_SIZE_SOL:.4f} SOL) to open position."
        return {"alpha_decisions": [], "pending_order": None, "last_decision_rationale": rationale}

    now_dt = now or utc_now()
    now_iso = now_dt.isoformat()
    watchlist_by_mint = {w["mint"]: w for w in db.get_watchlist()}
    leg2_results = [resolve_leg2(cand) for cand in safe_candidates]

    # Fetch 5m OHLCV candles for candidates lacking pre-attached candles_5m
    pairs_to_fetch: list[str] = []
    for cand in safe_candidates:
        if cand.get("candles_5m") is None:
            p_addr = str(cand.get("pair_address") or (watchlist_by_mint.get(cand["mint"]) or {}).get("pair_address") or "")
            if p_addr:
                pairs_to_fetch.append(p_addr)

    ohlcv_by_pair: dict[str, OhlcvFetchResult] = {}
    ohlcv_1m_by_pair: dict[str, OhlcvFetchResult] = {}
    if pairs_to_fetch:
        owns_client = http_client is None
        client = http_client or httpx.AsyncClient()
        try:
            ohlcv_by_pair = await fetch_ohlcv_batch_with_provenance(
                pairs_to_fetch, config, client, now_ts=now_dt.timestamp()
            )
            short_pairs = [
                p
                for p in pairs_to_fetch
                if should_fetch_1m_fallback(
                    p,
                    (ohlcv_by_pair[p].candles if p in ohlcv_by_pair else []),
                    now_ts=now_dt.timestamp(),
                )
            ]
            if short_pairs:
                ohlcv_1m_by_pair = await fetch_ohlcv_1m_batch_with_provenance(
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

        p_addr = str(cand.get("pair_address") or (watchlist_by_mint.get(cand["mint"]) or {}).get("pair_address") or "")
        unavail = OhlcvFetchResult(candles=[], source="unavailable", fetched_at=None, last_ok_at=None, last_status=None)
        if cand.get("candles_5m") is not None:
            raw_5m_prov = cand.get("_ohlcv_5m_result")
            res_5m = (
                raw_5m_prov
                if isinstance(raw_5m_prov, OhlcvFetchResult)
                else OhlcvFetchResult(
                    candles=list(cand["candles_5m"]) if isinstance(cand["candles_5m"], list) else [],
                    source="pre_attached_unknown",
                    fetched_at=None,
                    last_ok_at=None,
                    last_status=None,
                )
            )
        else:
            res_5m = ohlcv_by_pair.get(p_addr, unavail)

        if cand.get("candles_1m") is not None:
            raw_1m_prov = cand.get("_ohlcv_1m_result")
            res_1m = (
                raw_1m_prov
                if isinstance(raw_1m_prov, OhlcvFetchResult)
                else OhlcvFetchResult(
                    candles=list(cand["candles_1m"]) if isinstance(cand["candles_1m"], list) else [],
                    source="pre_attached_unknown",
                    fetched_at=None,
                    last_ok_at=None,
                    last_status=None,
                )
            )
        else:
            res_1m = ohlcv_1m_by_pair.get(p_addr, unavail)

        active_prov, prov_ok, _ = select_ohlcv_interval_provenance(
            res_5m,
            res_1m,
            decision_timestamp=now_dt.timestamp(),
            max_cache_age_seconds=float(config.OHLCV_CACHE_SECONDS),
        )
        candles_5m = res_5m.candles
        candles_1m = res_1m.candles
        sc_entry = evaluate_scalper_entry(
            candles_5m,
            cand,
            time_in_slot_min,
            config=config,
            candles_1m=candles_1m,
            decision_timestamp=now_dt.timestamp(),
            ohlcv_provenance=active_prov,
        )
        if prov_ok and (candles_5m or candles_1m) and sc_entry.breakout_telemetry:
            cache_breakout_telemetry(
                cand["mint"],
                sc_entry.breakout_telemetry,
                ohlcv_source=active_prov.source,
                ohlcv_last_ok_at=active_prov.last_ok_at,
                now_ts=now_dt.timestamp(),
            )
        scalper_results.append(sc_entry)

    active_social = db.get_active_social_signals(ttl_minutes=config.X_SIGNAL_TTL_MINUTES, now=now_dt)
    active_whales = (
        db.get_active_whale_signals(ttl_minutes=config.WHALE_SIGNAL_TTL_MINUTES, min_buys=0, now=now_dt)
        if config.WHALE_STRATEGY_ENABLED
        else {}
    )

    cache_ttl = float(getattr(config, "NARRATIVE_CACHE_TTL_SECONDS", 1800.0))
    now_ts = now_dt.timestamp()
    answers: dict[str, dict[str, Any]] = {}
    cache_meta_by_idx: dict[int, tuple[bool, str]] = {}
    miss_indices: list[int] = []

    # Cache HIT serves ONLY slow-moving narrative_strength & is_copycat_vamp; momentum_quality & action are evaluated fresh!
    for idx, (cand, leg2, sc) in enumerate(zip(safe_candidates, leg2_results, scalper_results)):
        cached = get_cached_narrative(cand["mint"], now_ts=now_ts, ttl_seconds=cache_ttl)
        if cached is not None:
            prefix = f"t{idx}"
            c_narr = cached["narrative_strength"]
            c_vamp = cached["is_copycat_vamp"]
            live_mom, live_act = compute_live_momentum_and_action(
                cand, leg2, sc,
                narrative_score=float(c_narr.get("score", 0.0)),
                vamp_score=float(c_vamp.get("noul", 1.0)),
                config=config,
            )
            answers[f"{prefix}_narrative_strength"] = c_narr
            answers[f"{prefix}_is_copycat_vamp"] = c_vamp
            answers[f"{prefix}_momentum_quality"] = live_mom
            answers[f"{prefix}_action"] = live_act
            cache_meta_by_idx[idx] = (True, str(cached["cached_at"]))
            logger.info(
                "[Alpha] $%s | cached narrative (at=%s) + fresh live momentum=%.2f action=%s",
                cand.get("symbol", "UNKNOWN"), cached["cached_at"], live_mom["score"], live_act["choice"],
            )
        else:
            miss_indices.append(idx)

    if miss_indices:
        miss_cands = [safe_candidates[i] for i in miss_indices]
        miss_leg2 = [leg2_results[i] for i in miss_indices]
        miss_scalper = [scalper_results[i] for i in miss_indices]
        input_text = build_batch_entry_prompt(miss_cands, miss_leg2, miss_scalper, social_by_mint=active_social, whale_by_mint=active_whales)
        questions = build_batch_entry_questions(miss_cands)
        fresh_answers = await decision_client.decide(input_text=input_text, questions=questions)
        for m_pos, orig_idx in enumerate(miss_indices):
            m_pref, o_pref = f"t{m_pos}", f"t{orig_idx}"
            raw_narr = fresh_answers.get(f"{m_pref}_narrative_strength") or {}
            raw_mom = fresh_answers.get(f"{m_pref}_momentum_quality") or {}
            raw_vamp = fresh_answers.get(f"{m_pref}_is_copycat_vamp") or {}
            raw_act = fresh_answers.get(f"{m_pref}_action") or {}
            answers[f"{o_pref}_narrative_strength"] = raw_narr
            answers[f"{o_pref}_momentum_quality"] = raw_mom
            answers[f"{o_pref}_is_copycat_vamp"] = raw_vamp
            answers[f"{o_pref}_action"] = raw_act
            stored = put_cached_narrative(
                safe_candidates[orig_idx]["mint"],
                narrative_strength=raw_narr,
                is_copycat_vamp=raw_vamp,
                now_dt=now_dt,
                ttl_seconds=cache_ttl,
            )
            cache_meta_by_idx[orig_idx] = (False, str(stored["cached_at"]))

    enabled_strats = config.chart_enabled_strategies()
    leg2_enabled = "LEG2_ENTRY" in enabled_strats
    decisions: list[AlphaDecision] = []
    approved_pairs: list[
        tuple[AlphaDecision, dict[str, Any], bool, float, str, Optional[dict[str, Any]], Optional[str], Optional[str]]
    ] = []

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
            leg2_enabled
            and leg2_score >= config.LEG2_FAST_TRACK_MIN_SCORE
            and vamp <= config.MAX_VAMP_ENTRY
            and mom >= config.LEG2_FAST_TRACK_MIN_MOM
            and (
                (action == "BUY" and conf >= config.LEG2_FAST_TRACK_MIN_CONF)
                or (action == "WATCH" and leg2_score >= config.LEG2_FAST_TRACK_WATCH_SCORE)
            )
        )
        wl_entry = watchlist_by_mint.get(cand["mint"]) or {}
        cand_pair_address, cand_dex_id, pool_identity_disagreement = _resolve_pool_identity(
            cand, wl_entry, mint=cand["mint"], symbol=cand.get("symbol", "UNKNOWN")
        )

        setup_enabled = scalper.setup_type in enabled_strats
        decision1_wants_buy = standard_approved or leg2_fast_track
        approved = decision1_wants_buy and scalper.eligible and setup_enabled
        strat_name = "LEG2_ENTRY" if (approved and leg2_fast_track and not standard_approved) else scalper.setup_type
        pool_identity_rejected = False
        if approved and strat_name == "BREAKOUT_IGNITION" and not is_valid_pool_address(cand_pair_address, mint=cand["mint"]):
            approved = False
            pool_identity_rejected = True

        dec = AlphaDecision(
            mint=cand["mint"], symbol=cand.get("symbol", "UNKNOWN"),
            narrative_strength=narr, momentum_quality=mom, is_copycat_vamp=vamp,
            action=action, action_confidence=conf, approved=approved, rationale="",
        )
        base_rationale = format_entry_rationale(dec)
        if pool_identity_rejected:
            dec.rationale = f"{base_rationale} [BLOCKED: MISSING_OR_INVALID_POOL_IDENTITY]"
        elif approved and leg2_fast_track and not standard_approved:
            dec.rationale = f"{base_rationale} [LEG2_ENTRY leg2={leg2_score:.0f}/100 setup={scalper.setup_type} rsi={scalper.rsi_5m:.1f}]"
        elif decision1_wants_buy and not scalper.eligible:
            dec.rationale = f"{base_rationale} [BLOCKED: {scalper.rejection_reason}]"
        else:
            dec.rationale = f"{base_rationale} [leg2={leg2_score:.0f}/100 setup={scalper.setup_type} rsi={scalper.rsi_5m:.1f}]"
        decisions.append(dec)

        c_hit, c_at = cache_meta_by_idx.get(idx, (False, now_iso))
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
            "pair_address": cand_pair_address or None,
            "dex_id": cand_dex_id,
            "pool_identity_rejected": pool_identity_rejected,
            "pool_identity_disagreement": pool_identity_disagreement,
            "narrative_cache_hit": c_hit,
            "narrative_cached_at": c_at,
            "momentum_source": "live_metrics" if c_hit else "gemini_decision_1",
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
            mint=dec.mint, symbol=dec.symbol, phase="ENTRY", action=dec.action,
            confidence=dec.action_confidence, scores=scores_payload, rationale=dec.rationale,
        )
        cand_mc = round(float(cand.get("market_cap_usd") or 0.0), 2) or None
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
        elif pool_identity_rejected:
            dec_reason = "BLOCKED: MISSING_OR_INVALID_POOL_IDENTITY (pair_address absent or invalid)"
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
            mode=config.EXECUTION_MODE, action="DECISION_1", symbol=dec.symbol, asset_id=dec.mint,
            strategy=strat_name, reason=dec_reason, timestamp_utc=now_iso,
            mc_usd=cand_mc, liq_usd=cand_liq, soak_minutes=cand_soak,
            rsi=round(scalper.rsi_5m, 2), leg2_score=round(leg2_score, 2),
            narrative_score=round(dec.narrative_strength, 2), momentum_score=round(dec.momentum_quality, 2),
            vamp_score=round(dec.is_copycat_vamp, 3), buy_confidence=round(dec.action_confidence, 3),
            pair_address=cand_pair_address or None, dex_id=cand_dex_id,
            **bt_log_kwargs,
        )
        out_decision = "ENTERED" if approved else ("BLOCKED" if (dec.action == "BUY" or decision1_wants_buy) else ("WATCH" if dec.action == "WATCH" else "REJECTED"))
        record_outcome_snapshot(
            db, mint=dec.mint, symbol=dec.symbol, source="CHART_ALPHA", decision=out_decision,
            decision_reason=dec_reason, strategy=strat_name,
            entry_price_usd=float(cand.get("price_usd") or 0.0) or None, entry_mc_usd=cand_mc, entry_liq_usd=cand_liq,
            breakout_score=bt.get("score"), breakout_state=bt.get("state"), leg2_score=leg2_score,
            vamp_score=dec.is_copycat_vamp, rsi=scalper.rsi_5m, now=now_dt,
        )

        if approved:
            if leg2_fast_track and not standard_approved:
                logger.info(
                    "[Alpha] $%s | LEG2_ENTRY (leg2=%.0f/100 action=%s conf=%.2f mom=%.1f vamp=%.2f setup=%s) -> APPROVED",
                    dec.symbol, leg2_score, dec.action, dec.action_confidence, dec.momentum_quality, dec.is_copycat_vamp, scalper.setup_type,
                )
            else:
                logger.info(
                    "[Alpha] $%s | action=%s (%.2f) | narr=%.1f mom=%.1f vamp=%.2f leg2=%.0f/100 setup=%s RSI=%.1f -> APPROVED",
                    dec.symbol, dec.action, dec.action_confidence, dec.narrative_strength,
                    dec.momentum_quality, dec.is_copycat_vamp, leg2_score, scalper.setup_type, scalper.rsi_5m,
                )
            db.update_watchlist_scores(
                mint=dec.mint, scores=scores_payload, last_evaluated_at=now_iso, vamp_strikes=0,
                rsi_5m=scalper.rsi_5m, setup_type=scalper.setup_type, scalper_eligible=scalper.eligible,
                dist_from_1h_high_pct=scalper.dist_from_1h_high_pct,
            )
            approved_pairs.append((
                dec, cand, leg2_fast_track and not standard_approved, leg2_score,
                scalper.setup_type, scalper.breakout_telemetry,
                cand_pair_address, cand_dex_id,
            ))
            continue

        if decision1_wants_buy and not scalper.eligible:
            db.update_watchlist_scores(
                mint=dec.mint, scores=scores_payload, last_evaluated_at=now_iso, vamp_strikes=0,
                rsi_5m=scalper.rsi_5m, setup_type=scalper.setup_type, scalper_eligible=False,
                dist_from_1h_high_pct=scalper.dist_from_1h_high_pct,
            )
            logger.info("[Alpha] $%s | BLOCKED (%s) | action=%s (%.2f) | narr=%.1f mom=%.1f vamp=%.2f", dec.symbol, scalper.rejection_reason, dec.action, dec.action_confidence, dec.narrative_strength, dec.momentum_quality, dec.is_copycat_vamp)
            continue

        # Eviction Rule 1: High Vamp Score (immediate >= 0.85, OR 2 consecutive cycles >= 0.75)
        prev_vamp_strikes = int((watchlist_by_mint.get(dec.mint) or {}).get("vamp_strikes") or 0)
        if dec.is_copycat_vamp >= config.VAMP_EVICT_THRESHOLD:
            evict_reason = f"HIGH_VAMP ({dec.is_copycat_vamp:.2f} >= {config.VAMP_EVICT_THRESHOLD:.2f})"
            logger.info("[Alpha] $%s | vamp=%.2f -> EVICTED (vamp >= %.2f, %dm cooldown)", dec.symbol, dec.is_copycat_vamp, config.VAMP_EVICT_THRESHOLD, config.VAMP_DROP_COOLDOWN_MINUTES)
            db.remove_from_watchlist(dec.mint)
            db.set_cooldown_minutes(dec.mint, minutes=config.VAMP_DROP_COOLDOWN_MINUTES, reason=evict_reason, now=now_dt)
            db.log_action(mode=config.EXECUTION_MODE, action="EVICT", symbol=dec.symbol, asset_id=dec.mint, strategy=scalper.setup_type, reason=evict_reason, timestamp_utc=now_iso, **bt_log_kwargs)
            continue

        new_vamp_strikes = 0
        if dec.is_copycat_vamp >= config.VAMP_WARN_THRESHOLD:
            new_vamp_strikes = prev_vamp_strikes + 1
            if new_vamp_strikes >= config.VAMP_CONSECUTIVE_STRIKES:
                evict_reason = f"CONSECUTIVE_VAMP ({dec.is_copycat_vamp:.2f} >= {config.VAMP_WARN_THRESHOLD:.2f}, {new_vamp_strikes}/{config.VAMP_CONSECUTIVE_STRIKES} strikes)"
                logger.info("[Alpha] $%s | vamp=%.2f -> EVICTED (%d/%d strikes, %dm cooldown)", dec.symbol, dec.is_copycat_vamp, new_vamp_strikes, config.VAMP_CONSECUTIVE_STRIKES, config.VAMP_DROP_COOLDOWN_MINUTES)
                db.remove_from_watchlist(dec.mint)
                db.set_cooldown_minutes(dec.mint, minutes=config.VAMP_DROP_COOLDOWN_MINUTES, reason=evict_reason, now=now_dt)
                db.log_action(mode=config.EXECUTION_MODE, action="EVICT", symbol=dec.symbol, asset_id=dec.mint, strategy=scalper.setup_type, reason=evict_reason, timestamp_utc=now_iso)
                continue

        is_coiling_base = (
            float(cand.get("liquidity_usd") or 0.0) >= 40_000.0
            and dec.is_copycat_vamp < 0.65
            and scalper.dist_from_1h_high_pct <= 0.05
        )
        prev_scores = (watchlist_by_mint.get(dec.mint) or {}).get("latest_scores") or {}
        prev_low_strikes = int(prev_scores.get("low_score_strikes") or 0)

        # Eviction Rule 2: Weak Narrative & Momentum (< 2.0 AND < 2.0; coiling bases get 2 strikes)
        if dec.narrative_strength < config.LOW_SCORE_EVICT_THRESHOLD and dec.momentum_quality < config.LOW_SCORE_EVICT_THRESHOLD:
            new_low_strikes = prev_low_strikes + 1
            if is_coiling_base and new_low_strikes < 2:
                scores_payload["low_score_strikes"] = new_low_strikes
                db.update_watchlist_scores(
                    mint=dec.mint, scores=scores_payload, last_evaluated_at=now_iso, vamp_strikes=new_vamp_strikes,
                    rsi_5m=scalper.rsi_5m, setup_type=scalper.setup_type, scalper_eligible=scalper.eligible,
                    dist_from_1h_high_pct=scalper.dist_from_1h_high_pct,
                )
                continue
            evict_reason = f"LOW_SCORE (narr={dec.narrative_strength:.2f}, mom={dec.momentum_quality:.2f} < {config.LOW_SCORE_EVICT_THRESHOLD:.1f})"
            logger.info("[Alpha] $%s | narr=%.2f mom=%.2f -> EVICTED (%dm cooldown)", dec.symbol, dec.narrative_strength, dec.momentum_quality, config.LOW_SCORE_COOLDOWN_MINUTES)
            db.remove_from_watchlist(dec.mint)
            db.set_cooldown_minutes(dec.mint, minutes=config.LOW_SCORE_COOLDOWN_MINUTES, reason=evict_reason, now=now_dt)
            db.log_action(mode=config.EXECUTION_MODE, action="EVICT", symbol=dec.symbol, asset_id=dec.mint, strategy=scalper.setup_type, reason=evict_reason, timestamp_utc=now_iso, **bt_log_kwargs)
            continue

        scores_payload["low_score_strikes"] = 0

        # Eviction Rule 3: Stale Watch Timeout (> 15m, or > 25m for coiling bases)
        stale_limit_min = max(25, config.STALE_WATCH_TIMEOUT_MINUTES) if is_coiling_base else config.STALE_WATCH_TIMEOUT_MINUTES
        added_at_str = cand.get("added_at") or (watchlist_by_mint.get(dec.mint) or {}).get("added_at")
        if added_at_str:
            added_dt = datetime.fromisoformat(added_at_str)
            if added_dt.tzinfo is None:
                added_dt = added_dt.replace(tzinfo=timezone.utc)
            elapsed_min = (now_dt - added_dt).total_seconds() / 60.0
            if elapsed_min > stale_limit_min:
                evict_reason = f"STALE_WATCH ({elapsed_min:.1f}m > {stale_limit_min}m)"
                logger.info("[Alpha] $%s | elapsed=%.1fm -> EVICTED (stale > %dm, %dm cooldown)", dec.symbol, elapsed_min, stale_limit_min, config.STALE_WATCH_COOLDOWN_MINUTES)
                db.remove_from_watchlist(dec.mint)
                db.set_cooldown_minutes(dec.mint, minutes=config.STALE_WATCH_COOLDOWN_MINUTES, reason=evict_reason, now=now_dt)
                db.log_action(mode=config.EXECUTION_MODE, action="EVICT", symbol=dec.symbol, asset_id=dec.mint, strategy=scalper.setup_type, reason=evict_reason, timestamp_utc=now_iso)
                continue

        db.update_watchlist_scores(
            mint=dec.mint, scores=scores_payload, last_evaluated_at=now_iso, vamp_strikes=new_vamp_strikes,
            rsi_5m=scalper.rsi_5m, setup_type=scalper.setup_type, scalper_eligible=scalper.eligible,
            dist_from_1h_high_pct=scalper.dist_from_1h_high_pct,
        )

    open_mints = db.get_all_open_mints()
    for p in (state.get("chart_positions") or []) + (state.get("whale_positions") or []) + [state.get("whale_position")]:
        if isinstance(p, dict) and p.get("mint"):
            open_mints.add(str(p["mint"]))
    if open_mints:
        approved_pairs = [item for item in approved_pairs if item[0].mint not in open_mints]

    approved_pairs = [item for item in approved_pairs if ("LEG2_ENTRY" if item[2] else item[4]) in enabled_strats]
    if not approved_pairs:
        return {
            "alpha_decisions": [d.model_dump() for d in decisions],
            "active_watchlist": db.get_watchlist(),
            "pending_order": None,
            "last_decision_rationale": decisions[0].rationale if decisions else "",
        }

    approved_pairs.sort(
        key=lambda item: (
            item[0].narrative_strength + item[0].momentum_quality + (item[3] / 25.0),
            item[0].action_confidence,
            -item[0].is_copycat_vamp,
        ),
        reverse=True,
    )
    winner, winner_cand, is_leg2_only, _, winner_setup, winner_bt, winner_pair, winner_dex = approved_pairs[0]
    winner_mc = round(float(winner_cand.get("market_cap_usd") or 0.0), 2) or None

    order = PendingOrder(
        side="BUY",
        mint=winner.mint,
        symbol=winner.symbol,
        pair_address=winner_pair,
        dex_id=winner_dex,
        amount_atomic=int(round(config.TRADE_SIZE_SOL * LAMPORTS_PER_SOL)),
        reason="LEG2_ENTRY" if is_leg2_only else "DECISION_1_ENTRY",
        target_state_after_fill=FocusState.IN_POSITION,
        apply_cooldown=False,
        entry_liquidity_usd=float(winner_cand.get("liquidity_usd") or 0.0),
        entry_mc_usd=winner_mc,
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
