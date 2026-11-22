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
