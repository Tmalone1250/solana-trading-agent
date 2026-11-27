"""Rate-limit-safe GeckoTerminal OHLCV fetcher with global pacing, global 429 backoff & shared cache."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

from breakout_ignition import normalize_and_filter_candles
from config import AgentConfig
from memory_guard import prune_timestamped_cache

logger = logging.getLogger(__name__)

MAX_OHLCV_CACHE_ENTRIES = 64
OHLCV_CACHE_MAX_TTL_SECONDS = 600.0
GLOBAL_429_BASE_SECONDS = 30.0
GLOBAL_429_MAX_SECONDS = 120.0
GLOBAL_MIN_SPACING_SECONDS = 6.5
MAX_BREAKOUT_CACHE_ENTRIES = 64
BREAKOUT_CACHE_TTL_SECONDS = 1800.0

# Per-pair_address cache:
# cache_key -> {"fetched_at": float, "candles": list[dict], "last_ok": bool, "last_status": int}
_ohlcv_cache: dict[str, dict[str, Any]] = {}

# Shared mint -> {"fetched_at": float, "telemetry": dict} cache for Watchlist & Whale Radar
_breakout_telemetry_cache: dict[str, dict[str, Any]] = {}

# Global GeckoTerminal rate-limit & backoff state shared across all pools and callers
_global_backoff_until: float = 0.0
_global_429_errors: int = 0
_last_gecko_request_ts: float = 0.0
_loop_locks: dict[int, asyncio.Lock] = {}


def _get_gecko_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lid = id(loop)
    lock = _loop_locks.get(lid)
    if lock is None:
        if len(_loop_locks) > 8:
            _loop_locks.clear()
        lock = asyncio.Lock()
        _loop_locks[lid] = lock
    return lock


def clear_ohlcv_cache() -> None:
    """Clear the in-memory OHLCV cache, global backoff, pacing clock, and breakout cache (for tests)."""
    global _global_backoff_until, _global_429_errors, _last_gecko_request_ts
    _ohlcv_cache.clear()
    _breakout_telemetry_cache.clear()
    _global_backoff_until = 0.0
    _global_429_errors = 0
    _last_gecko_request_ts = 0.0


def get_global_backoff_until() -> float:
    """Return the Unix timestamp until which all GeckoTerminal OHLCV requests are paused."""
    return _global_backoff_until


def cache_breakout_telemetry(
    mint: str,
    telemetry: Optional[dict[str, Any]],
    *,
    now_ts: Optional[float] = None,
) -> None:
    """Store latest non-empty breakout_telemetry for a mint in the shared bounded cache."""
    m = (mint or "").strip()
    if not m or not isinstance(telemetry, dict) or telemetry.get("score") is None:
        return
    ts = now_ts if now_ts is not None else time.time()
    _breakout_telemetry_cache[m] = {"fetched_at": ts, "telemetry": dict(telemetry)}
    prune_timestamped_cache(
        _breakout_telemetry_cache,
        now_ts=ts,
        max_entries=MAX_BREAKOUT_CACHE_ENTRIES,
        ttl_seconds=BREAKOUT_CACHE_TTL_SECONDS,
    )


def get_cached_breakout_telemetry(
    mint: str,
    *,
    now_ts: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Return cached breakout_telemetry for a mint if fresh within TTL, else None."""
    m = (mint or "").strip()
    if not m:
        return None
    ts = now_ts if now_ts is not None else time.time()
    prune_timestamped_cache(
        _breakout_telemetry_cache,
        now_ts=ts,
        max_entries=MAX_BREAKOUT_CACHE_ENTRIES,
        ttl_seconds=BREAKOUT_CACHE_TTL_SECONDS,
    )
    entry = _breakout_telemetry_cache.get(m)
    if entry and isinstance(entry.get("telemetry"), dict):
        return dict(entry["telemetry"])
    return None


def get_cached_ohlcv_for_pool(pair_address: str) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
    """Return (cached_5m_candles, cached_1m_candles) for a pool without making any network call."""
    addr = (pair_address or "").strip()
    if not addr:
        return [], []
    e5 = _ohlcv_cache.get(addr) or {}
    e1 = _ohlcv_cache.get(f"{addr}:1m") or {}
    return list(e5.get("candles") or []), list(e1.get("candles") or [])


