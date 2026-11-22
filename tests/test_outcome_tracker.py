"""Unit tests for Shadow Outcome Tracker (outcome_tracker.py), /api/export/outcomes-csv & PAPER_TX_FEE_SOL."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from config import LAMPORTS_PER_SOL, AgentConfig
from db import AgentDatabase
from executor_node import executor_node
from outcome_tracker import (
    MAX_TRACKED_OUTCOMES,
    OUTCOME_COLUMNS,
    clear_outcome_tracker,
    get_active_outcome_count,
    get_recent_outcomes,
    poll_active_outcomes_once,
    record_outcome_snapshot,
)
from state import ActivePosition, FocusState, PendingOrder
from telemetry_api import create_telemetry_app


def test_outcome_tracker_dedup_within_15m_and_90_candidate_cap(tmp_path):
    """Verify 15m per-mint deduplication and strict 90-candidate in-memory cap."""
    clear_outcome_tracker()
    db = AgentDatabase(db_path=str(tmp_path / "outcome_cap.duckdb"), starting_sol=1.0)
    base_dt = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Duplicate snapshot for the same mint within 15m -> suppressed (returns None)
    id1 = record_outcome_snapshot(
        db, mint="MintDup1", symbol="DUP", source="CHART_ALPHA", decision="WATCH",
        entry_price_usd=1.0, now=base_dt,
    )
    assert id1 is not None
    id_dup = record_outcome_snapshot(
        db, mint="MintDup1", symbol="DUP", source="CHART_ALPHA", decision="BLOCKED",
        entry_price_usd=1.02, now=base_dt + timedelta(minutes=10),
    )
    assert id_dup is None

    # After 15m (>= 900s), the same mint CAN be recorded again
    id_after = record_outcome_snapshot(
        db, mint="MintDup1", symbol="DUP", source="CHART_ALPHA", decision="ENTERED",
        entry_price_usd=1.05, now=base_dt + timedelta(minutes=16),
    )
    assert id_after is not None

    # 2. Insert 105 distinct mints -> active in-memory map MUST stay capped at 90
    for i in range(MAX_TRACKED_OUTCOMES + 15):
        oid = record_outcome_snapshot(
            db, mint=f"MintCap_{i:03d}", symbol=f"C{i}", source="WHALE_WEBHOOK",
            decision="WHALE_BUY", entry_price_usd=1.0,
            now=base_dt + timedelta(minutes=17, seconds=i),
        )
        assert oid is not None
        assert get_active_outcome_count() <= MAX_TRACKED_OUTCOMES

    assert get_active_outcome_count() == MAX_TRACKED_OUTCOMES
    db.close()
    clear_outcome_tracker()


@pytest.mark.asyncio
async def test_outcome_tracker_batched_dexscreener_mfe_mae_milestones_finalize_and_csv(tmp_path):
    """Verify batched DexScreener updates compute mfe_pct, mae_pct, price_5m_pct, price_30m_pct,
    finalize & evict at 30m, and export cleanly via GET /api/export/outcomes-csv."""
    clear_outcome_tracker()
    db = AgentDatabase(db_path=str(tmp_path / "outcome_poll.duckdb"), starting_sol=1.0)
    cfg = AgentConfig(_env_file=None)
    t0 = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
    mint = "MintOutcomeTrack1111111111111111111111111111"

    oid = record_outcome_snapshot(
        db,
        mint=mint,
        symbol="TRACK",
        source="CHART_ALPHA",
        decision="BLOCKED",
        decision_reason="VOL_SPIKE(1.2x<1.5x)",
        strategy="BREAKOUT_IGNITION",
        entry_price_usd=1.00,
        entry_mc_usd=250_000.0,
        entry_liq_usd=60_000.0,
        breakout_score=72,
        breakout_state="COILING",
        leg2_score=68.5,
        vamp_score=0.12,
        rsi=58.4,
        now=t0,
    )
    assert oid is not None
    assert get_active_outcome_count() == 1

    live_price = {"usd": 0.90}  # -10% dip at 1m

    def dex_handler(request: httpx.Request) -> httpx.Response:
        assert "/tokens/v1/solana/" in str(request.url)
        return httpx.Response(
            200,
            json=[
                {
                    "chainId": "solana",
                    "baseToken": {"address": mint, "symbol": "TRACK"},
                    "priceUsd": str(live_price["usd"]),
                    "marketCap": 250_000.0 * live_price["usd"],
                    "liquidity": {"usd": 60_000.0},
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(dex_handler)) as client:
        # T+1m: drops to $0.90 (-10.0%) -> mae_pct = -10.0, price_1m_pct = -10.0
        await poll_active_outcomes_once(db, cfg, client, now=t0 + timedelta(minutes=1))
        # T+5m: rallies to $1.25 (+25.0%) -> mfe_pct = +25.0, price_5m_pct = +25.0
        live_price["usd"] = 1.25
        await poll_active_outcomes_once(db, cfg, client, now=t0 + timedelta(minutes=5))
        # T+15m: spikes to $1.40 (+40.0%) -> mfe_pct = +40.0, price_15m_pct = +40.0
        live_price["usd"] = 1.40
        await poll_active_outcomes_once(db, cfg, client, now=t0 + timedelta(minutes=15))
        assert get_active_outcome_count() == 1
        # T+30m: settles at $1.15 (+15.0%) -> price_30m_pct = +15.0, finalized = True, evicted from memory!
        live_price["usd"] = 1.15
        res30 = await poll_active_outcomes_once(db, cfg, client, now=t0 + timedelta(minutes=30))
        assert res30["finalized"] == 1
        assert get_active_outcome_count() == 0

    rows = get_recent_outcomes(db, limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["price_1m_pct"] == pytest.approx(-10.0)
    assert r["price_5m_pct"] == pytest.approx(25.0)
    assert r["price_15m_pct"] == pytest.approx(40.0)
    assert r["price_30m_pct"] == pytest.approx(15.0)
    assert r["mfe_pct"] == pytest.approx(40.0)
    assert r["mae_pct"] == pytest.approx(-10.0)
    assert r["finalized"] is True

    # Verify GET /api/export/outcomes-csv returns valid CSV headers and row
    tc = TestClient(create_telemetry_app(config=cfg, db=db))
    resp = tc.get("/api/export/outcomes-csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers.get("content-type", "")
    parsed = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(parsed) == 1
    assert list(parsed[0].keys()) == OUTCOME_COLUMNS
    assert parsed[0]["symbol"] == "TRACK"
    assert float(parsed[0]["mfe_pct"]) == pytest.approx(40.0)
    assert float(parsed[0]["mae_pct"]) == pytest.approx(-10.0)
    assert float(parsed[0]["price_30m_pct"]) == pytest.approx(15.0)
    db.close()
    clear_outcome_tracker()


@pytest.mark.asyncio
async def test_paper_tx_fee_sol_deducted_on_buy_partial_sell_and_sell(tmp_path):
    """Verify PAPER_TX_FEE_SOL (default 0.001 SOL) is deducted on paper BUY, PARTIAL_SELL, and SELL."""
    db = AgentDatabase(db_path=str(tmp_path / "paper_fees.duckdb"), starting_sol=1.0)
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper", PAPER_TX_FEE_SOL=0.001)
    mint = "MintFeeTest11111111111111111111111111111111"

    quote_out = {"val": 1_000_000}

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"outAmount": str(quote_out["val"]), "transaction": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # 1. Paper BUY 0.50 SOL -> sol_balance = 1.0 - 0.50 - 0.001 = 0.499, daily_pnl_sol = -0.001
        buy_order = PendingOrder(
            side="BUY", mint=mint, symbol="FEE", amount_atomic=int(0.50 * LAMPORTS_PER_SOL),
            reason="DECISION_1_ENTRY", target_state_after_fill=FocusState.IN_POSITION,
            strategy="BREAKOUT_IGNITION",
        )
        out_buy = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 1.0, "daily_pnl_sol": 0.0, "pending_order": buy_order.model_dump()},
            config=cfg, db=db, http_client=client,
        )
        assert out_buy["sol_balance"] == pytest.approx(0.499, rel=1e-6)
        assert out_buy["daily_pnl_sol"] == pytest.approx(-0.001, rel=1e-6)

        # 2. Paper PARTIAL_SELL 50% for 0.2875 SOL -> proceeds 0.2875 - 0.001 fee = +0.2865 balance, +0.0365 partial PnL
        quote_out["val"] = int(round(0.2875 * LAMPORTS_PER_SOL))
        partial_order = PendingOrder(
            side="PARTIAL_SELL", mint=mint, symbol="FEE", amount_atomic=500_000,
            reason="PARTIAL_TP_50PCT (+15%)", target_state_after_fill=FocusState.IN_POSITION,
            strategy="BREAKOUT_IGNITION",
        )
        out_part = await executor_node(
            {
                "focus_state": FocusState.IN_POSITION.value,
                "sol_balance": out_buy["sol_balance"],
                "daily_pnl_sol": out_buy["daily_pnl_sol"],
                "active_position": out_buy["active_position"],
                "chart_positions": out_buy["chart_positions"],
                "pending_order": partial_order.model_dump(),
            },
            config=cfg, db=db, http_client=client,
        )
        assert out_part["sol_balance"] == pytest.approx(0.499 + 0.2875 - 0.001, rel=1e-6)
        assert out_part["daily_pnl_sol"] == pytest.approx(-0.001 + (0.2875 - 0.25 - 0.001), rel=1e-6)

        # 3. Paper SELL remaining 50% for 0.270 SOL -> proceeds 0.270 - 0.001 fee = +0.269 balance, +0.019 runner PnL
        quote_out["val"] = int(round(0.270 * LAMPORTS_PER_SOL))
        sell_order = PendingOrder(
            side="SELL", mint=mint, symbol="FEE", amount_atomic=500_000,
            reason="TRAILING_TP", target_state_after_fill=FocusState.SCANNING,
            strategy="BREAKOUT_IGNITION",
        )
        out_sell = await executor_node(
            {
                "focus_state": FocusState.IN_POSITION.value,
                "sol_balance": out_part["sol_balance"],
                "daily_pnl_sol": out_part["daily_pnl_sol"],
                "active_position": out_part["active_position"],
                "chart_positions": out_part["chart_positions"],
                "pending_order": sell_order.model_dump(),
            },
            config=cfg, db=db, http_client=client,
        )
        expected_bal = 0.499 + (0.2875 - 0.001) + (0.270 - 0.001)
        expected_pnl = -0.001 + (0.2875 - 0.25 - 0.001) + (0.270 - 0.25 - 0.001)
        assert out_sell["sol_balance"] == pytest.approx(expected_bal, rel=1e-6)
        assert out_sell["daily_pnl_sol"] == pytest.approx(expected_pnl, rel=1e-6)
    db.close()
