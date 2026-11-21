"""Unit tests for Jupiter Token API v2 discovery, enrichment, isSus guardrail, and Alpha injection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from alpha_node import alpha_node
from config import AgentConfig
from db import AgentDatabase
from jupiter_token_client import (
    clear_jupiter_token_cache,
    enrich_mints,
    fetch_discovery_mints,
)
from safety_gate_node import safety_gate_node
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


@pytest.mark.asyncio
async def test_jupiter_discovery_parsing_filtering_and_60s_cache():
    clear_jupiter_token_cache()
    cfg = AgentConfig(
        _env_file=None,
        JUPITER_API_KEY="",  # keyless mode: should NOT send x-api-key header
        JUPITER_POLL_CACHE_SECONDS=60.0,
        JUPITER_REQUEST_DELAY_SECONDS=0.0,
        MAX_MC_USD=3_000_000.0,
    )

    calls = {"organic": 0, "trending": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert "x-api-key" not in request.headers
        url = str(request.url)
        if "/toporganicscore/5m" in url:
            calls["organic"] += 1
            return httpx.Response(
                200,
                json=[
                    # Should be filtered out (major SOL mint)
                    {
                        "id": "So11111111111111111111111111111111111111112",
                        "symbol": "SOL",
                        "mcap": 80_000_000_000,
                        "organicScore": 99.0,
                    },
                    # Should be filtered out (USDC)
                    {
                        "id": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                        "symbol": "USDC",
                        "mcap": 40_000_000_000,
                        "organicScore": 99.0,
                    },
                    # Should be filtered out (blue-chip mcap > 3M)
                    {
                        "id": "MintBlueChip111111111111111111111111111111",
                        "symbol": "BLUE",
                        "mcap": 65_000_000,
                        "organicScore": 95.0,
                    },
                    # Valid memecoin runner
                    {
                        "id": "MintOrg11111111111111111111111111111111111",
                        "symbol": "ORG1",
                        "name": "Organic One",
                        "mcap": 550_000,
                        "organicScore": 84.5,
                        "isVerified": True,
                        "holderCount": 4200,
                        "audit": {
                            "isSus": False,
                            "mintAuthorityDisabled": True,
                            "freezeAuthorityDisabled": True,
                            "topHoldersPercentage": 14.2,
                        },
                    },
                ],
            )
        if "/toptrending/5m" in url:
            calls["trending"] += 1
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "MintTrend111111111111111111111111111111111",
                        "symbol": "TRND",
                        "mcap": 900_000,
                        "organicScore": 71.0,
                        "isVerified": False,
                        "holderCount": 1850,
                        "audit": {
                            "isSus": False,
                            "mintAuthorityDisabled": True,
                            "freezeAuthorityDisabled": True,
                            "topHoldersPercentage": 18.5,
                        },
                    }
                ],
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res1 = await fetch_discovery_mints(cfg, client, now_ts=1000.0)
        assert set(res1.keys()) == {
            "MintOrg11111111111111111111111111111111111",
            "MintTrend111111111111111111111111111111111",
        }
        org = res1["MintOrg11111111111111111111111111111111111"]
        assert org["organic_score"] == 84.5
        assert org["is_verified"] is True
        assert org["holder_count"] == 4200
        assert org["is_sus"] is False
        assert org["mint_authority_disabled"] is True
        assert org["freeze_authority_disabled"] is True
        assert org["top_holders_pct"] == 14.2
        assert calls == {"organic": 1, "trending": 1}

        # Within 60s cache -> zero new HTTP requests
        res2 = await fetch_discovery_mints(cfg, client, now_ts=1040.0)
        assert set(res2.keys()) == set(res1.keys())
        assert calls == {"organic": 1, "trending": 1}

    clear_jupiter_token_cache()


@pytest.mark.asyncio
async def test_jupiter_batch_search_enrichment_uses_cache_and_optional_key():
    clear_jupiter_token_cache()
    cfg = AgentConfig(
        _env_file=None,
        JUPITER_API_KEY="my-jup-key",
        JUPITER_POLL_CACHE_SECONDS=60.0,
        JUPITER_REQUEST_DELAY_SECONDS=0.0,
    )

    search_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-api-key") == "my-jup-key"
        assert "/tokens/v2/search" in str(request.url)
        q = request.url.params.get("query", "")
        search_queries.append(q)
        return httpx.Response(
            200,
            json=[
                {
                    "id": "MintA",
                    "symbol": "AAA",
                    "organicScore": 77.2,
                    "isVerified": True,
                    "holderCount": 3100,
                    "audit": {"isSus": False, "topHoldersPercentage": 12.1},
                },
                {
                    "id": "MintB",
                    "symbol": "BBB",
                    "organicScore": 41.0,
                    "isVerified": False,
                    "holderCount": 600,
                    "audit": {"isSus": False, "topHoldersPercentage": 19.0},
                },
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        e1 = await enrich_mints(["MintA", "MintB"], cfg, client, now_ts=2000.0)
        assert e1["MintA"]["organic_score"] == 77.2
        assert e1["MintB"]["holder_count"] == 600
        assert search_queries == ["MintA,MintB"]

        # Second call within 60s for MintA + new MintC -> only queries MintC!
        e2 = await enrich_mints(["MintA", "MintC"], cfg, client, now_ts=2025.0)
        assert e2["MintA"]["organic_score"] == 77.2
        assert search_queries == ["MintA,MintB", "MintC"]

    clear_jupiter_token_cache()


@pytest.mark.asyncio
async def test_jupiter_is_sus_rejected_at_scanner_and_safety_gate(tmp_path):
    clear_jupiter_token_cache()
    clear_geckoterminal_cache()
    db = AgentDatabase(db_path=str(tmp_path / "jup_sus.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(
        _env_file=None,
        ENABLE_JUPITER_DISCOVERY=True,
        JUPITER_REQUEST_DELAY_SECONDS=0.0,
        HUNTING_LANE="momentum_runners",
    )

    now_dt = datetime.now(timezone.utc)
    now_ms = int(now_dt.timestamp() * 1000)
    old_ms = now_ms - (2 * 3600 * 1000)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/toporganicscore/5m" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "MintScamSus",
                        "symbol": "SCAM",
                        "mcap": 500_000,
                        "organicScore": 80.0,
                        "audit": {"isSus": True, "topHoldersPercentage": 10.0},
                    },
                    {
                        "id": "MintCleanJup",
                        "symbol": "CLEAN",
                        "mcap": 500_000,
                        "organicScore": 88.0,
                        "isVerified": True,
                        "holderCount": 5100,
                        "audit": {"isSus": False, "topHoldersPercentage": 11.5},
                    },
                ],
            )
        if "/toptrending/5m" in url or "/tokens/v2/search" in url:
            return httpx.Response(200, json=[])
        if "geckoterminal.com" in url:
            return httpx.Response(200, json={"data": []})
        if any(p in url for p in ("token-boosts", "token-profiles", "community-takeovers")):
            return httpx.Response(200, json=[])
        if "/tokens/v1/solana/" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": "solana",
                        "pairAddress": "PoolScam",
                        "baseToken": {"address": "MintScamSus", "symbol": "SCAM"},
                        "marketCap": 500_000,
                        "liquidity": {"usd": 80_000},
                        "volume": {"h1": 200_000, "m5": 20_000},
                        "txns": {"m5": {"buys": 30, "sells": 10}},
                        "pairCreatedAt": old_ms,
                    },
                    {
                        "chainId": "solana",
                        "pairAddress": "PoolClean",
                        "baseToken": {"address": "MintCleanJup", "symbol": "CLEAN"},
                        "marketCap": 500_000,
                        "liquidity": {"usd": 80_000},
                        "volume": {"h1": 150_000, "m5": 18_000},
                        "txns": {"m5": {"buys": 30, "sells": 10}},
                        "pairCreatedAt": old_ms,
                    },
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

    # MintScamSus blocked by Hard Guardrail; only MintCleanJup enters watchlist tagged JUPITER
    assert [c["mint"] for c in scan_out["raw_candidates"]] == ["MintCleanJup"]
    cand = scan_out["raw_candidates"][0]
    assert cand["discovery_source"] == "JUPITER"
    assert cand["jup_organic_score"] == 88.0

    # Also verify safety_gate_node rejects jup_is_sus == True directly
    sg_out = await safety_gate_node(
        {
            "focus_state": FocusState.SCANNING.value,
            "raw_candidates": [{"mint": "MintScamSus2", "symbol": "SUS2", "jup_is_sus": True}],
        },
        config=cfg,
        db=db,
        now=now_dt,
    )
    assert sg_out["safe_candidates"] == []
    assert len(sg_out["rejected_candidates"]) == 1
    assert "audit.isSus=True" in sg_out["rejected_candidates"][0]["reasons"][0]

    # Verify alpha_node injects jup_organic_score, jup_is_verified, jup_holder_count, jup_top_holders_pct
    cand_soaked = dict(
        cand,
        added_at=(now_dt - timedelta(minutes=5)).isoformat(),
        candles_5m=make_sweep_reclaim_candles(),
    )
    dec = StubDecisionClient(
        {
            "t0_narrative_strength": {"type": "score", "score": 4.1, "confidence": 0.85},
            "t0_momentum_quality": {"type": "score", "score": 4.2, "confidence": 0.85},
            "t0_is_copycat_vamp": {"type": "noul", "noul": 0.12, "confidence": 0.90},
            "t0_action": {"type": "choice", "choice": "BUY", "confidence": 0.84},
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
    assert "jup_organic_score=88.0/100" in dec.last_input
    assert "jup_is_verified=True" in dec.last_input
    assert "jup_holder_count=5100" in dec.last_input
    assert "jup_top_holders_pct=11.5%" in dec.last_input

    # Verify GET /api/telemetry exposes jup_organic_score on active_watchlist
    app = create_telemetry_app(config=cfg, db=db)
    resp = TestClient(app).get("/api/telemetry")
    assert resp.status_code == 200
    slot = resp.json()["active_watchlist"][0]
    assert slot["jup_organic_score"] == 88.0
    assert slot["discovery_source"] == "JUPITER"

    db.close()
    clear_jupiter_token_cache()
    clear_geckoterminal_cache()


@pytest.mark.asyncio
async def test_jupiter_graceful_429_and_timeout_fallback():
    clear_jupiter_token_cache()
    cfg = AgentConfig(_env_file=None, JUPITER_REQUEST_DELAY_SECONDS=0.0)

    def handler_429(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "Too Many Requests"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_429)) as client:
        res_429 = await fetch_discovery_mints(cfg, client, now_ts=3000.0)
        assert res_429 == {}
        enr_429 = await enrich_mints(["MintAny"], cfg, client, now_ts=3000.0)
        assert enr_429 == {}

    clear_jupiter_token_cache()

    def handler_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_timeout)) as client:
        res_to = await fetch_discovery_mints(cfg, client, now_ts=4000.0)
        assert res_to == {}
        enr_to = await enrich_mints(["MintAny"], cfg, client, now_ts=4000.0)
        assert enr_to == {}

    clear_jupiter_token_cache()
