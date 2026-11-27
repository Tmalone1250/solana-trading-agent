"""Rate-limit-safe GeckoTerminal OHLCV fetcher with global pacing, global 429 backoff & shared cache."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Literal, Optional

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

OhlcvSource = Literal[
    "network_200",
    "cache_fresh",
    "stale_backoff",
    "stale_429",
    "stale_error",
    "unavailable",
    "pre_attached_verified",
    "pre_attached_unknown",
]

ELIGIBLE_OHLCV_SOURCES = frozenset({"network_200", "cache_fresh", "pre_attached_verified"})


@dataclass(frozen=True)
class OhlcvFetchResult:
    """Typed OHLCV fetch/cache result carrying authoritative provenance metadata."""

    candles: list[dict[str, float]]
    source: OhlcvSource
    fetched_at: Optional[float]
    last_ok_at: Optional[float]
    last_status: Optional[int]

    def age_seconds(self, decision_timestamp: float) -> Optional[float]:
        """Return age in seconds since last_ok_at, or None if last_ok_at is missing/invalid."""
        if self.last_ok_at is None or isinstance(self.last_ok_at, bool):
            return None
        if not isinstance(self.last_ok_at, (int, float)):
            return None
        ok_at = float(self.last_ok_at)
        if not math.isfinite(ok_at) or ok_at <= 0.0:
            return None
        return decision_timestamp - ok_at


@dataclass(frozen=True)
class BreakoutTelemetryCacheEntry:
    """Cached breakout telemetry paired with originating OHLCV timestamp and source metadata."""

    telemetry: dict[str, Any]
    cached_at: float
    ohlcv_last_ok_at: Optional[float]
    ohlcv_source: Optional[str]
    data_age_seconds: Optional[float]
    is_historical: bool
    is_trusted: bool


def evaluate_ohlcv_provenance(
    result: Any,
    *,
    decision_timestamp: float,
    max_cache_age_seconds: float = 90.0,
    clock_skew_seconds: float = 5.0,
) -> tuple[bool, Optional[str]]:
    """Authoritatively validate OHLCV provenance; fail closed on missing, stale, or contradictory metadata."""
    if result is None:
        return False, "missing OHLCV provenance"
    if not isinstance(result, OhlcvFetchResult):
        return False, "invalid OHLCV provenance type"
    if result.source not in ELIGIBLE_OHLCV_SOURCES:
        return False, f"ineligible OHLCV source ({result.source})"
    if result.last_status != 200:
        return False, f"ineligible OHLCV status ({result.last_status})"
    age = result.age_seconds(decision_timestamp)
    if age is None:
        return False, "missing or invalid OHLCV last_ok_at"
    if age < -clock_skew_seconds:
        return False, "future OHLCV last_ok_at"
    if age > max_cache_age_seconds:
        return False, f"expired OHLCV cache ({age:.1f}s > {max_cache_age_seconds:.1f}s)"
    return True, None


def select_ohlcv_interval_provenance(
    res_5m: Any,
    res_1m: Any,
    *,
    decision_timestamp: float,
    max_cache_age_seconds: float = 90.0,
) -> tuple[OhlcvFetchResult, bool, Optional[str]]:
    """Deterministically select active 5m/1m OhlcvFetchResult using provenance and closed-bar count."""
    unavail = OhlcvFetchResult(candles=[], source="unavailable", fetched_at=None, last_ok_at=None, last_status=None)
    r5 = res_5m if isinstance(res_5m, OhlcvFetchResult) else unavail
    r1 = res_1m if isinstance(res_1m, OhlcvFetchResult) else unavail

    ok_5m, err_5m = evaluate_ohlcv_provenance(
        r5, decision_timestamp=decision_timestamp, max_cache_age_seconds=max_cache_age_seconds
    )
    ok_1m, err_1m = evaluate_ohlcv_provenance(
        r1, decision_timestamp=decision_timestamp, max_cache_age_seconds=max_cache_age_seconds
    )
    closed_5m, _, _ = normalize_and_filter_candles(r5.candles, bar_minutes=5, decision_timestamp=decision_timestamp)
    closed_1m, _, _ = normalize_and_filter_candles(r1.candles, bar_minutes=1, decision_timestamp=decision_timestamp)

    if ok_5m and len(closed_5m) >= 12:
        return r5, True, None

    if ok_5m:
        if ok_1m and len(closed_1m) >= 15:
            return r1, True, None
        if not ok_1m:
            return r1, False, err_1m
        return r5, True, None

    if r1.source == "pre_attached_verified" and ok_1m and len(closed_1m) >= 15:
        return r1, True, None

    chosen = r5 if r5.source != "unavailable" else r1
    return chosen, False, err_5m if r5.source != "unavailable" else err_1m

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
    ohlcv_source: Optional[str] = None,
    ohlcv_last_ok_at: Optional[float] = None,
    now_ts: Optional[float] = None,
) -> None:
    """Store latest non-empty breakout_telemetry for a mint with originating OHLCV provenance."""
    m = (mint or "").strip()
    if not m or not isinstance(telemetry, dict) or telemetry.get("score") is None:
        return
    ts = now_ts if now_ts is not None else time.time()
    _breakout_telemetry_cache[m] = {
        "fetched_at": ts,
        "cached_at": ts,
        "ohlcv_last_ok_at": ohlcv_last_ok_at,
        "ohlcv_source": ohlcv_source,
        "telemetry": dict(telemetry),
    }
    prune_timestamped_cache(
        _breakout_telemetry_cache,
        now_ts=ts,
        max_entries=MAX_BREAKOUT_CACHE_ENTRIES,
        ttl_seconds=BREAKOUT_CACHE_TTL_SECONDS,
    )


def get_cached_breakout_telemetry_with_meta(
    mint: str,
    *,
    now_ts: Optional[float] = None,
    clock_skew_seconds: float = 5.0,
) -> Optional[BreakoutTelemetryCacheEntry]:
    """Return cached breakout_telemetry with originating data-age validation, else None."""
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
    if not entry or not isinstance(entry.get("telemetry"), dict):
        return None

    cached_at = float(entry.get("cached_at") or entry.get("fetched_at") or ts)
    raw_ok_at = entry.get("ohlcv_last_ok_at")
    ohlcv_source = entry.get("ohlcv_source")
    data_age: Optional[float] = None
    is_trusted = False
    validated_ok_at: Optional[float] = None

    if (
        raw_ok_at is not None
        and not isinstance(raw_ok_at, bool)
        and isinstance(raw_ok_at, (int, float))
    ):
        ok_at_f = float(raw_ok_at)
        if math.isfinite(ok_at_f) and ok_at_f > 0.0 and ok_at_f <= ts + clock_skew_seconds:
            validated_ok_at = ok_at_f
            data_age = max(0.0, ts - ok_at_f)
            is_trusted = True

    return BreakoutTelemetryCacheEntry(
        telemetry=dict(entry["telemetry"]),
        cached_at=cached_at,
        ohlcv_last_ok_at=validated_ok_at,
        ohlcv_source=str(ohlcv_source) if ohlcv_source is not None else None,
        data_age_seconds=data_age,
        is_historical=True,
        is_trusted=is_trusted,
    )


def get_cached_breakout_telemetry(
    mint: str,
    *,
    now_ts: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Return cached breakout_telemetry for a mint if fresh within TTL, else None."""
    meta = get_cached_breakout_telemetry_with_meta(mint, now_ts=now_ts)
    return dict(meta.telemetry) if meta is not None else None


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


