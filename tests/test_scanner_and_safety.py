"""Unit tests for Config, Scanner Node, and Anti-Cabal Safety Gate."""

from __future__ import annotations

import httpx
import pytest

from config import AgentConfig
from db import AgentDatabase
from safety_gate_node import safety_gate_node
from scanner_node import scanner_node
from state import FocusState


def test_config_defaults_and_live_validation():
    cfg = AgentConfig(_env_file=None)
    assert cfg.EXECUTION_MODE == "paper"
    assert cfg.PAPER_STARTING_SOL == 0.7
    assert cfg.TRADE_SIZE_SOL == 0.5
    assert cfg.HARD_STOP_LOSS_PCT == 0.10
    assert cfg.BREAKEVEN_ARM_PCT == 0.08
    assert cfg.BREAKEVEN_FLOOR_PCT == 0.02
    assert cfg.TRAIL_ARM_PCT == 0.12
    assert cfg.TRAIL_PULLBACK_PCT == 0.06
    assert cfg.REENTRY_CURL_PCT == 0.05
    assert cfg.EXODUS_SELL_RATIO == 0.65
    assert cfg.EXODUS_LIQ_DROP_PCT == 0.10
    assert cfg.WATCH_TIMEOUT_MINUTES == 20
    assert cfg.DAILY_MAX_LOSS_PCT == 0.20

    with pytest.raises(ValueError, match="SOLANA_PRIVATE_KEY_BS58"):
        AgentConfig(_env_file=None, EXECUTION_MODE="live", SOLANA_PRIVATE_KEY_BS58="")


