"""Unit tests for 5-slot watchlist, 90s OHLCV caching/backoff, and Scalper Expert Entry Engine."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from alpha_node import alpha_node
from config import AgentConfig
from db import AgentDatabase
from ohlcv_client import clear_ohlcv_cache, fetch_ohlcv_5m, fetch_ohlcv_batch
from scalper_entry_engine import evaluate_scalper_entry
from scanner_node import clear_geckoterminal_cache, scanner_node
from state import FocusState
from telemetry_api import create_telemetry_app


class StubDecisionClient:
    def __init__(self, answers: dict[str, dict[str, Any]]):
        self.answers = answers
        self.last_input = ""

    async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        self.last_input = input_text
        return self.answers


def make_sweep_reclaim_candles() -> list[dict[str, float]]:
    """15 x 5m candles: 1h high at 1.30, prior 10-candle swing low at 1.00,
    candle -2 wicks to 0.96 (< 1.00) and reclaims 1.04, current candle green at 1.08
    (16.9% below 1h high, RSI ~50)."""
    rows = [
        # (open, high, low, close)
        (1.20, 1.30, 1.15, 1.22),
        (1.22, 1.25, 1.12, 1.15),
        (1.15, 1.18, 1.08, 1.10),
        (1.10, 1.14, 1.04, 1.12),
        (1.12, 1.16, 1.06, 1.08),
        (1.08, 1.12, 1.02, 1.05),
        (1.05, 1.10, 1.00, 1.06),  # prior swing low = 1.00
        (1.06, 1.11, 1.03, 1.09),
        (1.09, 1.12, 1.04, 1.05),
        (1.05, 1.08, 1.01, 1.03),
        (1.03, 1.06, 1.01, 1.02),
        (1.02, 1.05, 1.00, 1.01),
        # Last 3 candles: liquidity sweep below 1.00 & reclaim + green confirmation
        (1.01, 1.03, 0.99, 1.01),
        (1.01, 1.05, 0.95, 1.04),  # wicked below 1.00, closed 1.04 > 1.00
        (1.04, 1.10, 1.03, 1.08),  # green confirmation, 16.9% below 1.30 1h high
    ]
    return [
        {"timestamp": 1_700_000_000 + i * 300, "open": o, "high": h, "low": l, "close": c, "volume": 1000.0}
        for i, (o, h, l, c) in enumerate(rows)
    ]


def make_displacement_retest_candles() -> list[dict[str, float]]:
    """16 x 5m candles: quiet consolidation, strong bullish displacement (+0.30 body >> 1.5x avg),
    controlled -18% pullback into displacement midpoint / 9 EMA, RSI cooled into 42-65,
    and green curl closing above prior candle's high (>5% below 1h high)."""
    rows = [
        # 10 quiet candles with slight downward drift (avg body ~ 0.02) so RSI starts ~45
        (1.08, 1.10, 1.05, 1.06),
        (1.06, 1.08, 1.03, 1.05),
        (1.05, 1.07, 1.02, 1.03),
        (1.03, 1.06, 1.01, 1.04),
        (1.04, 1.05, 1.00, 1.02),
        (1.02, 1.04, 0.99, 1.01),
        (1.01, 1.03, 0.98, 1.00),
        (1.00, 1.03, 0.98, 1.01),
        (1.01, 1.02, 0.98, 0.99),
        (0.99, 1.01, 0.97, 1.00),
        # Bullish displacement candle (body = 0.36 >> 1.5 * 0.015, midpoint = 1.18)
        (1.00, 1.40, 0.99, 1.36),
        # Follow-through high = 1.44 (1h high = 1.44)
        (1.36, 1.44, 1.30, 1.33),
        # Controlled pullback into midpoint (1.18) / 9 EMA: low = 1.14 (-20.8% from 1.44)
        (1.33, 1.34, 1.22, 1.24),
        (1.24, 1.26, 1.14, 1.17),
        (1.17, 1.20, 1.15, 1.18),
        # Green curl closing above prior candle's high (1.20), still 14.6% below 1.44 1h high, RSI cooled in [42, 65]
        (1.18, 1.24, 1.17, 1.23),
    ]
    return [
        {"timestamp": 1_700_000_000 + i * 300, "open": o, "high": h, "low": l, "close": c, "volume": 1500.0}
        for i, (o, h, l, c) in enumerate(rows)
    ]