def should_fetch_1m_fallback(
    pair_address: str,
    candles_5m: list[dict[str, float]],
    *,
    now_ts: Optional[float] = None,
) -> bool:
    """Return True ONLY when 5m succeeded (200 OK) with < 12 closed candles and global backoff is not active."""
    addr = (pair_address or "").strip()
    if not addr:
        return False
    current_ts = now_ts if now_ts is not None else time.time()
    closed_5m, err, _ = normalize_and_filter_candles(
        candles_5m if candles_5m is not None else [],
        bar_minutes=5,
        decision_timestamp=current_ts,
    )
    if err is not None or len(closed_5m) >= 12:
        return False
    if current_ts < _global_backoff_until:
        return False
    entry_5m = _ohlcv_cache.get(addr)
    if not isinstance(entry_5m, dict):
        return False
    return bool(entry_5m.get("last_ok")) and int(entry_5m.get("last_status") or 0) == 200


def _prune_ohlcv_cache(now_ts: float, config: AgentConfig) -> None:
    ttl = max(OHLCV_CACHE_MAX_TTL_SECONDS, float(config.OHLCV_CACHE_SECONDS) * 2.0)
    prune_timestamped_cache(
        _ohlcv_cache,
        now_ts=now_ts,
        max_entries=MAX_OHLCV_CACHE_ENTRIES,
        ttl_seconds=ttl,
    )