@pytest.mark.asyncio
async def test_scanner_filters_lane_liquidity_volume_and_cooldown(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "test.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None, HUNTING_LANE="momentum_runners")

    now_ms = 1_700_000_000_000
    old_enough_ms = now_ms - (2 * 3600 * 1000)  # 2 hours old
    too_new_ms = now_ms - (20 * 60 * 1000)  # 20 min old

    db.set_cooldown("MintCooldown1111111111111111111111111111111", hours=2.0, reason="stop")

    boosts = [
        {"chainId": "solana", "tokenAddress": "MintGood11111111111111111111111111111111111", "totalAmount": 500},
        {"chainId": "solana", "tokenAddress": "MintCooldown1111111111111111111111111111111", "totalAmount": 500},
        {"chainId": "solana", "tokenAddress": "MintTooNew111111111111111111111111111111111", "totalAmount": 500},
        {"chainId": "solana", "tokenAddress": "MintLowLiq111111111111111111111111111111111", "totalAmount": 500},
        {"chainId": "solana", "tokenAddress": "MintLowVol111111111111111111111111111111111", "totalAmount": 500},
    ]
    pairs = [
        {
            "chainId": "solana",
            "baseToken": {"address": "MintGood11111111111111111111111111111111111", "symbol": "GOOD", "name": "Good Coin"},
            "marketCap": 600_000,
            "liquidity": {"usd": 80_000},
            "volume": {"h1": 120_000, "m5": 15_000},
            "txns": {"m5": {"buys": 40, "sells": 15}},
            "pairCreatedAt": old_enough_ms,
        },
        {
            "chainId": "solana",
            "baseToken": {"address": "MintCooldown1111111111111111111111111111111", "symbol": "COOL"},
            "marketCap": 600_000,
            "liquidity": {"usd": 80_000},
            "volume": {"h1": 120_000},
            "pairCreatedAt": old_enough_ms,
        },
        {
            "chainId": "solana",
            "baseToken": {"address": "MintTooNew111111111111111111111111111111111", "symbol": "NEW"},
            "marketCap": 600_000,
            "liquidity": {"usd": 80_000},
            "volume": {"h1": 120_000},
            "pairCreatedAt": too_new_ms,
        },
        {
            "chainId": "solana",
            "baseToken": {"address": "MintLowLiq111111111111111111111111111111111", "symbol": "LIQ"},
            "marketCap": 600_000,
            "liquidity": {"usd": 10_000},
            "volume": {"h1": 120_000},
            "pairCreatedAt": old_enough_ms,
        },
        {
            "chainId": "solana",
            "baseToken": {"address": "MintLowVol111111111111111111111111111111111", "symbol": "VOL"},
            "marketCap": 600_000,
            "liquidity": {"usd": 80_000},
            "volume": {"h1": 10_000},
            "pairCreatedAt": old_enough_ms,
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if "token-boosts" in str(request.url):
            return httpx.Response(200, json=boosts)
        if "token-profiles" in str(request.url):
            return httpx.Response(200, json=[])
        if "/tokens/v1/solana/" in str(request.url):
            return httpx.Response(200, json=pairs)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await scanner_node(
            {"focus_state": FocusState.SCANNING.value, "daily_start_sol": 0.7, "daily_pnl_sol": 0.0},
            config=cfg,
            db=db,
            http_client=client,
            now_ms=now_ms,
        )
        assert len(out["raw_candidates"]) == 1
        assert out["raw_candidates"][0]["symbol"] == "GOOD"

        # Scanner must NOT run when IN_POSITION
        out_in_pos = await scanner_node(
            {"focus_state": FocusState.IN_POSITION.value},
            config=cfg,
            db=db,
            http_client=client,
            now_ms=now_ms,
        )
        assert out_in_pos["raw_candidates"] == []

    db.close()


@pytest.mark.asyncio
async def test_safety_gate_deterministic_rejections_and_pass():
    cfg = AgentConfig(_env_file=None, JUPITER_API_KEY="test-jup-key")
    candidates = [
        {"mint": "MintSafe", "symbol": "SAFE"},
        {"mint": "MintAuth", "symbol": "MAUTH"},
        {"mint": "MintFreeze", "symbol": "FRZ"},
        {"mint": "MintUnlockedLP", "symbol": "UNLP"},
        {"mint": "MintCabalTop10", "symbol": "CABAL"},
        {"mint": "MintShieldCrit", "symbol": "SHLD"},
    ]

    rug_responses = {
        "MintSafe": {
            "mint": "MintSafe",
            "riskScore": 100,
            "rugged": False,
            "markets": [{"lp": {"lpLockedPct": 100.0}}],
            "risks": [],
        },
        "MintAuth": {
            "mint": "MintAuth",
            "rugged": False,
            "risks": [{"name": "Mint Authority still enabled", "level": "danger"}],
        },
        "MintFreeze": {
            "mint": "MintFreeze",
            "rugged": False,
            "freezeAuthority": "SomeBadAuthorityPubkey111111111111111111111",
            "risks": [],
        },
        "MintUnlockedLP": {
            "mint": "MintUnlockedLP",
            "rugged": False,
            "markets": [{"lp": {"lpLockedPct": 40.0}}],
            "risks": [],
        },
        "MintCabalTop10": {
            "mint": "MintCabalTop10",
            "rugged": False,
            "risks": [{"name": "Top 10 holders high ownership", "value": "34.5%", "level": "warn"}],
        },
        "MintShieldCrit": {
            "mint": "MintShieldCrit",
            "rugged": False,
            "markets": [{"lp": {"lpLockedPct": 99.0}}],
            "risks": [],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "rugcheck.xyz" in url:
            mint = url.split("/v1/tokens/")[1].split("/report/summary")[0]
            return httpx.Response(200, json=rug_responses[mint])
        if "/ultra/v1/shield" in url:
            assert request.headers.get("x-api-key") == "test-jup-key"
            mint = request.url.params["mints"]
            if mint == "MintShieldCrit":
                return httpx.Response(
                    200,
                    json={
                        "warnings": {
                            mint: [{"type": "HAS_FREEZE_AUTHORITY", "severity": "critical", "message": "Freeze enabled"}]
                        }
                    },
                )
            return httpx.Response(200, json={"warnings": {mint: []}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await safety_gate_node(
            {"focus_state": FocusState.SCANNING.value, "raw_candidates": candidates},
            config=cfg,
            http_client=client,
        )

    assert [c["mint"] for c in out["safe_candidates"]] == ["MintSafe"]
    rejected_mints = {r["mint"] for r in out["rejected_candidates"]}
    assert rejected_mints == {"MintAuth", "MintFreeze", "MintUnlockedLP", "MintCabalTop10", "MintShieldCrit"}