def make_overbought_spike_candles() -> list[dict[str, float]]:
    """15 candles in a vertical green spike: RSI > 70 and price right at 1h high."""
    candles = []
    price = 1.00
    for i in range(15):
        nxt = round(price * 1.04, 4)
        candles.append(
            {
                "timestamp": 1_700_000_000 + i * 300,
                "open": price,
                "high": round(nxt * 1.005, 4),
                "low": round(price * 0.995, 4),
                "close": nxt,
                "volume": 2000.0,
            }
        )
        price = nxt
    return candles


@pytest.mark.asyncio
async def test_five_slot_watchlist_capacity_and_pair_address_persistence(tmp_path):
    clear_geckoterminal_cache()
    db = AgentDatabase(db_path=str(tmp_path / "five_slots.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None, HUNTING_LANE="momentum_runners")
    assert cfg.MAX_WATCH_BATCH == 5

    now_dt = datetime(2026, 11, 19, 18, 0, 0, tzinfo=timezone.utc)
    now_ms = int(now_dt.timestamp() * 1000)
    old_ms = now_ms - (2 * 3600 * 1000)

    pairs = [
        {
            "chainId": "solana",
            "pairAddress": f"PoolAddr{i}",
            "baseToken": {"address": f"Mint{i}", "symbol": f"TK{i}", "name": f"Token {i}"},
            "marketCap": 400_000,
            "liquidity": {"usd": 60_000},
            "volume": {"h1": 500_000 - i * 40_000, "m5": 15_000},
            "txns": {"m5": {"buys": 30, "sells": 10}},
            "pairCreatedAt": old_ms,
        }
        for i in range(7)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "geckoterminal.com" in url:
            return httpx.Response(200, json={"data": []})
        if "/token-boosts/latest/v1" in url:
            return httpx.Response(
                200,
                json=[{"chainId": "solana", "tokenAddress": f"Mint{i}"} for i in range(7)],
            )
        if "/token-boosts/top/v1" in url or "/token-profiles/latest/v1" in url or "/community-takeovers/latest/v1" in url:
            return httpx.Response(200, json=[])
        if "/tokens/v1/solana/" in url:
            return httpx.Response(200, json=pairs)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await scanner_node(
            {"focus_state": FocusState.SCANNING.value, "daily_start_sol": 0.7, "daily_pnl_sol": 0.0},
            config=cfg,
            db=db,
            http_client=client,
            now_ms=now_ms,
            now=now_dt,
        )

    assert len(out["raw_candidates"]) == 5
    wl = db.get_watchlist()
    assert len(wl) == 5
    assert [w["mint"] for w in wl] == ["Mint0", "Mint1", "Mint2", "Mint3", "Mint4"]
    assert [w["pair_address"] for w in wl] == ["PoolAddr0", "PoolAddr1", "PoolAddr2", "PoolAddr3", "PoolAddr4"]
    db.close()
    clear_geckoterminal_cache()


@pytest.mark.asyncio
async def test_ohlcv_90s_cache_and_429_exponential_backoff():
    clear_ohlcv_cache()
    cfg = AgentConfig(_env_file=None, OHLCV_CACHE_SECONDS=90.0, OHLCV_PACING_DELAY_SECONDS=0.0)
    calls = {"count": 0, "mode": "ok"}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        assert "/networks/solana/pools/PoolXYZ/ohlcv/minute" in str(request.url)
        assert request.url.params.get("aggregate") == "5"
        assert request.url.params.get("limit") == "30"
        assert request.url.params.get("currency") == "usd"
        if calls["mode"] == "429":
            return httpx.Response(429, json={"error": "rate limit"})
        return httpx.Response(
            200,
            json={
                "data": {
                    "attributes": {
                        "ohlcv_list": [
                            [1700000600, 1.05, 1.10, 1.02, 1.08, 500.0],
                            [1700000300, 1.00, 1.06, 0.99, 1.05, 400.0],
                        ]
                    }
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # T=1000: cold fetch -> sorted oldest-first
        c1, did1 = await fetch_ohlcv_5m("PoolXYZ", cfg, client, now_ts=1000.0)
        assert did1 is True
        assert len(c1) == 2
        assert c1[0]["timestamp"] < c1[1]["timestamp"]
        assert calls["count"] == 1

        # T=1060 (< 90s): cached hit -> zero network calls
        c2, did2 = await fetch_ohlcv_5m("PoolXYZ", cfg, client, now_ts=1060.0)
        assert did2 is False
        assert len(c2) == 2
        assert calls["count"] == 1

        # T=1095 (> 90s) + HTTP 429 -> graceful fallback to cached candles & 30s backoff
        calls["mode"] = "429"
        c3, did3 = await fetch_ohlcv_5m("PoolXYZ", cfg, client, now_ts=1095.0)
        assert did3 is True
        assert len(c3) == 2
        assert calls["count"] == 2

        # T=1110 (within 30s backoff): skipped cleanly
        batch = await fetch_ohlcv_batch(["PoolXYZ"], cfg, client, now_ts=1110.0)
        assert len(batch["PoolXYZ"]) == 2
        assert calls["count"] == 2

    clear_ohlcv_cache()


def test_scalper_blocks_tick1_soak_and_overbought_spike():
    from tests.test_e3b_cache_provenance import make_verified_ohlcv_result
    cfg = AgentConfig(_env_file=None)
    good_flow = {"buys_5m": 30, "sells_5m": 10}  # 75% buys, n=40

    # (c) Block tick-1 chase via 3m soak even on a valid SWEEP_RECLAIM
    c_sr = make_sweep_reclaim_candles()
    t_sr = c_sr[-1]["timestamp"] + 300.0
    res_soak = evaluate_scalper_entry(
        c_sr,
        good_flow,
        time_in_slot_min=0.2,
        config=cfg,
        decision_timestamp=t_sr,
        ohlcv_provenance=make_verified_ohlcv_result(c_sr, last_ok_at=t_sr),
    )
    assert res_soak.eligible is False
    assert res_soak.rejection_reason is not None
    assert "soak 0.2m < 3m" in res_soak.rejection_reason

    # (d) Block overbought vertical spike (RSI > 70 and within 5% of 1h high)
    c_sp = make_overbought_spike_candles()
    t_sp = c_sp[-1]["timestamp"] + 300.0
    res_spike = evaluate_scalper_entry(
        c_sp,
        good_flow,
        time_in_slot_min=5.0,
        config=cfg,
        decision_timestamp=t_sp,
        ohlcv_provenance=make_verified_ohlcv_result(c_sp, last_ok_at=t_sp),
    )
    assert res_spike.eligible is False
    assert res_spike.rsi_5m > 70.0
    assert res_spike.dist_from_1h_high_pct < 0.05
    assert "RSI=" in (res_spike.rejection_reason or "")
    assert "near 1h high" in (res_spike.rejection_reason or "")


def test_scalper_approves_sweep_reclaim_and_displacement_retest_with_order_flow():
    from tests.test_e3b_cache_provenance import make_verified_ohlcv_result
    cfg = AgentConfig(_env_file=None)
    good_flow = {"txns": {"m5": {"buys": 24, "sells": 12}}}  # 66.7% >= 58%, n=36 >= 15
    weak_flow = {"txns": {"m5": {"buys": 8, "sells": 10}}}  # 44% < 58%

    # SWEEP_RECLAIM approved with strong order flow
    c_sr = make_sweep_reclaim_candles()
    t_sr = c_sr[-1]["timestamp"] + 300.0
    res_sweep = evaluate_scalper_entry(
        c_sr,
        good_flow,
        time_in_slot_min=4.5,
        config=cfg,
        decision_timestamp=t_sr,
        ohlcv_provenance=make_verified_ohlcv_result(c_sr, last_ok_at=t_sr),
    )
    assert res_sweep.eligible is True
    assert res_sweep.setup_type == "SWEEP_RECLAIM"
    assert res_sweep.rsi_5m <= 70.0
    assert res_sweep.dist_from_1h_high_pct >= 0.05
    assert res_sweep.rejection_reason is None

    # DISPLACEMENT_RETEST approved with strong order flow
    c_dr = make_displacement_retest_candles()
    t_dr = c_dr[-1]["timestamp"] + 300.0
    res_disp = evaluate_scalper_entry(
        c_dr,
        good_flow,
        time_in_slot_min=6.0,
        config=cfg,
        decision_timestamp=t_dr,
        ohlcv_provenance=make_verified_ohlcv_result(c_dr, last_ok_at=t_dr),
    )
    assert res_disp.eligible is True
    assert res_disp.setup_type == "DISPLACEMENT_RETEST"
    assert 42.0 <= res_disp.rsi_5m <= 65.0
    assert res_disp.rejection_reason is None

    # Weak order flow blocks even a valid SWEEP_RECLAIM
    res_weak = evaluate_scalper_entry(
        c_sr,
        weak_flow,
        time_in_slot_min=5.0,
        config=cfg,
        decision_timestamp=t_sr,
        ohlcv_provenance=make_verified_ohlcv_result(c_sr, last_ok_at=t_sr),
    )
    assert res_weak.eligible is False
    assert "buy_ratio" in (res_weak.rejection_reason or "")


@pytest.mark.asyncio
async def test_alpha_blocks_unsoaked_spike_logs_reason_and_exposes_telemetry(tmp_path, caplog):
    db = AgentDatabase(db_path=str(tmp_path / "alpha_scalper.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None)
    now_dt = datetime.now(timezone.utc)
    just_added = (now_dt - timedelta(seconds=12)).isoformat()  # 0.2m soak

    db.upsert_watchlist_entry(
        mint="MintWiggles",
        symbol="WIGGLES",
        pair_address="PoolWiggles",
        candidate={"mint": "MintWiggles", "symbol": "WIGGLES", "pair_address": "PoolWiggles"},
        added_at=just_added,
        safety_passed=True,
    )

    dec = StubDecisionClient(
        {
            "t0_narrative_strength": {"type": "score", "score": 4.2, "confidence": 0.88},
            "t0_momentum_quality": {"type": "score", "score": 4.3, "confidence": 0.89},
            "t0_is_copycat_vamp": {"type": "noul", "noul": 0.15, "confidence": 0.92},
            "t0_action": {"type": "choice", "choice": "BUY", "confidence": 0.85},
        }
    )

    c_w = make_overbought_spike_candles()
    cand = {
        "mint": "MintWiggles",
        "symbol": "WIGGLES",
        "pair_address": "PoolWiggles",
        "liquidity_usd": 80_000,
        "buys_5m": 30,
        "sells_5m": 10,
        "added_at": just_added,
        "candles_5m": c_w,
        "_ohlcv_5m_result": __import__("tests.test_e3b_cache_provenance", fromlist=["make_verified_ohlcv_result"]).make_verified_ohlcv_result(c_w, last_ok_at=now_dt.timestamp()),
    }

    with caplog.at_level(logging.INFO, logger="alpha_node"):
        out = await alpha_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 0.7, "safe_candidates": [cand]},
            config=cfg,
            db=db,
            decision_client=dec,  # type: ignore[arg-type]
            now=now_dt,
        )

    assert "scalper_setup=" in dec.last_input
    assert "rsi_5m=" in dec.last_input
    assert "dist_from_1h_high=" in dec.last_input
    assert out["pending_order"] is None
    assert len(db.get_watchlist()) == 1  # retained in watchlist
    assert any(
        "[Alpha] $WIGGLES | BLOCKED (" in r.message and "soak 0.2m < 3m" in r.message
        for r in caplog.records
    )

    # Telemetry exposes rsi_5m, setup_type, and scalper_eligible per slot
    app = create_telemetry_app(config=cfg, db=db)
    resp = TestClient(app).get("/api/telemetry")
    assert resp.status_code == 200
    slot = resp.json()["active_watchlist"][0]
    assert slot["symbol"] == "WIGGLES"
    assert slot["rsi_5m"] > 70.0
    assert slot["setup_type"] == "NONE"
    assert slot["scalper_eligible"] is False
    db.close()


@pytest.mark.asyncio
async def test_ohlcv_no_1m_fallback_on_429_global_backoff_and_whale_radar_cached_meter(tmp_path):
    """Verify:
    (a) A 5m 429 does NOT trigger a 1m fallback call (while a 200 OK with < 12 bars does).
    (b) A 429 on Pool A activates global backoff for Pool B and serves stale cached candles.
    (c) Whale Radar confluence rows include breakout_telemetry from the shared cache even when COOLDOWN or STALE > 60s.
    """
    from ohlcv_client import (
        cache_breakout_telemetry,
        fetch_ohlcv_1m_batch,
        get_global_backoff_until,
        should_fetch_1m_fallback,
    )

    clear_ohlcv_cache()
    cfg = AgentConfig(_env_file=None, OHLCV_CACHE_SECONDS=90.0, OHLCV_PACING_DELAY_SECONDS=0.0)
    req_log: list[tuple[str, str]] = []
    mode = {"pool_a": "200", "pool_b": "200"}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        agg = request.url.params.get("aggregate") or ""
        pool = "PoolA" if "PoolA" in url else ("PoolB" if "PoolB" in url else "PoolFresh")
        req_log.append((pool, agg))
        if pool == "PoolA" and mode["pool_a"] == "429":
            return httpx.Response(429, json={"error": "rate limit"})
        if pool == "PoolFresh":
            # 200 OK with only 4 x 5m candles (< 12) -> SHOULD trigger 1m fallback
            return httpx.Response(
                200,
                json={"data": {"attributes": {"ohlcv_list": [[1700000000 + i * 300, 1.0, 1.1, 0.9, 1.05, 500.0] for i in range(4)]}}},
            )
        return httpx.Response(
            200,
            json={"data": {"attributes": {"ohlcv_list": [[1700000000 + i * 300, 1.0, 1.1, 0.9, 1.05, 500.0] for i in range(6)]}}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # Warm cache for PoolB so stale-while-revalidate has cached candles to serve during global backoff
        c_b0, did_b0 = await fetch_ohlcv_5m("PoolB", cfg, client, now_ts=1000.0)
        assert did_b0 is True and len(c_b0) == 6

        # (a) PoolA returns 429 on 5m -> MUST NOT trigger 1m fallback!
        mode["pool_a"] = "429"
        req_log.clear()
        ohlcv_5m = await fetch_ohlcv_batch(["PoolA", "PoolB"], cfg, client, now_ts=1200.0)
        assert ("PoolA", "5") in req_log
        # (b) PoolB was paused by global 429 backoff from PoolA and served its 6 cached candles!
        assert ("PoolB", "5") not in req_log
        assert len(ohlcv_5m["PoolB"]) == 6
        assert get_global_backoff_until() == pytest.approx(1230.0)

        # Calling fetch_ohlcv_1m_batch after PoolA's 5m 429 MUST make zero 1m calls!
        assert should_fetch_1m_fallback("PoolA", ohlcv_5m["PoolA"], now_ts=1200.0) is False
        ohlcv_1m = await fetch_ohlcv_1m_batch(["PoolA"], cfg, client, now_ts=1200.0)
        assert ("PoolA", "1") not in req_log
        assert ohlcv_1m["PoolA"] == []

        # After global backoff expires (T=1235 > 1230), PoolFresh 5m returns 200 OK with 4 bars -> 1m fallback DOES run
        req_log.clear()
        fresh_5m = await fetch_ohlcv_batch(["PoolFresh"], cfg, client, now_ts=1235.0)
        assert should_fetch_1m_fallback("PoolFresh", fresh_5m["PoolFresh"], now_ts=1235.0) is True
        fresh_1m = await fetch_ohlcv_1m_batch(["PoolFresh"], cfg, client, now_ts=1235.0)
        assert req_log == [("PoolFresh", "5"), ("PoolFresh", "1")]
        assert len(fresh_1m["PoolFresh"]) == 4

    # (c) Whale Radar confluence rows include breakout_telemetry from shared cache even when COOLDOWN or STALE > 60s
    db = AgentDatabase(db_path=str(tmp_path / "whale_meter_cache.duckdb"), starting_sol=1.0)
    now_dt = datetime.now(timezone.utc)
    mint_stale = "MintWhaleStaleMeter111111111111111111111111"
    db.record_whale_event(
        signature="sig-stale-1",
        wallet="4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk",
        mint=mint_stale,
        side="BUY",
        sol_amount=2.5,
        token_amount=100_000.0,
        timestamp_utc=(now_dt - timedelta(seconds=120)).isoformat(),
    )
    db.recompute_whale_signal(mint_stale, ttl_minutes=30, now=now_dt)
    db.upsert_whale_decision(mint_stale, "SKIPPED", "STALE > 60s", updated_at=now_dt.isoformat())

    cached_meter = {
        "score": 82,
        "state": "IGNITION",
        "timeframe": "5m",
        "vol_ratio": 2.1,
        "ema_ext_pct": 3.4,
        "passed_gates": ["COIL", "RES", "VOL_SPIKE"],
        "failed_gates": [],
    }
    cache_breakout_telemetry(mint_stale, cached_meter, now_ts=now_dt.timestamp())

    tc = TestClient(create_telemetry_app(config=cfg, db=db))
    tel = tc.get("/api/telemetry").json()
    sigs = {s["mint"]: s for s in tel["whale_discovery"]["active_signals"]}
    assert sigs[mint_stale]["agent_reason"] == "STALE > 60s"
    assert sigs[mint_stale]["breakout_telemetry"]["score"] == 82
    assert sigs[mint_stale]["breakout_telemetry"]["state"] == "IGNITION"
    db.close()
    clear_ohlcv_cache()