def parse_geckoterminal_ohlcv(payload: dict[str, Any]) -> list[dict[str, float]]:
    """Parse GeckoTerminal OHLCV JSON:API payload into oldest-first candle dicts."""
    raw_list = (
        ((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list")
        or []
    )
    candles: list[dict[str, float]] = []
    for row in raw_list:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        candles.append(
            {
                "timestamp": float(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
        )
    candles.sort(key=lambda c: c["timestamp"])
    return candles


async def _fetch_ohlcv_interval(
    pair_address: str,
    config: AgentConfig,
    client: httpx.AsyncClient,
    *,
    aggregate: str,
    limit: str,
    cache_suffix: str,
    now_ts: Optional[float] = None,
) -> tuple[list[dict[str, float]], bool]:
    """Fetch GeckoTerminal minute OHLCV bars with global spacing, global 429 backoff & stale-while-revalidate."""
    global _global_backoff_until, _global_429_errors, _last_gecko_request_ts

    addr = (pair_address or "").strip()
    if not addr:
        return [], False

    cache_key = f"{addr}{cache_suffix}"
    current_ts = now_ts if now_ts is not None else time.time()
    entry = _ohlcv_cache.get(cache_key)
    prev_candles = list((entry or {}).get("candles") or [])

    # 1. Global 429 Backoff Check (stale-while-revalidate: serve cached candles without hitting API)
    if current_ts < _global_backoff_until:
        logger.debug(
            "[OHLCV] Global GeckoTerminal backoff active (%.0fs remaining) — serving %d cached candles for %s..",
            _global_backoff_until - current_ts,
            len(prev_candles),
            addr[:8],
        )
        return prev_candles, False

    # 2. Fresh Cache Hit Check
    if entry is not None:
        fetched_at = entry.get("fetched_at")
        if (
            entry.get("last_ok")
            and fetched_at is not None
            and (current_ts - float(fetched_at)) < config.OHLCV_CACHE_SECONDS
        ):
            return prev_candles, False

    url = f"{config.GECKOTERMINAL_BASE_URL}/networks/solana/pools/{addr}/ohlcv/minute"
    params = {"aggregate": aggregate, "limit": limit, "currency": "usd"}
    min_spacing = float(config.OHLCV_PACING_DELAY_SECONDS)

    async with _get_gecko_lock():
        # Re-check global backoff inside the lock in case another coroutine just hit 429
        check_ts = now_ts if now_ts is not None else time.time()
        if check_ts < _global_backoff_until:
            return prev_candles, False

        # Global minimum spacing across all GeckoTerminal requests (default 6.5s)
        if min_spacing > 0 and _last_gecko_request_ts > 0:
            elapsed = check_ts - _last_gecko_request_ts
            if 0.0 <= elapsed < min_spacing:
                await asyncio.sleep(min_spacing - elapsed)
                check_ts = now_ts if now_ts is not None else time.time()

        _last_gecko_request_ts = check_ts
        try:
            resp = await client.get(
                url,
                params=params,
                headers={"Accept": "application/json"},
                timeout=10.0,
            )
            if resp.status_code == 429:
                _global_429_errors += 1
                backoff = min(
                    GLOBAL_429_MAX_SECONDS,
                    GLOBAL_429_BASE_SECONDS * (2 ** (_global_429_errors - 1)),
                )
                _global_backoff_until = check_ts + backoff
                _ohlcv_cache[cache_key] = {
                    "fetched_at": (entry or {}).get("fetched_at") or check_ts,
                    "candles": prev_candles,
                    "last_ok": False,
                    "last_status": 429,
                }
                _prune_ohlcv_cache(check_ts, config)
                logger.warning(
                    "[OHLCV] GeckoTerminal 429 (%sm) on pool %s..; GLOBAL backoff %.0fs (cached=%d candles)",
                    aggregate,
                    addr[:8],
                    backoff,
                    len(prev_candles),
                )
                return prev_candles, True

            resp.raise_for_status()
            candles = parse_geckoterminal_ohlcv(resp.json())
            _global_429_errors = 0
            _global_backoff_until = 0.0
            _ohlcv_cache[cache_key] = {
                "fetched_at": check_ts,
                "candles": candles,
                "last_ok": True,
                "last_status": 200,
            }
            _prune_ohlcv_cache(check_ts, config)
            return list(candles), True
        except Exception as exc:
            _ohlcv_cache[cache_key] = {
                "fetched_at": (entry or {}).get("fetched_at") or check_ts,
                "candles": prev_candles,
                "last_ok": False,
                "last_status": 0,
            }
            _prune_ohlcv_cache(check_ts, config)
            logger.warning(
                "[OHLCV] Fetch failed (%sm) for pool %s.. (%s); serving %d cached candles",
                aggregate,
                addr[:8],
                exc,
                len(prev_candles),
            )
            return prev_candles, True


async def fetch_ohlcv_5m(
    pair_address: str,
    config: AgentConfig,
    client: httpx.AsyncClient,
    *,
    now_ts: Optional[float] = None,
) -> tuple[list[dict[str, float]], bool]:
    """Fetch 30 x 5m USD candles for a Solana pool."""
    return await _fetch_ohlcv_interval(
        pair_address, config, client, aggregate="5", limit="30", cache_suffix="", now_ts=now_ts
    )


async def fetch_ohlcv_1m(
    pair_address: str,
    config: AgentConfig,
    client: httpx.AsyncClient,
    *,
    now_ts: Optional[float] = None,
) -> tuple[list[dict[str, float]], bool]:
    """Fetch 30 x 1m USD candles for fresh Solana runners lacking 12 x 5m bars."""
    return await _fetch_ohlcv_interval(
        pair_address, config, client, aggregate="1", limit="30", cache_suffix=":1m", now_ts=now_ts
    )


async def fetch_ohlcv_batch(
    pair_addresses: list[str],
    config: AgentConfig,
    client: httpx.AsyncClient,
    *,
    now_ts: Optional[float] = None,
) -> dict[str, list[dict[str, float]]]:
    """Fetch 5m OHLCV for up to MAX_WATCH_BATCH pools using global spacing and global 429 backoff."""
    results: dict[str, list[dict[str, float]]] = {}
    for addr in pair_addresses:
        if not addr or addr in results:
            continue
        candles, _did_net = await fetch_ohlcv_5m(addr, config, client, now_ts=now_ts)
        results[addr] = candles
    return results


async def fetch_ohlcv_1m_batch(
    pair_addresses: list[str],
    config: AgentConfig,
    client: httpx.AsyncClient,
    *,
    now_ts: Optional[float] = None,
) -> dict[str, list[dict[str, float]]]:
    """Fetch 1m OHLCV fallback ONLY for pools whose 5m request succeeded (200 OK) with < 12 candles."""
    results: dict[str, list[dict[str, float]]] = {}
    for addr in pair_addresses:
        if not addr or addr in results:
            continue
        c5_cached, c1_cached = get_cached_ohlcv_for_pool(addr)
        if not should_fetch_1m_fallback(addr, c5_cached, now_ts=now_ts):
            results[addr] = c1_cached
            continue
        candles, _did_net = await fetch_ohlcv_1m(addr, config, client, now_ts=now_ts)
        results[addr] = candles
    return results
