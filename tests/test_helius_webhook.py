"""Unit tests for Phase 2: Helius Enhanced Webhook Whale Tracking."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from alpha_node import alpha_node
from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase
from helius_webhook import clear_helius_roster_cache, fetch_helius_tracked_wallets
from jupiter_token_client import clear_jupiter_token_cache
from scanner_node import clear_geckoterminal_cache, scanner_node
from state import FocusState
from telemetry_api import create_telemetry_app
from tests.test_scalper_entry_engine import make_sweep_reclaim_candles


class StubDecisionClient:
    def __init__(self, answers: dict[str, dict[str, Any]]):
        self.answers = answers
        self.last_input = ""

    async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        self.last_input = input_text
        return self.answers


def _make_swap_tx(
    *,
    signature: str,
    wallet: str,
    mint: str,
    side: str,
    sol_amount: float,
    token_amount: float,
    ts: int,
) -> dict[str, Any]:
    pool = "PoolVault1111111111111111111111111111111111"
    if side == "BUY":
        return {
            "signature": signature,
            "type": "SWAP",
            "timestamp": ts,
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
    return {
        "signature": signature,
        "type": "SWAP",
        "timestamp": ts,
        "feePayer": wallet,
        "nativeTransfers": [
            {
                "fromUserAccount": pool,
                "toUserAccount": wallet,
                "amount": int(round(sol_amount * LAMPORTS_PER_SOL)),
            }
        ],
        "tokenTransfers": [
            {
                "mint": mint,
                "fromUserAccount": wallet,
                "toUserAccount": pool,
                "tokenAmount": token_amount,
            }
        ],
    }


def test_helius_webhook_auth_401_and_200_empty(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "helius_auth.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(
        _env_file=None,
        ENABLE_HELIUS_WHALE_WEBHOOK=True,
        HELIUS_AUTH_HEADER="Bearer super-secret-whale-token",
    )
    tc = TestClient(create_telemetry_app(config=cfg, db=db))

    # Missing Authorization header -> 401
    r_missing = tc.post("/api/webhooks/helius", json=[])
    assert r_missing.status_code == 401

    # Wrong Authorization header -> 401
    r_wrong = tc.post(
        "/api/webhooks/helius",
        json=[],
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r_wrong.status_code == 401

    # Valid Authorization with empty / non-swap payload -> 200 OK quickly
    r_ok = tc.post(
        "/api/webhooks/helius",
        json=[{"signature": "sig-transfer", "type": "TRANSFER", "feePayer": "WalletA"}],
        headers={"Authorization": "Bearer super-secret-whale-token"},
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["recorded"] == 0
    assert r_ok.json()["ignored"] == 1
    db.close()


def test_helius_webhook_parses_buy_sell_filters_dust_and_deduplicates(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "helius_parse.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(
        _env_file=None,
        ENABLE_HELIUS_WHALE_WEBHOOK=True,
        HELIUS_AUTH_HEADER="Bearer secret123",
        HELIUS_TRACKED_WALLETS="WhaleWallet1,WhaleWallet2",
        WHALE_MIN_SOL_SPENT=0.5,
        WHALE_SIGNAL_TTL_MINUTES=30,
    )
    tc = TestClient(create_telemetry_app(config=cfg, db=db))
    now_ts = int(datetime.now(timezone.utc).timestamp())
    mint = "MintWhaleGem1111111111111111111111111111111"

    payload = [
        # Valid BUY by WhaleWallet1 (2.5 SOL)
        _make_swap_tx(
            signature="sig-buy-1",
            wallet="WhaleWallet1",
            mint=mint,
            side="BUY",
            sol_amount=2.5,
            token_amount=500_000.0,
            ts=now_ts,
        ),
        # Valid BUY by WhaleWallet2 (1.5 SOL)
        _make_swap_tx(
            signature="sig-buy-2",
            wallet="WhaleWallet2",
            mint=mint,
            side="BUY",
            sol_amount=1.5,
            token_amount=300_000.0,
            ts=now_ts,
        ),
        # Valid SELL by WhaleWallet1 (1.0 SOL)
        _make_swap_tx(
            signature="sig-sell-1",
            wallet="WhaleWallet1",
            mint=mint,
            side="SELL",
            sol_amount=1.0,
            token_amount=200_000.0,
            ts=now_ts,
        ),
        # Sub-threshold dust BUY (0.1 SOL < 0.5) -> ignored
        _make_swap_tx(
            signature="sig-dust",
            wallet="WhaleWallet1",
            mint=mint,
            side="BUY",
            sol_amount=0.1,
            token_amount=10_000.0,
            ts=now_ts,
        ),
        # Untracked wallet BUY -> ignored
        _make_swap_tx(
            signature="sig-untracked",
            wallet="RandomWallet999",
            mint=mint,
            side="BUY",
            sol_amount=5.0,
            token_amount=900_000.0,
            ts=now_ts,
        ),
        # Base-only swap (SOL -> USDC) -> ignored
        _make_swap_tx(
            signature="sig-usdc",
            wallet="WhaleWallet1",
            mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            side="BUY",
            sol_amount=10.0,
            token_amount=1500.0,
            ts=now_ts,
        ),
    ]

    headers = {"Authorization": "Bearer secret123"}
    r1 = tc.post("/api/webhooks/helius", json=payload, headers=headers)
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["recorded"] == 3
    assert body1["ignored"] == 3
    assert body1["duplicates"] == 0

    # Retry delivery of the exact same payload -> idempotent deduplication!
    r2 = tc.post("/api/webhooks/helius", json=payload, headers=headers)
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["recorded"] == 0
    assert body2["duplicates"] == 3

    signals = db.get_active_whale_signals(ttl_minutes=30, min_buys=1)
    assert mint in signals
    assert signals[mint]["buy_count"] == 2
    assert signals[mint]["unique_whales"] == 2
    assert signals[mint]["total_sol_spent"] == pytest.approx(4.0, rel=1e-4)
    assert signals[mint]["sell_count"] == 1
    assert db.count_whale_events_today() == 3
    db.close()


@pytest.mark.asyncio
async def test_scanner_merges_whale_mints_and_alpha_injects_whale_confluence_and_telemetry(tmp_path):
    clear_geckoterminal_cache()
    clear_jupiter_token_cache()
    db = AgentDatabase(db_path=str(tmp_path / "whale_pipeline.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(
        _env_file=None,
        ENABLE_HELIUS_WHALE_WEBHOOK=True,
        ENABLE_JUPITER_DISCOVERY=False,
        HUNTING_LANE="momentum_runners",
    )

    now_dt = datetime.now(timezone.utc)
    now_ms = int(now_dt.timestamp() * 1000)
    old_ms = now_ms - (2 * 3600 * 1000)
    whale_mint = "MintWhaleAlpha11111111111111111111111111111"

    # Seed whale_events -> whale_signals
    db.record_whale_event(
        signature="sig-w1",
        wallet="WhaleA",
        mint=whale_mint,
        side="BUY",
        sol_amount=3.2,
        token_amount=800_000.0,
        timestamp_utc=now_dt.isoformat(),
    )
    db.record_whale_event(
        signature="sig-w2",
        wallet="WhaleB",
        mint=whale_mint,
        side="BUY",
        sol_amount=1.8,
        token_amount=450_000.0,
        timestamp_utc=now_dt.isoformat(),
    )
    db.recompute_whale_signal(whale_mint, ttl_minutes=30, now=now_dt)

    batched_mints: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "geckoterminal.com" in url:
            return httpx.Response(200, json={"data": []})
        if any(p in url for p in ("token-boosts", "token-profiles", "community-takeovers")):
            return httpx.Response(200, json=[])
        if "/tokens/v1/solana/" in url:
            batched_mints.extend(url.split("/tokens/v1/solana/")[1].split(","))
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": "solana",
                        "pairAddress": "PoolWhale1",
                        "baseToken": {"address": whale_mint, "symbol": "WHALE", "name": "Whale Coin"},
                        "marketCap": 600_000,
                        "liquidity": {"usd": 90_000},
                        "volume": {"h1": 180_000, "m5": 24_000},
                        "priceChange": {"h1": 28.0, "m5": 6.5},
                        "txns": {"m5": {"buys": 32, "sells": 12}},
                        "pairCreatedAt": old_ms,
                    }
                ],
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        scan_out = await scanner_node(
            {"focus_state": FocusState.SCANNING.value, "daily_start_sol": 0.7, "daily_pnl_sol": 0.0},
            config=cfg,
            db=db,
            http_client=client,
            now_ms=now_ms,
            now=now_dt,
        )

    assert whale_mint in batched_mints
    assert len(scan_out["raw_candidates"]) == 1
    cand = scan_out["raw_candidates"][0]
    assert cand["discovery_source"] == "WHALE"

    # Verify alpha_node injects whale confluence into Decision 1 prompt context
    cand_soaked = dict(
        cand,
        added_at=(now_dt - timedelta(minutes=5)).isoformat(),
        candles_5m=make_sweep_reclaim_candles(),
        _ohlcv_5m_result=__import__("tests.test_e3b_cache_provenance", fromlist=["make_verified_ohlcv_result"]).make_verified_ohlcv_result(make_sweep_reclaim_candles(), last_ok_at=now_dt.timestamp()),
    )
    dec = StubDecisionClient(
        {
            "t0_narrative_strength": {"type": "score", "score": 4.0, "confidence": 0.86},
            "t0_momentum_quality": {"type": "score", "score": 4.2, "confidence": 0.87},
            "t0_is_copycat_vamp": {"type": "noul", "noul": 0.14, "confidence": 0.90},
            "t0_action": {"type": "choice", "choice": "BUY", "confidence": 0.85},
        }
    )
    alpha_out = await alpha_node(
        {"focus_state": FocusState.SCANNING.value, "sol_balance": 0.7, "safe_candidates": [cand_soaked]},
        config=cfg,
        db=db,
        decision_client=dec,  # type: ignore[arg-type]
        now=now_dt,
    )
    assert alpha_out["pending_order"] is not None
    assert "whale_buy_count=2" in dec.last_input
    assert "whale_unique_wallets=2" in dec.last_input
    assert "whale_total_sol_spent=5.000" in dec.last_input
    assert "whale_sell_count=0" in dec.last_input

    # Verify GET /api/telemetry exposes whale_discovery summary + whale_buys on active_watchlist
    app = create_telemetry_app(config=cfg, db=db)
    resp = TestClient(app).get("/api/telemetry")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["whale_discovery"]["enabled"] is True
    assert payload["whale_discovery"]["active_whale_mints"] == 1
    assert payload["whale_discovery"]["events_today"] == 2
    slot = payload["active_watchlist"][0]
    assert slot["discovery_source"] == "WHALE"
    assert slot["whale_buys"] == 2

    db.close()
    clear_geckoterminal_cache()
    clear_jupiter_token_cache()


@pytest.mark.asyncio
async def test_helius_roster_dynamic_sync_5m_cache_and_expanded_telemetry(tmp_path, monkeypatch):
    clear_helius_roster_cache()
    db = AgentDatabase(db_path=str(tmp_path / "helius_roster.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(
        _env_file=None,
        ENABLE_HELIUS_WHALE_WEBHOOK=True,
        HELIUS_API_KEY="test-helius-key",
        HELIUS_WEBHOOK_ID="",  # auto-match solana-api.projectkami.net
        HELIUS_ROSTER_CACHE_SECONDS=300.0,
    )

    jijo = "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk"
    new_axiom_wallet = "Ax1oMNewWalletAddr1111111111111111111111wXyZ"
    calls = {"count": 0, "wallets": [jijo, new_axiom_wallet]}

    def helius_handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        assert "/v0/webhooks" in str(request.url)
        assert request.url.params.get("api-key") == "test-helius-key"
        return httpx.Response(
            200,
            json=[
                {
                    "webhookID": "other-hook",
                    "webhookURL": "https://other.example.com/hook",
                    "accountAddresses": ["OtherWallet111"],
                },
                {
                    "webhookID": "kami-hook",
                    "webhookURL": "https://solana-api.projectkami.net/api/webhooks/helius",
                    "accountAddresses": calls["wallets"],
                },
            ],
        )

    # 1. Verify 5-minute caching on fetch_helius_tracked_wallets
    async with httpx.AsyncClient(transport=httpx.MockTransport(helius_handler)) as client:
        w1 = await fetch_helius_tracked_wallets(cfg, http_client=client, now_ts=1000.0)
        assert w1 == [jijo, new_axiom_wallet]
        assert calls["count"] == 1

        # Within 300s -> cached hit (0 extra network calls)
        w2 = await fetch_helius_tracked_wallets(cfg, http_client=client, now_ts=1200.0)
        assert w2 == [jijo, new_axiom_wallet]
        assert calls["count"] == 1

        # force_refresh=True -> hits Helius API immediately
        calls["wallets"] = [jijo, new_axiom_wallet, "BQVz7fQ1WsQmSTMY3umdPEPPTm1sdcBcX9sP7o6kPRmB"]
        w3 = await fetch_helius_tracked_wallets(cfg, force_refresh=True, http_client=client, now_ts=1205.0)
        assert len(w3) == 3
        assert calls["count"] == 2

    # 2. Seed one swap for Jijo (leave new_axiom_wallet at 0 swaps to verify left-join)
    now_iso = datetime.now(timezone.utc).isoformat()
    db.record_whale_event(
        signature="sig-jijo-1",
        wallet=jijo,
        mint="MintHotWhale1111111111111111111111111111111",
        side="BUY",
        sol_amount=4.25,
        token_amount=950_000.0,
        timestamp_utc=now_iso,
    )
    db.recompute_whale_signal("MintHotWhale1111111111111111111111111111111", ttl_minutes=30)

    # Patch fetch_helius_tracked_wallets in telemetry_api to use our mock transport
    async def _mock_fetch(c: AgentConfig, *, force_refresh: bool = False):
        async with httpx.AsyncClient(transport=httpx.MockTransport(helius_handler)) as client:
            return await fetch_helius_tracked_wallets(c, force_refresh=force_refresh, http_client=client)

    import telemetry_api

    monkeypatch.setattr(telemetry_api, "fetch_helius_tracked_wallets", _mock_fetch)

    app = create_telemetry_app(config=cfg, db=db)
    tc = TestClient(app)

    # Verify POST /api/webhooks/helius/sync
    sync_resp = tc.post("/api/webhooks/helius/sync", headers={"Origin": "http://localhost:3000"})
    assert sync_resp.status_code == 200
    assert sync_resp.headers.get("access-control-allow-origin") == "*"
    sync_body = sync_resp.json()
    assert sync_body["synced_count"] == 3
    assert jijo in sync_body["wallets"]

    # Verify expanded GET /api/telemetry whale_discovery payload
    tel_resp = tc.get("/api/telemetry")
    assert tel_resp.status_code == 200
    wd = tel_resp.json()["whale_discovery"]
    assert wd["enabled"] is True
    assert wd["tracked_wallet_count"] == 3
    assert wd["active_whale_mints"] == 1
    assert wd["events_today"] == 1
    assert wd["total_sol_volume_today"] == pytest.approx(4.25, rel=1e-4)
    assert len(wd["active_signals"]) == 1
    assert len(wd["recent_events"]) == 1
    assert wd["recent_events"][0]["wallet_label"] == "Jijo"

    roster_by_wallet = {r["wallet"]: r for r in wd["roster"]}
    assert set(roster_by_wallet.keys()) == set(calls["wallets"])
    assert roster_by_wallet[jijo]["wallet_label"] == "Jijo"
    assert roster_by_wallet[jijo]["buys_24h"] == 1
    assert roster_by_wallet[jijo]["total_sol_buys"] == pytest.approx(4.25, rel=1e-4)
    # Newly added Axiom wallet appears immediately at 0 swaps with truncated AbCd...wXyZ label
    assert roster_by_wallet[new_axiom_wallet]["wallet_label"] == "Ax1o...wXyZ"
    assert roster_by_wallet[new_axiom_wallet]["buys_24h"] == 0
    assert roster_by_wallet["BQVz7fQ1WsQmSTMY3umdPEPPTm1sdcBcX9sP7o6kPRmB"]["wallet_label"] == "Limfork.eth"

    db.close()
    clear_helius_roster_cache()


@pytest.mark.asyncio
async def test_helius_roster_hydrates_missing_account_addresses_via_detail_endpoint():
    clear_helius_roster_cache()
    cfg = AgentConfig(
        _env_file=None,
        ENABLE_HELIUS_WHALE_WEBHOOK=True,
        HELIUS_API_KEY="test-helius-key",
        HELIUS_WEBHOOK_ID="",
        HELIUS_ROSTER_CACHE_SECONDS=300.0,
    )

    jijo = "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk"
    shawty = "387FRwow6MKDhSwuMULhKjqeXuxoBY4BbbaSntdk9LaH"
    other_wallet = "OtherActiveWallet11111111111111111111111111"
    requested_paths: list[str] = []

    def handler_matching_url(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        assert request.url.params.get("api-key") == "test-helius-key"
        if request.url.path == "/v0/webhooks":
            # Summary metadata only — no accountAddresses field
            return httpx.Response(
                200,
                json=[
                    {
                        "webhookID": "inactive-hook",
                        "webhookURL": "https://solana-api.projectkami.net/old",
                        "active": False,
                    },
                    {
                        "webhookID": "kami-hook-123",
                        "webhookURL": "https://solana-api.projectkami.net/api/webhooks/helius",
                        "active": True,
                    },
                ],
            )
        if request.url.path == "/v0/webhooks/kami-hook-123":
            return httpx.Response(
                200,
                json={
                    "webhookID": "kami-hook-123",
                    "webhookURL": "https://solana-api.projectkami.net/api/webhooks/helius",
                    "accountAddresses": [jijo, shawty],
                },
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_matching_url)) as client:
        wallets = await fetch_helius_tracked_wallets(cfg, force_refresh=True, http_client=client, now_ts=2000.0)

    assert wallets == [jijo, shawty]
    assert requested_paths == ["/v0/webhooks", "/v0/webhooks/kami-hook-123"]

    # Fallback: when no filter matches, hydrate all active webhooks and deduplicate
    requested_paths.clear()

    def handler_fallback_all_active(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        assert request.url.params.get("api-key") == "test-helius-key"
        if request.url.path == "/v0/webhooks":
            return httpx.Response(
                200,
                json=[
                    {"webhookID": "hook-a", "webhookURL": "https://custom-a.example.com/hook", "accountAddresses": []},
                    {"webhookID": "hook-b", "webhookURL": "https://custom-b.example.com/hook"},
                    {"webhookID": "hook-disabled", "webhookURL": "https://disabled.example.com/hook", "active": False},
                ],
            )
        if request.url.path == "/v0/webhooks/hook-a":
            return httpx.Response(200, json={"webhookID": "hook-a", "accountAddresses": [jijo]})
        if request.url.path == "/v0/webhooks/hook-b":
            return httpx.Response(200, json={"webhookID": "hook-b", "accountAddresses": [jijo, other_wallet]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_fallback_all_active)) as client:
        wallets_all = await fetch_helius_tracked_wallets(cfg, force_refresh=True, http_client=client, now_ts=3000.0)

    assert wallets_all == [jijo, other_wallet]
    assert requested_paths == ["/v0/webhooks", "/v0/webhooks/hook-a", "/v0/webhooks/hook-b"]
    clear_helius_roster_cache()
