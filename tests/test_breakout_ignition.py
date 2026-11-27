"""Unit tests for BREAKOUT_IGNITION setup, 1m fallback, coiling retention & CSV logging."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from alpha_node import alpha_node
from config import AgentConfig
from db import AgentDatabase
from scalper_entry_engine import evaluate_scalper_entry
from state import FocusState
from telemetry_api import create_telemetry_app


def _make_breakout_candles(
    count: int = 18,
    *,
    bar_seconds: int = 300,
    breakout_close: float = 1.12,
    breakout_high: float = 1.13,
    breakout_low: float = 1.06,
    breakout_vol: float = 3000.0,
) -> list[dict[str, float]]:
    """Build a realistic uptrend with pullbacks (RSI ~72) coiling under 1.08, then breaking out."""
    candles: list[dict[str, float]] = []
    t0 = 1_760_000_000
    base_len = count - 1
    # Alternating impulse + small red pullback bars into a tight 4-bar coil under 1.08
    for i in range(base_len):
        if i < base_len - 4:
            trend = 0.96 + (0.09 * i / max(1, base_len - 5))
            # Add natural red pullback candles every 3rd bar so RSI sits in [71, 76]
            if i % 3 == 2:
                o, c = trend + 0.014, trend - 0.018
                h, l = o + 0.005, c - 0.005
            else:
                o, c = trend - 0.006, trend + 0.008
                h, l = c + 0.006, o - 0.005
        else:
            step = i - (base_len - 4)
            # Tight 4-bar coil with higher lows (1.035 -> 1.042 -> 1.048 -> 1.054) under 1.078
            l = 1.035 + step * 0.006
            if step == 1:
                o, c = l + 0.022, l + 0.010  # small red pause inside the coil
            else:
                o, c = l + 0.008, l + 0.018
            h = 1.078
        candles.append(
            {
                "timestamp": float(t0 + i * bar_seconds),
                "open": round(o, 6),
                "high": round(h, 6),
                "low": round(l, 6),
                "close": round(c, 6),
                "volume": 1000.0,
            }
        )
    candles.append(
        {
            "timestamp": float(t0 + base_len * bar_seconds),
            "open": 1.065,
            "high": breakout_high,
            "low": breakout_low,
            "close": breakout_close,
            "volume": breakout_vol,
        }
    )
    return candles


def _strong_flow_pair(price_usd: float = 1.12) -> dict[str, Any]:
    return {
        "price_usd": price_usd,
        "liquidity_usd": 85_000.0,
        "market_cap_usd": 650_000.0,
        "txns": {"m5": {"buys": 32, "sells": 14}, "h1": {"buys": 180, "sells": 120}},
    }


def test_breakout_ignition_valid_5m_bypasses_near_1h_high_and_allows_rsi_up_to_78():
    candles = _make_breakout_candles(
        count=18, breakout_close=1.12, breakout_high=1.13, breakout_low=1.06, breakout_vol=3000.0
    )
    res = evaluate_scalper_entry(candles, _strong_flow_pair(1.12), time_in_slot_min=5.0)
    assert res.eligible is True
    assert res.setup_type == "BREAKOUT_IGNITION"
    assert res.dist_from_1h_high_pct < 0.05  # Near 1h high bypassed for breakout ignition
    assert res.rsi_5m <= 78.0
    assert res.breakout_metrics is not None
    assert res.breakout_metrics["vol_ratio"] >= 2.0
    assert res.breakout_metrics["ext_above_ema9_pct"] <= 0.12
    assert res.breakout_metrics["ext_above_vwap_pct"] <= 0.12


def test_breakout_ignition_rejects_over_extended_chase_above_12_pct():
    # Huge parabolic spike to 1.35 (> 12% above EMA9 ~1.08 and VWAP ~1.05) -> rejected!
    candles = _make_breakout_candles(
        count=18, breakout_close=1.34, breakout_high=1.35, breakout_low=1.07, breakout_vol=3500.0
    )
    res = evaluate_scalper_entry(candles, _strong_flow_pair(1.34), time_in_slot_min=5.0)
    assert res.eligible is False
    assert res.setup_type == "NONE"
    assert res.rejection_reason is not None
    assert "over-extended" in res.rejection_reason


def test_breakout_ignition_rejects_upper_wick_fakeout():
    # Wicks up to 1.20 above resistance, but closes back at 1.095 (bottom 25% of range -> fakeout!)
    candles = _make_breakout_candles(
        count=18, breakout_close=1.095, breakout_high=1.20, breakout_low=1.06, breakout_vol=3000.0
    )
    res = evaluate_scalper_entry(candles, _strong_flow_pair(1.095), time_in_slot_min=5.0)
    assert res.eligible is False
    assert res.setup_type == "NONE"
    assert res.rejection_reason is not None
    assert "upper-wick fakeout" in res.rejection_reason


def test_breakout_ignition_1m_candle_fallback_when_fewer_than_12_5m_bars():
    short_5m = _make_breakout_candles(count=5)  # Only 25 minutes old (< 12 x 5m bars)
    bars_1m = _make_breakout_candles(
        count=20, bar_seconds=60, breakout_close=1.12, breakout_high=1.13, breakout_low=1.06, breakout_vol=3000.0
    )
    res = evaluate_scalper_entry(
        short_5m, _strong_flow_pair(1.12), time_in_slot_min=4.0, candles_1m=bars_1m
    )
    assert res.eligible is True
    assert res.setup_type == "BREAKOUT_IGNITION"
    assert res.breakout_metrics is not None
    assert res.breakout_metrics["timeframe"] == "1m"


@pytest.mark.asyncio
async def test_coiling_base_retention_and_breakout_ignition_csv_logging(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "breakout_csv.duckdb"), starting_sol=1.0)
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper")
    now_dt = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Coiling base (liq >= $40K, vamp < 0.65, within 5% of 1h high) survives 1st LOW_SCORE & 20m watch
    coil_mint = "MintCoil11111111111111111111111111111111111"
    coil_candles = _make_breakout_candles(
        count=18, breakout_close=1.075, breakout_high=1.078, breakout_low=1.055, breakout_vol=900.0
    )
    added_20m_ago = (now_dt - timedelta(minutes=20)).isoformat()
    db.upsert_watchlist_entry(
        mint=coil_mint, symbol="COIL", added_at=added_20m_ago,
        candidate={"mint": coil_mint, "symbol": "COIL", "liquidity_usd": 60_000.0, "market_cap_usd": 400_000.0},
    )

    class LowScoreDecision:
        async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
            return {
                "t0_narrative_strength": {"type": "score", "score": 1.5, "confidence": 0.7},
                "t0_momentum_quality": {"type": "score", "score": 1.5, "confidence": 0.7},
                "t0_is_copycat_vamp": {"type": "noul", "noul": 0.30, "confidence": 0.8},
                "t0_action": {"type": "choice", "choice": "WATCH", "confidence": 0.7},
            }

    cand_coil = {
        "mint": coil_mint, "symbol": "COIL", "added_at": added_20m_ago,
        "liquidity_usd": 60_000.0, "market_cap_usd": 400_000.0,
        "price_usd": 1.075, "candles_5m": coil_candles,
        "txns": {"m5": {"buys": 10, "sells": 10}},
    }
    # Strike 1 -> retained (NOT evicted!)
    await alpha_node(
        {"focus_state": FocusState.SCANNING.value, "sol_balance": 1.0, "safe_candidates": [cand_coil]},
        config=cfg, db=db, decision_client=LowScoreDecision(), now=now_dt,  # type: ignore[arg-type]
    )
    assert any(w["mint"] == coil_mint for w in db.get_watchlist())

    # Strike 2 -> now evicted!
    await alpha_node(
        {"focus_state": FocusState.SCANNING.value, "sol_balance": 1.0, "safe_candidates": [cand_coil]},
        config=cfg, db=db, decision_client=LowScoreDecision(), now=now_dt,  # type: ignore[arg-type]
    )
    assert not any(w["mint"] == coil_mint for w in db.get_watchlist())

    # 2. Approved BREAKOUT_IGNITION logs strategy=BREAKOUT_IGNITION + metrics in DuckDB & CSV
    class BuyDecision:
        async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
            return {
                "t0_narrative_strength": {"type": "score", "score": 4.0, "confidence": 0.85},
                "t0_momentum_quality": {"type": "score", "score": 4.2, "confidence": 0.88},
                "t0_is_copycat_vamp": {"type": "noul", "noul": 0.15, "confidence": 0.9},
                "t0_action": {"type": "choice", "choice": "BUY", "confidence": 0.86},
            }

    bo_mint = "MintBreakout1111111111111111111111111111111"
    bo_cand = {
        "mint": bo_mint, "symbol": "IGNITE",
        "pair_address": "PoolIgnite11111111111111111111111111111111",
        "dex_id": "pumpswap",
        "added_at": (now_dt - timedelta(minutes=5)).isoformat(),
        "candles_5m": _make_breakout_candles(count=18),
        **_strong_flow_pair(1.12),
    }
    out = await alpha_node(
        {"focus_state": FocusState.SCANNING.value, "sol_balance": 1.0, "safe_candidates": [bo_cand]},
        config=cfg, db=db, decision_client=BuyDecision(), now=now_dt,  # type: ignore[arg-type]
    )
    assert out["pending_order"] is not None
    assert out["pending_order"]["strategy"] == "BREAKOUT_IGNITION"

    tc = TestClient(create_telemetry_app(config=cfg, db=db))
    rows = list(csv.DictReader(io.StringIO(tc.get("/api/export/csv?type=watchlist").text)))
    d1 = next(r for r in rows if r["action"] == "DECISION_1" and r["symbol"] == "IGNITE")
    assert d1["strategy"] == "BREAKOUT_IGNITION"
    assert "ext_ema9=" in d1["reason"] and "vol=" in d1["reason"]
    # Verify all 6 new breakout_* columns are serialized in DuckDB & /api/export/csv
    assert int(d1["breakout_score"]) == 100
    assert d1["breakout_state"] == "IGNITION"
    assert d1["breakout_timeframe"] == "5m"
    assert float(d1["breakout_vol_ratio"]) >= 2.0
    assert float(d1["breakout_ema_ext_pct"]) > 0.0
    assert d1["breakout_failed_gates"] == ""

    # Verify EVICT row also carries breakout_* columns
    evict_row = next(r for r in rows if r["action"] == "EVICT" and r["symbol"] == "COIL")
    assert evict_row["breakout_score"] != ""
    assert evict_row["breakout_state"] in {"FORMING", "COILING", "IGNITION", "EXTENDED"}
    assert "VOL_SPIKE(" in evict_row["breakout_failed_gates"]

    # Verify /api/telemetry surfaces breakout_telemetry inside active_watchlist[]
    db.upsert_watchlist_entry(
        mint=bo_mint, symbol="IGNITE", added_at=(now_dt - timedelta(minutes=5)).isoformat(),
        candidate={"mint": bo_mint, "symbol": "IGNITE", "liquidity_usd": 85_000.0, "market_cap_usd": 650_000.0},
    )
    await alpha_node(
        {"focus_state": FocusState.SCANNING.value, "sol_balance": 1.0, "safe_candidates": [bo_cand]},
        config=cfg, db=db, decision_client=BuyDecision(), now=now_dt,  # type: ignore[arg-type]
    )
    tel = tc.get("/api/telemetry").json()
    slot = next(s for s in tel["active_watchlist"] if s["mint"] == bo_mint)
    bt = slot["breakout_telemetry"]
    assert bt is not None
    assert bt["score"] == 100
    assert bt["state"] == "IGNITION"
    assert bt["timeframe"] == "5m"
    assert "COIL" in bt["passed_gates"] and "RES" in bt["passed_gates"]
    assert bt["failed_gates"] == []
    db.close()


def test_breakout_telemetry_score_states_and_failed_gates_formatting():
    # Low-volume (1.2x < 1.5x) breakout -> score >= 75 (95), but VOL_SPIKE failed so state is capped at COILING!
    candles_low_vol = _make_breakout_candles(
        count=18, breakout_close=1.12, breakout_high=1.13, breakout_low=1.06, breakout_vol=1200.0
    )
    res_lv = evaluate_scalper_entry(candles_low_vol, _strong_flow_pair(1.12), time_in_slot_min=5.0)
    bt_lv = res_lv.breakout_telemetry
    assert bt_lv is not None
    assert bt_lv["score"] == 95  # 100 - 5 (partial vol credit for 1.2x >= 1.2x)
    assert bt_lv["state"] == "COILING"  # Capped at COILING because VOL_SPIKE failed
    assert res_lv.eligible is False
    assert "VOL_SPIKE(1.2x<1.5x)" in bt_lv["failed_gates"]

    # Over-extended breakout -> state == EXTENDED and hard-blocked even though score >= 75
    candles_ext = _make_breakout_candles(
        count=18, breakout_close=1.34, breakout_high=1.35, breakout_low=1.07, breakout_vol=3500.0
    )
    res_ext = evaluate_scalper_entry(candles_ext, _strong_flow_pair(1.34), time_in_slot_min=5.0)
    assert res_ext.eligible is False
    assert res_ext.breakout_telemetry is not None
    assert res_ext.breakout_telemetry["score"] >= 75
    assert res_ext.breakout_telemetry["state"] == "EXTENDED"
    assert any(g.startswith("EXT(") for g in res_ext.breakout_telemetry["failed_gates"])

    # 1m fallback reports timeframe == "1m_fallback"
    res_1m = evaluate_scalper_entry(
        _make_breakout_candles(count=5),
        _strong_flow_pair(1.12),
        time_in_slot_min=4.0,
        candles_1m=_make_breakout_candles(count=20, bar_seconds=60),
    )
    assert res_1m.breakout_telemetry is not None
    assert res_1m.breakout_telemetry["timeframe"] == "1m_fallback"


def test_breakout_ignition_allows_1_6x_vol_9_5_pct_coil_and_1_0x_buy_accel_and_caps_state_without_res_or_vol():
    # 1. Valid BREAKOUT_IGNITION fill with vol_ratio=1.6x, coil_range=~9.5% (<= 10.0%), and BUY_ACCEL=1.0x (no longer vetoed)
    candles = _make_breakout_candles(
        count=18, breakout_close=1.12, breakout_high=1.13, breakout_low=1.06, breakout_vol=1600.0
    )
    # Widen the 4-bar coil range to ~9.5% (low=0.985 to high=1.0786 -> 9.50% range, without monotonic higher lows)
    candles[-5]["low"] = 1.010
    candles[-4]["low"] = 0.985
    candles[-3]["low"] = 1.005
    candles[-2]["low"] = 1.000
    candles[-2]["high"] = 1.0786

    # Set 5m buys=20, 1h buys=240 -> 1h 5m-avg = 20 -> BUY_ACCEL = 1.0x (< 1.5x)
    pair_1x_accel = {
        "price_usd": 1.12,
        "liquidity_usd": 85_000.0,
        "market_cap_usd": 650_000.0,
        "txns": {"m5": {"buys": 20, "sells": 8}, "h1": {"buys": 240, "sells": 120}},
    }
    res = evaluate_scalper_entry(candles, pair_1x_accel, time_in_slot_min=5.0)
    assert res.eligible is True
    assert res.setup_type == "BREAKOUT_IGNITION"
    assert res.breakout_telemetry is not None
    assert res.breakout_telemetry["state"] == "IGNITION"
    assert res.breakout_telemetry["score"] >= 75
    assert res.breakout_telemetry["vol_ratio"] == pytest.approx(1.6, rel=1e-2)
    assert "COIL" in res.breakout_telemetry["passed_gates"]
    assert "RES" in res.breakout_telemetry["passed_gates"]
    assert "VOL" in res.breakout_telemetry["passed_gates"]
    assert any(g.startswith("BUY_ACCEL(1.0x<1.5x)") for g in res.breakout_telemetry["failed_gates"])

    # 2. Like $greg: score >= 75 (77/100) with 0.12x volume -> state MUST be capped at COILING (never IGNITION)
    candles_greg = _make_breakout_candles(
        count=18, breakout_close=1.12, breakout_high=1.13, breakout_low=1.06, breakout_vol=120.0
    )
    res_greg = evaluate_scalper_entry(candles_greg, _strong_flow_pair(1.12), time_in_slot_min=5.0)
    assert res_greg.eligible is False
    assert res_greg.breakout_telemetry is not None
    assert res_greg.breakout_telemetry["score"] >= 75
    assert res_greg.breakout_telemetry["state"] == "COILING"

    # 3. Score >= 75 (75/100) when RES fails (close=1.077 <= swing_high=1.078) -> state capped at COILING
    candles_no_res = _make_breakout_candles(
        count=18, breakout_close=1.077, breakout_high=1.078, breakout_low=1.06, breakout_vol=3000.0
    )
    res_no_res = evaluate_scalper_entry(candles_no_res, _strong_flow_pair(1.077), time_in_slot_min=5.0)
    assert res_no_res.eligible is False
    assert res_no_res.breakout_telemetry is not None
    assert res_no_res.breakout_telemetry["score"] == 75
    assert res_no_res.breakout_telemetry["state"] == "COILING"
