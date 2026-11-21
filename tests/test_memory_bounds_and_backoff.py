"""Unit tests for Pi 4 (2GB) memory caps, bounded cache eviction, 60s roster backoff, and RSS logging."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from batched_price_monitor import (
    MAX_PRICE_BACKOFF_ENTRIES,
    _price_backoff,
    _record_429,
    clear_price_backoff,
)
from config import AgentConfig
from db import AgentDatabase
from helius_webhook import (
    HELIUS_ROSTER_ERROR_BACKOFF_SECONDS,
    MAX_RECENT_WEBHOOK_SIGS,
    _helius_roster_cache,
    _recent_webhook_sigs,
    clear_helius_roster_cache,
    fetch_helius_tracked_wallets,
    process_helius_webhook_payload,
)
from jupiter_token_client import (
    MAX_MINT_CACHE_ENTRIES,
    _mint_cache,
    clear_jupiter_token_cache,
    enrich_mints,
)
from memory_guard import (
    get_process_rss_mb,
    maybe_log_rss_memory,
    prune_timestamped_cache,
    reset_memory_log_timer,
)
from ohlcv_client import (
    MAX_OHLCV_CACHE_ENTRIES,
    _ohlcv_cache,
    clear_ohlcv_cache,
    fetch_ohlcv_5m,
)
from safety_gate_node import (
    MAX_SAFETY_CACHE_ENTRIES,
    _safety_cache,
    cache_safety_report,
    clear_safety_cache,
    get_cached_safety_report,
)
from state import SafetyReport


def test_duckdb_memory_limit_threads_and_insertion_order(tmp_path: Path):
    """Verify every AgentDatabase connection caps memory to 256MB, threads=2, preserve_insertion_order=false."""
    db = AgentDatabase(db_path=str(tmp_path / "mem_cap.duckdb"), starting_sol=1.0)
    try:
        mem_limit = db.get_duckdb_setting("memory_limit")
        threads = db.get_duckdb_setting("threads")
        preserve_order = db.get_duckdb_setting("preserve_insertion_order")

        # DuckDB formats 256MB as '244.1 MiB' or '256.0 MiB' or '256MB'
        assert any(tok in mem_limit for tok in ("244.1", "256", "MiB", "MB")), f"unexpected memory_limit={mem_limit}"
        assert int(threads) == 2
        assert preserve_order.lower() in ("false", "0")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_bounded_in_memory_caches_evict_ttl_and_max_size(tmp_path: Path):
    """Verify OHLCV, Jupiter mint cache, SafetyGate cache, webhook sigs, and price backoff are strictly bounded."""
    clear_ohlcv_cache()
    clear_jupiter_token_cache()
    clear_safety_cache()
    clear_helius_roster_cache()
    clear_price_backoff()

    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper")
    base_ts = 1_700_000_000.0

    # 1. OHLCV cache capped at MAX_OHLCV_CACHE_ENTRIES (64)
    def _ohlcv_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"attributes": {"ohlcv_list": [[base_ts, 1.0, 1.1, 0.9, 1.05, 1000.0]]}}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_ohlcv_handler)) as client:
        for i in range(MAX_OHLCV_CACHE_ENTRIES + 20):
            await fetch_ohlcv_5m(f"PoolAddr_{i:03d}", cfg, client, now_ts=base_ts + i)
    assert len(_ohlcv_cache) <= MAX_OHLCV_CACHE_ENTRIES
    assert "PoolAddr_000" not in _ohlcv_cache

    # 2. Jupiter _mint_cache capped at MAX_MINT_CACHE_ENTRIES (256)
    def _jup_handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("query", "")
        items = [{"id": m, "symbol": "TOK", "mcap": 200_000, "organicScore": 70.0} for m in q.split(",") if m]
        return httpx.Response(200, json=items)

    all_mints = [f"MintJup_{i:03d}" for i in range(MAX_MINT_CACHE_ENTRIES + 44)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(_jup_handler)) as client:
        await enrich_mints(all_mints, cfg, client, now_ts=base_ts)
    assert len(_mint_cache) <= MAX_MINT_CACHE_ENTRIES

    # 3. SafetyGate cache capped at MAX_SAFETY_CACHE_ENTRIES (128) & expires after TTL
    for i in range(MAX_SAFETY_CACHE_ENTRIES + 25):
        rep = SafetyReport(mint=f"SafeMint_{i:03d}", passed=True, reasons=[])
        cache_safety_report(rep.mint, rep, now_ts=base_ts + i)
    assert len(_safety_cache) <= MAX_SAFETY_CACHE_ENTRIES
    assert get_cached_safety_report("SafeMint_000", now_ts=base_ts + 200) is None
    # Verify TTL expiration after 301s
    latest_mint = f"SafeMint_{MAX_SAFETY_CACHE_ENTRIES + 24:03d}"
    assert get_cached_safety_report(latest_mint, now_ts=base_ts + 200) is not None
    assert get_cached_safety_report(latest_mint, now_ts=base_ts + 1000) is None

    # 4. Helius webhook signature ring buffer capped at MAX_RECENT_WEBHOOK_SIGS (500)
    db = AgentDatabase(db_path=str(tmp_path / "wh_bound.duckdb"), starting_sol=1.0)
    try:
        wallet = "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk"
        now_dt = datetime(2026, 11, 21, 16, 0, 0, tzinfo=timezone.utc)
        for batch_idx in range(6):
            payload = [
                {
                    "signature": f"sig-{batch_idx}-{j}",
                    "type": "SWAP",
                    "timestamp": int(now_dt.timestamp()),
                    "feePayer": wallet,
                    "nativeTransfers": [{"fromUserAccount": wallet, "toUserAccount": "Pool1", "amount": 1_000_000_000}],
                    "tokenTransfers": [
                        {
                            "mint": "MintWhale111111111111111111111111111111111",
                            "fromUserAccount": "Pool1",
                            "toUserAccount": wallet,
                            "tokenAmount": 1000.0,
                        }
                    ],
                }
                for j in range(100)
            ]
            process_helius_webhook_payload(
                payload,
                config=cfg,
                db=db,
                active_roster={wallet},
                strict_roster=True,
                now=now_dt + timedelta(seconds=batch_idx),
            )
        assert len(_recent_webhook_sigs) <= MAX_RECENT_WEBHOOK_SIGS
    finally:
        db.close()

    # 5. Batched price backoff map capped at MAX_PRICE_BACKOFF_ENTRIES (32)
    for i in range(MAX_PRICE_BACKOFF_ENTRIES + 10):
        _record_429(f"ep_{i}", base_ts)
    assert len(_price_backoff) <= MAX_PRICE_BACKOFF_ENTRIES


@pytest.mark.asyncio
async def test_whale_roster_60s_failure_backoff_prevents_retry_storm():
    """Verify [WhaleRoster] sync failure sets a 60s backoff and skips network calls until backoff expires."""
    clear_helius_roster_cache()
    cfg = AgentConfig(
        _env_file=None,
        EXECUTION_MODE="paper",
        HELIUS_API_KEY="test-key",
        HELIUS_WEBHOOK_ID="hook-123",
        HELIUS_TRACKED_WALLETS="FallbackWallet1111111111111111111111111111",
    )
    calls = {"count": 0}

    def _failing_then_ok(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json={"webhookID": "hook-123", "accountAddresses": ["SyncedWallet222222222222222222222222222222"]})

    t0 = 1_700_000_000.0
    async with httpx.AsyncClient(transport=httpx.MockTransport(_failing_then_ok)) as client:
        # 1st call fails -> enters 60s backoff and returns fallback wallet
        w1 = await fetch_helius_tracked_wallets(cfg, http_client=client, now_ts=t0)
        assert w1 == ["FallbackWallet1111111111111111111111111111"]
        assert calls["count"] == 1
        assert _helius_roster_cache["backoff_until"] == t0 + HELIUS_ROSTER_ERROR_BACKOFF_SECONDS

        # Calls at t0 + 10s and t0 + 59s must NOT hit the network
        w2 = await fetch_helius_tracked_wallets(cfg, http_client=client, now_ts=t0 + 10.0)
        w3 = await fetch_helius_tracked_wallets(cfg, http_client=client, now_ts=t0 + 59.0)
        assert w2 == w1 and w3 == w1
        assert calls["count"] == 1

        # Call at t0 + 61s (after 60s backoff) retries and succeeds
        w4 = await fetch_helius_tracked_wallets(cfg, http_client=client, now_ts=t0 + 61.0)
        assert w4 == ["SyncedWallet222222222222222222222222222222"]
        assert calls["count"] == 2
        assert _helius_roster_cache["backoff_until"] == 0.0


def test_periodic_rss_memory_log_and_table_pruning(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """Verify maybe_log_rss_memory logs every 10 minutes and prunes expired DuckDB rows."""
    reset_memory_log_timer()
    db = AgentDatabase(db_path=str(tmp_path / "prune.duckdb"), starting_sol=1.0)
    try:
        past_dt = datetime.now(timezone.utc) - timedelta(hours=5)
        db.set_cooldown("ExpiredMint111", hours=1.0, reason="OLD", now=past_dt)
        assert get_process_rss_mb() > 0.0

        t0 = 1_700_000_000.0
        with caplog.at_level(logging.INFO, logger="solana-meme-agent"):
            rss1 = maybe_log_rss_memory(db=db, now_ts=t0)
            assert rss1 is not None and rss1 > 0.0
            assert any("[Memory] RSS=" in rec.message for rec in caplog.records)

            # Calling 60s later (< 600s) returns None and does not spam logs
            caplog.clear()
            rss2 = maybe_log_rss_memory(db=db, now_ts=t0 + 60.0)
            assert rss2 is None
            assert not any("[Memory] RSS=" in rec.message for rec in caplog.records)

            # Calling at t0 + 601s logs again
            rss3 = maybe_log_rss_memory(db=db, now_ts=t0 + 601.0)
            assert rss3 is not None and rss3 > 0.0
            assert any("[Memory] RSS=" in rec.message for rec in caplog.records)

        # Verify expired cooldown row was physically deleted from DuckDB table
        row = db._conn.execute("SELECT COUNT(*) FROM cooldowns WHERE mint = 'ExpiredMint111'").fetchone()
        assert row is not None and int(row[0]) == 0
    finally:
        db.close()
