"""Unit tests for 2-Slot Concurrent Lanes, WHALE_SCALP hardening, strict roster filter & CSV cleanup."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from alpha_node import alpha_node
from config import LAMPORTS_PER_SOL, AgentConfig
from db import AgentDatabase
from executor_node import executor_node
from helius_webhook import clear_helius_roster_cache
from state import ActivePosition, FocusState, PendingOrder
from telemetry_api import create_telemetry_app
from whale_lane import evaluate_whale_exit_order, run_whale_lane_tick


@pytest.mark.asyncio
async def test_concurrent_chart_and_whale_slots_and_duplicate_mint_guard(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "two_slots.duckdb"), starting_sol=1.5)
    cfg = AgentConfig(
        _env_file=None, PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0,
        WHALE_STRATEGY_ENABLED=True,
        WHALE_POSITION_SOL=0.25,
        WHALE_MIN_UNIQUE_WALLETS=2,
        WHALE_MIN_LIQ_USD=40_000.0,
        ENABLE_JUPITER_DISCOVERY=False,
        PAPER_TX_FEE_SOL=0.0,
    )
    now_dt = datetime.now(timezone.utc)
    chart_mint = "MintChartOpen111111111111111111111111111111"
    whale_mint = "MintWhaleSlot211111111111111111111111111111"

    # Slot 1 (chart_slot) is currently in WATCHING_REENTRY on chart_mint
    chart_pos = ActivePosition(
        mint=chart_mint,
        symbol="CHART",
        entry_sol=0.5,
        token_amount=0,
        strategy="SWEEP_RECLAIM",
        current_sol_value=0.55,
        peak_sol_value=0.60,
        run_peak_sol=0.60,
        retrace_low_sol=0.55,
        watch_started_at=now_dt.isoformat(),
    ).model_dump()
    db.save_state(
        focus_state=FocusState.WATCHING_REENTRY.value,
        sol_balance=1.0,
        daily_pnl_sol=0.05,
        active_position=chart_pos,
    )

    # Seed 2-whale confluence on BOTH chart_mint (duplicate!) and whale_mint (fresh!)
    for idx, (m, w1, w2) in enumerate(
        [(chart_mint, "WhaleA", "WhaleB"), (whale_mint, "WhaleC", "WhaleD")]
    ):
        db.record_whale_event(
            signature=f"sig-{idx}-1", wallet=w1, mint=m, side="BUY",
            sol_amount=2.0, token_amount=100_000.0,
            timestamp_utc=(now_dt - timedelta(seconds=20)).isoformat(),
        )
        db.record_whale_event(
            signature=f"sig-{idx}-2", wallet=w2, mint=m, side="BUY",
            sol_amount=1.8, token_amount=90_000.0,
            timestamp_utc=(now_dt - timedelta(seconds=10)).isoformat(),
        )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/tokens/v1/solana/" in url:
            return httpx.Response(
                200,
                json=[
                    {"chainId": "solana", "baseToken": {"address": chart_mint, "symbol": "CHART"}, "marketCap": 200_000, "liquidity": {"usd": 80_000}},
                    {"chainId": "solana", "baseToken": {"address": whale_mint, "symbol": "WRUN"}, "marketCap": 350_000, "liquidity": {"usd": 65_000}},
                ],
            )
        if "rugcheck.xyz" in url:
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        if "/tokens/v2/search" in url:
            return httpx.Response(200, json=[{"id": request.url.params.get("query", whale_mint), "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "topHoldersPercentage": 15.46}}])
        if "/ultra/v1/shield" in url:
            return httpx.Response(200, json={"warnings": {whale_mint: []}})
        if "/swap/v2/order" in url:
            return httpx.Response(200, json={"outAmount": "1500000", "transaction": None})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await run_whale_lane_tick(config=cfg, db=db, http_client=client, now=now_dt)

    # Slot 2 skipped chart_mint (duplicate!) and entered whale_mint concurrently!
    assert db.get_whale_decisions()[chart_mint]["reason"] == "DUPLICATE_MINT_IN_CHART_SLOT"
    assert res["whale_position"]["mint"] == whale_mint
    assert res["whale_position"]["strategy"] == "WHALE_SCALP"
    assert res["whale_position"]["entry_mc_usd"] == pytest.approx(350_000.0)
    assert res["whale_position"]["whale_sol_flow"] == pytest.approx(3.8)

    # Slot 1 remained intact in WATCHING_REENTRY while Slot 2 opened!
    st = db.get_state()
    assert st["focus_state"] == FocusState.WATCHING_REENTRY.value
    assert st["active_position"]["mint"] == chart_mint
    assert st["whale_position"]["mint"] == whale_mint
    assert st["sol_balance"] == pytest.approx(0.75, rel=1e-4)

    # Verify reverse Duplicate Mint guard: Slot 1 executor refuses to buy whale_mint
    dup_order = PendingOrder(
        side="BUY", mint=whale_mint, symbol="WRUN", amount_atomic=500_000_000,
        reason="DECISION_1_ENTRY", target_state_after_fill=FocusState.IN_POSITION, strategy="SWEEP_RECLAIM",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        dup_out = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 0.75, "daily_pnl_sol": 0.05, "pending_order": dup_order.model_dump()},
            config=cfg, db=db, http_client=client,
        )
    assert dup_out == {"pending_order": None}

    # Verify /api/telemetry exposes both slots without breaking existing fields
    tc = TestClient(create_telemetry_app(config=cfg, db=db))
    tel = tc.get("/api/telemetry").json()
    assert tel["active_position"]["mint"] == chart_mint
    assert tel["whale_position"]["mint"] == whale_mint
    assert len(tel["active_positions"]) == 2
    assert {p["slot"] for p in tel["active_positions"]} == {"chart_slot", "whale_slot"}

    # Verify WHALE_SCALP BUY CSV row includes mc_usd and whale_sol_flow
    csv_rows = list(csv.DictReader(io.StringIO(tc.get("/api/export/csv?type=trades").text)))
    buy_row = next(r for r in csv_rows if r["action"] == "BUY")
    assert float(buy_row["mc_usd"]) == pytest.approx(350_000.0)
    assert float(buy_row["whale_sol_flow"]) == pytest.approx(3.8)
    db.close()


@pytest.mark.asyncio
async def test_whale_hardening_liq_40k_time_stop_env_and_be_shield_5_1(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "hardening.duckdb"), starting_sol=1.0)
    cfg = AgentConfig(
        _env_file=None, PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0,
        WHALE_MIN_UNIQUE_WALLETS=2,
        WHALE_MIN_LIQ_USD=40_000.0,
        WHALE_BREAKEVEN_ARM_PCT=5.0,
        WHALE_BREAKEVEN_FLOOR_PCT=1.0,
        WHALE_TIME_STOP_SECONDS=300,
        ENABLE_JUPITER_DISCOVERY=False,
    )
    now_dt = datetime.now(timezone.utc)
    pdog_mint = "MintPdogLowLiq11111111111111111111111111111"

    # 1. $PDOG at $12.7K liquidity (< $40K) is rejected even with 2 unique whales
    db.record_whale_event(signature="p1", wallet="W1", mint=pdog_mint, side="BUY", sol_amount=2.0, token_amount=100.0, timestamp_utc=(now_dt - timedelta(seconds=15)).isoformat())
    db.record_whale_event(signature="p2", wallet="W2", mint=pdog_mint, side="BUY", sol_amount=2.0, token_amount=100.0, timestamp_utc=(now_dt - timedelta(seconds=10)).isoformat())

    def liq_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[{"chainId": "solana", "baseToken": {"address": pdog_mint, "symbol": "PDOG"}, "marketCap": 80_000, "liquidity": {"usd": 12_700}}],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(liq_handler)) as client:
        out = await run_whale_lane_tick(config=cfg, db=db, http_client=client, now=now_dt)
    assert out["event"] == "ALL_CANDIDATES_FILTERED"
    assert db.get_whale_decisions()[pdog_mint]["reason"] == "LIQ < $40K"

    # 2. Whale Breakeven Shield (+5% arm / +1% floor): $win hits +5.70% peak -> exits at +0.9% instead of -10%
    pos = ActivePosition(
        mint="MintWinBE1111111111111111111111111111111111",
        symbol="WIN",
        entry_sol=0.25,
        token_amount=1_000_000,
        strategy="WHALE_SCALP",
        opened_at=now_dt.isoformat(),
        current_sol_value=0.25,
        peak_sol_value=0.25,
    )
    # Peak +5.70% (0.26425 SOL) -> arms breakeven shield, holds
    ord1, ev1 = evaluate_whale_exit_order(pos, current_sol_value=0.26425, config=cfg, db=db, now_dt=now_dt)
    assert ord1 is None
    assert ev1 == "HOLD_WHALE_SCALP"
    assert pos.breakeven_armed is True

    # Pulls back to +0.9% (0.25225 SOL <= +1.0% floor) -> exits WHALE_BREAKEVEN_SHIELD!
    ord2, ev2 = evaluate_whale_exit_order(pos, current_sol_value=0.25225, config=cfg, db=db, now_dt=now_dt)
    assert ord2 is not None
    assert ev2 == "WHALE_BREAKEVEN_SHIELD"
    assert ord2.reason == "WHALE_BREAKEVEN_SHIELD"

    # 3. Custom WHALE_TIME_STOP_SECONDS from config (e.g., 180s) respected dynamically
    cfg_180 = AgentConfig(_env_file=None, PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0, WHALE_TIME_STOP_SECONDS=180)
    pos_t = ActivePosition(
        mint="MintTime180", symbol="TIME", entry_sol=0.25, token_amount=1_000_000,
        strategy="WHALE_SCALP", opened_at=(now_dt - timedelta(seconds=185)).isoformat(),
        current_sol_value=0.25, peak_sol_value=0.25,
    )
    ord_t, ev_t = evaluate_whale_exit_order(pos_t, current_sol_value=0.252, config=cfg_180, db=db, now_dt=now_dt)
    assert ord_t is not None and ev_t == "WHALE_TIME_STOP"
    db.close()


@pytest.mark.asyncio
async def test_strict_webhook_roster_filter_and_decision_1_blocked_logging(tmp_path):
    clear_helius_roster_cache()
    db = AgentDatabase(db_path=str(tmp_path / "strict_roster.duckdb"), starting_sol=1.0)
    roster_wallet = "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk"  # Jijo
    non_roster_wallet = "EqiFgyNw6kgrmYstWyrP8VjKhka7XEmKTZzHSmwpr1Zb"  # Previously malformed "ban"
    cfg = AgentConfig(
        _env_file=None, PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0,
        ENABLE_HELIUS_WHALE_WEBHOOK=True,
        HELIUS_AUTH_HEADER="Bearer secret-tok",
        HELIUS_TRACKED_WALLETS=roster_wallet,
    )
    tc = TestClient(create_telemetry_app(config=cfg, db=db))
    now_ts = int(datetime.now(timezone.utc).timestamp())
    mint = "MintRosterTest11111111111111111111111111111"

    payload = [
        # Non-roster wallet -> MUST be dropped
        {
            "signature": "sig-non-roster", "type": "SWAP", "timestamp": now_ts, "feePayer": non_roster_wallet,
            "nativeTransfers": [{"fromUserAccount": non_roster_wallet, "toUserAccount": "Pool", "amount": 2 * LAMPORTS_PER_SOL}],
            "tokenTransfers": [{"mint": mint, "fromUserAccount": "Pool", "toUserAccount": non_roster_wallet, "tokenAmount": 100_000.0}],
        },
        # Roster wallet -> MUST be recorded with short mint / ticker in CSV symbol (not "BUY")
        {
            "signature": "sig-roster-ok", "type": "SWAP", "timestamp": now_ts, "feePayer": roster_wallet,
            "nativeTransfers": [{"fromUserAccount": roster_wallet, "toUserAccount": "Pool", "amount": 2 * LAMPORTS_PER_SOL}],
            "tokenTransfers": [{"mint": mint, "fromUserAccount": "Pool", "toUserAccount": roster_wallet, "tokenAmount": 100_000.0}],
        },
    ]
    r = tc.post("/api/webhooks/helius", json=payload, headers={"Authorization": "Bearer secret-tok"})
    assert r.status_code == 200
    assert r.json()["recorded"] == 1
    assert r.json()["ignored"] == 1

    wh_rows = list(csv.DictReader(io.StringIO(tc.get("/api/export/csv?type=whales").text)))
    assert len(wh_rows) == 1
    assert wh_rows[0]["symbol"] == "MintRo..1111"  # short mint, NOT "BUY"
    assert wh_rows[0]["triggering_whales"] == "Jijo"

    # Verify DECISION_1 logs BLOCKED (never "BUY -> BUY") when strategy=NONE / high RSI rejects BUY
    class StubBuyDecision:
        async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
            return {
                "t0_narrative_strength": {"type": "score", "score": 3.5, "confidence": 0.8},
                "t0_momentum_quality": {"type": "score", "score": 3.5, "confidence": 0.8},
                "t0_is_copycat_vamp": {"type": "noul", "noul": 0.2, "confidence": 0.8},
                "t0_action": {"type": "choice", "choice": "BUY", "confidence": 0.85},
            }

    now_dt = datetime.now(timezone.utc)
    # No candles + unsoaked -> scalper rejects with strategy=NONE
    await alpha_node(
        {
            "focus_state": FocusState.SCANNING.value,
            "sol_balance": 1.0,
            "safe_candidates": [{"mint": "MintCC131", "symbol": "CC131", "added_at": now_dt.isoformat(), "candles_5m": []}],
        },
        config=cfg,
        db=db,
        decision_client=StubBuyDecision(),  # type: ignore[arg-type]
        now=now_dt,
    )
    watch_rows = list(csv.DictReader(io.StringIO(tc.get("/api/export/csv?type=watchlist").text)))
    d1 = next(row for row in watch_rows if row["action"] == "DECISION_1")
    assert d1["reason"].startswith("BLOCKED:")
    assert "BUY -> BUY" not in d1["reason"]
    db.close()
