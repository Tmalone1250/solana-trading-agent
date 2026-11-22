"""Unit tests for the on-demand Insider Wallet Scanner (insider_scanner.py & POST /api/insider-scan)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from config import LAMPORTS_PER_SOL, AgentConfig
from db import AgentDatabase
from helius_webhook import clear_helius_roster_cache
from insider_scanner import MAX_HELIUS_PAGES, InsiderScanRequest, run_insider_scan
from telemetry_api import create_telemetry_app


def _make_pool_buy_tx(
    *,
    signature: str,
    wallet: str,
    mint: str,
    pool: str,
    sol_amount: float,
    token_amount: float,
    timestamp: int,
) -> dict[str, Any]:
    return {
        "signature": signature,
        "type": "SWAP",
        "timestamp": timestamp,
        "feePayer": wallet,
        "nativeTransfers": [
            {
                "fromUserAccount": wallet,
                "toUserAccount": pool,
                "amount": int(round(sol_amount * LAMPORTS_PER_SOL)),
            }
        ],
        "tokenTransfers": [
            {
                "mint": mint,
                "fromUserAccount": pool,
                "toUserAccount": wallet,
                "tokenAmount": token_amount,
            }
        ],
    }


@pytest.mark.asyncio
async def test_insider_scanner_autodetect_breakout_aggregation_min_sol_and_roster_flag(tmp_path):
    """Verify:
    (a) Auto-detecting breakout_ts from highest-volume green 5m candle on GeckoTerminal.
    (b) Aggregating multiple buys per wallet and filtering out swaps < min_sol.
    (c) Flagging already_in_roster wallets and formatting gmgn_url / solscan_url.
    """
    clear_helius_roster_cache()
    roster_whale = "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk"  # Jijo
    new_insider_early = "EarlyInsider11111111111111111111111111111111"
    small_retail = "SmallBuyer1111111111111111111111111111111111"
    target_mint = "MintRunner1111111111111111111111111111111111"
    pool_addr = "PoolRunner1111111111111111111111111111111111"
    breakout_candle_ts = 1_700_000_000

    cfg = AgentConfig(
        _env_file=None,
        HELIUS_API_KEY="test-helius-key",
        HELIUS_TRACKED_WALLETS=roster_whale,
    )

    # GeckoTerminal 5m candles: [timestamp, open, high, low, close, volume]
    ohlcv_list = [
        [breakout_candle_ts - 600, 1.00, 1.02, 0.99, 1.01, 12_000.0],
        # Red candle with high volume (should NOT be chosen over highest green volume candle)
        [breakout_candle_ts - 300, 1.05, 1.06, 0.95, 0.96, 95_000.0],
        # Highest-volume GREEN breakout candle at breakout_candle_ts
        [breakout_candle_ts, 1.00, 1.45, 0.99, 1.40, 88_000.0],
    ]

    helius_calls = {"pages": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if f"/tokens/v1/solana/{target_mint}" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": "solana",
                        "pairAddress": pool_addr,
                        "baseToken": {"address": target_mint, "symbol": "RUNNER"},
                        "liquidity": {"usd": 120_000.0},
                    }
                ],
            )
        if f"/networks/solana/pools/{pool_addr}/ohlcv/minute" in url:
            return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": ohlcv_list}}})
        if "/v0/webhooks" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "webhookID": "wh-1",
                        "webhookURL": "https://solana-api.projectkami.net/api/webhooks/helius",
                        "accountAddresses": [roster_whale],
                    }
                ],
            )
        if f"/v0/addresses/{pool_addr}/transactions" in url:
            helius_calls["pages"] += 1
            if helius_calls["pages"] > 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    # Early new insider: 2 buys (1.5 SOL at -15m, 2.25 SOL at -10m -> 3.75 SOL total, 2 buys)
                    _make_pool_buy_tx(
                        signature="sig-early-1",
                        wallet=new_insider_early,
                        mint=target_mint,
                        pool=pool_addr,
                        sol_amount=1.5,
                        token_amount=250_000.0,
                        timestamp=breakout_candle_ts - 900,
                    ),
                    _make_pool_buy_tx(
                        signature="sig-early-2",
                        wallet=new_insider_early,
                        mint=target_mint,
                        pool=pool_addr,
                        sol_amount=2.25,
                        token_amount=300_000.0,
                        timestamp=breakout_candle_ts - 600,
                    ),
                    # Tracked roster whale: 1 buy (3.0 SOL at -5m -> already_in_roster=True)
                    _make_pool_buy_tx(
                        signature="sig-roster-1",
                        wallet=roster_whale,
                        mint=target_mint,
                        pool=pool_addr,
                        sol_amount=3.0,
                        token_amount=400_000.0,
                        timestamp=breakout_candle_ts - 300,
                    ),
                    # Small retail buyer: 0.4 SOL (< min_sol=1.0) -> filtered out!
                    _make_pool_buy_tx(
                        signature="sig-small-1",
                        wallet=small_retail,
                        mint=target_mint,
                        pool=pool_addr,
                        sol_amount=0.4,
                        token_amount=50_000.0,
                        timestamp=breakout_candle_ts - 400,
                    ),
                ],
            )
        return httpx.Response(404)

    db = AgentDatabase(db_path=str(tmp_path / "insider.duckdb"), starting_sol=1.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        app = create_telemetry_app(config=cfg, db=db, http_client=client)
        tc = TestClient(app)
        resp = tc.post(
            "/api/insider-scan",
            json={"mint": target_mint, "breakout_ts": None, "lookback_mins": 30, "min_sol": 1.0},
        )
        assert resp.status_code == 200
        data = resp.json()

    assert data["mint"] == target_mint
    assert data["symbol"] == "RUNNER"
    assert data["pool_address"] == pool_addr
    assert data["breakout_ts"] == breakout_candle_ts
    assert len(data["candidates"]) == 2

    # Sorted by earliest first_buy_utc: new_insider_early (-15m) then roster_whale (-5m)
    c0 = data["candidates"][0]
    assert c0["wallet"] == new_insider_early
    assert c0["buy_count"] == 2
    assert pytest.approx(c0["total_sol_spent"]) == 3.75
    assert pytest.approx(c0["mins_before_breakout"]) == 15.0
    assert c0["already_in_roster"] is False
    assert c0["gmgn_url"] == f"https://gmgn.ai/sol/address/{new_insider_early}"
    assert c0["solscan_url"] == f"https://solscan.io/account/{new_insider_early}"

    c1 = data["candidates"][1]
    assert c1["wallet"] == roster_whale
    assert c1["buy_count"] == 1
    assert pytest.approx(c1["total_sol_spent"]) == 3.0
    assert pytest.approx(c1["mins_before_breakout"]) == 5.0
    assert c1["already_in_roster"] is True

    db.close()
    clear_helius_roster_cache()


@pytest.mark.asyncio
async def test_insider_scanner_enforces_5_page_pagination_cap():
    """Verify Helius pagination stops at MAX_HELIUS_PAGES=5 even when more pages exist."""
    clear_helius_roster_cache()
    target_mint = "MintPaginate11111111111111111111111111111111"
    pool_addr = "PoolPaginate11111111111111111111111111111111"
    breakout_ts = 1_700_010_000
    cfg = AgentConfig(_env_file=None, HELIUS_API_KEY="test-key")

    page_counter = {"count": 0, "cursors": []}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/tokens/v1/solana/" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": "solana",
                        "pairAddress": pool_addr,
                        "baseToken": {"address": target_mint, "symbol": "PAGE"},
                        "liquidity": {"usd": 90_000.0},
                    }
                ],
            )
        if "/v0/webhooks" in url:
            return httpx.Response(200, json=[])
        if f"/v0/addresses/{pool_addr}/transactions" in url:
            page_counter["count"] += 1
            page_counter["cursors"].append(request.url.params.get("before"))
            p_idx = page_counter["count"]
            return httpx.Response(
                200,
                json=[
                    _make_pool_buy_tx(
                        signature=f"sig-page-{p_idx}",
                        wallet=f"WalletPage{p_idx}111111111111111111111111111111",
                        mint=target_mint,
                        pool=pool_addr,
                        sol_amount=2.0,
                        token_amount=100_000.0,
                        timestamp=breakout_ts - (p_idx * 60),
                    )
                ],
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await run_insider_scan(
            InsiderScanRequest(mint=target_mint, breakout_ts=breakout_ts, lookback_mins=30, min_sol=1.0),
            config=cfg,
            http_client=client,
        )

    assert page_counter["count"] == MAX_HELIUS_PAGES == 5
    assert res["pages_scanned"] == 5
    assert len(res["candidates"]) == 5
    assert page_counter["cursors"] == [None, "sig-page-1", "sig-page-2", "sig-page-3", "sig-page-4"]
    clear_helius_roster_cache()


@pytest.mark.asyncio
async def test_unconditional_hard_stop_loss_on_leg2_reentry_chart_and_whale_positions_at_minus_11_75_pct(tmp_path):
    """Regression test for $MAYA:
    Verify that when a LEG_2_REENTRY chart position (basis=0.5000 SOL) and a WHALE_SCALP position
    (basis=0.2500 SOL) drop to -11.75% on batched_price_monitor (even with partial_tp_taken,
    breakeven_armed, trail_armed, or when Jupiter swap/v2/order fails), HARD_STOP_LOSS immediately
    fetches swap/v2/order (with live price fallback), executes a 100% SELL with reason='HARD_STOP_LOSS',
    and removes both positions from chart_positions and whale_positions.
    """
    from batched_price_monitor import (
        clear_price_backoff,
        monitor_and_enforce_hard_stops,
        needs_exact_jupiter_quote,
    )
    from position_monitor_node import position_monitor_node
    from state import ActivePosition, FocusState
    from whale_lane import run_whale_lane_tick

    clear_price_backoff()
    db = AgentDatabase(db_path=str(tmp_path / "maya_hard_stop.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(_env_file=None, HARD_STOP_LOSS_PCT=0.10, WHALE_HARD_STOP_PCT=-10.0)

    maya_mint = "MayaLeg2Reentry1111111111111111111111111111"
    whale_mint = "WhaleHardStop111111111111111111111111111111"

    # $MAYA after LEG_2_REENTRY (basis=0.5000 SOL, 1,000,000 atomic units = 1.0 token at 6 decimals)
    maya_pos = ActivePosition(
        mint=maya_mint,
        symbol="MAYA",
        entry_sol=0.50,
        initial_entry_sol=0.50,
        remaining_cost_basis_sol=0.50,
        token_amount=1_000_000,
        strategy="LEG2_ENTRY",
        reentry_count=1,
        breakeven_armed=True,
        trail_armed=True,
        current_sol_value=0.50,
        peak_sol_value=0.55,
    )
    # WHALE_SCALP position (basis=0.2500 SOL, 1,000,000 atomic units = 1.0 token at 6 decimals)
    whale_pos = ActivePosition(
        mint=whale_mint,
        symbol="WSTOP",
        entry_sol=0.25,
        initial_entry_sol=0.25,
        remaining_cost_basis_sol=0.25,
        token_amount=1_000_000,
        strategy="WHALE_SCALP",
        triggering_wallets=["4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk"],
        breakeven_armed=True,
        trail_armed=True,
        current_sol_value=0.25,
        peak_sol_value=0.27,
    )

    # -11.75% on 0.5000 SOL = 0.44125 SOL; -11.75% on 0.2500 SOL = 0.220625 SOL
    assert needs_exact_jupiter_quote(maya_pos, 0.44125, cfg) is True
    assert needs_exact_jupiter_quote(whale_pos, 0.220625, cfg) is True

    db.save_lane_positions(
        lane="chart",
        positions=[maya_pos.model_dump()],
        sol_balance=1.25,
        daily_pnl_sol=0.0,
        focus_state=FocusState.IN_POSITION.value,
        active_position=maya_pos.model_dump(),
    )
    db.save_lane_positions(
        lane="whale",
        positions=[whale_pos.model_dump()],
        sol_balance=1.25,
        daily_pnl_sol=0.0,
    )

    swap_quote_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/price/v3" in url:
            return httpx.Response(200, json={"So11111111111111111111111111111111111111112": {"usdPrice": 200.0, "decimals": 9}})
        if "/tokens/v1/solana/" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": "solana",
                        "baseToken": {"address": maya_mint, "symbol": "MAYA"},
                        "priceNative": "0.44125",  # -11.75% vs 0.5000 SOL basis
                        "liquidity": {"usd": 65_000.0},
                        "txns": {"m5": {"buys": 18, "sells": 22}},
                    },
                    {
                        "chainId": "solana",
                        "baseToken": {"address": whale_mint, "symbol": "WSTOP"},
                        "priceNative": "0.220625",  # -11.75% vs 0.2500 SOL basis
                        "liquidity": {"usd": 75_000.0},
                        "txns": {"m5": {"buys": 15, "sells": 25}},
                    },
                ],
            )
        if "/swap/v2/order" in url:
            in_mint = request.url.params.get("inputMint") or ""
            swap_quote_calls.append(in_mint)
            out_sol = 0.44125 if in_mint == maya_mint else 0.220625
            return httpx.Response(
                200,
                json={"outAmount": str(int(round(out_sol * LAMPORTS_PER_SOL))), "transaction": None},
            )
        return httpx.Response(404)

    class _NoopDecisionClient:
        async def decide(self, **_kwargs):
            return {}

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # 1. Run position_monitor_node on $MAYA (LEG_2_REENTRY chart position at -11.75%)
        st_before = db.get_state()
        mon_chart = await position_monitor_node(
            st_before,
            config=cfg,
            db=db,
            decision_client=_NoopDecisionClient(),  # type: ignore[arg-type]
            http_client=client,
        )
        assert "HARD_STOP_LOSS" in mon_chart["monitor_event"]
        assert mon_chart["chart_positions"] == []
        assert db.get_state()["chart_positions"] == []
        assert maya_mint in swap_quote_calls
        assert db.is_on_cooldown(maya_mint)

        # 2. Run whale lane tick on $WSTOP (WHALE_SCALP position at -11.75%)
        await run_whale_lane_tick(config=cfg, db=db, http_client=client)
        assert db.get_state()["whale_positions"] == []
        assert whale_mint in swap_quote_calls
        assert db.is_on_cooldown(whale_mint)

        # 3. Also verify direct monitor_and_enforce_hard_stops on batched_price_monitor when Jupiter /swap/v2/order fails (fallback to live price quote)
        maya_fallback = maya_pos.model_copy(update={"mint": "MayaFallback1111111111111111111111111111111", "symbol": "MAYAFB"})
        db.save_lane_positions(
            lane="chart",
            positions=[maya_fallback.model_dump()],
            sol_balance=1.0,
            daily_pnl_sol=0.0,
            focus_state=FocusState.IN_POSITION.value,
            active_position=maya_fallback.model_dump(),
        )

        def fail_jup_handler(req: httpx.Request) -> httpx.Response:
            u = str(req.url)
            if "/swap/v2/order" in u:
                return httpx.Response(503, json={"error": "Jupiter unavailable"})
            if "/tokens/v1/solana/" in u:
                return httpx.Response(
                    200,
                    json=[
                        {
                            "chainId": "solana",
                            "baseToken": {"address": maya_fallback.mint, "symbol": "MAYAFB"},
                            "priceNative": "0.44125",
                            "liquidity": {"usd": 50_000.0},
                            "txns": {"m5": {"buys": 10, "sells": 20}},
                        }
                    ],
                )
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(fail_jup_handler)) as fail_client:
            stops = await monitor_and_enforce_hard_stops(config=cfg, db=db, client=fail_client)
            assert len(stops) == 1
            assert "HARD_STOP_LOSS" in stops[0]["monitor_event"]
            assert db.get_state()["chart_positions"] == []

    sell_rows = db.get_action_log(actions=["SELL"], limit=10)
    assert len(sell_rows) == 3
    assert all("HARD_STOP_LOSS" in r["reason"] for r in sell_rows)
    db.close()
