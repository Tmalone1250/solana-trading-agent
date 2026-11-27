"""Unit and integration tests for Phase E3B — Cache Provenance & Stale-Data Handling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from alpha_node import alpha_node
from config import AgentConfig
from db import AgentDatabase
from ohlcv_client import (
    OhlcvFetchResult,
    cache_breakout_telemetry,
    clear_ohlcv_cache,
    evaluate_ohlcv_provenance,
    fetch_ohlcv_1m_batch_with_provenance,
    fetch_ohlcv_batch_with_provenance,
    get_cached_breakout_telemetry_with_meta,
    select_ohlcv_interval_provenance,
)
from scalper_entry_engine import evaluate_scalper_entry
from state import FocusState
from tests.test_breakout_ignition import _make_breakout_candles, _strong_flow_pair
from whale_lane import run_whale_lane_tick


def make_verified_ohlcv_result(
    candles: list[dict[str, float]],
    *,
    last_ok_at: float,
    source: str = "pre_attached_verified",
) -> OhlcvFetchResult:
    """Test helper to supply trusted OHLCV provenance without weakening production validation."""
    return OhlcvFetchResult(
        candles=list(candles),
        source=source,  # type: ignore[arg-type]
        fetched_at=last_ok_at,
        last_ok_at=last_ok_at,
        last_status=200,
    )


class _StubBuyClient:
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


def _gecko_payload(candles: list[dict[str, float]]) -> dict[str, Any]:
    return {
        "data": {
            "attributes": {
                "ohlcv_list": [
                    [int(c["timestamp"]), c["open"], c["high"], c["low"], c["close"], c["volume"]]
                    for c in candles
                ]
            }
        }
    }


@pytest.mark.asyncio
async def test_b1_to_b6_network_cache_expired_429_error_and_backoff(tmp_path):
    clear_ohlcv_cache()
    cfg = AgentConfig(_env_file=None, OHLCV_CACHE_SECONDS=90.0, OHLCV_PACING_DELAY_SECONDS=0.0)
    candles = _make_breakout_candles(count=18, bar_seconds=300)
    t0 = candles[-1]["timestamp"] + 300.0
    pool = "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2"
    mint = "MintE3B111111111111111111111111111111111111"

    mode = {"val": "200"}

    def handler(request: httpx.Request) -> httpx.Response:
        if mode["val"] == "429":
            return httpx.Response(429, json={"error": "rate limit"})
        if mode["val"] == "err":
            raise httpx.ConnectTimeout("timeout")
        return httpx.Response(200, json=_gecko_payload(candles))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # B1: Fresh 200 OK
        r1 = (await fetch_ohlcv_batch_with_provenance([pool], cfg, client, now_ts=t0))[pool]
        assert r1.source == "network_200"
        assert r1.last_status == 200
        assert evaluate_ohlcv_provenance(r1, decision_timestamp=t0) == (True, None)

        # B2: Fresh cache hit (< 90s)
        r2 = (await fetch_ohlcv_batch_with_provenance([pool], cfg, client, now_ts=t0 + 45.0))[pool]
        assert r2.source == "cache_fresh"
        assert r2.age_seconds(t0 + 45.0) == pytest.approx(45.0)
        assert evaluate_ohlcv_provenance(r2, decision_timestamp=t0 + 45.0) == (True, None)

        # B3: Expired cache (> 90s) + 200 OK refresh
        r3 = (await fetch_ohlcv_batch_with_provenance([pool], cfg, client, now_ts=t0 + 100.0))[pool]
        assert r3.source == "network_200"
        assert r3.last_ok_at == pytest.approx(t0 + 100.0)
        assert evaluate_ohlcv_provenance(r3, decision_timestamp=t0 + 100.0) == (True, None)

        # B4 & B6: Expired cache + HTTP 429 -> stale_429, then global backoff -> stale_backoff
        mode["val"] = "429"
        r4 = (await fetch_ohlcv_batch_with_provenance([pool], cfg, client, now_ts=t0 + 200.0))[pool]
        assert r4.source == "stale_429"
        assert r4.last_status == 429
        ok4, err4 = evaluate_ohlcv_provenance(r4, decision_timestamp=t0 + 200.0)
        assert ok4 is False and "stale_429" in (err4 or "")

        r6 = (await fetch_ohlcv_batch_with_provenance([pool], cfg, client, now_ts=t0 + 210.0))[pool]
        assert r6.source == "stale_backoff"
        ok6, _ = evaluate_ohlcv_provenance(r6, decision_timestamp=t0 + 210.0)
        assert ok6 is False

        # alpha_node blocks BUY during stale_backoff
        db = AgentDatabase(db_path=str(tmp_path / "b6.duckdb"), starting_sol=1.0)
        cand = {
            "mint": mint,
            "symbol": "E3B",
            "pair_address": pool,
            "added_at": datetime.fromtimestamp(t0 - 300.0, tz=timezone.utc).isoformat(),
            **_strong_flow_pair(1.12),
        }
        out = await alpha_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 1.0, "safe_candidates": [cand]},
            config=cfg,
            db=db,
            decision_client=_StubBuyClient(),
            http_client=client,
            now=datetime.fromtimestamp(t0 + 210.0, tz=timezone.utc),
        )
        assert out["alpha_decisions"][0]["approved"] is False
        assert "stale_backoff" in out["alpha_decisions"][0]["rationale"]
        db.close()

    # B5: Provider timeout / network error -> stale_error
    clear_ohlcv_cache()
    mode["val"] = "200"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await fetch_ohlcv_batch_with_provenance([pool], cfg, client, now_ts=t0)
        mode["val"] = "err"
        r5 = (await fetch_ohlcv_batch_with_provenance([pool], cfg, client, now_ts=t0 + 100.0))[pool]
        assert r5.source == "stale_error"
        assert r5.last_status == 0
        assert evaluate_ohlcv_provenance(r5, decision_timestamp=t0 + 100.0)[0] is False
    clear_ohlcv_cache()


@pytest.mark.asyncio
async def test_b7_1m_batch_with_provenance_never_serves_stale_c1_cached_during_backoff():
    clear_ohlcv_cache()
    cfg = AgentConfig(_env_file=None, OHLCV_CACHE_SECONDS=90.0, OHLCV_PACING_DELAY_SECONDS=0.0)
    bars_1m = _make_breakout_candles(count=20, bar_seconds=60)
    t0 = bars_1m[-1]["timestamp"] + 60.0
    pool = "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2"

    # Seed 1m cache when 5m had < 12 bars
    short_5m = _make_breakout_candles(count=6, bar_seconds=300)
    mode = {"val": "ok"}

    def handler(request: httpx.Request) -> httpx.Response:
        if mode["val"] == "429":
            return httpx.Response(429)
        if request.url.params.get("aggregate") == "5":
            return httpx.Response(200, json=_gecko_payload(short_5m))
        return httpx.Response(200, json=_gecko_payload(bars_1m))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await fetch_ohlcv_batch_with_provenance([pool], cfg, client, now_ts=t0)
        c1_ok = (await fetch_ohlcv_1m_batch_with_provenance([pool], cfg, client, now_ts=t0))[pool]
        assert c1_ok.source == "network_200" and len(c1_ok.candles) == 20

        # Now 5m hits 429 -> backoff active -> fetch_ohlcv_1m_batch_with_provenance MUST return unavailable, []
        mode["val"] = "429"
        await fetch_ohlcv_batch_with_provenance([pool], cfg, client, now_ts=t0 + 100.0)
        c1_blocked = (await fetch_ohlcv_1m_batch_with_provenance([pool], cfg, client, now_ts=t0 + 100.0))[pool]
        assert c1_blocked.source == "unavailable"
        assert c1_blocked.candles == []
        assert evaluate_ohlcv_provenance(c1_blocked, decision_timestamp=t0 + 100.0)[0] is False
    clear_ohlcv_cache()


@pytest.mark.asyncio
async def test_b8_b9_b13_pre_attached_verified_vs_unknown_and_expired(tmp_path):
    candles = _make_breakout_candles(count=18, bar_seconds=300)
    t0 = candles[-1]["timestamp"] + 300.0
    now_dt = datetime.fromtimestamp(t0, tz=timezone.utc)
    cfg = AgentConfig(_env_file=None)

    # B8: pre_attached_verified passes
    res_ok = evaluate_scalper_entry(
        candles,
        _strong_flow_pair(1.12),
        time_in_slot_min=5.0,
        decision_timestamp=t0,
        ohlcv_provenance=make_verified_ohlcv_result(candles, last_ok_at=t0),
    )
    assert res_ok.eligible is True

    # B9: omitted provenance or pre_attached_unknown or dict/bool bypasses fail closed
    res_none = evaluate_scalper_entry(candles, _strong_flow_pair(1.12), time_in_slot_min=5.0, decision_timestamp=t0)
    assert res_none.eligible is False and "missing OHLCV provenance" in (res_none.rejection_reason or "")

    db = AgentDatabase(db_path=str(tmp_path / "b9.duckdb"), starting_sol=1.0)
    cand_unknown = {
        "mint": "MintUnk111111111111111111111111111111111111",
        "symbol": "UNK",
        "pair_address": "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2",
        "added_at": (now_dt - timedelta(minutes=5)).isoformat(),
        "candles_5m": candles,
        "_ohlcv_5m_result": {"source": "network_200", "is_entry_eligible": True},  # dict bypass attempt ignored!
        **_strong_flow_pair(1.12),
    }
    out_unk = await alpha_node(
        {"focus_state": FocusState.SCANNING.value, "sol_balance": 1.0, "safe_candidates": [cand_unknown]},
        config=cfg,
        db=db,
        decision_client=_StubBuyClient(),
        now=now_dt,
    )
    assert out_unk["alpha_decisions"][0]["approved"] is False
    assert "pre_attached_unknown" in out_unk["alpha_decisions"][0]["rationale"]
    db.close()

    # B13: pre_attached_verified with missing or expired last_ok_at fails closed
    p_missing = OhlcvFetchResult(
        candles=candles, source="pre_attached_verified", fetched_at=t0, last_ok_at=None, last_status=200
    )
    assert evaluate_ohlcv_provenance(p_missing, decision_timestamp=t0)[0] is False
    p_expired = make_verified_ohlcv_result(candles, last_ok_at=t0 - 95.0)
    assert evaluate_ohlcv_provenance(p_expired, decision_timestamp=t0, max_cache_age_seconds=90.0)[0] is False


@pytest.mark.asyncio
async def test_b10_whale_lane_with_stale_ohlcv_does_not_gate_or_cache_stale_telemetry(tmp_path):
    clear_ohlcv_cache()
    db = AgentDatabase(db_path=str(tmp_path / "b10_whale.duckdb"), starting_sol=2.0)
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
        OHLCV_CACHE_SECONDS=90.0,
        OHLCV_PACING_DELAY_SECONDS=0.0,
    )
    ext_candles = _make_breakout_candles(count=18, breakout_close=1.34, breakout_high=1.35, breakout_low=1.06, breakout_vol=3500.0)
    t0 = ext_candles[-1]["timestamp"] + 300.0
    pool = "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2"
    mint = "MintWhaleStale11111111111111111111111111111"

    # Seed 5m cache with EXTENDED candles, then advance >90s and return 429!
    mode = {"val": "200"}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/tokens/v1/solana/" in url:
            return httpx.Response(200, json=[{
                "chainId": "solana", "pairAddress": pool,
                "baseToken": {"address": mint, "symbol": "WSTALE"},
                "marketCap": 250_000, "liquidity": {"usd": 80_000},
                "txns": {"m5": {"buys": 32, "sells": 14}, "h1": {"buys": 180, "sells": 120}},
            }])
        if "ohlcv" in url:
            if mode["val"] == "429":
                return httpx.Response(429)
            return httpx.Response(200, json=_gecko_payload(ext_candles))
        if "rugcheck.xyz" in url:
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        if "/ultra/v1/shield" in url:
            return httpx.Response(200, json={"warnings": {mint: []}})
        if "/swap/v2/order" in url:
            return httpx.Response(200, json={"outAmount": "1500000", "transaction": None})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await fetch_ohlcv_batch_with_provenance([pool], cfg, client, now_ts=t0)
        mode["val"] = "429"
        now_dt = datetime.fromtimestamp(t0 + 120.0, tz=timezone.utc)
        db.record_whale_event(
            signature="ws-1", wallet="W1", mint=mint, side="BUY", sol_amount=2.0, token_amount=100.0,
            timestamp_utc=(now_dt - timedelta(seconds=20)).isoformat(),
        )
        db.record_whale_event(
            signature="ws-2", wallet="W2", mint=mint, side="BUY", sol_amount=2.0, token_amount=100.0,
            timestamp_utc=(now_dt - timedelta(seconds=10)).isoformat(),
        )
        db.recompute_whale_signal(mint, ttl_minutes=30, now=now_dt)

        res = await run_whale_lane_tick(config=cfg, db=db, http_client=client, now=now_dt)
        # Stale EXTENDED OHLCV must NOT block independent whale entry nor populate _breakout_telemetry_cache
        assert res["event"] == "WHALE_SCALP_CONFLUENCE_ENTERED"
        assert get_cached_breakout_telemetry_with_meta(mint, now_ts=t0 + 120.0) is None
    clear_ohlcv_cache()
    db.close()


def test_b11_to_b12c_5m_1m_selection_matrix():
    c5_full = _make_breakout_candles(count=18, bar_seconds=300)
    c5_short = _make_breakout_candles(count=6, bar_seconds=300)
    c1_full = _make_breakout_candles(count=20, bar_seconds=60)
    t0 = max(c5_full[-1]["timestamp"] + 300.0, c1_full[-1]["timestamp"] + 60.0)

    fresh_5m_short = OhlcvFetchResult(candles=c5_short, source="network_200", fetched_at=t0, last_ok_at=t0, last_status=200)
    fresh_5m_full = OhlcvFetchResult(candles=c5_full, source="network_200", fetched_at=t0, last_ok_at=t0, last_status=200)
    stale_5m_full = OhlcvFetchResult(candles=c5_full, source="stale_429", fetched_at=t0, last_ok_at=t0 - 150.0, last_status=429)
    unavail = OhlcvFetchResult(candles=[], source="unavailable", fetched_at=None, last_ok_at=None, last_status=None)
    stale_1m = OhlcvFetchResult(candles=c1_full, source="stale_backoff", fetched_at=t0, last_ok_at=t0 - 150.0, last_status=429)
    verified_1m = make_verified_ohlcv_result(c1_full, last_ok_at=t0)

    # B11: Fresh 5m (< 12) + stale 1m -> fail closed
    chosen, ok, err = select_ohlcv_interval_provenance(fresh_5m_short, stale_1m, decision_timestamp=t0)
    assert ok is False and chosen is stale_1m and "stale_backoff" in (err or "")

    # B12a: 12+ closed stale 5m + pre_attached_verified 1m -> uses verified 1m; without trusted 1m -> fails closed on 5m
    chosen_a1, ok_a1, _ = select_ohlcv_interval_provenance(stale_5m_full, verified_1m, decision_timestamp=t0)
    assert ok_a1 is True and chosen_a1 is verified_1m
    chosen_a2, ok_a2, err_a2 = select_ohlcv_interval_provenance(stale_5m_full, unavail, decision_timestamp=t0)
    assert ok_a2 is False and chosen_a2 is stale_5m_full and "stale_429" in (err_a2 or "")

    # B12b: 12+ closed fresh 5m + stale 1m -> selects 5m and ignores stale 1m
    chosen_b, ok_b, _ = select_ohlcv_interval_provenance(fresh_5m_full, stale_1m, decision_timestamp=t0)
    assert ok_b is True and chosen_b is fresh_5m_full

    # B12c: Unavailable 5m + pre_attached_verified 1m -> uses verified 1m; without trusted 1m -> fails closed
    chosen_c1, ok_c1, _ = select_ohlcv_interval_provenance(unavail, verified_1m, decision_timestamp=t0)
    assert ok_c1 is True and chosen_c1 is verified_1m
    chosen_c2, ok_c2, _ = select_ohlcv_interval_provenance(unavail, unavail, decision_timestamp=t0)
    assert ok_c2 is False


def test_b14_and_b15_telemetry_source_timestamp_and_contradictory_metadata():
    clear_ohlcv_cache()
    t_now = 1_760_010_000.0
    # B14a: Telemetry computed from 900s-old candles, inserted 5s ago
    cache_breakout_telemetry(
        "MintOld",
        {"score": 82, "state": "IGNITION"},
        ohlcv_source="cache_fresh",
        ohlcv_last_ok_at=t_now - 900.0,
        now_ts=t_now - 5.0,
    )
    meta = get_cached_breakout_telemetry_with_meta("MintOld", now_ts=t_now)
    assert meta is not None
    assert meta.data_age_seconds == pytest.approx(900.0)
    assert meta.is_historical is True
    assert meta.is_trusted is True

    # B14b: Missing or materially future telemetry source timestamp (never clamped to 0.0)
    cache_breakout_telemetry(
        "MintFuture",
        {"score": 80, "state": "COILING"},
        ohlcv_source="network_200",
        ohlcv_last_ok_at=t_now + 60.0,
        now_ts=t_now,
    )
    meta_fut = get_cached_breakout_telemetry_with_meta("MintFuture", now_ts=t_now)
    assert meta_fut is not None
    assert meta_fut.data_age_seconds is None
    assert meta_fut.is_historical is True
    assert meta_fut.is_trusted is False

    # B15: Contradictory source / status / last_ok_at rejected
    c1 = OhlcvFetchResult(candles=[], source="stale_429", fetched_at=t_now, last_ok_at=t_now, last_status=200)
    c2 = OhlcvFetchResult(candles=[], source="network_200", fetched_at=t_now, last_ok_at=t_now, last_status=429)
    c3 = OhlcvFetchResult(candles=[], source="network_200", fetched_at=t_now, last_ok_at=None, last_status=200)
    for bad in (c1, c2, c3):
        assert evaluate_ohlcv_provenance(bad, decision_timestamp=t_now)[0] is False
    clear_ohlcv_cache()
