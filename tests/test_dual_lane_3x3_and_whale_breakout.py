"""Unit tests for 3+3 Dual-Lane concurrency, batched price polling & Whale Breakout Gate."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from batched_price_monitor import (
    clear_price_backoff,
    fetch_batched_sol_prices,
    needs_exact_jupiter_quote,
    resolve_position_sol_value,
)
from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase
from executor_node import executor_node
from ohlcv_client import clear_ohlcv_cache
from position_monitor_node import position_monitor_node
from state import ActivePosition, FocusState, PendingOrder
from telemetry_api import create_telemetry_app
from tests.test_breakout_ignition import _make_breakout_candles
from whale_lane import run_whale_lane_tick


@pytest.mark.asyncio
async def test_3x3_dual_lane_caps_and_duplicate_mint_rejection(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "dual_3x3.duckdb"), starting_sol=5.0)
    cfg = AgentConfig(
        _env_file=None,
        MAX_CHART_POSITIONS=3,
        MAX_WHALE_POSITIONS=3,
        MAX_OPEN_POSITIONS=6,
    )

    def jup_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"outAmount": "1000000", "transaction": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(jup_handler)) as client:
        chart_pools = [
            "LbUiWL3xVV8hTFYBVdbTNrpDo41NKS6o3LHHuDzjfcY",
            "QWmroo4YnnMqYW3cnxWkFdaTxGD3P7vMSzwMHGbUzwF",
            "US517G5965aydkZ46HS38QLi7UQiSojurfbQfKCELFx",
        ]
        # Open 3 Chart positions -> all 3 succeed
        for i in range(3):
            st = db.get_state()
            order = PendingOrder(
                side="BUY", mint=f"MintChart{i}", symbol=f"CH{i}",
                pair_address=chart_pools[i], dex_id="pumpswap",
                amount_atomic=int(0.5 * LAMPORTS_PER_SOL), reason="DECISION_1_ENTRY",
                target_state_after_fill=FocusState.IN_POSITION, strategy="BREAKOUT_IGNITION",
            )
            out = await executor_node(
                {**st, "pending_order": order.model_dump()},
                config=cfg, db=db, http_client=client,
            )
            assert len(out["chart_positions"]) == i + 1

        # 4th Chart position -> BLOCKED by MAX_CHART_POSITIONS=3
        st = db.get_state()
        order_c4 = PendingOrder(
            side="BUY", mint="MintChart3", symbol="CH3",
            amount_atomic=int(0.5 * LAMPORTS_PER_SOL), reason="DECISION_1_ENTRY",
            target_state_after_fill=FocusState.IN_POSITION, strategy="SWEEP_RECLAIM",
        )
        out_c4 = await executor_node({**st, "pending_order": order_c4.model_dump()}, config=cfg, db=db, http_client=client)
        assert out_c4 == {"pending_order": None}
        assert len(db.get_state()["chart_positions"]) == 3

        # Duplicate mint from Chart lane into Whale lane -> BLOCKED
        order_dup = PendingOrder(
            side="BUY", mint="MintChart0", symbol="CH0",
            amount_atomic=int(0.25 * LAMPORTS_PER_SOL), reason="WHALE_SCALP_CONFLUENCE",
            target_state_after_fill=FocusState.IN_POSITION, strategy="WHALE_SCALP",
        )
        out_dup = await executor_node({**db.get_state(), "pending_order": order_dup.model_dump(), "slot": "whale_slot"}, config=cfg, db=db, http_client=client)
        assert out_dup == {"pending_order": None}

        # Open 3 Whale positions -> all 3 succeed (total 6 open positions!)
        for i in range(3):
            st = db.get_state()
            order = PendingOrder(
                side="BUY", mint=f"MintWhale{i}", symbol=f"WH{i}",
                amount_atomic=int(0.25 * LAMPORTS_PER_SOL), reason="WHALE_BREAKOUT_CONFLUENCE",
                target_state_after_fill=FocusState.IN_POSITION, strategy="WHALE_SCALP",
            )
            out = await executor_node(
                {**st, "pending_order": order.model_dump(), "slot": "whale_slot"},
                config=cfg, db=db, http_client=client,
            )
            assert len(out["whale_positions"]) == i + 1

        # 4th Whale position -> BLOCKED by MAX_WHALE_POSITIONS=3 / MAX_OPEN_POSITIONS=6
        order_w4 = PendingOrder(
            side="BUY", mint="MintWhale3", symbol="WH3",
            amount_atomic=int(0.25 * LAMPORTS_PER_SOL), reason="WHALE_SCALP_CONFLUENCE",
            target_state_after_fill=FocusState.IN_POSITION, strategy="WHALE_SCALP",
        )
        out_w4 = await executor_node({**db.get_state(), "pending_order": order_w4.model_dump(), "slot": "whale_slot"}, config=cfg, db=db, http_client=client)
        assert out_w4 == {"pending_order": None}

    tc = TestClient(create_telemetry_app(config=cfg, db=db))
    tel = tc.get("/api/telemetry").json()
    assert len(tel["chart_positions"]) == 3
    assert len(tel["whale_positions"]) == 3
    assert len(tel["active_positions"]) == 6
    db.close()


@pytest.mark.asyncio
async def test_batched_price_polling_and_on_demand_quote_near_thresholds():
    clear_price_backoff()
    cfg = AgentConfig(_env_file=None)
    calls = {"price_v3": 0, "dex": 0, "swap_quote": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/price/v3" in url:
            calls["price_v3"] += 1
            return httpx.Response(
                200,
                json={
                    SOL_MINT: {"usdPrice": 200.0},
                    "MintA": {"usdPrice": 102.0},
                    "MintB": {"usdPrice": 91.0},
                },
            )
        if "/tokens/v1/solana/" in url:
            calls["dex"] += 1
            return httpx.Response(200, json=[])
        if "/swap/v2/order" in url:
            calls["swap_quote"] += 1
            return httpx.Response(200, json={"outAmount": str(int(0.455 * LAMPORTS_PER_SOL))})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        prices, _ = await fetch_batched_sol_prices(["MintA", "MintB"], cfg, client, now_ts=1000.0)
        assert calls["price_v3"] == 1
        assert prices["MintA"] == pytest.approx(0.51, rel=1e-4)

        # Safe mid-range position (+2% PnL, far from -10% SL and +12% TP) -> NO individual swap quote!
        pos_safe = ActivePosition(mint="MintA", symbol="A", entry_sol=0.50, token_amount=1_000_000, current_sol_value=0.51, peak_sol_value=0.51)
        assert needs_exact_jupiter_quote(pos_safe, 0.51, cfg) is False
        val_safe, used_q1 = await resolve_position_sol_value(pos_safe, estimated_sol_value=0.51, config=cfg, client=client)
        assert used_q1 is False
        assert val_safe == pytest.approx(0.51)
        assert calls["swap_quote"] == 0

        # Position within 2% of -10% hard stop (-9% = 0.455 SOL) -> FIRES exact Jupiter swap quote!
        pos_near_sl = ActivePosition(mint="MintB", symbol="B", entry_sol=0.50, token_amount=1_000_000, current_sol_value=0.455, peak_sol_value=0.50)
        assert needs_exact_jupiter_quote(pos_near_sl, 0.455, cfg) is True
        val_exact, used_q2 = await resolve_position_sol_value(pos_near_sl, estimated_sol_value=0.455, config=cfg, client=client)
        assert used_q2 is True
        assert val_exact == pytest.approx(0.455)
        assert calls["swap_quote"] == 1


@pytest.mark.asyncio
async def test_whale_breakout_gate_blocks_extended_and_low_score_and_logs_csv(tmp_path):
    clear_ohlcv_cache()
    db = AgentDatabase(db_path=str(tmp_path / "whale_bo.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(
        _env_file=None,
        WHALE_STRATEGY_ENABLED=True,
        WHALE_MIN_UNIQUE_WALLETS=2,
        WHALE_MIN_LIQ_USD=40_000.0,
        WHALE_MIN_BREAKOUT_SCORE=50,
        WHALE_BLOCK_EXTENDED=True,
        ENABLE_JUPITER_DISCOVERY=False,
        OHLCV_PACING_DELAY_SECONDS=0.0,
    )
    now_dt = datetime.now(timezone.utc)
    ext_mint = "MintWhaleExt1111111111111111111111111111111"
    ign_mint = "MintWhaleIgn1111111111111111111111111111111"

    for tag, m in (("ext", ext_mint), ("ign", ign_mint)):
        db.record_whale_event(signature=f"{tag}-1", wallet=f"W{tag}1", mint=m, side="BUY", sol_amount=2.0, token_amount=100.0, timestamp_utc=(now_dt - timedelta(seconds=20)).isoformat())
        db.record_whale_event(signature=f"{tag}-2", wallet=f"W{tag}2", mint=m, side="BUY", sol_amount=2.0, token_amount=100.0, timestamp_utc=(now_dt - timedelta(seconds=10)).isoformat())
        db.recompute_whale_signal(m, ttl_minutes=30, now=now_dt)

    ext_candles = _make_breakout_candles(count=18, breakout_close=1.34, breakout_high=1.35, breakout_low=1.07, breakout_vol=3500.0)
    ign_candles = _make_breakout_candles(count=18, breakout_close=1.12, breakout_high=1.13, breakout_low=1.06, breakout_vol=3000.0)

    def _to_gecko(cs: list[dict[str, float]]) -> dict[str, Any]:
        return {"data": {"attributes": {"ohlcv_list": [[c["timestamp"], c["open"], c["high"], c["low"], c["close"], c["volume"]] for c in cs]}}}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/tokens/v1/solana/" in url:
            return httpx.Response(
                200,
                json=[
                    {"chainId": "solana", "pairAddress": "PoolExt", "baseToken": {"address": ext_mint, "symbol": "EXTW"}, "marketCap": 250_000, "liquidity": {"usd": 80_000}, "txns": {"m5": {"buys": 32, "sells": 14}, "h1": {"buys": 180, "sells": 120}}},
                    {"chainId": "solana", "pairAddress": "PoolIgn", "baseToken": {"address": ign_mint, "symbol": "IGNW"}, "marketCap": 300_000, "liquidity": {"usd": 90_000}, "txns": {"m5": {"buys": 32, "sells": 14}, "h1": {"buys": 180, "sells": 120}}},
                ],
            )
        if "pools/PoolExt/ohlcv" in url:
            return httpx.Response(200, json=_to_gecko(ext_candles))
        if "pools/PoolIgn/ohlcv" in url:
            return httpx.Response(200, json=_to_gecko(ign_candles))
        if "rugcheck.xyz" in url:
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        if "/ultra/v1/shield" in url:
            return httpx.Response(200, json={"warnings": {ign_mint: []}})
        if "/swap/v2/order" in url:
            return httpx.Response(200, json={"outAmount": "1500000", "transaction": None})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await run_whale_lane_tick(config=cfg, db=db, http_client=client, now=now_dt)

    # EXTENDED mint was rejected; IGNITION mint was entered with WHALE_BREAKOUT_CONFLUENCE!
    decs = db.get_whale_decisions()
    assert decs[ext_mint]["status"] == "SKIPPED"
    assert "BREAKOUT_EXTENDED" in decs[ext_mint]["reason"]
    assert decs[ext_mint]["breakout_telemetry"]["state"] == "EXTENDED"
    assert res["whale_position"]["mint"] == ign_mint
    assert res["event"] == "WHALE_BREAKOUT_CONFLUENCE_ENTERED"

    # Verify /api/export/csv has breakout_* columns populated on WHALE_CONFLUENCE & BUY rows
    tc = TestClient(create_telemetry_app(config=cfg, db=db))
    rows = list(csv.DictReader(io.StringIO(tc.get("/api/export/csv?type=all").text)))
    buy_row = next(r for r in rows if r["action"] == "BUY" and r["symbol"] == "IGNW")
    assert buy_row["reason"] == "WHALE_BREAKOUT_CONFLUENCE"
    assert int(buy_row["breakout_score"]) == 100
    assert buy_row["breakout_state"] == "IGNITION"
    assert buy_row["breakout_timeframe"] == "5m"

    ext_log = next(r for r in rows if r["action"] == "WHALE_CONFLUENCE" and r["symbol"] == "EXTW")
    assert ext_log["breakout_state"] == "EXTENDED"
    assert "EXT(" in ext_log["breakout_failed_gates"]
    db.close()
    clear_ohlcv_cache()


@pytest.mark.asyncio
async def test_jupiter_v3_flat_schema_dexscreener_pricenative_fallback_and_telemetry_chart_positions(tmp_path):
    """Verify (a) Jupiter /price/v3 flat usdPrice+decimals parsing, (b) DexScreener priceNative fallback
    when Jupiter omits a meme coin, and (c) /api/telemetry returning live updated chart_positions."""
    clear_price_backoff()
    db = AgentDatabase(db_path=str(tmp_path / "live_chart_tel.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(_env_file=None)
    mint_v3 = "MintJupV3Flat111111111111111111111111111111"
    mint_meme = "Fn95NtGXWPaJXc4N5y9rVkUq1NwZbjBNJzPVvNRLpump"  # omitted from /price/v3 -> uses priceNative

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/swap/v2/order" in url:
            # 10,000,000,000 atomic units (10,000 tokens at 6 decimals)
            return httpx.Response(200, json={"outAmount": "10000000000", "transaction": None})
        if "/price/v3" in url:
            ids = request.url.params.get("ids", "")
            assert SOL_MINT in ids
            # Flat V3 schema: includes SOL_MINT and mint_v3 (9 decimals), but omits mint_meme
            return httpx.Response(
                200,
                json={
                    SOL_MINT: {"usdPrice": 200.0, "decimals": 9},
                    mint_v3: {"usdPrice": 0.11, "decimals": 9},
                },
            )
        if "/tokens/v1/solana/" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": "solana",
                        "baseToken": {"address": mint_v3, "symbol": "V3TOK"},
                        "priceNative": "0.00050",
                        "liquidity": {"usd": 95_000.0},
                        "txns": {"m5": {"buys": 30, "sells": 10}},
                    },
                    {
                        "chainId": "solana",
                        "pairAddress": "YMN9Qj5jPNp7j14VPcML1B6xGgcPWVZUGLFU3Mnyfaf",
                        "dexId": "pumpswap",
                        "baseToken": {"address": mint_meme, "symbol": "SHIT"},
                        "priceNative": "0.0000535",  # 10,000 tokens * 0.0000535 = 0.535 SOL (+7.0% vs 0.50 entry)
                        "liquidity": {"usd": 131_250.5},
                        "txns": {"m5": {"buys": 40, "sells": 10}},
                    },
                ],
            )
        return httpx.Response(404)

    class _NoopDecisionClient:
        async def decide(self, **_kwargs):
            return {}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # (a) Direct verification of flat V3 usdPrice + decimals AND (b) priceNative fallback
        sol_prices, dex_flow = await fetch_batched_sol_prices([mint_v3, mint_meme], cfg, client, now_ts=2000.0)
        # mint_v3: 0.11 / 200.0 = 0.00055 SOL/token with 9 decimals from V3
        assert sol_prices[mint_v3] == pytest.approx(0.00055, rel=1e-5)
        assert dex_flow[mint_v3]["decimals"] == 9
        # mint_meme omitted by V3 -> falls back to DexScreener priceNative = 0.0000535 SOL/token
        assert sol_prices[mint_meme] == pytest.approx(0.0000535, rel=1e-5)

        # (c) Open $SHIT in chart_slot via executor_node (entry_sol = 0.50,entry_liq = 124395.86)
        buy_order = PendingOrder(
            side="BUY",
            mint=mint_meme,
            symbol="SHIT",
            pair_address="YMN9Qj5jPNp7j14VPcML1B6xGgcPWVZUGLFU3Mnyfaf",
            dex_id="pumpswap",
            amount_atomic=int(0.50 * LAMPORTS_PER_SOL),
            reason="DECISION_1_ENTRY",
            target_state_after_fill=FocusState.IN_POSITION,
            entry_liquidity_usd=124_395.86,
            strategy="DISPLACEMENT_RETEST",
        )
        st0 = db.get_state()
        await executor_node({**st0, "pending_order": buy_order.model_dump()}, config=cfg, db=db, http_client=client)

        # Run position_monitor_node tick -> must update chart_positions in-place using priceNative fallback
        st1 = db.get_state()
        mon_out = await position_monitor_node(
            st1,
            config=cfg,
            db=db,
            decision_client=_NoopDecisionClient(),  # type: ignore[arg-type]
            http_client=client,
        )
        assert mon_out["monitor_event"] == "HOLD_IN_POSITION"

    tc = TestClient(create_telemetry_app(config=cfg, db=db))
    tel = tc.get("/api/telemetry").json()
    assert len(tel["chart_positions"]) == 1
    cp0 = tel["chart_positions"][0]
    assert cp0["symbol"] == "SHIT"
    assert cp0["current_sol_value"] == pytest.approx(0.535, rel=1e-4)
    assert cp0["peak_sol_value"] == pytest.approx(0.535, rel=1e-4)
    assert cp0["pnl_sol"] == pytest.approx(0.035, rel=1e-4)
    assert cp0["pnl_pct"] == pytest.approx(7.0, rel=1e-3)
    assert cp0["last_liquidity_usd"] == pytest.approx(131_250.5, rel=1e-3)
    assert cp0["last_5m_buy_ratio"] == pytest.approx(0.80, rel=1e-3)
    assert cp0["last_5m_sell_ratio"] == pytest.approx(0.20, rel=1e-3)
    db.close()


@pytest.mark.asyncio
async def test_whale_scalp_two_step_partial_tp_runner_trail_and_shadow_exit_on_runner(tmp_path):
    """Verify:
    1. WHALE_SCALP scaling out 50% at +15%, arming breakeven (+2% floor), logging PARTIAL_SELL
       with strategy='WHALE_SCALP' and reason='WHALE_PARTIAL_TP_50PCT (+15%)', and trailing the
       remaining 50% runner at -10%.
    2. WHALE_SHADOW_EXIT firing on the remaining 50% runner and closing the position cleanly
       with accurate remaining PnL.
    """
    clear_price_backoff()
    db = AgentDatabase(db_path=str(tmp_path / "whale_partial_tp.duckdb"), starting_sol=1.0)
    cfg = AgentConfig(
        _env_file=None,
        WHALE_STRATEGY_ENABLED=True,
        WHALE_POSITION_SOL=0.25,
        WHALE_PARTIAL_TP_TRIGGER_PCT=15.0,
        WHALE_PARTIAL_TP_SELL_FRACTION=0.50,
        WHALE_RUNNER_TRAIL_PULLBACK_PCT=10.0,
        PAPER_TX_FEE_SOL=0.0,
    )
    now_dt = datetime.now(timezone.utc)
    mint_a = "MintWhalePartialA111111111111111111111111111"
    mint_b = "MintWhaleShadowB1111111111111111111111111111"

    pos_a = ActivePosition(
        mint=mint_a, symbol="WPART", entry_sol=0.25, token_amount=1_000_000,
        strategy="WHALE_SCALP", triggering_wallets=["WhaleAlpha"],
        opened_at=now_dt.isoformat(), entry_liquidity_usd=80_000.0,
        current_sol_value=0.25, peak_sol_value=0.25, reference_token_amount=1_000_000,
    ).model_dump()
    pos_b = ActivePosition(
        mint=mint_b, symbol="WSHAD", entry_sol=0.25, token_amount=1_000_000,
        strategy="WHALE_SCALP", triggering_wallets=["WhaleBeta"],
        opened_at=now_dt.isoformat(), entry_liquidity_usd=80_000.0,
        current_sol_value=0.25, peak_sol_value=0.25, reference_token_amount=1_000_000,
    ).model_dump()
    db.save_lane_positions(lane="whale", positions=[pos_a, pos_b], sol_balance=0.50, daily_pnl_sol=0.0)

    # Proportional price per atomic token unit for mint_a and mint_b
    unit_prices = {
        mint_a: 0.2875 / 1_000_000,  # +15% on 0.25 SOL
        mint_b: 0.2875 / 1_000_000,  # +15% on 0.25 SOL
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/price/v3" in url:
            return httpx.Response(200, json={SOL_MINT: {"usdPrice": 200.0, "decimals": 9}})
        if "/tokens/v1/solana/" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": "solana",
                        "baseToken": {"address": m, "symbol": "WPART" if m == mint_a else "WSHAD"},
                        "priceNative": f"{unit_prices[m] * (10 ** 6):.10f}",
                        "liquidity": {"usd": 85_000.0},
                        "txns": {"m5": {"buys": 35, "sells": 15}},
                    }
                    for m in (mint_a, mint_b)
                ],
            )
        if "/swap/v2/order" in url:
            in_mint = request.url.params.get("inputMint") or mint_a
            amt = int(request.url.params.get("amount") or 500_000)
            out_sol = amt * unit_prices[in_mint]
            return httpx.Response(200, json={"outAmount": str(int(round(out_sol * LAMPORTS_PER_SOL))), "transaction": None})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # Tick 1: Both Whale positions hit +15% (0.2875 SOL) -> Both scale out 50% (500,000 tokens for 0.14375 SOL each)
        await run_whale_lane_tick(config=cfg, db=db, http_client=client, now=now_dt)
        st1 = db.get_state()
        assert len(st1["whale_positions"]) == 2
        by_mint1 = {p["mint"]: p for p in st1["whale_positions"]}
        for m in (mint_a, mint_b):
            wp = by_mint1[m]
            assert wp["partial_tp_taken"] is True
            assert wp["token_amount"] == 500_000
            assert pytest.approx(wp["remaining_cost_basis_sol"]) == 0.125
            assert pytest.approx(wp["realized_partial_pnl_sol"]) == 0.01875
            assert wp["breakeven_armed"] is True
            assert wp["trail_armed"] is True
        # Both partial sells credited to sol_balance (0.50 + 2 * 0.14375 = 0.7875) & daily_pnl_sol (2 * 0.01875 = 0.0375)
        assert pytest.approx(st1["sol_balance"]) == 0.7875
        assert pytest.approx(st1["daily_pnl_sol"]) == 0.0375

        # Verify PARTIAL_SELL rows in DuckDB & CSV export + /api/telemetry whale_positions
        partial_rows = db.get_action_log(actions=["PARTIAL_SELL"], limit=10)
        assert len(partial_rows) == 2
        assert all(r["strategy"] == "WHALE_SCALP" for r in partial_rows)
        assert all(r["reason"] == "WHALE_PARTIAL_TP_50PCT (+15%)" for r in partial_rows)

        tc = TestClient(create_telemetry_app(config=cfg, db=db))
        tel1 = tc.get("/api/telemetry").json()
        assert all(wp["partial_tp_taken"] is True for wp in tel1["whale_positions"])
        assert all(pytest.approx(wp["realized_partial_pnl_sol"]) == 0.01875 for wp in tel1["whale_positions"])

        # Tick 2: Both runners climb to +28% on remaining 0.125 SOL basis (500,000 tokens -> 0.160 SOL) -> hold
        unit_prices[mint_a] = 0.160 / 500_000
        unit_prices[mint_b] = 0.160 / 500_000
        await run_whale_lane_tick(config=cfg, db=db, http_client=client, now=now_dt)
        st2 = db.get_state()
        assert len(st2["whale_positions"]) == 2

        # Tick 3: mint_a pulls back -7.5% from 0.160 peak (to 0.148 SOL -> survives -10% runner trail!),
        # while mint_b's triggering whale ("WhaleBeta") dumps -> WHALE_SHADOW_EXIT closes mint_b's remaining 50% runner!
        unit_prices[mint_a] = 0.148 / 500_000
        unit_prices[mint_b] = 0.150 / 500_000  # +20% on 0.125 remaining cost basis (+0.025 SOL runner PnL)
        assert db.flag_whale_shadow_exit(mint_b, "WhaleBeta") is True
        await run_whale_lane_tick(config=cfg, db=db, http_client=client, now=now_dt)
        st3 = db.get_state()
        assert [p["mint"] for p in st3["whale_positions"]] == [mint_a]
        # mint_b runner sold for 0.150 SOL against 0.125 remaining cost basis -> +0.025 SOL realized PnL
        assert pytest.approx(st3["sol_balance"]) == 0.7875 + 0.150
        assert pytest.approx(st3["daily_pnl_sol"]) == 0.0375 + 0.025
        sell_b = next(r for r in db.get_action_log(actions=["SELL"], limit=5) if r["asset_id"] == mint_b)
        assert sell_b["reason"] == "WHALE_SHADOW_EXIT"
        assert pytest.approx(sell_b["pnl_amount"]) == 0.025

        # Tick 4: mint_a pulls back -10% from 0.160 peak (to 0.144 SOL) -> exits via WHALE_PEAK_TRAIL (-10% runner trail)
        unit_prices[mint_a] = 0.144 / 500_000
        await run_whale_lane_tick(config=cfg, db=db, http_client=client, now=now_dt)
        st4 = db.get_state()
        assert st4["whale_positions"] == []
        sell_a = next(r for r in db.get_action_log(actions=["SELL"], limit=5) if r["asset_id"] == mint_a)
        assert sell_a["reason"] == "WHALE_PEAK_TRAIL"
        assert pytest.approx(sell_a["pnl_amount"]) == 0.144 - 0.125  # +0.019 SOL on remaining 50% runner

    db.close()
