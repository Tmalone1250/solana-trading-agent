"""Rate-limit-safe GeckoTerminal 5m OHLCV fetcher with 90s per-pool cache & backoff."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

from config import AgentConfig

logger = logging.getLogger(__name__)

# Per-pair_address cache:
# pair_address -> {"fetched_at": float, "candles": list[dict], "backoff_until": float, "errors": int}
_ohlcv_cache: dict[str, dict[str, Any]] = {}


def clear_ohlcv_cache() -> None:
    """Clear the in-memory OHLCV cache (useful for tests)."""
    _ohlcv_cache.clear()


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


async def fetch_ohlcv_5m(
    pair_address: str,
    config: AgentConfig,
    client: httpx.AsyncClient,
    *,
    now_ts: Optional[float] = None,
) -> tuple[list[dict[str, float]], bool]:
    """Fetch 30 x 5m USD candles for a Solana pool.

    Returns (candles_oldest_first, made_network_call).
    """
    addr = (pair_address or "").strip()
    if not addr:
        return [], False

    current_ts = now_ts if now_ts is not None else time.time()
    entry = _ohlcv_cache.get(addr)
    if entry is not None:
        if current_ts < float(entry.get("backoff_until") or 0.0):
            return list(entry.get("candles") or []), False
        fetched_at = entry.get("fetched_at")
        if fetched_at is not None and (current_ts - float(fetched_at)) < config.OHLCV_CACHE_SECONDS:
            return list(entry.get("candles") or []), False

    url = f"{config.GECKOTERMINAL_BASE_URL}/networks/solana/pools/{addr}/ohlcv/minute"
    params = {"aggregate": "5", "limit": "30", "currency": "usd"}
    prev_candles = list((entry or {}).get("candles") or [])
    prev_errors = int((entry or {}).get("errors") or 0)

    try:
        resp = await client.get(
            url,
            params=params,
            headers={"Accept": "application/json"},
            timeout=10.0,
        )
        if resp.status_code == 429:
            errors = prev_errors + 1
            backoff = min(300.0, 30.0 * (2 ** (errors - 1)))
            _ohlcv_cache[addr] = {
                "fetched_at": (entry or {}).get("fetched_at"),
                "candles": prev_candles,
                "backoff_until": current_ts + backoff,
                "errors": errors,
            }
            logger.warning(
                "[OHLCV] GeckoTerminal 429 for pool %s..; backing off %.0fs (cached=%d candles)",
                addr[:8],
                backoff,
                len(prev_candles),
            )
            return prev_candles, True

        resp.raise_for_status()
        candles = parse_geckoterminal_ohlcv(resp.json())
        _ohlcv_cache[addr] = {
            "fetched_at": current_ts,
            "candles": candles,
            "backoff_until": 0.0,
            "errors": 0,
        }
        return list(candles), True
    except Exception as exc:
        errors = prev_errors + 1
        backoff = min(300.0, 15.0 * (2 ** (errors - 1)))
        _ohlcv_cache[addr] = {
            "fetched_at": (entry or {}).get("fetched_at"),
            "candles": prev_candles,
            "backoff_until": current_ts + backoff,
            "errors": errors,
        }
        logger.warning(
            "[OHLCV] Fetch failed for pool %s.. (%s); backing off %.0fs (cached=%d candles)",
            addr[:8],
            exc,
            backoff,
            len(prev_candles),
        )
        return prev_candles, True


async def fetch_ohlcv_batch(
    pair_addresses: list[str],
    config: AgentConfig,
    client: httpx.AsyncClient,
    *,
    now_ts: Optional[float] = None,
) -> dict[str, list[dict[str, float]]]:
    """Fetch 5m OHLCV for up to MAX_WATCH_BATCH pools, pacing network calls cleanly."""
    results: dict[str, list[dict[str, float]]] = {}
    network_calls = 0

    for addr in pair_addresses:
        if not addr or addr in results:
            continue
        if network_calls > 0 and config.OHLCV_PACING_DELAY_SECONDS > 0:
            await asyncio.sleep(config.OHLCV_PACING_DELAY_SECONDS)
        candles, did_net = await fetch_ohlcv_5m(addr, config, client, now_ts=now_ts)
        results[addr] = candles
        if did_net:
            network_calls += 1

    return results
