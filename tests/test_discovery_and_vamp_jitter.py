"""Unit tests for GeckoTerminal trending discovery, DexScreener CTOs, and jitter-resistant vamp eviction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from alpha_node import alpha_node
from config import AgentConfig
from db import AgentDatabase
from scanner_node import (
    clear_geckoterminal_cache,
    fetch_dexscreener_trending_solana,
    fetch_geckoterminal_trending_solana,
    parse_geckoterminal_trending_mints,
)
from state import FocusState


class StubDecisionClient:
    def __init__(self, answers: dict[str, dict[str, Any]]):
        self.answers = answers

    async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return self.answers


def test_parse_geckoterminal_trending_mints():
    payload = {
        "data": [
            {
                "id": "solana_pool1",
                "type": "pool",
                "relationships": {"base_token": {"data": {"id": "solana_MintGecko1", "type": "token"}}},
            },
            {
                "id": "solana_pool2",
                "type": "pool",
                "relationships": {"base_token": {"data": {"id": "solana_MintGecko2", "type": "token"}}},
            },
            # Duplicate base token across another pool should be deduplicated
            {
                "id": "solana_pool3",
                "type": "pool",
                "relationships": {"base_token": {"data": {"id": "solana_MintGecko1", "type": "token"}}},
            },
        ],
        "included": [
            {
                "id": "solana_MintGecko1",
                "type": "token",
                "attributes": {"address": "MintGecko1", "symbol": "GKO1"},
            }
        ],
    }
    mints = parse_geckoterminal_trending_mints(payload)
    assert mints == ["MintGecko1", "MintGecko2"]


@pytest.mark.asyncio
async def test_geckoterminal_60s_cache_and_429_graceful_fallback():
    clear_geckoterminal_cache()
    cfg = AgentConfig(_env_file=None, GECKOTERMINAL_CACHE_SECONDS=60.0)
    calls = {"count": 0, "mode": "ok"}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        assert "networks/solana/trending_pools" in str(request.url)
        assert request.url.params.get("include") == "base_token"
        if calls["mode"] == "429":
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"relationships": {"base_token": {"data": {"id": "solana_MintGT1"}}}},
                    {"relationships": {"base_token": {"data": {"id": "solana_MintGT2"}}}},
                ],
                "included": [],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # T=1000: cold cache -> 1 HTTP call
        m1 = await fetch_geckoterminal_trending_solana(cfg, client, now_ts=1000.0)
        assert m1 == ["MintGT1", "MintGT2"]
        assert calls["count"] == 1

        # T=1030 (within 60s): cache hit -> still 1 HTTP call
        m2 = await fetch_geckoterminal_trending_solana(cfg, client, now_ts=1030.0)
        assert m2 == ["MintGT1", "MintGT2"]
        assert calls["count"] == 1

        # T=1065 (cache expired) + 429 rate limit -> falls back gracefully to cached mints & backs off 60s
        calls["mode"] = "429"
        m3 = await fetch_geckoterminal_trending_solana(cfg, client, now_ts=1065.0)
        assert m3 == ["MintGT1", "MintGT2"]
        assert calls["count"] == 2

        # T=1080 (within backoff window): does NOT hammer GeckoTerminal again
        m4 = await fetch_geckoterminal_trending_solana(cfg, client, now_ts=1080.0)
        assert m4 == ["MintGT1", "MintGT2"]
        assert calls["count"] == 2

    clear_geckoterminal_cache()


@pytest.mark.asyncio
async def test_dexscreener_cto_and_gecko_merged_batch_lookup():
    clear_geckoterminal_cache()
    cfg = AgentConfig(_env_file=None)
    batched_mints_requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "geckoterminal.com" in url:
            return httpx.Response(
                200,
                json={"data": [{"relationships": {"base_token": {"data": {"id": "solana_MintGecko"}}}}]},
            )
        if "/community-takeovers/latest/v1" in url:
            return httpx.Response(
                200,
                json=[
                    {"chainId": "solana", "tokenAddress": "MintCTO"},
                    {"chainId": "ethereum", "tokenAddress": "0xIgnoreEth"},
                    {"chainId": "solana", "tokenAddress": "MintGecko"},  # duplicate should dedupe
                ],
            )
        if "/token-boosts/latest/v1" in url:
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": "MintBoost"}])
        if "/token-boosts/top/v1" in url or "/token-profiles/latest/v1" in url:
            return httpx.Response(200, json=[])
        if "/tokens/v1/solana/" in url:
            chunk = url.split("/tokens/v1/solana/")[1]
            batched_mints_requested.extend(chunk.split(","))
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await fetch_dexscreener_trending_solana(cfg, client, now_ts=2000.0)

    assert batched_mints_requested == ["MintGecko", "MintBoost", "MintCTO"]
    clear_geckoterminal_cache()


@pytest.mark.asyncio
async def test_vamp_jitter_two_consecutive_strikes_and_reset(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "vamp_jitter.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None, NARRATIVE_CACHE_TTL_SECONDS=0)
    now_dt = datetime(2026, 11, 19, 17, 0, 0, tzinfo=timezone.utc)

    db.upsert_watchlist_entry(
        mint="MintRunner",
        symbol="RUN",
        candidate={"mint": "MintRunner", "symbol": "RUN"},
        added_at=now_dt.isoformat(),
    )

    cand = {"mint": "MintRunner", "symbol": "RUN", "leg2_score": 68.0, "added_at": now_dt.isoformat()}
    jitter_78 = StubDecisionClient(
        {
            "t0_narrative_strength": {"type": "score", "score": 3.0, "confidence": 0.70},
            "t0_momentum_quality": {"type": "score", "score": 3.2, "confidence": 0.70},
            "t0_is_copycat_vamp": {"type": "noul", "noul": 0.78, "confidence": 0.80},
            "t0_action": {"type": "choice", "choice": "WATCH", "confidence": 0.65},
        }
    )

    # Tick 1: vamp=0.78 (between 0.75 and 0.85) -> strike 1/2, RETAINED (not evicted!)
    await alpha_node(
        {"focus_state": FocusState.SCANNING.value, "sol_balance": 0.7, "safe_candidates": [cand]},
        config=cfg,
        db=db,
        decision_client=jitter_78,  # type: ignore[arg-type]
        now=now_dt,
    )
    wl1 = db.get_watchlist()
    assert len(wl1) == 1
    assert wl1[0]["vamp_strikes"] == 1
    assert not db.is_on_cooldown("MintRunner", now=now_dt)

    # Tick 2: vamp drops back to 0.45 (< 0.75) -> resets vamp_strikes to 0
    calm_45 = StubDecisionClient(
        {
            "t0_narrative_strength": {"type": "score", "score": 3.0, "confidence": 0.70},
            "t0_momentum_quality": {"type": "score", "score": 3.2, "confidence": 0.70},
            "t0_is_copycat_vamp": {"type": "noul", "noul": 0.45, "confidence": 0.80},
            "t0_action": {"type": "choice", "choice": "WATCH", "confidence": 0.65},
        }
    )
    await alpha_node(
        {"focus_state": FocusState.SCANNING.value, "sol_balance": 0.7, "safe_candidates": [cand]},
        config=cfg,
        db=db,
        decision_client=calm_45,  # type: ignore[arg-type]
        now=now_dt + timedelta(seconds=15),
    )
    assert db.get_watchlist()[0]["vamp_strikes"] == 0

    # Tick 3 & Tick 4: TWO consecutive cycles at 0.78 -> evicted on Tick 4 with 20m cooldown
    await alpha_node(
        {"focus_state": FocusState.SCANNING.value, "sol_balance": 0.7, "safe_candidates": [cand]},
        config=cfg,
        db=db,
        decision_client=jitter_78,  # type: ignore[arg-type]
        now=now_dt + timedelta(seconds=30),
    )
    assert db.get_watchlist()[0]["vamp_strikes"] == 1

    evict_dt = now_dt + timedelta(seconds=45)
    await alpha_node(
        {"focus_state": FocusState.SCANNING.value, "sol_balance": 0.7, "safe_candidates": [cand]},
        config=cfg,
        db=db,
        decision_client=jitter_78,  # type: ignore[arg-type]
        now=evict_dt,
    )
    assert db.get_watchlist() == []
    assert db.is_on_cooldown("MintRunner", now=evict_dt + timedelta(minutes=19))
    assert not db.is_on_cooldown("MintRunner", now=evict_dt + timedelta(minutes=21))
    db.close()
