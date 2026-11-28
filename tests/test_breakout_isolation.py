"""Directive 02 Verification Suite: BREAKOUT_IGNITION-only Chart isolation & Whale isolation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from alpha_node import alpha_node
from batched_price_monitor import clear_price_backoff
from chart_exit_engine import evaluate_chart_position_exit
from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase
from executor_node import executor_node
from graph import build_agent_graph
from helius_webhook import clear_helius_roster_cache
from narrative_cache import clear_narrative_cache
from position_monitor_node import position_monitor_node
from scalper_entry_engine import evaluate_scalper_entry
from scanner_node import clear_geckoterminal_cache, scanner_node
from state import ActivePosition, FocusState, PendingOrder
from state_ledger import clear_buy_reservations, clear_sell_guard, update_lane_positions_only
from telemetry_api import create_telemetry_app
from tests.test_breakout_ignition import _make_breakout_candles, _strong_flow_pair
from tests.test_e3b_cache_provenance import make_verified_ohlcv_result
from tests.test_scalper_entry_engine import make_displacement_retest_candles, make_sweep_reclaim_candles


def _isolation_cfg(**overrides: Any) -> AgentConfig:
    defaults: dict[str, Any] = dict(
        _env_file=None,
        EXECUTION_MODE="paper",
        CHART_ENABLED_STRATEGIES="BREAKOUT_IGNITION",
        WHALE_STRATEGY_ENABLED=False,
        ENABLE_HELIUS_WHALE_WEBHOOK=False,
        MAX_REENTRIES_PER_MINT=0,
        PAPER_TX_FEE_SOL=0.001,
        TRADE_SIZE_SOL=0.50,
        PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0,
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


class _StubDecision:
    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        self.answers = answers
        self.calls = 0
        self.last_input = ""

    async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        self.calls += 1
        self.last_input = input_text
        return self.answers


def test_config_chart_enabled_strategies_normalization_and_invalid_rejection():
    """Config normalizes whitespace/case and explicitly rejects unknown strategy names."""
    cfg = AgentConfig(_env_file=None, CHART_ENABLED_STRATEGIES=" breakout_ignition , leg2_entry ")
    assert cfg.chart_enabled_strategies() == {"BREAKOUT_IGNITION", "LEG2_ENTRY"}
    assert cfg.is_chart_strategy_enabled("breakout_ignition") is True
    assert cfg.is_chart_strategy_enabled("SWEEP_RECLAIM") is False
    assert cfg.is_chart_strategy_enabled("NONE") is False

    with pytest.raises(ValueError, match="Unknown CHART_ENABLED_STRATEGIES"):
        AgentConfig(_env_file=None, CHART_ENABLED_STRATEGIES="BREAKOUT_IGNITION,INVALID_STRAT")


def test_evaluator_isolation_rejects_disabled_setups_and_prevents_shadowing():
    """A. BREAKOUT_IGNITION qualifies; SWEEP_RECLAIM & DISPLACEMENT_RETEST rejected and cannot shadow breakout."""
    cfg = _isolation_cfg()
    flow = _strong_flow_pair(1.12)

    # 1. Valid BREAKOUT_IGNITION qualifies
    bo_candles = _make_breakout_candles(count=18)
    bo_t = bo_candles[-1]["timestamp"] + 300.0
    bo_res = evaluate_scalper_entry(
        bo_candles, flow, time_in_slot_min=5.0, config=cfg,
        decision_timestamp=bo_t, ohlcv_provenance=make_verified_ohlcv_result(bo_candles, last_ok_at=bo_t),
    )
    assert bo_res.eligible is True
    assert bo_res.setup_type == "BREAKOUT_IGNITION"

    # 2. Valid SWEEP_RECLAIM is rejected when disabled
    sr_c = make_sweep_reclaim_candles()
    sr_t = sr_c[-1]["timestamp"] + 300.0
    sr_res = evaluate_scalper_entry(
        sr_c, {"buys_5m": 25, "sells_5m": 10}, time_in_slot_min=5.0, config=cfg,
        decision_timestamp=sr_t, ohlcv_provenance=make_verified_ohlcv_result(sr_c, last_ok_at=sr_t),
    )
    assert sr_res.eligible is False
    assert sr_res.setup_type == "NONE"

    # 3. Valid DISPLACEMENT_RETEST is rejected when disabled
    dr_c = make_displacement_retest_candles()
    dr_t = dr_c[-1]["timestamp"] + 300.0
    dr_res = evaluate_scalper_entry(
        dr_c, {"buys_5m": 25, "sells_5m": 10}, time_in_slot_min=5.0, config=cfg,
        decision_timestamp=dr_t, ohlcv_provenance=make_verified_ohlcv_result(dr_c, last_ok_at=dr_t),
    )
    assert dr_res.eligible is False
    assert dr_res.setup_type == "NONE"

    # 4. Shadowing test: candle that satisfies BOTH SWEEP_RECLAIM and BREAKOUT_IGNITION selects BREAKOUT_IGNITION when SWEEP_RECLAIM disabled
    hybrid_candles = _make_breakout_candles(count=18)
    # Force a sweep-reclaim wick on candle -2 below prior 10-bar swing low while keeping breakout close on -1
    prior_low = min(c["low"] for c in hybrid_candles[-13:-3])
    hybrid_candles[-2]["low"] = prior_low * 0.99
    hybrid_candles[-2]["close"] = prior_low * 1.005
    hyb_t = hybrid_candles[-1]["timestamp"] + 300.0
    hyb_prov = make_verified_ohlcv_result(hybrid_candles, last_ok_at=hyb_t)
    # With default config, SWEEP_RECLAIM shadows BREAKOUT_IGNITION:
    default_res = evaluate_scalper_entry(
        hybrid_candles, flow, time_in_slot_min=5.0, config=AgentConfig(_env_file=None),
        decision_timestamp=hyb_t, ohlcv_provenance=hyb_prov,
    )
    assert default_res.setup_type == "SWEEP_RECLAIM"
    # With BREAKOUT_IGNITION-only config, SWEEP_RECLAIM is skipped and BREAKOUT_IGNITION wins:
    iso_res = evaluate_scalper_entry(
        hybrid_candles, flow, time_in_slot_min=5.0, config=cfg,
        decision_timestamp=hyb_t, ohlcv_provenance=hyb_prov,
    )
    assert iso_res.eligible is True
    assert iso_res.setup_type == "BREAKOUT_IGNITION"


@pytest.mark.asyncio
async def test_alpha_isolation_blocks_leg2_fast_track_and_cached_promotion_while_allowing_breakout(tmp_path):
    """B. LEG2 fast-track & cached momentum cannot promote disabled strategies; BREAKOUT_IGNITION still approves."""
    clear_narrative_cache()
    db = AgentDatabase(db_path=str(tmp_path / "alpha_iso.duckdb"), starting_sol=1.5)
    cfg = _isolation_cfg()
    now_dt = datetime(2026, 11, 25, 22, 0, 0, tzinfo=timezone.utc)
    soaked = (now_dt - timedelta(minutes=6)).isoformat()

    # 1. Strong leg2_score=95 + SWEEP_RECLAIM -> blocked even on WATCH fast-track and BUY
    sr_cand = {
        "mint": "MintSweepLeg2", "symbol": "SRL2", "market_cap_usd": 500_000.0, "liquidity_usd": 70_000.0,
        "buys_5m": 32, "sells_5m": 12, "volume_5m_usd": 18_000.0, "volume_1h_usd": 100_000.0,
        "price_change_5m_pct": 6.0, "price_change_1h_pct": 24.0, "leg2_score": 95.0,
        "added_at": soaked, "candles_5m": make_sweep_reclaim_candles(),
    }
    dec_watch = _StubDecision({
        "t0_narrative_strength": {"type": "score", "score": 3.5, "confidence": 0.85},
        "t0_momentum_quality": {"type": "score", "score": 3.8, "confidence": 0.85},
        "t0_is_copycat_vamp": {"type": "noul", "noul": 0.20, "confidence": 0.90},
        "t0_action": {"type": "choice", "choice": "WATCH", "confidence": 0.70},
    })
    out1 = await alpha_node({"focus_state": FocusState.SCANNING.value, "sol_balance": 1.5, "safe_candidates": [sr_cand]}, config=cfg, db=db, decision_client=dec_watch, now=now_dt)  # type: ignore[arg-type]
    assert out1["pending_order"] is None

    # Tick 2 cache hit with high leg2_score=95: cached live momentum must NOT promote disabled strategy to BUY
    out2 = await alpha_node({"focus_state": FocusState.SCANNING.value, "sol_balance": 1.5, "safe_candidates": [sr_cand]}, config=cfg, db=db, decision_client=dec_watch, now=now_dt + timedelta(seconds=30))  # type: ignore[arg-type]
    assert dec_watch.calls == 1
    assert out2["pending_order"] is None
    assert out2["alpha_decisions"][0]["action"] != "BUY"

    # 2. Enabled BREAKOUT_IGNITION approves normally
    bo_c = _make_breakout_candles(count=18)
    bo_cand = {
        "mint": "MintBOAllowed", "symbol": "BO1", "added_at": soaked,
        "pair_address": "CktRuQ2mttgRGkXJtyksdKHjUdc2C4TgDzyB98oEzy8",
        "dex_id": "pumpswap",
        "candles_5m": bo_c,
        "_ohlcv_5m_result": make_verified_ohlcv_result(bo_c, last_ok_at=now_dt.timestamp()),
        **_strong_flow_pair(1.12),
    }
    dec_buy = _StubDecision({
        "t0_narrative_strength": {"type": "score", "score": 3.8, "confidence": 0.85},
        "t0_momentum_quality": {"type": "score", "score": 4.2, "confidence": 0.88},
        "t0_is_copycat_vamp": {"type": "noul", "noul": 0.15, "confidence": 0.90},
        "t0_action": {"type": "choice", "choice": "BUY", "confidence": 0.84},
    })
    out_bo = await alpha_node({"focus_state": FocusState.SCANNING.value, "sol_balance": 1.5, "safe_candidates": [bo_cand]}, config=cfg, db=db, decision_client=dec_buy, now=now_dt)  # type: ignore[arg-type]
    assert out_bo["pending_order"] is not None
    assert out_bo["pending_order"]["strategy"] == "BREAKOUT_IGNITION"
    db.close()


@pytest.mark.asyncio
async def test_reentry_isolation_blocks_watching_reentry_even_with_nonzero_max_reentries(tmp_path):
    """C. Even if MAX_REENTRIES_PER_MINT=2, disabling LEG2_ENTRY prevents TRAILING_TP -> WATCHING_REENTRY and LEG_2_REENTRY."""
    clear_price_backoff()
    db = AgentDatabase(db_path=str(tmp_path / "reentry_iso.duckdb"), starting_sol=1.5)
    # Intentionally set MAX_REENTRIES_PER_MINT=2 to prove LEG2_ENTRY allowlist enforces isolation independently
    cfg = _isolation_cfg(MAX_REENTRIES_PER_MINT=2)
    now_dt = datetime(2026, 11, 25, 22, 10, 0, tzinfo=timezone.utc)

    # 1. TRAILING_TP routes directly to SCANNING (not WATCHING_REENTRY) with cooldown
    pos = ActivePosition(
        mint="MintTrailIso", symbol="TRL", entry_sol=0.50, token_amount=1_000_000,
        entry_liquidity_usd=70_000.0, current_sol_value=0.60, peak_sol_value=0.60,
        trail_armed=True, strategy="BREAKOUT_IGNITION", reentry_count=0,
    )
    order, _ev, _prio = evaluate_chart_position_exit(
        pos, current_sol_value=0.55, buys_5m=15, sells_5m=10, current_liquidity_usd=70_000.0, config=cfg,
    )
    assert order is not None
    assert order.target_state_after_fill == FocusState.SCANNING
    assert order.apply_cooldown is True

    # 2. Pre-existing WATCHING_REENTRY is abandoned cleanly without emitting LEG_2_REENTRY
    watch_pos = ActivePosition(
        mint="MintWatchIso", symbol="WISO", entry_sol=0.50, token_amount=0,
        entry_liquidity_usd=70_000.0, current_sol_value=0.54, peak_sol_value=0.62,
        run_peak_sol=0.62, retrace_low_sol=0.50, reference_token_amount=1_000_000,
        watch_started_at=now_dt.isoformat(), reentry_count=0,
    ).model_dump()
    db.save_state(focus_state=FocusState.WATCHING_REENTRY.value, sol_balance=1.0, daily_pnl_sol=0.05, active_position=watch_pos)

    def _dex_ok(request: httpx.Request) -> httpx.Response:
        if "/tokens/v1/solana/" in str(request.url):
            return httpx.Response(200, json=[{"chainId": "solana", "baseToken": {"address": "MintWatchIso", "symbol": "WISO"}, "priceNative": "0.54", "priceUsd": "0.108", "marketCap": 500_000.0, "liquidity": {"usd": 75_000.0}, "volume": {"m5": 18_000.0, "h1": 100_000.0}, "priceChange": {"h1": 25.0, "m5": 7.0}, "txns": {"m5": {"buys": 32, "sells": 12}}}])
        return httpx.Response(404)

    dec = _StubDecision({})
    async with httpx.AsyncClient(transport=httpx.MockTransport(_dex_ok)) as client:
        out = await position_monitor_node(
            {"focus_state": FocusState.WATCHING_REENTRY.value, "sol_balance": 1.0, "daily_pnl_sol": 0.05, "active_position": watch_pos},
            config=cfg, db=db, decision_client=dec, http_client=client, now=now_dt,  # type: ignore[arg-type]
        )
    assert out["pending_order"] is None
    assert out["focus_state"] == FocusState.SCANNING.value
    assert out["monitor_event"] == "LEG2_ENTRY_DISABLED"
    assert dec.calls == 0
    db.close()


@pytest.mark.asyncio
async def test_executor_final_guard_blocks_unauthorized_chart_buy_allows_sells_and_respects_whale_flag(tmp_path):
    """D. Final executor guard blocks unauthorized Chart BUYs, allows BREAKOUT_IGNITION/SELL/PARTIAL_SELL, and keeps Whale separate."""
    clear_buy_reservations()
    clear_sell_guard()
    db = AgentDatabase(db_path=str(tmp_path / "exec_iso.duckdb"), starting_sol=2.0)
    cfg = _isolation_cfg()

    def _jup_ok(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/tokens/v2/search" in url:
            return httpx.Response(200, json=[{"id": request.url.params.get("query", "MintGoodBO"), "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "topHoldersPercentage": 15.46}}])
        if "rugcheck.xyz" in url:
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        if "/tokens/v2/search" in url:
            return httpx.Response(200, json=[{"id": bo_mint, "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "topHoldersPercentage": 15.46}}])
        if "/ultra/v1/shield" in url:
            return httpx.Response(200, json={"warnings": {request.url.params.get("mints", "MintGoodBO"): []}})
        if "/swap/v2/order" in url:
            if f"inputMint={SOL_MINT}" in str(request.url):
                return httpx.Response(200, json={"outAmount": "1000000", "priceImpactPct": "-0.3", "transaction": None})
            return httpx.Response(200, json={"outAmount": str(int(0.30 * LAMPORTS_PER_SOL)), "priceImpactPct": "-0.3", "transaction": None})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_jup_ok)) as client:
        # 1. Unauthorized Chart strategies (SWEEP_RECLAIM, LEG2_ENTRY, NONE, SCALPER) are blocked
        for bad_strat in ("SWEEP_RECLAIM", "DISPLACEMENT_RETEST", "LEG2_ENTRY", "NONE", "SCALPER"):
            bad_order = PendingOrder(
                side="BUY", mint=f"MintBad_{bad_strat}", symbol="BAD", amount_atomic=int(0.50 * LAMPORTS_PER_SOL),
                reason="DECISION_1_ENTRY", target_state_after_fill=FocusState.IN_POSITION, strategy=bad_strat,
            ).model_dump()
            out_bad = await executor_node({"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "pending_order": bad_order, "slot": "chart_slot"}, config=cfg, db=db, http_client=client)
            assert out_bad.get("last_execution") is None
        assert db.get_state()["chart_positions"] == []

        # 2. Authorized BREAKOUT_IGNITION BUY is accepted
        db.upsert_watchlist_entry(
            mint="MintGoodBO", symbol="GBO", pair_address="GgBaCs3NCBuZN12kCJgAW63ydqohFkHEdfdEXBPzLHq",
            candidate={"mint": "MintGoodBO"}, safety_passed=True,
        )
        db.mark_watchlist_safety_passed("MintGoodBO", True, checked_at=datetime.now(timezone.utc).isoformat())
        ok_order = PendingOrder(
            side="BUY", mint="MintGoodBO", symbol="GBO", amount_atomic=int(0.50 * LAMPORTS_PER_SOL),
            pair_address="GgBaCs3NCBuZN12kCJgAW63ydqohFkHEdfdEXBPzLHq", dex_id="pumpswap",
            reason="DECISION_1_ENTRY", target_state_after_fill=FocusState.IN_POSITION,
            entry_liquidity_usd=80_000.0, entry_mc_usd=500_000.0, strategy="BREAKOUT_IGNITION",
        ).model_dump()
        out_ok = await executor_node({"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "pending_order": ok_order, "slot": "chart_slot"}, config=cfg, db=db, http_client=client)
        assert out_ok.get("last_execution") is not None
        opened_pos = out_ok["chart_positions"][0]

        # 3. PARTIAL_SELL and SELL remain permitted (even if position strategy were legacy SWEEP_RECLAIM)
        ps_order = PendingOrder(
            side="PARTIAL_SELL", mint="MintGoodBO", symbol="GBO", opened_at=opened_pos["opened_at"],
            amount_atomic=500_000, reason="PARTIAL_TP_50PCT (+15%)", target_state_after_fill=FocusState.IN_POSITION,
            strategy="SWEEP_RECLAIM",
        ).model_dump()
        out_ps = await executor_node({"focus_state": FocusState.IN_POSITION.value, "active_position": opened_pos, "chart_positions": [opened_pos], "pending_order": ps_order, "slot": "chart_slot"}, config=cfg, db=db, http_client=client)
        assert out_ps.get("last_execution") is not None

        runner = out_ps["chart_positions"][0]
        sell_order = PendingOrder(
            side="SELL", mint="MintGoodBO", symbol="GBO", opened_at=runner["opened_at"],
            amount_atomic=500_000, reason="HARD_STOP_LOSS", target_state_after_fill=FocusState.SCANNING,
            strategy="SWEEP_RECLAIM",
        ).model_dump()
        out_sell = await executor_node({"focus_state": FocusState.IN_POSITION.value, "active_position": runner, "chart_positions": [runner], "pending_order": sell_order, "slot": "chart_slot"}, config=cfg, db=db, http_client=client)
        assert out_sell.get("last_execution") is not None
        assert db.get_state()["chart_positions"] == []

        # 4. Whale BUY blocked when WHALE_STRATEGY_ENABLED=False, allowed when WHALE_STRATEGY_ENABLED=True (independent of Chart allowlist!)
        wh_order = PendingOrder(
            side="BUY", mint="MintWhaleCheck", symbol="WHC", amount_atomic=int(0.25 * LAMPORTS_PER_SOL),
            reason="WHALE_SCALP_SOLO", target_state_after_fill=FocusState.IN_POSITION, strategy="WHALE_SCALP",
        ).model_dump()
        out_wh_off = await executor_node({"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "pending_order": wh_order, "slot": "whale_slot"}, config=cfg, db=db, http_client=client)
        assert out_wh_off.get("last_execution") is None

        cfg_wh_on = _isolation_cfg(WHALE_STRATEGY_ENABLED=True)
        out_wh_on = await executor_node({"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "pending_order": wh_order, "slot": "whale_slot"}, config=cfg_wh_on, db=db, http_client=client)
        assert out_wh_on.get("last_execution") is not None
        assert len(db.get_state()["whale_positions"]) == 1
    db.close()


@pytest.mark.asyncio
async def test_whale_isolation_blocks_webhook_and_prevents_chart_discovery_or_alpha_leak(tmp_path):
    """E. Disabled Whale webhook writes zero events; historical whale signals stay intact but do not leak into Chart."""
    clear_helius_roster_cache()
    clear_geckoterminal_cache()
    clear_narrative_cache()

    db = AgentDatabase(db_path=str(tmp_path / "whale_iso.duckdb"), starting_sol=1.5)
    cfg = _isolation_cfg(HELIUS_AUTH_HEADER="Bearer secret-token", ENABLE_JUPITER_DISCOVERY=False)
    now_dt = datetime(2026, 11, 25, 22, 20, 0, tzinfo=timezone.utc)

    # Seed pre-existing historical whale record to verify it is NOT deleted AND NOT injected into Chart
    db.record_whale_event(signature="hist_sig_1", wallet="WhaleHist1", mint="HistWhaleMint111111111111111111111111111111", side="BUY", sol_amount=2.5, token_amount=100_000.0, timestamp_utc=now_dt.isoformat())
    db.recompute_whale_signal("HistWhaleMint111111111111111111111111111111", ttl_minutes=30, now=now_dt)

    # 1. Helius webhook returns disabled and writes zero new events
    app = create_telemetry_app(config=cfg, db=db)
    tc = TestClient(app)
    resp = tc.post(
        "/api/webhooks/helius",
        headers={"Authorization": "Bearer secret-token"},
        json=[{"type": "SWAP", "signature": "new_sig_blocked", "feePayer": "WhaleHist1", "timestamp": int(now_dt.timestamp()), "nativeTransfers": [{"fromUserAccount": "WhaleHist1", "toUserAccount": "Pool", "amount": int(2.0 * LAMPORTS_PER_SOL)}], "tokenTransfers": [{"fromUserAccount": "Pool", "toUserAccount": "WhaleHist1", "mint": "NewWhaleMint", "tokenAmount": 100.0}]}],
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"
    assert resp.json()["recorded"] == 0
    assert len(db.get_recent_whale_events(limit=10)) == 1  # Historical record intact, new event not written

    # 2. Scanner does NOT inject historical whale mints into DexScreener discovery batch
    seen_urls: list[str] = []

    def _scan_handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"data": [], "included": []} if "geckoterminal" in str(request.url) else [])

    async with httpx.AsyncClient(transport=httpx.MockTransport(_scan_handler)) as client:
        await scanner_node({"focus_state": FocusState.SCANNING.value, "daily_start_sol": 1.5, "daily_pnl_sol": 0.0, "chart_positions": []}, config=cfg, db=db, http_client=client, now=now_dt)
    assert not any("HistWhaleMint111111111111111111111111111111" in u for u in seen_urls)

    # 3. Alpha prompt does NOT inject whale context for historical whale mint
    dec = _StubDecision({
        "t0_narrative_strength": {"type": "score", "score": 3.0, "confidence": 0.8},
        "t0_momentum_quality": {"type": "score", "score": 3.0, "confidence": 0.8},
        "t0_is_copycat_vamp": {"type": "noul", "noul": 0.2, "confidence": 0.8},
        "t0_action": {"type": "choice", "choice": "WATCH", "confidence": 0.7},
    })
    await alpha_node(
        {"focus_state": FocusState.SCANNING.value, "sol_balance": 1.5, "safe_candidates": [{"mint": "HistWhaleMint111111111111111111111111111111", "symbol": "HWM", "added_at": (now_dt - timedelta(minutes=5)).isoformat(), "candles_5m": []}]},
        config=cfg, db=db, decision_client=dec, now=now_dt,  # type: ignore[arg-type]
    )
    assert "whale_buy_count=" not in dec.last_input
    assert tc.get("/api/telemetry").status_code == 200
    db.close()


@pytest.mark.asyncio
async def test_end_to_end_breakout_ignition_paper_lifecycle_partial_and_final_exit(tmp_path):
    """F. Full LangGraph paper lifecycle: Scanner -> Safety -> Alpha (BREAKOUT_IGNITION) -> BUY -> PARTIAL_SELL -> SELL."""
    clear_price_backoff()
    clear_narrative_cache()
    clear_buy_reservations()
    clear_sell_guard()
    clear_geckoterminal_cache()

    db = AgentDatabase(db_path=str(tmp_path / "e2e_bo.duckdb"), starting_sol=1.5)
    cfg = _isolation_cfg(ENABLE_JUPITER_DISCOVERY=False, MIN_WATCHLIST_SOAK_MINUTES=0.0)
    now_dt = datetime.now(timezone.utc)
    bo_mint = "MintE2EBreakout1111111111111111111111111111"
    pair_addr = "PairE2EBreakout1111111111111111111111111111"
    bo_candles = _make_breakout_candles(count=18)
    ohlcv_rows = [[int(c["timestamp"]), c["open"], c["high"], c["low"], c["close"], c["volume"]] for c in bo_candles]
    phase = {"step": "entry"}  # entry -> partial -> final

    def _e2e_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "geckoterminal" in url and "ohlcv" in url:
            return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": ohlcv_rows}}})
        if "geckoterminal" in url:
            return httpx.Response(200, json={"data": [], "included": []})
        if "/token-boosts/latest/v1" in url:
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": bo_mint, "totalAmount": 100}])
        if "/token-boosts/top/v1" in url or "/token-profiles/" in url or "/community-takeovers/" in url:
            return httpx.Response(200, json=[])
        if "/tokens/v1/solana/" in url:
            px_native = "0.50" if phase["step"] == "entry" else ("0.59" if phase["step"] == "partial" else "0.42")
            return httpx.Response(200, json=[{
                "chainId": "solana", "pairAddress": pair_addr, "dexId": "raydium",
                "baseToken": {"address": bo_mint, "symbol": "E2EBO", "name": "E2E Breakout"},
                "priceUsd": "1.12", "priceNative": px_native, "marketCap": 650_000.0,
                "liquidity": {"usd": 85_000.0}, "volume": {"h1": 180_000.0, "m5": 35_000.0},
                "txns": {"m5": {"buys": 32, "sells": 10}, "h1": {"buys": 180, "sells": 90}},
                "priceChange": {"h1": 18.0, "m5": 5.5},
                "pairCreatedAt": int((now_dt - timedelta(hours=3)).timestamp() * 1000),
            }])
        if "rugcheck.xyz" in url:
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        if "/tokens/v2/search" in url:
            return httpx.Response(200, json=[{"id": bo_mint, "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "topHoldersPercentage": 15.46}}])
        if "/ultra/v1/shield" in url:
            return httpx.Response(200, json={"warnings": {bo_mint: []}})
        if "/swap/v2/order" in url:
            if f"inputMint={SOL_MINT}" in url:
                return httpx.Response(200, json={"outAmount": "1000000", "priceImpactPct": "-0.3", "transaction": None})
            if phase["step"] == "partial":
                amt = 0.295 if "amount=500000" in url else 0.590
                return httpx.Response(200, json={"outAmount": str(int(amt * LAMPORTS_PER_SOL)), "priceImpactPct": "-0.3", "transaction": None})
            return httpx.Response(200, json={"outAmount": str(int(0.210 * LAMPORTS_PER_SOL)), "priceImpactPct": "-0.4", "transaction": None})
        return httpx.Response(404)

    dec = _StubDecision({
        "t0_narrative_strength": {"type": "score", "score": 4.0, "confidence": 0.88},
        "t0_momentum_quality": {"type": "score", "score": 4.3, "confidence": 0.90},
        "t0_is_copycat_vamp": {"type": "noul", "noul": 0.12, "confidence": 0.92},
        "t0_action": {"type": "choice", "choice": "BUY", "confidence": 0.88},
    })

    async with httpx.AsyncClient(transport=httpx.MockTransport(_e2e_handler)) as client:
        graph = build_agent_graph(config=cfg, db=db, decision_client=dec, http_client=client)  # type: ignore[arg-type]

        # Tick 1: Scanner -> SafetyGate -> Alpha (BREAKOUT_IGNITION) -> Executor BUY
        st0 = db.get_state()
        r1 = await graph.ainvoke({"focus_state": st0["focus_state"], "sol_balance": st0["sol_balance"], "daily_start_sol": st0["daily_start_sol"], "daily_pnl_sol": st0["daily_pnl_sol"], "chart_positions": [], "whale_positions": []})
        assert len(r1["chart_positions"]) == 1
        assert r1["chart_positions"][0]["strategy"] == "BREAKOUT_IGNITION"

        # Tick 2: +18% triggers PARTIAL_TP_50PCT -> 50% PARTIAL_SELL
        phase["step"] = "partial"
        st1 = db.get_state()
        await graph.ainvoke({"focus_state": st1["focus_state"], "sol_balance": st1["sol_balance"], "daily_start_sol": st1["daily_start_sol"], "daily_pnl_sol": st1["daily_pnl_sol"], "active_position": st1["active_position"], "chart_positions": st1["chart_positions"], "whale_positions": []})
        st2 = db.get_state()
        assert len(st2["chart_positions"]) == 1 and st2["chart_positions"][0]["partial_tp_taken"] is True

        # Tick 3: Runner drops -16% vs remaining 0.25 basis -> HARD_STOP_LOSS final SELL
        phase["step"] = "final"
        await graph.ainvoke({"focus_state": st2["focus_state"], "sol_balance": st2["sol_balance"], "daily_start_sol": st2["daily_start_sol"], "daily_pnl_sol": st2["daily_pnl_sol"], "active_position": st2["active_position"], "chart_positions": st2["chart_positions"], "whale_positions": []})

    assert db.get_state()["chart_positions"] == []
    trade_rows = db._conn.execute("SELECT side, strategy FROM trades ORDER BY id ASC").fetchall()
    assert trade_rows == [("BUY", "BREAKOUT_IGNITION"), ("PARTIAL_SELL", "BREAKOUT_IGNITION"), ("SELL", "BREAKOUT_IGNITION")]
    db.close()
