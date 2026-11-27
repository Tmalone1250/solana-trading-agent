"""Unit and integration tests for Phase E3A — Closed-Candle Eligibility & decision_timestamp propagation."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from alpha_node import alpha_node
from breakout_ignition import (
    _aggregate_to_15m,
    _is_htf_15m_bullish,
    detect_breakout_ignition,
    normalize_and_filter_candles,
)
from config import AgentConfig
from db import AgentDatabase
from ohlcv_client import (
    clear_ohlcv_cache,
    fetch_ohlcv_1m_batch,
    fetch_ohlcv_5m,
    fetch_ohlcv_batch,
    should_fetch_1m_fallback,
)
from scalper_entry_engine import evaluate_scalper_entry
from state import FocusState
from tests.test_breakout_ignition import _make_breakout_candles, _strong_flow_pair
from tests.test_e3b_cache_provenance import make_verified_ohlcv_result


class _StubDecisionClient:
    async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for k in questions:
            if k.endswith("_narrative_strength"):
                out[k] = {"score": 4.2}
            elif k.endswith("_momentum_quality"):
                out[k] = {"score": 4.1}
            elif k.endswith("_is_copycat_vamp"):
                out[k] = {"noul": 0.05}
            elif k.endswith("_action"):
                out[k] = {"choice": "BUY", "confidence": 0.85}
        return out


def test_t1_forming_5m_candle_dropped_and_blocks_breakout():
    candles = _make_breakout_candles(count=18, bar_seconds=300)
    last_ts = candles[-1]["timestamp"]
    # 150s into the last 5m bar -> forming!
    decision_ts = last_ts + 150.0
    res = evaluate_scalper_entry(
        candles, _strong_flow_pair(1.12), time_in_slot_min=5.0, decision_timestamp=decision_ts,
        ohlcv_provenance=make_verified_ohlcv_result(candles, last_ok_at=decision_ts),
    )
    assert res.eligible is False
    assert res.setup_type != "BREAKOUT_IGNITION"
    assert res.breakout_metrics is not None
    assert res.breakout_metrics["detected"] is False


def test_t2_forming_1m_candle_dropped_and_blocks_breakout():
    bars_1m = _make_breakout_candles(count=20, bar_seconds=60)
    last_ts = bars_1m[-1]["timestamp"]
    decision_ts = last_ts + 30.0
    res = evaluate_scalper_entry(
        [],
        _strong_flow_pair(1.12),
        time_in_slot_min=5.0,
        candles_1m=bars_1m,
        decision_timestamp=decision_ts,
        ohlcv_provenance=make_verified_ohlcv_result(bars_1m, last_ok_at=decision_ts),
    )
    assert res.eligible is False
    assert res.setup_type == "NONE"
    assert res.breakout_metrics is not None
    assert res.breakout_metrics["detected"] is False


def test_t3a_exact_interval_boundary_is_eligible():
    candles_5m = _make_breakout_candles(count=18, bar_seconds=300)
    t_5m = candles_5m[-1]["timestamp"] + 300.0
    res_5m = evaluate_scalper_entry(
        candles_5m,
        _strong_flow_pair(1.12),
        time_in_slot_min=5.0,
        decision_timestamp=t_5m,
        ohlcv_provenance=make_verified_ohlcv_result(candles_5m, last_ok_at=t_5m),
    )
    assert res_5m.eligible is True
    assert res_5m.setup_type == "BREAKOUT_IGNITION"

    candles_1m = _make_breakout_candles(count=20, bar_seconds=60)
    t_1m = candles_1m[-1]["timestamp"] + 60.0
    res_1m = evaluate_scalper_entry(
        [],
        _strong_flow_pair(1.12),
        time_in_slot_min=5.0,
        candles_1m=candles_1m,
        decision_timestamp=t_1m,
        ohlcv_provenance=make_verified_ohlcv_result(candles_1m, last_ok_at=t_1m),
    )
    assert res_1m.eligible is True
    assert res_1m.setup_type == "BREAKOUT_IGNITION"


def test_t3b_one_second_before_close_is_ineligible():
    candles_5m = _make_breakout_candles(count=18, bar_seconds=300)
    t_5m = candles_5m[-1]["timestamp"] + 299.0
    res_5m = evaluate_scalper_entry(
        candles_5m,
        _strong_flow_pair(1.12),
        time_in_slot_min=5.0,
        decision_timestamp=t_5m,
        ohlcv_provenance=make_verified_ohlcv_result(candles_5m, last_ok_at=t_5m),
    )
    assert res_5m.eligible is False
    assert res_5m.setup_type != "BREAKOUT_IGNITION"

    candles_1m = _make_breakout_candles(count=20, bar_seconds=60)
    t_1m = candles_1m[-1]["timestamp"] + 59.0
    res_1m = evaluate_scalper_entry(
        [],
        _strong_flow_pair(1.12),
        time_in_slot_min=5.0,
        candles_1m=candles_1m,
        decision_timestamp=t_1m,
        ohlcv_provenance=make_verified_ohlcv_result(candles_1m, last_ok_at=t_1m),
    )
    assert res_1m.eligible is False
    assert res_1m.setup_type == "NONE"


def test_t4_stale_but_closed_candles_retained_by_closed_filter():
    candles_5m = _make_breakout_candles(count=18, bar_seconds=300)
    # 900s after last bar start (600s after close) -> still closed
    t_5m = candles_5m[-1]["timestamp"] + 900.0
    res = evaluate_scalper_entry(
        candles_5m,
        _strong_flow_pair(1.12),
        time_in_slot_min=5.0,
        decision_timestamp=t_5m,
        ohlcv_provenance=make_verified_ohlcv_result(candles_5m, last_ok_at=t_5m),
    )
    assert res.eligible is True
    assert res.setup_type == "BREAKOUT_IGNITION"


def test_t5_normalization_hygiene_and_fail_closed_decision_timestamp():
    base_candles = _make_breakout_candles(count=18, bar_seconds=300)
    last_ts = base_candles[-1]["timestamp"]

    # Out-of-order, duplicate, millisecond timestamp, missing timestamp, NaN/zero OHLC
    dirty: list[Any] = list(reversed(base_candles))
    dirty.append({"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 10.0})  # missing timestamp
    dirty.append({"timestamp": True, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 10.0})  # bool ts
    dirty.append({"timestamp": last_ts + 300.0, "open": math.nan, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 1.0})
    dirty.append({"timestamp": last_ts * 1000.0, **{k: v for k, v in base_candles[-1].items() if k != "timestamp"}})

    closed, err, _ = normalize_and_filter_candles(dirty, bar_minutes=5, decision_timestamp=last_ts + 300.0)
    assert err is None
    assert len(closed) == 18
    assert [c["timestamp"] for c in closed] == sorted(c["timestamp"] for c in closed)

    # Invalid decision_timestamp fails closed on both functions
    for bad_ts in (float("nan"), float("inf"), -1.0, 0.0, True, "1760000000"):  # type: ignore[arg-type]
        bi = detect_breakout_ignition(base_candles, _strong_flow_pair(1.12), decision_timestamp=bad_ts)  # type: ignore[arg-type]
        assert bi.detected is False
        assert bi.rejection_reason == "invalid decision_timestamp"
        assert "INVALID_DECISION_TS" in bi.telemetry.failed_gates

        sc = evaluate_scalper_entry(
            base_candles, _strong_flow_pair(1.12), time_in_slot_min=5.0, decision_timestamp=bad_ts  # type: ignore[arg-type]
        )
        assert sc.eligible is False
        assert sc.rejection_reason == "invalid decision_timestamp"


@pytest.mark.asyncio
async def test_t6a_and_t6b_12_raw_11_closed_5m_triggers_1m_fallback_only_when_allowed(tmp_path):
    clear_ohlcv_cache()
    cfg = AgentConfig(_env_file=None, OHLCV_CACHE_SECONDS=90.0, OHLCV_PACING_DELAY_SECONDS=0.0)
    db = AgentDatabase(db_path=str(tmp_path / "e3a_fb.duckdb"), starting_sol=0.7)

    # 12 raw 5m bars: first 11 closed at decision_ts, 12th is forming (ts + 300 > decision_ts)
    bars_1m = _make_breakout_candles(count=20, bar_seconds=60)
    decision_ts = bars_1m[-1]["timestamp"] + 60.0
    decision_dt = datetime.fromtimestamp(decision_ts, tz=timezone.utc)
    t_start_5m = int(decision_ts // 300) * 300
    raw_5m_rows = [
        [t_start_5m - (11 - i) * 300, 1.00, 1.03, 0.99, 1.02, 500.0]
        for i in range(12)
    ]  # i=11 is t_start_5m, which is forming at decision_ts!
    raw_1m_rows = [
        [int(c["timestamp"]), c["open"], c["high"], c["low"], c["close"], c["volume"]]
        for c in bars_1m
    ]

    net_calls = {"5m": 0, "1m": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("aggregate") == "5":
            net_calls["5m"] += 1
            return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": raw_5m_rows}}})
        net_calls["1m"] += 1
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": raw_1m_rows}}})

    cand = {
        "mint": "MintFB1111111111111111111111111111111111111",
        "symbol": "FB1M",
        "pair_address": "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2",
        "added_at": datetime.fromtimestamp(decision_ts - 360.0, tz=timezone.utc).isoformat(),
        **_strong_flow_pair(1.12),
    }
    state = {"focus_state": FocusState.SCANNING.value, "sol_balance": 0.7, "safe_candidates": [cand]}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await alpha_node(
            state,
            config=cfg,
            db=db,
            decision_client=_StubDecisionClient(),
            http_client=client,
            now=decision_dt,
        )

    assert net_calls["5m"] == 1
    assert net_calls["1m"] == 1
    assert out["alpha_decisions"][0]["approved"] is True

    # T6b: During 429 backoff, 12 raw / 11 closed must NOT make any 1m request
    clear_ohlcv_cache()
    calls_429 = {"5m": 0, "1m": 0}

    def handler_429(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("aggregate") == "5":
            calls_429["5m"] += 1
            return httpx.Response(429, json={"error": "rate limit"})
        calls_429["1m"] += 1
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": raw_1m_rows}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_429)) as client:
        c5_res = await fetch_ohlcv_batch(["Pool429"], cfg, client, now_ts=decision_ts)
        assert should_fetch_1m_fallback("Pool429", c5_res["Pool429"], now_ts=decision_ts) is False
        c1_res = await fetch_ohlcv_1m_batch(["Pool429"], cfg, client, now_ts=decision_ts)
        assert c1_res["Pool429"] == []
        assert calls_429["1m"] == 0

    clear_ohlcv_cache()
    db.close()


@pytest.mark.asyncio
async def test_t6c_malformed_ms_duplicate_cached_5m_cannot_suppress_1m_fallback():
    clear_ohlcv_cache()
    cfg = AgentConfig(_env_file=None, OHLCV_CACHE_SECONDS=90.0, OHLCV_PACING_DELAY_SECONDS=0.0)
    decision_ts = 1_760_003_600.0
    # 10 unique closed 5m bars + 2 duplicates + 1 malformed + 1 ms-scaled duplicate = 14 raw rows, only 10 unique closed!
    base_ts = int(decision_ts) - 300
    raw_rows = [[base_ts - i * 300, 1.0, 1.05, 0.99, 1.02, 500.0] for i in range(10)]
    raw_rows.append([base_ts, 1.0, 1.05, 0.99, 1.02, 500.0])  # duplicate
    raw_rows.append([base_ts * 1000, 1.0, 1.05, 0.99, 1.02, 500.0])  # ms duplicate

    calls = {"1m": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("aggregate") == "5":
            return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": raw_rows}}})
        calls["1m"] += 1
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": []}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        c5, _ = await fetch_ohlcv_5m("PoolDup", cfg, client, now_ts=decision_ts)
        # Inject an extra malformed dict into a test list passed to should_fetch_1m_fallback
        dirty_5m: list[Any] = list(c5) + [
            {"timestamp": True, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 1.0},
            {"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 1.0},
        ]
        assert should_fetch_1m_fallback("PoolDup", dirty_5m, now_ts=decision_ts) is True
        await fetch_ohlcv_1m_batch(["PoolDup"], cfg, client, now_ts=decision_ts)
        assert calls["1m"] == 1

    clear_ohlcv_cache()


@pytest.mark.asyncio
async def test_t6d_preattached_5m_only_candidate_makes_no_1m_network_request(tmp_path):
    clear_ohlcv_cache()
    cfg = AgentConfig(_env_file=None)
    db = AgentDatabase(db_path=str(tmp_path / "e3a_preattached.duckdb"), starting_sol=0.7)
    short_5m = _make_breakout_candles(count=8, bar_seconds=300)
    decision_ts = short_5m[-1]["timestamp"] + 300.0
    decision_dt = datetime.fromtimestamp(decision_ts, tz=timezone.utc)

    net_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        net_calls["count"] += 1
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": []}}})

    cand = {
        "mint": "MintPre111111111111111111111111111111111111",
        "symbol": "PRE5M",
        "pair_address": "PoolPre111111111111111111111111111111111111",
        "added_at": datetime.fromtimestamp(decision_ts - 360.0, tz=timezone.utc).isoformat(),
        "candles_5m": short_5m,
        "_ohlcv_5m_result": make_verified_ohlcv_result(short_5m, last_ok_at=decision_ts),
        **_strong_flow_pair(1.12),
    }
    state = {"focus_state": FocusState.SCANNING.value, "sol_balance": 0.7, "safe_candidates": [cand]}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await alpha_node(
            state,
            config=cfg,
            db=db,
            decision_client=_StubDecisionClient(),
            http_client=client,
            now=decision_dt,
        )

    assert net_calls["count"] == 0
    assert out["alpha_decisions"][0]["approved"] is False
    assert "ineligible OHLCV source (unavailable)" in out["alpha_decisions"][0]["rationale"]
    db.close()


def test_t7_diagnostic_gap_does_not_reject_valid_closed_candles():
    candles = _make_breakout_candles(count=18, bar_seconds=300)
    # Introduce a 900s gap between [-5] and [-4] by shifting earlier bars back 600s
    for i in range(len(candles) - 4):
        candles[i]["timestamp"] -= 600.0
    decision_ts = candles[-1]["timestamp"] + 300.0

    closed, err, gap_diag = normalize_and_filter_candles(
        candles, bar_minutes=5, decision_timestamp=decision_ts
    )
    assert err is None
    assert len(closed) == 18
    assert gap_diag["recent_gap_count"] >= 1
    assert gap_diag["max_recent_gap_seconds"] == 900.0

    res = evaluate_scalper_entry(
        candles, _strong_flow_pair(1.12), time_in_slot_min=5.0, decision_timestamp=decision_ts,
        ohlcv_provenance=make_verified_ohlcv_result(candles, last_ok_at=decision_ts),
    )
    assert "insufficient 5m candles" not in (res.rejection_reason or "")
    assert res.eligible is True


def test_t8a_and_t8b_15m_aggregation_rejects_noncontiguous_and_incomplete_buckets():
    # Skipped 12:05 and 12:15/12:20 -> [12:00, 12:10, 12:25] must NOT form a 15m bar
    t0 = 1_760_000_400  # divisible by 900
    assert t0 % 900 == 0
    noncontiguous = [
        {"timestamp": float(t0), "open": 1.0, "high": 1.1, "low": 0.95, "close": 1.05, "volume": 100.0},
        {"timestamp": float(t0 + 600), "open": 1.05, "high": 1.15, "low": 1.0, "close": 1.1, "volume": 100.0},
        {"timestamp": float(t0 + 1500), "open": 1.1, "high": 1.2, "low": 1.05, "close": 1.15, "volume": 100.0},
    ]
    agg = _aggregate_to_15m(noncontiguous, bar_minutes=5)
    assert agg == []
    assert _is_htf_15m_bullish(agg, 1.15) is False

    # One complete 15m bucket [t0, t0+300, t0+600] + one incomplete bucket [t0+900, t0+1200]
    mixed = [
        {"timestamp": float(t0), "open": 1.0, "high": 1.08, "low": 0.98, "close": 1.04, "volume": 100.0},
        {"timestamp": float(t0 + 300), "open": 1.04, "high": 1.12, "low": 1.02, "close": 1.09, "volume": 120.0},
        {"timestamp": float(t0 + 600), "open": 1.09, "high": 1.15, "low": 1.05, "close": 1.14, "volume": 140.0},
        {"timestamp": float(t0 + 900), "open": 1.14, "high": 1.18, "low": 1.10, "close": 1.16, "volume": 80.0},
        {"timestamp": float(t0 + 1200), "open": 1.16, "high": 1.20, "low": 1.12, "close": 1.19, "volume": 90.0},
    ]
    agg_mixed = _aggregate_to_15m(mixed, bar_minutes=5)
    assert len(agg_mixed) == 1
    assert agg_mixed[0]["timestamp"] == float(t0)
    assert agg_mixed[0]["volume"] == 360.0


@pytest.mark.asyncio
async def test_whale_lane_propagates_decision_timestamp_and_drops_forming_5m(tmp_path):
    from datetime import timedelta
    from whale_lane import run_whale_lane_tick

    clear_ohlcv_cache()
    db = AgentDatabase(db_path=str(tmp_path / "e3a_whale.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(
        _env_file=None,
        EXECUTION_MODE="paper",
        WHALE_STRATEGY_ENABLED=True,
        WHALE_MIN_LIQUIDITY_USD=20_000.0,
        WHALE_MIN_MCAP_USD=50_000.0,
        WHALE_MAX_MCAP_USD=10_000_000.0,
        WHALE_MIN_BREAKOUT_SCORE=75,
        WHALE_BLOCK_EXTENDED=True,
        ENABLE_JUPITER_DISCOVERY=False,
        OHLCV_PACING_DELAY_SECONDS=0.0,
    )
    candles = _make_breakout_candles(count=18, bar_seconds=300)
    # Place decision_dt 150s into the final 5m candle -> forming!
    decision_ts = candles[-1]["timestamp"] + 150.0
    now_dt = datetime.fromtimestamp(decision_ts, tz=timezone.utc)
    mint = "MintWhaleForming111111111111111111111111111"
    db.record_whale_event(
        signature="wf-1", wallet="W1", mint=mint, side="BUY", sol_amount=2.0, token_amount=100.0,
        timestamp_utc=(now_dt - timedelta(seconds=20)).isoformat(),
    )
    db.record_whale_event(
        signature="wf-2", wallet="W2", mint=mint, side="BUY", sol_amount=2.0, token_amount=100.0,
        timestamp_utc=(now_dt - timedelta(seconds=10)).isoformat(),
    )
    db.recompute_whale_signal(mint, ttl_minutes=30, now=now_dt)

    gecko_rows = [[c["timestamp"], c["open"], c["high"], c["low"], c["close"], c["volume"]] for c in candles]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/tokens/v1/solana/" in url:
            return httpx.Response(200, json=[{
                "chainId": "solana", "pairAddress": "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2",
                "baseToken": {"address": mint, "symbol": "WFORM"},
                "marketCap": 300_000, "liquidity": {"usd": 90_000},
                "txns": {"m5": {"buys": 32, "sells": 14}, "h1": {"buys": 180, "sells": 120}},
            }])
        if "ohlcv" in url:
            return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": gecko_rows}}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run_whale_lane_tick(config=cfg, db=db, http_client=client, now=now_dt)

    decs = db.get_whale_decisions()
    assert decs[mint]["status"] == "SKIPPED"
    assert decs[mint]["breakout_telemetry"]["state"] != "IGNITION"
    clear_ohlcv_cache()
    db.close()
