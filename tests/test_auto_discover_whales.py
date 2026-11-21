"""Deterministic unit tests for scripts/auto_discover_whales.py."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from scripts.auto_discover_whales import run_discovery
from whale_discovery_scoring import (
    HeliusEnhancedPacer,
    RunnerPool,
    extract_buyer_candidates,
    score_wallet_history,
)

NOW_TS = 1_760_000_000  # Fixed UTC timestamp for deterministic tests


def _make_swap(
    *,
    sig: str,
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
            "signature": sig,
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
        "signature": sig,
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


def _build_human_profitable_txs(wallet: str, now_ts: int) -> list[dict[str, Any]]:
    """Build 60 swaps across 5 mints (all active in hours 6..21 UTC -> 8h sleep gap, diversified PnL)."""
    txs: list[dict[str, Any]] = []
    # Align base to midnight UTC 3 days ago
    day_start = (now_ts // 86400 - 3) * 86400
    mints = [f"MintGood{i}111111111111111111111111111111111" for i in range(5)]
    idx = 0
    for m_idx, mint in enumerate(mints):
        # 6 buys + 6 sells per mint = 60 txs total; each mint nets +4.0 SOL (total +20 SOL, 20% each)
        for j in range(6):
            hour = 8 + ((m_idx * 2 + j) % 12)  # Only hours 8..19 UTC -> hours 20..7 (12h) empty
            ts_buy = day_start + (j % 3) * 86400 + hour * 3600 + 60
            ts_sell = ts_buy + 300
            txs.append(
                _make_swap(
                    sig=f"{wallet}-b-{idx}",
                    wallet=wallet,
                    mint=mint,
                    side="BUY",
                    sol_amount=1.0,
                    token_amount=100_000.0,
                    ts=ts_buy,
                )
            )
            txs.append(
                _make_swap(
                    sig=f"{wallet}-s-{idx}",
                    wallet=wallet,
                    mint=mint,
                    side="SELL",
                    sol_amount=1.6667,
                    token_amount=100_000.0,
                    ts=ts_sell,
                )
            )
            idx += 1
    return txs


def test_rejects_24_7_bot_without_sleep_gap():
    wallet = "Bot247Wallet1111111111111111111111111111111"
    day_start = (NOW_TS // 86400 - 2) * 86400
    txs: list[dict[str, Any]] = []
    # Place trades in every single UTC hour 0..23 (0h sleep gap) across 5 profitable mints
    for h in range(24):
        mint = f"MintBot{h % 5}1111111111111111111111111111111111"
        ts_b = day_start + h * 3600 + 10
        ts_s = day_start + h * 3600 + 120
        txs.append(_make_swap(sig=f"b-{h}", wallet=wallet, mint=mint, side="BUY", sol_amount=1.0, token_amount=100.0, ts=ts_b))
        txs.append(_make_swap(sig=f"s-{h}", wallet=wallet, mint=mint, side="SELL", sol_amount=1.8, token_amount=100.0, ts=ts_s))
    # Add 4 more to exceed 50 txs
    for k in range(4):
        ts_b = day_start + 86400 + k * 3600 + 10
        txs.append(_make_swap(sig=f"extra-{k}", wallet=wallet, mint="MintBot01111111111111111111111111111111111", side="BUY", sol_amount=1.0, token_amount=100.0, ts=ts_b))

    res = score_wallet_history(wallet, txs, liquid_sol_balance=50.0, now_ts=NOW_TS)
    assert res.passed is False
    assert res.sleep_gap_hrs == 0
    assert any("NO_SLEEP_GAP" in r for r in res.reject_reasons)


def test_rejects_serial_deployer_dumper():
    wallet = "DeployerWallet11111111111111111111111111111"
    day_start = (NOW_TS // 86400 - 2) * 86400
    txs: list[dict[str, Any]] = []
    # 12 buys and 40 sells (sells > 2 * buys) in daytime hours
    for i in range(12):
        mint = f"MintDep{i % 4}1111111111111111111111111111111111"
        txs.append(_make_swap(sig=f"db-{i}", wallet=wallet, mint=mint, side="BUY", sol_amount=1.0, token_amount=1000.0, ts=day_start + 10 * 3600 + i * 60))
    for i in range(40):
        mint = f"MintDep{i % 4}1111111111111111111111111111111111"
        txs.append(_make_swap(sig=f"ds-{i}", wallet=wallet, mint=mint, side="SELL", sol_amount=0.8, token_amount=200.0, ts=day_start + 12 * 3600 + i * 60))

    res = score_wallet_history(wallet, txs, liquid_sol_balance=40.0, now_ts=NOW_TS)
    assert res.passed is False
    assert res.sell_count > 3 * res.buy_count
    assert any("DEPLOYER_DUMPER" in r for r in res.reject_reasons)

    # Human ladder-exiter (2.5x sells per buy <= 3x) should NOT be flagged as DEPLOYER_DUMPER
    ladder_txs = txs[:12] + txs[12:42]  # 12 buys, 30 sells (2.5x)
    ladder_res = score_wallet_history(wallet, ladder_txs, liquid_sol_balance=40.0, now_ts=NOW_TS)
    assert not any("DEPLOYER_DUMPER" in r for r in ladder_res.reject_reasons)


def test_rejects_one_hit_wonder_over_60_pct_pnl_from_single_mint():
    wallet = "LotteryWallet111111111111111111111111111111"
    day_start = (NOW_TS // 86400 - 2) * 86400
    txs: list[dict[str, Any]] = []
    # Mint 0: +20 SOL profit (83.3% of total +24 SOL)
    for i in range(10):
        ts = day_start + 10 * 3600 + i * 120
        txs.append(_make_swap(sig=f"lot-b0-{i}", wallet=wallet, mint="MintLottery0111111111111111111111111111111", side="BUY", sol_amount=1.0, token_amount=100.0, ts=ts))
        txs.append(_make_swap(sig=f"lot-s0-{i}", wallet=wallet, mint="MintLottery0111111111111111111111111111111", side="SELL", sol_amount=3.0, token_amount=100.0, ts=ts + 60))
    # Mints 1..4: +1 SOL each (+4 SOL combined)
    for m in range(1, 5):
        mint = f"MintSmall{m}11111111111111111111111111111111"
        for i in range(5):
            ts = day_start + 14 * 3600 + m * 600 + i * 60
            txs.append(_make_swap(sig=f"lot-b{m}-{i}", wallet=wallet, mint=mint, side="BUY", sol_amount=1.0, token_amount=100.0, ts=ts))
            txs.append(_make_swap(sig=f"lot-s{m}-{i}", wallet=wallet, mint=mint, side="SELL", sol_amount=1.2, token_amount=100.0, ts=ts + 30))

    res = score_wallet_history(wallet, txs, liquid_sol_balance=35.0, now_ts=NOW_TS)
    assert res.passed is False
    assert res.max_single_mint_share > 0.60
    assert any("ONE_HIT_WONDER" in r for r in res.reject_reasons)


@pytest.mark.asyncio
async def test_end_to_end_apply_preserves_pinned_and_calls_put_once(tmp_path):
    pinned_file = tmp_path / "pinned_whales.json"
    pinned_wallet_1 = "CupseyPinnedWallet1111111111111111111111111"
    pinned_wallet_2 = "DecuPinnedWallet111111111111111111111111111"
    stale_non_pinned = "StaleNonPinnedWallet11111111111111111111111"
    good_candidate = "GoodHumanWhale11111111111111111111111111111"
    bot_candidate = "Bot247Candidate1111111111111111111111111111"

    # Pre-seed pinned_whales.json so stale_non_pinned is NOT pinned
    pinned_file.write_text(
        json.dumps({"wallets": [pinned_wallet_1, pinned_wallet_2]}),
        encoding="utf-8",
    )

    cfg = AgentConfig(
        _env_file=None,
        HELIUS_API_KEY="test-helius-key",
        HELIUS_WEBHOOK_ID="hook-prod-99",
    )

    good_txs = _build_human_profitable_txs(good_candidate, NOW_TS)
    # 24/7 bot txs (0h sleep gap)
    day_start = (NOW_TS // 86400 - 2) * 86400
    bot_txs = [
        _make_swap(
            sig=f"bot-{h}-{k}",
            wallet=bot_candidate,
            mint=f"MintB{h % 4}11111111111111111111111111111111111",
            side="BUY" if k == 0 else "SELL",
            sol_amount=1.0 if k == 0 else 1.5,
            token_amount=100.0,
            ts=day_start + h * 3600 + k * 60,
        )
        for h in range(24)
        for k in (0, 1)
    ]

    put_calls: list[dict[str, Any]] = []
    sync_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        path = request.url.path

        # DexScreener Stage 1a
        if "token-boosts/top/v1" in url:
            return httpx.Response(
                200,
                json=[{"chainId": "solana", "tokenAddress": "RunnerMint111111111111111111111111111111111"}],
            )
        if "token-boosts/latest/v1" in url:
            return httpx.Response(200, json=[])
        if "/tokens/v1/solana/" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": "solana",
                        "pairAddress": "RunnerPool111111111111111111111111111111111",
                        "baseToken": {
                            "address": "RunnerMint111111111111111111111111111111111",
                            "symbol": "RUN",
                        },
                        "liquidity": {"usd": 120_000},
                        "marketCap": 2_500_000,
                        "priceChange": {"h24": 45.0},
                        "volume": {"h24": 900_000},
                    }
                ],
            )

        # Helius Webhook GET / PUT
        if path == "/v0/webhooks/hook-prod-99" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "webhookID": "hook-prod-99",
                    "webhookURL": "https://solana-api.projectkami.net/api/webhooks/helius",
                    "transactionTypes": ["SWAP"],
                    "webhookType": "enhanced",
                    "authHeader": "Bearer super-secret-auth",
                    "accountAddresses": [pinned_wallet_1, pinned_wallet_2, stale_non_pinned],
                },
            )
        if path == "/v0/webhooks/hook-prod-99" and request.method == "PUT":
            put_calls.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"webhookID": "hook-prod-99"})

        # Helius Enhanced Transactions for Runner Pool (Stage 1b)
        if path == "/v0/addresses/RunnerPool111111111111111111111111111111111/transactions":
            return httpx.Response(
                200,
                json=[
                    _make_swap(
                        sig="pool-buy-good",
                        wallet=good_candidate,
                        mint="RunnerMint111111111111111111111111111111111",
                        side="BUY",
                        sol_amount=3.0,
                        token_amount=50_000.0,
                        ts=NOW_TS - 600,
                    ),
                    _make_swap(
                        sig="pool-buy-bot",
                        wallet=bot_candidate,
                        mint="RunnerMint111111111111111111111111111111111",
                        side="BUY",
                        sol_amount=2.0,
                        token_amount=30_000.0,
                        ts=NOW_TS - 500,
                    ),
                ],
            )

        # Helius Enhanced Transactions for wallets (Stage 2 + staleness)
        if path == f"/v0/addresses/{stale_non_pinned}/transactions":
            # Last tx 96 hours ago -> stale (>72h)
            return httpx.Response(200, json=[{"signature": "old-tx", "timestamp": NOW_TS - 96 * 3600}])
        if path == f"/v0/addresses/{good_candidate}/transactions":
            return httpx.Response(200, json=good_txs)
        if path == f"/v0/addresses/{bot_candidate}/transactions":
            return httpx.Response(200, json=bot_txs)

        # Helius RPC getBalance
        if "mainnet.helius-rpc.com" in url and request.method == "POST":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {"value": int(42.5 * LAMPORTS_PER_SOL)}},
            )

        # Local live agent sync POST
        if path == "/api/webhooks/helius/sync" and request.method == "POST":
            sync_calls.append(url)
            return httpx.Response(200, json={"status": "ok"})

        return httpx.Response(404)

    async def _noop_sleep(_seconds: float) -> None:
        return None

    fast_pacer = HeliusEnhancedPacer(min_interval=0.0, sleep_fn=_noop_sleep)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # 1. Dry-run must NOT call PUT
        dry_res = await run_discovery(
            cfg,
            pinned_path=pinned_file,
            apply_changes=False,
            pacer=fast_pacer,
            http_client=client,
            now_ts=NOW_TS,
        )
        assert dry_res["put_called"] is False
        assert len(put_calls) == 0
        assert stale_non_pinned in dry_res["stale_pruned"]
        assert [c.wallet for c in dry_res["passing_candidates"]] == [good_candidate]

        # 2. --apply calls PUT exactly once, preserves pinned & webhook fields verbatim, and POSTs sync to :8002
        apply_res = await run_discovery(
            cfg,
            pinned_path=pinned_file,
            apply_changes=True,
            pacer=fast_pacer,
            http_client=client,
            now_ts=NOW_TS,
        )
        assert apply_res["put_called"] is True
        assert apply_res["sync_ok"] is True
        assert len(put_calls) == 1
        assert sync_calls == ["http://127.0.0.1:8002/api/webhooks/helius/sync"]

        put_body = put_calls[0]
        assert put_body["webhookURL"] == "https://solana-api.projectkami.net/api/webhooks/helius"
        assert put_body["transactionTypes"] == ["SWAP"]
        assert put_body["webhookType"] == "enhanced"
        assert put_body["authHeader"] == "Bearer super-secret-auth"
        assert put_body["accountAddresses"] == [pinned_wallet_1, pinned_wallet_2, good_candidate]
        assert stale_non_pinned not in put_body["accountAddresses"]
        assert bot_candidate not in put_body["accountAddresses"]


@pytest.mark.asyncio
async def test_helius_pacer_429_exponential_backoff_retry_succeeds():
    sleeps: list[float] = []
    attempts = {"n": 0}

    async def fake_sleep(sec: float) -> None:
        sleeps.append(sec)

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"ok": True})

    pacer = HeliusEnhancedPacer(min_interval=0.0, base_backoff=1.0, max_retries=4, sleep_fn=fake_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resp = await pacer.request(client, "GET", "https://api.helius.xyz/v0/webhooks")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert attempts["n"] == 3
    assert len(sleeps) == 2
    assert 0.75 <= sleeps[0] <= 1.25
    assert 1.50 <= sleeps[1] <= 2.50


@pytest.mark.asyncio
async def test_low_sol_prefilter_skips_enhanced_tx_and_unchanged_roster_skips_put(tmp_path):
    pinned_file = tmp_path / "pinned_whales.json"
    pinned_w = "PinnedWalletOnly111111111111111111111111111"
    broke_cand = "BrokeWalletUnder15Sol1111111111111111111111"
    pinned_file.write_text(json.dumps({"wallets": [pinned_w]}), encoding="utf-8")

    cfg = AgentConfig(_env_file=None, HELIUS_API_KEY="test-key", HELIUS_WEBHOOK_ID="hook-1")
    enhanced_tx_queried: list[str] = []
    put_count = {"n": 0}
    sync_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        path = request.url.path
        if "token-boosts/top/v1" in url:
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": "RunMint11111111111111111111111111111111111"}])
        if "token-boosts/latest/v1" in url:
            return httpx.Response(200, json=[])
        if "/tokens/v1/solana/" in url:
            return httpx.Response(
                200,
                json=[{
                    "chainId": "solana",
                    "pairAddress": "RunPool11111111111111111111111111111111111",
                    "baseToken": {"address": "RunMint11111111111111111111111111111111111", "symbol": "RUN"},
                    "liquidity": {"usd": 80_000},
                    "marketCap": 1_000_000,
                    "priceChange": {"h24": 25.0},
                    "volume": {"h24": 300_000},
                }],
            )
        if path == "/v0/webhooks/hook-1" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "webhookID": "hook-1",
                    "webhookURL": "https://solana-api.projectkami.net/api/webhooks/helius",
                    "transactionTypes": ["SWAP"],
                    "webhookType": "enhanced",
                    "accountAddresses": [pinned_w],
                },
            )
        if path == "/v0/webhooks/hook-1" and request.method == "PUT":
            put_count["n"] += 1
            return httpx.Response(200, json={"webhookID": "hook-1"})
        if path == "/v0/addresses/RunPool11111111111111111111111111111111111/transactions":
            return httpx.Response(
                200,
                json=[_make_swap(sig="b1", wallet=broke_cand, mint="RunMint11111111111111111111111111111111111", side="BUY", sol_amount=2.0, token_amount=1000.0, ts=NOW_TS - 60)],
            )
        if path == f"/v0/addresses/{broke_cand}/transactions":
            enhanced_tx_queried.append(broke_cand)
            return httpx.Response(200, json=[])
        if "mainnet.helius-rpc.com" in url and request.method == "POST":
            # Only 2.5 SOL (< 5.0 SOL default pre-filter)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": int(2.5 * LAMPORTS_PER_SOL)}})
        if path == "/api/webhooks/helius/sync":
            sync_count["n"] += 1
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    async def _noop_sleep(_sec: float) -> None:
        return None

    pacer = HeliusEnhancedPacer(min_interval=0.0, sleep_fn=_noop_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await run_discovery(cfg, pinned_path=pinned_file, apply_changes=True, verbose=True, pacer=pacer, http_client=client, now_ts=NOW_TS)

    # Low-SOL wallet rejected BEFORE Helius Enhanced Transactions is queried
    assert enhanced_tx_queried == []
    assert any(r.wallet == broke_cand and any("LOW_AMMO" in x for x in r.reject_reasons) for r in res["rejected_candidates"])
    # Roster unchanged -> skip both PUT and local sync POST
    assert res["roster_unchanged"] is True
    assert res["put_called"] is False
    assert put_count["n"] == 0
    assert sync_count["n"] == 0
    assert res["sync_url"] == "http://127.0.0.1:8002/api/webhooks/helius/sync"
    # Rejection funnel summary counts & report formatting
    assert res["funnel"]["candidates_found"] == 1
    assert res["funnel"]["rejected_low_sol"] == 1
    assert res["funnel"]["passed"] == 0
    assert "Rejection Funnel Summary:" in res["report"]
    assert "- Candidates Found: 1" in res["report"]
    assert "- Rejected (Low SOL): 1" in res["report"]


@pytest.mark.asyncio
async def test_wsol_and_native_input_swap_extraction():
    wsol_buyer = "WsolBuyerWallet1111111111111111111111111111"
    native_ev_buyer = "NativeEvBuyer111111111111111111111111111111"
    dust_buyer = "DustBuyerWallet1111111111111111111111111111"
    pool = "PoolVault1111111111111111111111111111111111"
    mint = "RunnerMint991111111111111111111111111111111"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                # Pure WSOL tokenTransfer swap (no nativeTransfers) >= 1.0 SOL
                {
                    "signature": "sig-wsol",
                    "type": "SWAP",
                    "timestamp": NOW_TS - 120,
                    "feePayer": wsol_buyer,
                    "nativeTransfers": [],
                    "tokenTransfers": [
                        {"mint": SOL_MINT, "fromUserAccount": wsol_buyer, "toUserAccount": pool, "tokenAmount": 1.25},
                        {"mint": mint, "fromUserAccount": pool, "toUserAccount": wsol_buyer, "tokenAmount": 50_000.0},
                    ],
                },
                # events.swap.nativeInput swap >= 1.0 SOL
                {
                    "signature": "sig-ev",
                    "type": "SWAP",
                    "timestamp": NOW_TS - 90,
                    "feePayer": native_ev_buyer,
                    "nativeTransfers": [],
                    "tokenTransfers": [
                        {"mint": mint, "fromUserAccount": pool, "toUserAccount": native_ev_buyer, "tokenAmount": 40_000.0},
                    ],
                    "events": {"swap": {"nativeInput": {"account": native_ev_buyer, "amount": str(int(1.1 * LAMPORTS_PER_SOL))}}},
                },
                # Sub-1.0 SOL dust swap -> excluded
                {
                    "signature": "sig-dust",
                    "type": "SWAP",
                    "timestamp": NOW_TS - 60,
                    "feePayer": dust_buyer,
                    "nativeTransfers": [],
                    "tokenTransfers": [
                        {"mint": SOL_MINT, "fromUserAccount": dust_buyer, "toUserAccount": pool, "tokenAmount": 0.4},
                        {"mint": mint, "fromUserAccount": pool, "toUserAccount": dust_buyer, "tokenAmount": 10_000.0},
                    ],
                },
            ],
        )

    async def _noop_sleep(_sec: float) -> None:
        return None

    runner = RunnerPool(mint=mint, symbol="RUN", pair_address=pool, liquidity_usd=100_000, market_cap_usd=1_000_000, price_change_24h=30.0, volume_24h=500_000)
    pacer = HeliusEnhancedPacer(min_interval=0.0, sleep_fn=_noop_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        found = await extract_buyer_candidates(
            client, [runner], api_key="k", helius_base_url="https://api.helius.xyz/v0", exclude_wallets=set(), pacer=pacer, min_buy_sol=1.0
        )

    assert found == [wsol_buyer, native_ev_buyer]