async def _fetch_ohlcv_interval_with_provenance(
    pair_address: str,
    config: AgentConfig,
    client: httpx.AsyncClient,
    *,
    aggregate: str,
    limit: str,
    cache_suffix: str,
    now_ts: Optional[float] = None,
) -> OhlcvFetchResult:
    """Fetch GeckoTerminal minute OHLCV bars and return typed provenance metadata."""
    global _global_backoff_until, _global_429_errors, _last_gecko_request_ts

    addr = (pair_address or "").strip()
    if not addr:
        return OhlcvFetchResult(
            candles=[], source="unavailable", fetched_at=None, last_ok_at=None, last_status=None
        )

    cache_key = f"{addr}{cache_suffix}"
    current_ts = now_ts if now_ts is not None else time.time()
    entry = _ohlcv_cache.get(cache_key)
    prev_candles = list((entry or {}).get("candles") or [])
    prev_ok_at: Optional[float] = (entry or {}).get("last_ok_at")
    if prev_ok_at is None and (entry or {}).get("last_ok"):
        prev_ok_at = (entry or {}).get("fetched_at")

    # 1. Global 429 Backoff Check
    if current_ts < _global_backoff_until:
        logger.debug(
            "[OHLCV] Global GeckoTerminal backoff active (%.0fs remaining) — serving %d cached candles for %s..",
            _global_backoff_until - current_ts,
            len(prev_candles),
            addr[:8],
        )
        return OhlcvFetchResult(
            candles=prev_candles,
            source="stale_backoff",
            fetched_at=(entry or {}).get("fetched_at"),
            last_ok_at=prev_ok_at,
            last_status=(entry or {}).get("last_status"),
        )

    # 2. Fresh Cache Hit Check
    if entry is not None:
        fetched_at = entry.get("fetched_at")
        if (
            entry.get("last_ok")
            and fetched_at is not None
            and (current_ts - float(fetched_at)) < config.OHLCV_CACHE_SECONDS
        ):
            return OhlcvFetchResult(
                candles=prev_candles,
                source="cache_fresh",
                fetched_at=float(fetched_at),
                last_ok_at=prev_ok_at,
                last_status=int(entry.get("last_status") or 200),
            )

    url = f"{config.GECKOTERMINAL_BASE_URL}/networks/solana/pools/{addr}/ohlcv/minute"
    params = {"aggregate": aggregate, "limit": limit, "currency": "usd"}
    min_spacing = float(config.OHLCV_PACING_DELAY_SECONDS)

    async with _get_gecko_lock():
        check_ts = now_ts if now_ts is not None else time.time()
        if check_ts < _global_backoff_until:
            return OhlcvFetchResult(
                candles=prev_candles,
                source="stale_backoff",
                fetched_at=(entry or {}).get("fetched_at"),
                last_ok_at=prev_ok_at,
                last_status=(entry or {}).get("last_status"),
            )

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
                entry_fetched_at = (entry or {}).get("fetched_at") or check_ts
                _ohlcv_cache[cache_key] = {
                    "fetched_at": entry_fetched_at,
                    "last_ok_at": prev_ok_at,
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
                return OhlcvFetchResult(
                    candles=prev_candles,
                    source="stale_429",
                    fetched_at=entry_fetched_at,
                    last_ok_at=prev_ok_at,
                    last_status=429,
                )

            resp.raise_for_status()
            candles = parse_geckoterminal_ohlcv(resp.json())
            _global_429_errors = 0
            _global_backoff_until = 0.0
            _ohlcv_cache[cache_key] = {
                "fetched_at": check_ts,
                "last_ok_at": check_ts,
                "candles": candles,
                "last_ok": True,
                "last_status": 200,
            }
            _prune_ohlcv_cache(check_ts, config)
            return OhlcvFetchResult(
                candles=list(candles),
                source="network_200",
                fetched_at=check_ts,
                last_ok_at=check_ts,
                last_status=200,
            )
        except Exception as exc:
            entry_fetched_at = (entry or {}).get("fetched_at") or check_ts
            _ohlcv_cache[cache_key] = {
                "fetched_at": entry_fetched_at,
                "last_ok_at": prev_ok_at,
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
            return OhlcvFetchResult(
                candles=prev_candles,
                source="stale_error",
                fetched_at=entry_fetched_at,
                last_ok_at=prev_ok_at,
                last_status=0,
            )


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
    """Legacy wrapper returning (candles, did_network)."""
    res = await _fetch_ohlcv_interval_with_provenance(
        pair_address, config, client, aggregate=aggregate, limit=limit, cache_suffix=cache_suffix, now_ts=now_ts
    )
    return res.candles, res.source in ("network_200", "stale_429", "stale_error")


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


async def fetch_ohlcv_batch_with_provenance(
    pair_addresses: list[str],
    config: AgentConfig,
    client: httpx.AsyncClient,
    *,
    now_ts: Optional[float] = None,
) -> dict[str, OhlcvFetchResult]:
    """Fetch 5m OHLCV for up to MAX_WATCH_BATCH pools with authoritative provenance metadata."""
    results: dict[str, OhlcvFetchResult] = {}
    for addr in pair_addresses:
        if not addr or addr in results:
            continue
        results[addr] = await _fetch_ohlcv_interval_with_provenance(
            addr, config, client, aggregate="5", limit="30", cache_suffix="", now_ts=now_ts
        )
    return results


async def fetch_ohlcv_1m_batch_with_provenance(
    pair_addresses: list[str],
    config: AgentConfig,
    client: httpx.AsyncClient,
    *,
    now_ts: Optional[float] = None,
) -> dict[str, OhlcvFetchResult]:
    """Fetch 1m OHLCV fallback ONLY when 5m succeeded (200 OK) with < 12 closed bars; never serve unflagged c1_cached."""
    results: dict[str, OhlcvFetchResult] = {}
    for addr in pair_addresses:
        if not addr or addr in results:
            continue
        c5_cached, _ = get_cached_ohlcv_for_pool(addr)
        if not should_fetch_1m_fallback(addr, c5_cached, now_ts=now_ts):
            results[addr] = OhlcvFetchResult(
                candles=[],
                source="unavailable",
                fetched_at=None,
                last_ok_at=None,
                last_status=None,
            )
            continue
        results[addr] = await _fetch_ohlcv_interval_with_provenance(
            addr, config, client, aggregate="1", limit="30", cache_suffix=":1m", now_ts=now_ts
        )
    return results


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
