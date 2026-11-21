"""Jupiter Token API v2 client for organic discovery and batch token enrichment."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

from config import AgentConfig

logger = logging.getLogger(__name__)

# Known Solana base, stablecoin, and major LST mints to exclude from memecoin discovery
IGNORED_MAJOR_MINTS: set[str] = {
    "So11111111111111111111111111111111111111112",  # Wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # jitoSOL
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",  # bSOL
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",  # JUP
    "11111111111111111111111111111111",  # System Program
}

# In-memory 60s caches:
# discovery cache stores the latest /toporganicscore/5m + /toptrending/5m snapshot
_discovery_cache: dict[str, Any] = {
    "fetched_at": None,
    "backoff_until": 0.0,
    "tokens": {},
}
# per-mint cache stores {mint: {"fetched_at": float, "meta": dict}}
_mint_cache: dict[str, dict[str, Any]] = {}


def clear_jupiter_token_cache() -> None:
    """Clear in-memory Jupiter Token v2 caches (useful for tests)."""
    _discovery_cache["fetched_at"] = None
    _discovery_cache["backoff_until"] = 0.0
    _discovery_cache["tokens"] = {}
    _mint_cache.clear()


def _build_headers(config: AgentConfig) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json"}
    api_key = config.JUPITER_API_KEY.strip()
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def parse_jupiter_token_v2(item: dict[str, Any]) -> Optional[tuple[str, dict[str, Any]]]:
    """Parse a single Jupiter Token API v2 object into (mint, normalized_meta)."""
    if not isinstance(item, dict):
        return None
    mint = str(item.get("id") or item.get("address") or "").strip()
    if not mint:
        return None

    audit = item.get("audit") if isinstance(item.get("audit"), dict) else {}
    top_holders_raw = audit.get("topHoldersPercentage")
    top_holders_pct = float(top_holders_raw) if top_holders_raw is not None else None

    mcap_raw = item.get("mcap")
    if mcap_raw is None:
        mcap_raw = item.get("fdv")
    mcap = float(mcap_raw) if mcap_raw is not None else 0.0

    meta = {
        "mint": mint,
        "symbol": str(item.get("symbol") or ""),
        "name": str(item.get("name") or ""),
        "mcap": mcap,
        "organic_score": round(float(item.get("organicScore") or 0.0), 2),
        "is_verified": bool(item.get("isVerified", False)),
        "holder_count": int(item.get("holderCount") or 0),
        "is_sus": bool(audit.get("isSus", False)),
        "mint_authority_disabled": bool(audit.get("mintAuthorityDisabled", False)),
        "freeze_authority_disabled": bool(audit.get("freezeAuthorityDisabled", False)),
        "top_holders_pct": round(top_holders_pct, 2) if top_holders_pct is not None else None,
    }
    return mint, meta


def _extract_token_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("tokens", "data", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


async def fetch_discovery_mints(
    config: AgentConfig,
    client: httpx.AsyncClient,
    *,
    now_ts: Optional[float] = None,
) -> dict[str, dict[str, Any]]:
    """Fetch /toporganicscore/5m and /toptrending/5m (60s cached, rate-limit safe).

    Filters out known base/major mints and blue-chips above our max market cap.
    """
    current_ts = now_ts if now_ts is not None else time.time()
    if current_ts < float(_discovery_cache.get("backoff_until") or 0.0):
        return dict(_discovery_cache["tokens"])

    fetched_at = _discovery_cache.get("fetched_at")
    if fetched_at is not None and (current_ts - float(fetched_at)) < config.JUPITER_POLL_CACHE_SECONDS:
        return dict(_discovery_cache["tokens"])

    headers = _build_headers(config)
    endpoints = [
        f"{config.JUPITER_TOKENS_V2_BASE_URL}/toporganicscore/5m",
        f"{config.JUPITER_TOKENS_V2_BASE_URL}/toptrending/5m",
    ]
    max_mcap_cap = min(50_000_000.0, float(config.MAX_MC_USD))

    discovered: dict[str, dict[str, Any]] = {}
    any_success = False

    for idx, url in enumerate(endpoints):
        if idx > 0 and config.JUPITER_REQUEST_DELAY_SECONDS > 0:
            await asyncio.sleep(config.JUPITER_REQUEST_DELAY_SECONDS)
        try:
            resp = await client.get(url, headers=headers, timeout=10.0)
            if resp.status_code == 429:
                logger.warning(
                    "[JupiterV2] Rate-limited (429) on %s; backing off %ds.",
                    url,
                    int(config.JUPITER_POLL_CACHE_SECONDS),
                )
                _discovery_cache["backoff_until"] = current_ts + config.JUPITER_POLL_CACHE_SECONDS
                return {} if not discovered else discovered
            resp.raise_for_status()
            any_success = True
            for raw in _extract_token_list(resp.json()):
                parsed = parse_jupiter_token_v2(raw)
                if parsed is None:
                    continue
                mint, meta = parsed
                # Cache raw metadata for enrichment lookups even before discovery filtering
                _mint_cache[mint] = {"fetched_at": current_ts, "meta": meta}

                if mint in IGNORED_MAJOR_MINTS:
                    continue
                if meta["mcap"] > 0 and meta["mcap"] > max_mcap_cap:
                    continue
                if meta["organic_score"] < config.JUPITER_MIN_ORGANIC_SCORE:
                    continue
                discovered[mint] = meta
        except Exception as exc:
            logger.warning("[JupiterV2] Discovery fetch failed (%s): %s", url, exc)

    if any_success:
        _discovery_cache["fetched_at"] = current_ts
        _discovery_cache["backoff_until"] = 0.0
        _discovery_cache["tokens"] = discovered
        logger.info(
            "[JupiterV2] Discovery fetched: %d eligible mints (60s cache)",
            len(discovered),
        )
        return dict(discovered)

    return {}


async def enrich_mints(
    mints: list[str],
    config: AgentConfig,
    client: httpx.AsyncClient,
    *,
    now_ts: Optional[float] = None,
) -> dict[str, dict[str, Any]]:
    """Batch-enrich mints via GET /tokens/v2/search?query=<csv> (up to 100 per call, 60s cached)."""
    current_ts = now_ts if now_ts is not None else time.time()
    result: dict[str, dict[str, Any]] = {}
    uncached: list[str] = []
    seen_req: set[str] = set()

    for m in mints:
        addr = m.strip()
        if not addr or addr in seen_req:
            continue
        seen_req.add(addr)
        cached = _mint_cache.get(addr)
        if cached is not None and (current_ts - float(cached["fetched_at"])) < config.JUPITER_POLL_CACHE_SECONDS:
            result[addr] = dict(cached["meta"])
        else:
            uncached.append(addr)

    if not uncached:
        return result

    headers = _build_headers(config)
    url = f"{config.JUPITER_TOKENS_V2_BASE_URL}/search"

    for idx in range(0, len(uncached), 100):
        chunk = uncached[idx : idx + 100]
        if idx > 0 and config.JUPITER_REQUEST_DELAY_SECONDS > 0:
            await asyncio.sleep(config.JUPITER_REQUEST_DELAY_SECONDS)
        try:
            resp = await client.get(
                url,
                params={"query": ",".join(chunk)},
                headers=headers,
                timeout=10.0,
            )
            if resp.status_code == 429:
                logger.warning("[JupiterV2] Rate-limited (429) on /search; returning partial enrichment.")
                return result
            resp.raise_for_status()
            for raw in _extract_token_list(resp.json()):
                parsed = parse_jupiter_token_v2(raw)
                if parsed is None:
                    continue
                mint, meta = parsed
                _mint_cache[mint] = {"fetched_at": current_ts, "meta": meta}
                if mint in seen_req:
                    result[mint] = meta
        except Exception as exc:
            logger.warning("[JupiterV2] Batch /search enrichment failed: %s", exc)
            return result

    return result
