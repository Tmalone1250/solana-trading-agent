"""Unit tests for Leg 2 Predictor, safety-gate caching, and Leg 2 fast-track entries."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from alpha_node import alpha_node
from config import LAMPORTS_PER_SOL, AgentConfig
from db import AgentDatabase
from leg2_predictor import compute_leg2_score
from position_monitor_node import position_monitor_node
from safety_gate_node import safety_gate_node
from state import ActivePosition, FocusState
from telemetry_api import create_telemetry_app


class StubDecisionClient:
    def __init__(self, answers: dict[str, dict[str, Any]]):
        self.answers = answers
        self.last_input: str = ""

    async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        self.last_input = input_text
        return self.answers


def test_leg2_predictor_high_score_bounce():
    pair = {
        "marketCap": 600_000,
        "liquidity": {"usd": 80_000},  # liq/mc = 0.133 >= 0.10, drop = 0% -> 20/20
        "volume": {"m5": 15_000, "h1": 100_000},  # (15k*12)/100k = 1.8 >= 1.3 -> 25/25
        "priceChange": {"h1": 28.0, "m5": 8.5},  # h1 >= 15%, m5 in [2,25] -> 25/25
        "txns": {"m5": {"buys": 32, "sells": 18}},  # 64% >= 62%, n=50 >= 15 -> 30/30
    }
    res = compute_leg2_score(
        pair,
        tracked_peak_mc=680_000,
        tracked_low_mc=540_000,
        entry_liquidity_usd=80_000,
    )
    assert res.buy_absorption == 30.0
    assert res.volume_acceleration == 25.0
    assert res.bounce_structure == 25.0
    assert res.liquidity_stability == 20.0
    assert res.score == 100.0


def test_leg2_predictor_dead_cat_and_low_volume_bounce():
    pair = {
        "marketCap": 500_000,
        "liquidity": {"usd": 20_000},  # liq/mc = 0.04, dropped 20% from 25k
        "volume": {"m5": 500, "h1": 100_000},  # accel = 0.06
        "priceChange": {"h1": -12.0, "m5": -4.0},  # negative 5m -> 0 bounce
        "txns": {"m5": {"buys": 8, "sells": 4}},  # n=12 < 15 -> 0 buy absorption
    }
    res = compute_leg2_score(
        pair,
        tracked_peak_mc=700_000,
        tracked_low_mc=500_000,
        entry_liquidity_usd=25_000,
    )
    assert res.buy_absorption == 0.0
    assert res.bounce_structure == 0.0
    assert res.volume_acceleration < 2.0
    assert res.liquidity_stability < 5.0
    assert res.score < 10.0


@pytest.mark.asyncio
async def test_safety_gate_caches_pass_for_retained_watchlist_slots(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "safety_cache.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None, JUPITER_API_KEY="test-key")
    now_iso = datetime.now(timezone.utc).isoformat()

    db.upsert_watchlist_entry(
        mint="MintCached1",
        symbol="CACHE",
        candidate={"mint": "MintCached1", "symbol": "CACHE", "market_cap_usd": 400_000, "liquidity_usd": 50_000},
        added_at=now_iso,
        safety_passed=False,
    )

    http_calls = {"rugcheck": 0, "shield": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "rugcheck.xyz" in url:
            http_calls["rugcheck"] += 1
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        if "/ultra/v1/shield" in url:
            http_calls["shield"] += 1
            return httpx.Response(200, json={"warnings": {"MintCached1": []}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # Cycle 1: uncached -> calls RugCheck and Jupiter Shield once, persists safety_passed=True
        out1 = await safety_gate_node(
            {"focus_state": FocusState.SCANNING.value, "raw_candidates": [{"mint": "MintCached1", "symbol": "CACHE"}]},
            config=cfg,
            db=db,
            http_client=client,
        )
        assert len(out1["safe_candidates"]) == 1
        assert http_calls == {"rugcheck": 1, "shield": 1}
        assert db.get_watchlist()[0]["safety_passed"] is True

        # Cycle 2: retained slot -> skips BOTH RugCheck and Jupiter Shield completely
        out2 = await safety_gate_node(
            {"focus_state": FocusState.SCANNING.value, "raw_candidates": [{"mint": "MintCached1", "symbol": "CACHE"}]},
            config=cfg,
            db=db,
            http_client=client,
        )
        assert len(out2["safe_candidates"]) == 1
        assert out2["safe_candidates"][0]["safety_report"]["cached"] is True
        assert http_calls == {"rugcheck": 1, "shield": 1}

    db.close()


@pytest.mark.asyncio
async def test_alpha_leg2_fast_track_entries_and_telemetry(tmp_path, caplog):
    from tests.test_scalper_entry_engine import make_sweep_reclaim_candles

    db = AgentDatabase(db_path=str(tmp_path / "leg2_fast.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None)
    now_dt = datetime.now(timezone.utc)
    soaked_added = (now_dt - timedelta(minutes=5)).isoformat()
    valid_candles = make_sweep_reclaim_candles()

    # Strong Leg 2 pair (score ~ 100 >= 80)
    strong_cand = {
        "mint": "MintLeg2Fast",
        "symbol": "CURL",
        "market_cap_usd": 500_000,
        "liquidity_usd": 65_000,
        "entry_liquidity_usd": 65_000,
        "volume_1h_usd": 100_000,
        "volume_5m_usd": 15_000,
        "price_change_1h_pct": 25.0,
        "price_change_5m_pct": 7.0,
        "buys_5m": 32,
        "sells_5m": 18,
        "added_at": soaked_added,
        "candles_5m": valid_candles,
    }
    leg2 = compute_leg2_score(strong_cand, tracked_peak_mc=550_000, tracked_low_mc=460_000)
    assert leg2.score >= 80.0

    db.upsert_watchlist_entry(
        mint="MintLeg2Fast",
        symbol="CURL",
        candidate=strong_cand,
        added_at=soaked_added,
        safety_passed=True,
        peak_mc=550_000,
        low_mc=460_000,
        leg2_score=leg2.score,
        leg2_breakdown=leg2.model_dump(),
    )

    # Case 1: action=WATCH with leg2_score >= 80, vamp <= 0.55, mom >= 2.8 -> LEG2_ENTRY fast-track!
    dec_watch = StubDecisionClient(
        {
            "t0_narrative_strength": {"type": "score", "score": 2.2, "confidence": 0.70},
            "t0_momentum_quality": {"type": "score", "score": 2.9, "confidence": 0.75},
            "t0_is_copycat_vamp": {"type": "noul", "noul": 0.40, "confidence": 0.85},
            "t0_action": {"type": "choice", "choice": "WATCH", "confidence": 0.65},
        }
    )

    with caplog.at_level(logging.INFO, logger="alpha_node"):
        out_watch = await alpha_node(
            {
                "focus_state": FocusState.SCANNING.value,
                "sol_balance": 0.7,
                "safe_candidates": [{**strong_cand, "_ohlcv_5m_result": __import__("tests.test_e3b_cache_provenance", fromlist=["make_verified_ohlcv_result"]).make_verified_ohlcv_result(valid_candles, last_ok_at=now_dt.timestamp())}],
            },
            config=cfg,
            db=db,
            decision_client=dec_watch,  # type: ignore[arg-type]
            now=now_dt,
        )

    assert "leg2_predictor=" in dec_watch.last_input
    assert out_watch["pending_order"] is not None
    assert out_watch["pending_order"]["reason"] == "LEG2_ENTRY"
    assert any("[Alpha] $CURL | LEG2_ENTRY (leg2=" in r.message for r in caplog.records)

    # Case 2: action=BUY with conf=0.56 (< 0.60 standard gate, >= 0.55 fast-track) & leg2=75 -> LEG2_ENTRY!
    cand_75_simple = {
        "mint": "MintLeg2Buy75",
        "symbol": "CURL2",
        "liquidity_usd": 65_000,
        "buys_5m": 24,
        "sells_5m": 12,
        "leg2_score": 75.0,
        "leg2_breakdown": {
            "score": 75.0,
            "buy_absorption": 25.0,
            "volume_acceleration": 20.0,
            "bounce_structure": 15.0,
            "liquidity_stability": 15.0,
            "summary": "leg2=75.0/100",
        },
        "added_at": soaked_added,
        "candles_5m": valid_candles,
    }
    dec_buy56 = StubDecisionClient(
        {
            "t0_narrative_strength": {"type": "score", "score": 2.1, "confidence": 0.70},
            "t0_momentum_quality": {"type": "score", "score": 2.85, "confidence": 0.75},
            "t0_is_copycat_vamp": {"type": "noul", "noul": 0.50, "confidence": 0.85},
            "t0_action": {"type": "choice", "choice": "BUY", "confidence": 0.56},
        }
    )
    out_buy = await alpha_node(
        {
            "focus_state": FocusState.SCANNING.value,
            "sol_balance": 0.7,
            "safe_candidates": [{**cand_75_simple, "_ohlcv_5m_result": __import__("tests.test_e3b_cache_provenance", fromlist=["make_verified_ohlcv_result"]).make_verified_ohlcv_result(valid_candles, last_ok_at=now_dt.timestamp())}],
        },
        config=cfg,
        db=db,
        decision_client=dec_buy56,  # type: ignore[arg-type]
        now=now_dt,
    )
    assert out_buy["pending_order"] is not None
    assert out_buy["pending_order"]["reason"] == "LEG2_ENTRY"

    # Verify telemetry exposes leg2_score on active_watchlist
    app = create_telemetry_app(config=cfg, db=db)
    resp = TestClient(app).get("/api/telemetry")
    assert resp.status_code == 200
    wl = resp.json()["active_watchlist"]
    assert len(wl) == 1
    assert wl[0]["leg2_score"] == leg2.score
    assert wl[0]["safety_passed"] is True
    assert wl[0]["peak_mc"] == 550_000.0
    assert wl[0]["low_mc"] == 460_000.0
    db.close()


@pytest.mark.asyncio
async def test_watching_reentry_requires_leg2_score_ge_65(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "watch_leg2.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None)
    dec = StubDecisionClient(
        {
            "leg_2_momentum": {"type": "score", "score": 4.2, "confidence": 0.90},
            "is_dead_cat_bounce": {"type": "noul", "noul": 0.10, "confidence": 0.90},
        }
    )
    watch_pos = ActivePosition(
        mint="MintWatchGate",
        symbol="WGATE",
        entry_sol=0.5,
        token_amount=0,
        entry_liquidity_usd=100_000.0,
        current_sol_value=0.52,
        peak_sol_value=0.60,
        run_peak_sol=0.60,
        retrace_low_sol=0.50,
        reference_token_amount=1_000_000,
        watch_started_at=datetime.now(timezone.utc).isoformat(),
    ).model_dump()

    # Weak Leg 2 pair (score < 65) even though curl (+6%) and 5m buy ratio (65%) pass
    def weak_handler(request: httpx.Request) -> httpx.Response:
        if "/swap/v2/order" in str(request.url):
            return httpx.Response(200, json={"outAmount": str(int(0.53 * LAMPORTS_PER_SOL)), "transaction": None})
        return httpx.Response(
            200,
            json=[
                {
                    "chainId": "solana",
                    "marketCap": 2_000_000,
                    "liquidity": {"usd": 98_000},
                    "volume": {"m5": 1_000, "h1": 200_000},
                    "priceChange": {"h1": 5.0, "m5": -1.0},
                    "txns": {"m5": {"buys": 13, "sells": 7}},
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(weak_handler)) as client:
        out_blocked = await position_monitor_node(
            {"focus_state": FocusState.WATCHING_REENTRY.value, "sol_balance": 0.75, "daily_pnl_sol": 0.05, "active_position": watch_pos},
            config=cfg,
            db=db,
            decision_client=dec,  # type: ignore[arg-type]
            http_client=client,
        )
    assert out_blocked["pending_order"] is None
    assert out_blocked["monitor_event"] == "WATCHING_REENTRY"
    db.close()
