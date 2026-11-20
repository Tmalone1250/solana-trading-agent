"""Unit tests for the 2-slot rotating SCANNING watchlist, eviction rules, and telemetry."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from alpha_node import alpha_node
from config import AgentConfig
from db import AgentDatabase
from safety_gate_node import safety_gate_node
from scanner_node import scanner_node
from state import FocusState
from telemetry_api import create_telemetry_app


class StubDecisionClient:
    def __init__(self, answers: dict[str, dict[str, Any]]):
        self.answers = answers

    async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return self.answers


def _make_pair(mint: str, symbol: str, vol_1h: float, created_ms: int) -> dict[str, Any]:
    return {
        "chainId": "solana",
        "baseToken": {"address": mint, "symbol": symbol, "name": symbol},
        "marketCap": 500_000,
        "liquidity": {"usd": 60_000},
        "volume": {"h1": vol_1h, "m5": 10_000},
        "txns": {"m5": {"buys": 25, "sells": 10}},
        "pairCreatedAt": created_ms,
    }


@pytest.mark.asyncio
async def test_two_slot_cap_widened_feed_and_backfill_after_eviction(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "watchlist_cap.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None, HUNTING_LANE="momentum_runners", MAX_WATCH_BATCH=2)

    now_dt = datetime(2026, 11, 19, 17, 0, 0, tzinfo=timezone.utc)
    now_ms = int(now_dt.timestamp() * 1000)
    old_ms = now_ms - (2 * 3600 * 1000)

    seen_urls: list[str] = []
    pairs = [
        _make_pair("MintA", "AAA", 300_000, old_ms),
        _make_pair("MintB", "BBB", 200_000, old_ms),
        _make_pair("MintC", "CCC", 150_000, old_ms),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen_urls.append(url)
        if "/token-boosts/latest/v1" in url:
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": "MintC", "amount": 50}])
        if "/token-boosts/top/v1" in url:
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": "MintA", "totalAmount": 300}])
        if "/token-profiles/latest/v1" in url:
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": "MintB"}])
        if "/tokens/v1/solana/" in url:
            return httpx.Response(200, json=pairs)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # Cycle 1: 3 eligible tokens -> capped at MAX_WATCH_BATCH=2 (highest 1h volume: MintA, MintB)
        out1 = await scanner_node(
            {"focus_state": FocusState.SCANNING.value, "daily_start_sol": 0.7, "daily_pnl_sol": 0.0},
            config=cfg,
            db=db,
            http_client=client,
            now_ms=now_ms,
            now=now_dt,
        )
        assert any("/token-boosts/latest/v1" in u for u in seen_urls)
        assert [c["mint"] for c in out1["raw_candidates"]] == ["MintA", "MintB"]
        assert len(db.get_watchlist()) == 2

        # Evict MintA (e.g., via safety reject -> 60m cooldown)
        db.remove_from_watchlist("MintA")
        db.set_cooldown_minutes("MintA", minutes=60, reason="SAFETY_REJECT", now=now_dt)

        # Cycle 2: MintB stays in slot, empty slot automatically backfills with MintC
        out2 = await scanner_node(
            {"focus_state": FocusState.SCANNING.value, "daily_start_sol": 0.7, "daily_pnl_sol": 0.0},
            config=cfg,
            db=db,
            http_client=client,
            now_ms=now_ms + 15_000,
            now=now_dt + timedelta(seconds=15),
        )
        assert [c["mint"] for c in out2["raw_candidates"]] == ["MintB", "MintC"]
        assert len(db.get_watchlist()) == 2

    db.close()


@pytest.mark.asyncio
async def test_safety_gate_rejection_evicts_and_sets_60m_cooldown(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "safety_evict.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None, SAFETY_REJECT_COOLDOWN_MINUTES=60)
    now_dt = datetime(2026, 11, 19, 17, 0, 0, tzinfo=timezone.utc)

    db.upsert_watchlist_entry(
        mint="MintRug",
        symbol="RUG",
        candidate={"mint": "MintRug", "symbol": "RUG"},
        added_at=now_dt.isoformat(),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "rugcheck.xyz" in str(request.url):
            return httpx.Response(200, json={"rugged": True, "risks": []})
        if "/ultra/v1/shield" in str(request.url):
            return httpx.Response(200, json={"warnings": {"MintRug": []}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await safety_gate_node(
            {
                "focus_state": FocusState.SCANNING.value,
                "raw_candidates": [{"mint": "MintRug", "symbol": "RUG"}],
            },
            config=cfg,
            db=db,
            http_client=client,
            now=now_dt,
        )

    assert out["safe_candidates"] == []
    assert db.get_watchlist() == []
    # Cooldown active at +59m, expired at +61m
    assert db.is_on_cooldown("MintRug", now=now_dt + timedelta(minutes=59))
    assert not db.is_on_cooldown("MintRug", now=now_dt + timedelta(minutes=61))
    db.close()


@pytest.mark.asyncio
async def test_alpha_evicts_high_vamp_with_20m_cooldown_and_logs(tmp_path, caplog):
    db = AgentDatabase(db_path=str(tmp_path / "vamp_evict.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None)
    now_dt = datetime(2026, 11, 19, 17, 0, 0, tzinfo=timezone.utc)

    db.upsert_watchlist_entry(
        mint="MintVamp",
        symbol="VAMP",
        candidate={"mint": "MintVamp", "symbol": "VAMP"},
        added_at=now_dt.isoformat(),
    )

    dec = StubDecisionClient(
        {
            "t0_narrative_strength": {"type": "score", "score": 3.2, "confidence": 0.85},
            "t0_momentum_quality": {"type": "score", "score": 3.4, "confidence": 0.85},
            "t0_is_copycat_vamp": {"type": "noul", "noul": 0.88, "confidence": 0.90},
            "t0_action": {"type": "choice", "choice": "WATCH", "confidence": 0.62},
        }
    )

    with caplog.at_level(logging.INFO, logger="alpha_node"):
        out = await alpha_node(
            {
                "focus_state": FocusState.SCANNING.value,
                "sol_balance": 0.7,
                "safe_candidates": [{"mint": "MintVamp", "symbol": "VAMP", "added_at": now_dt.isoformat()}],
            },
            config=cfg,
            db=db,
            decision_client=dec,  # type: ignore[arg-type]
            now=now_dt,
        )

    assert out["pending_order"] is None
    assert db.get_watchlist() == []
    assert db.is_on_cooldown("MintVamp", now=now_dt + timedelta(minutes=19))
    assert not db.is_on_cooldown("MintVamp", now=now_dt + timedelta(minutes=21))
    assert any("[Alpha] $VAMP" in r.message and "EVICTED (vamp >= 0.85, 20m cooldown)" in r.message for r in caplog.records)
    db.close()


@pytest.mark.asyncio
async def test_alpha_evicts_low_narrative_and_momentum_with_15m_cooldown(tmp_path, caplog):
    db = AgentDatabase(db_path=str(tmp_path / "low_score.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None)
    now_dt = datetime(2026, 11, 19, 17, 0, 0, tzinfo=timezone.utc)

    db.upsert_watchlist_entry(
        mint="MintWeak",
        symbol="WEAK",
        candidate={"mint": "MintWeak", "symbol": "WEAK"},
        added_at=now_dt.isoformat(),
    )
    db.upsert_watchlist_entry(
        mint="MintKeep",
        symbol="KEEP",
        candidate={"mint": "MintKeep", "symbol": "KEEP"},
        added_at=now_dt.isoformat(),
    )

    dec = StubDecisionClient(
        {
            # Both < 2.0 -> EVICT (15m cooldown)
            "t0_narrative_strength": {"type": "score", "score": 1.8, "confidence": 0.80},
            "t0_momentum_quality": {"type": "score", "score": 1.5, "confidence": 0.80},
            "t0_is_copycat_vamp": {"type": "noul", "noul": 0.20, "confidence": 0.85},
            "t0_action": {"type": "choice", "choice": "REJECT", "confidence": 0.75},
            # Only narrative < 2.0, momentum >= 2.0 -> RETAINED in watchlist
            "t1_narrative_strength": {"type": "score", "score": 1.9, "confidence": 0.80},
            "t1_momentum_quality": {"type": "score", "score": 3.1, "confidence": 0.85},
            "t1_is_copycat_vamp": {"type": "noul", "noul": 0.15, "confidence": 0.85},
            "t1_action": {"type": "choice", "choice": "WATCH", "confidence": 0.70},
        }
    )

    with caplog.at_level(logging.INFO, logger="alpha_node"):
        await alpha_node(
            {
                "focus_state": FocusState.SCANNING.value,
                "sol_balance": 0.7,
                "safe_candidates": [
                    {"mint": "MintWeak", "symbol": "WEAK", "added_at": now_dt.isoformat()},
                    {"mint": "MintKeep", "symbol": "KEEP", "added_at": now_dt.isoformat()},
                ],
            },
            config=cfg,
            db=db,
            decision_client=dec,  # type: ignore[arg-type]
            now=now_dt,
        )

    remaining = db.get_watchlist()
    assert [w["mint"] for w in remaining] == ["MintKeep"]
    assert remaining[0]["latest_scores"]["momentum_quality"] == 3.1
    assert db.is_on_cooldown("MintWeak", now=now_dt + timedelta(minutes=14))
    assert not db.is_on_cooldown("MintWeak", now=now_dt + timedelta(minutes=16))
    assert any("$WEAK" in r.message and "15m cooldown" in r.message for r in caplog.records)
    db.close()


@pytest.mark.asyncio
async def test_stale_watch_eviction_and_telemetry_watchlist_exposure(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "stale_watch.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None, HUNTING_LANE="momentum_runners")

    now_dt = datetime.now(timezone.utc)
    stale_added = now_dt - timedelta(minutes=16)
    fresh_added = now_dt - timedelta(minutes=4)
    now_ms = int(now_dt.timestamp() * 1000)
    old_ms = now_ms - (2 * 3600 * 1000)

    db.upsert_watchlist_entry(
        mint="MintStale",
        symbol="STALE",
        candidate={"mint": "MintStale", "symbol": "STALE"},
        added_at=stale_added.isoformat(),
    )
    db.upsert_watchlist_entry(
        mint="MintFresh",
        symbol="FRESH",
        candidate={"mint": "MintFresh", "symbol": "FRESH"},
        added_at=fresh_added.isoformat(),
    )
    db.update_watchlist_scores(
        mint="MintFresh",
        scores={"narrative_strength": 3.2, "momentum_quality": 3.4, "is_copycat_vamp": 0.12, "action": "WATCH"},
        last_evaluated_at=now_dt.isoformat(),
    )

    pairs = [
        _make_pair("MintStale", "STALE", 400_000, old_ms),
        _make_pair("MintFresh", "FRESH", 250_000, old_ms),
        _make_pair("MintReplacement", "NEWB", 200_000, old_ms),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/token-boosts/latest/v1" in url:
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": "MintReplacement"}])
        if "/token-boosts/top/v1" in url:
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": "MintStale"}])
        if "/token-profiles/latest/v1" in url:
            return httpx.Response(200, json=[])
        if "/tokens/v1/solana/" in url:
            return httpx.Response(200, json=pairs)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await scanner_node(
            {"focus_state": FocusState.SCANNING.value, "daily_start_sol": 0.7, "daily_pnl_sol": 0.0},
            config=cfg,
            db=db,
            http_client=client,
            now_ms=now_ms,
            now=now_dt,
        )

    # MintStale evicted (>15m) with 10m cooldown; MintFresh kept; MintReplacement backfilled
    assert [c["mint"] for c in out["raw_candidates"]] == ["MintFresh", "MintReplacement"]
    assert db.is_on_cooldown("MintStale", now=now_dt + timedelta(minutes=9))
    assert not db.is_on_cooldown("MintStale", now=now_dt + timedelta(minutes=11))

    # Verify GET /api/telemetry exposes active_watchlist with mints, scores, and time in slot
    app = create_telemetry_app(config=cfg, db=db)
    resp = TestClient(app).get("/api/telemetry")
    assert resp.status_code == 200
    wl = resp.json()["active_watchlist"]
    assert len(wl) == 2
    fresh_slot = next(item for item in wl if item["mint"] == "MintFresh")
    assert fresh_slot["symbol"] == "FRESH"
    assert fresh_slot["scores"]["narrative_strength"] == 3.2
    assert fresh_slot["time_in_slot_minutes"] >= 3.9
    db.close()
