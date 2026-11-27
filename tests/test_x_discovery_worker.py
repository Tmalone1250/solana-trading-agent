"""Unit tests for X (TwitterAPI.io) Discovery Worker, DuckDB social tables, scanner & alpha wiring."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from alpha_node import alpha_node
from config import AgentConfig
from db import AgentDatabase
from scanner_node import clear_geckoterminal_cache, scanner_node
from state import FocusState
from telemetry_api import create_telemetry_app
from tests.test_scalper_entry_engine import make_sweep_reclaim_candles
from x_discovery_worker import extract_solana_mints, poll_x_once


class StubDecisionClient:
    def __init__(self, answers: dict[str, dict[str, Any]]):
        self.answers = answers
        self.last_input = ""

    async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        self.last_input = input_text
        return self.answers


def test_extract_solana_mints_links_plain_text_and_ignores_base_mints():
    ca_pump = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
    ca_dex = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    ca_plain = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
    text = (
        f"Check https://pump.fun/{ca_pump} and https://dexscreener.com/solana/{ca_dex}! "
        f"Also watching {ca_plain} vs So11111111111111111111111111111111111111112 "
        "and EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v and Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
    )
    mints = extract_solana_mints(text)
    assert mints == [ca_pump, ca_dex, ca_plain]


@pytest.mark.asyncio
async def test_tweet_dedup_and_daily_cap_enforcement(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "x_cap.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(
        _env_file=None,
        ENABLE_X_DISCOVERY=True,
        TWITTERAPI_IO_KEY="bare-secret-key",
        X_LIST_IDS="123456",
        X_SEARCH_QUERY="",
        X_DAILY_TWEET_CAP=2,
    )
    now_dt = datetime(2026, 11, 20, 18, 0, 0, tzinfo=timezone.utc)
    ca = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"

    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        # Verify bare X-API-Key header (no Bearer prefix) and camelCase listId
        assert request.headers.get("X-API-Key") == "bare-secret-key"
        assert request.url.params.get("listId") == "123456"
        assert request.url.params.get("includeReplies") == "false"
        return httpx.Response(
            200,
            json={
                "tweets": [
                    {
                        "id": "tw-1",
                        "text": f"Bullish curl on https://pump.fun/{ca}",
                        "createdAt": "2026-11-20T17:55:00Z",
                        "likeCount": 42,
                        "retweetCount": 10,
                        "author": {"userName": "sol_alpha", "followers": 15000, "isBlueVerified": True},
                    },
                    {
                        "id": "tw-1",  # duplicate tweet_id in same batch
                        "text": f"Bullish curl on https://pump.fun/{ca}",
                        "likeCount": 42,
                        "retweetCount": 10,
                        "author": {"userName": "sol_alpha", "followers": 15000, "isBlueVerified": True},
                    },
                    {
                        "id": "tw-2",
                        "text": f"Second caller on {ca}",
                        "likeCount": 15,
                        "retweetCount": 3,
                        "author": {"userName": "degen_scout", "followers": 4200, "isBlueVerified": False},
                    },
                    {
                        "id": "tw-3",  # exceeds daily cap=2
                        "text": f"Third tweet {ca}",
                        "likeCount": 99,
                        "retweetCount": 20,
                        "author": {"userName": "whale_x", "followers": 50000, "isBlueVerified": True},
                    },
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        res1 = await poll_x_once(cfg, db, http_client=client, now=now_dt)
        assert res1["new_tweets"] == 2
        assert res1["tweets_today"] == 2
        assert calls["count"] == 1

        # Second poll on same UTC day must stop BEFORE making any HTTP request
        res2 = await poll_x_once(cfg, db, http_client=client, now=now_dt + timedelta(minutes=30))
        assert res2["status"] == "daily_cap_reached"
        assert calls["count"] == 1

    signals = db.get_active_social_signals(ttl_minutes=60, now=now_dt)
    assert ca in signals
    assert signals[ca]["mention_count"] == 2
    assert signals[ca]["unique_authors"] == 2
    assert signals[ca]["total_likes"] == 57
    assert len(signals[ca]["top_snippets"]) == 2
    assert signals[ca]["top_snippets"][0]["author"] == "sol_alpha"
    db.close()


@pytest.mark.asyncio
async def test_x_worker_handles_402_and_429_gracefully(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "x_errors.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(
        _env_file=None,
        ENABLE_X_DISCOVERY=True,
        TWITTERAPI_IO_KEY="bare-key",
        X_LIST_IDS="999",
        X_SEARCH_QUERY='"pump" solana',
    )
    now_dt = datetime(2026, 11, 20, 18, 0, 0, tzinfo=timezone.utc)

    def handler(request: httpx.Request) -> httpx.Response:
        if "/twitter/list/tweets" in str(request.url):
            return httpx.Response(402, json={"error": "insufficient credits"})
        return httpx.Response(429, json={"error": "rate limit"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        res = await poll_x_once(cfg, db, http_client=client, now=now_dt)

    assert res["status"] == "partial_error"
    assert res["new_tweets"] == 0
    assert len(res["errors"]) == 2
    assert any("402" in e for e in res["errors"])
    assert any("429" in e for e in res["errors"])
    db.close()


@pytest.mark.asyncio
async def test_scanner_merges_x_mints_and_alpha_injects_x_snippets_and_telemetry(tmp_path):
    clear_geckoterminal_cache()
    db = AgentDatabase(db_path=str(tmp_path / "x_pipeline.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None, ENABLE_X_DISCOVERY=True, HUNTING_LANE="momentum_runners")

    now_dt = datetime.now(timezone.utc)
    now_ms = int(now_dt.timestamp() * 1000)
    old_ms = now_ms - (2 * 3600 * 1000)
    ca_x = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"

    # Seed social_signals from X discovery
    db.upsert_social_signal(
        mint=ca_x,
        author_username="solana_caller",
        author_followers=22000,
        like_count=85,
        retweet_count=19,
        is_blue_verified=True,
        text=f"Organic community breakout on {ca_x}, zero cabal wallets",
        seen_at_utc=now_dt.isoformat(),
    )
    db.set_x_last_poll_utc(now_dt.isoformat())

    batched_requested: list[str] = []

    def dex_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "geckoterminal.com" in url:
            return httpx.Response(200, json={"data": []})
        if any(p in url for p in ("token-boosts", "token-profiles", "community-takeovers")):
            return httpx.Response(200, json=[])
        if "/tokens/v1/solana/" in url:
            batched_requested.extend(url.split("/tokens/v1/solana/")[1].split(","))
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": "solana",
                        "pairAddress": "PoolX1111",
                        "baseToken": {"address": ca_x, "symbol": "XRUN", "name": "X Runner"},
                        "marketCap": 450_000,
                        "liquidity": {"usd": 65_000},
                        "volume": {"h1": 110_000, "m5": 16_000},
                        "priceChange": {"h1": 22.0, "m5": 5.0},
                        "txns": {"m5": {"buys": 28, "sells": 10}},
                        "pairCreatedAt": old_ms,
                    }
                ],
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(dex_handler)) as client:
        scan_out = await scanner_node(
            {"focus_state": FocusState.SCANNING.value, "daily_start_sol": 0.7, "daily_pnl_sol": 0.0},
            config=cfg,
            db=db,
            http_client=client,
            now_ms=now_ms,
            now=now_dt,
        )

    # X mint merged into DexScreener batch lookup and tagged discovery_source == "X"
    assert ca_x in batched_requested
    assert len(scan_out["raw_candidates"]) == 1
    cand = scan_out["raw_candidates"][0]
    assert cand["mint"] == ca_x
    assert cand["discovery_source"] == "X"

    # Inject X sentiment & snippets into alpha_node Decision 1 prompt context
    cand_soaked = dict(
        cand,
        added_at=(now_dt - timedelta(minutes=5)).isoformat(),
        candles_5m=make_sweep_reclaim_candles(),
        _ohlcv_5m_result=__import__("tests.test_e3b_cache_provenance", fromlist=["make_verified_ohlcv_result"]).make_verified_ohlcv_result(make_sweep_reclaim_candles(), last_ok_at=now_dt.timestamp()),
    )
    dec = StubDecisionClient(
        {
            "t0_narrative_strength": {"type": "score", "score": 4.2, "confidence": 0.88},
            "t0_momentum_quality": {"type": "score", "score": 4.0, "confidence": 0.88},
            "t0_is_copycat_vamp": {"type": "noul", "noul": 0.15, "confidence": 0.90},
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
    assert "x_mentions=1" in dec.last_input
    assert "x_unique_authors=1" in dec.last_input
    assert "x_total_likes=85" in dec.last_input
    assert "@solana_caller" in dec.last_input
    assert "Organic community breakout" in dec.last_input

    # Verify telemetry exposes x_discovery summary + x_mentions & discovery_source per slot
    app = create_telemetry_app(config=cfg, db=db)
    resp = TestClient(app).get("/api/telemetry")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["x_discovery"]["enabled"] is True
    assert payload["x_discovery"]["daily_cap"] == 1000
    assert payload["x_discovery"]["active_signal_mints"] == 1
    assert payload["x_discovery"]["last_poll_utc"] == now_dt.isoformat()
    slot = payload["active_watchlist"][0]
    assert slot["discovery_source"] == "X"
    assert slot["x_mentions"] == 1

    db.close()
    clear_geckoterminal_cache()
