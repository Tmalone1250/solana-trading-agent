"""Deterministic unit tests for scripts/auto_discover_whales.py."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from config import LAMPORTS_PER_SOL, AgentConfig
from scripts.auto_discover_whales import run_discovery
from whale_discovery_scoring import score_wallet_history

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
    assert res.sell_count > 2 * res.buy_count
    assert any("DEPLOYER_DUMPER" in r for r in res.reject_reasons)


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

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # 1. Dry-run must NOT call PUT
        dry_res = await run_discovery(
            cfg,
            pinned_path=pinned_file,
            apply_changes=False,
            http_client=client,
            now_ts=NOW_TS,
        )
        assert dry_res["put_called"] is False
        assert len(put_calls) == 0
        assert stale_non_pinned in dry_res["stale_pruned"]
        assert [c.wallet for c in dry_res["passing_candidates"]] == [good_candidate]

        # 2. --apply calls PUT exactly once, preserves pinned & webhook fields verbatim, and POSTs sync
        apply_res = await run_discovery(
            cfg,
            pinned_path=pinned_file,
            apply_changes=True,
            http_client=client,
            now_ts=NOW_TS,
        )
        assert apply_res["put_called"] is True
        assert apply_res["sync_ok"] is True
        assert len(put_calls) == 1
        assert len(sync_calls) == 1

        put_body = put_calls[0]
        assert put_body["webhookURL"] == "https://solana-api.projectkami.net/api/webhooks/helius"
        assert put_body["transactionTypes"] == ["SWAP"]
        assert put_body["webhookType"] == "enhanced"
        assert put_body["authHeader"] == "Bearer super-secret-auth"
        assert put_body["accountAddresses"] == [pinned_wallet_1, pinned_wallet_2, good_candidate]
        assert stale_non_pinned not in put_body["accountAddresses"]
        assert bot_candidate not in put_body["accountAddresses"]
