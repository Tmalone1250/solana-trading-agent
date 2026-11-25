"""Bounded 30-minute TTL cache for slow-moving Gemini narrative/vamp classifications ONLY.

Never caches or serves fast-changing `momentum_quality` or `action` decisions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_NARRATIVE_CACHE_TTL_SECONDS = 1800.0  # 30 minutes
NARRATIVE_CACHE_MAX_SIZE = 200

# Bounded in-memory cache: mint -> {mint, cached_at, cached_ts, narrative_strength, is_copycat_vamp}
_narrative_cache: dict[str, dict[str, Any]] = {}


def clear_narrative_cache() -> None:
    """Clear all cached Gemini narrative assessments (for tests)."""
    _narrative_cache.clear()


def get_narrative_cache_size() -> int:
    """Return current number of entries in the Gemini narrative cache."""
    return len(_narrative_cache)


def _prune_expired(now_ts: float, ttl_seconds: float) -> None:
    if ttl_seconds <= 0:
        _narrative_cache.clear()
        return
    for mint_key, entry in list(_narrative_cache.items()):
        if (now_ts - float(entry.get("cached_ts") or 0.0)) >= ttl_seconds:
            _narrative_cache.pop(mint_key, None)


def get_cached_narrative(
    mint: str,
    *,
    now_ts: Optional[float] = None,
    ttl_seconds: float = DEFAULT_NARRATIVE_CACHE_TTL_SECONDS,
) -> Optional[dict[str, Any]]:
    """Return cached slow-moving narrative/vamp classification for `mint`, or None on miss/expiry."""
    if not mint or ttl_seconds <= 0:
        return None
    ts = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
    _prune_expired(ts, ttl_seconds)
    entry = _narrative_cache.get(mint)
    if entry is None:
        return None
    if (ts - float(entry.get("cached_ts") or 0.0)) >= ttl_seconds:
        _narrative_cache.pop(mint, None)
        return None
    return entry


def put_cached_narrative(
    mint: str,
    *,
    narrative_strength: dict[str, Any],
    is_copycat_vamp: dict[str, Any],
    now_dt: Optional[datetime] = None,
    ttl_seconds: float = DEFAULT_NARRATIVE_CACHE_TTL_SECONDS,
    max_size: int = NARRATIVE_CACHE_MAX_SIZE,
    **_ignored_live_fields: Any,
) -> dict[str, Any]:
    """Store slow-moving Gemini narrative/vamp scores & timestamp (excludes momentum_quality & action)."""
    dt = now_dt or datetime.now(timezone.utc)
    ts = dt.timestamp()
    entry = {
        "mint": mint,
        "cached_at": dt.isoformat(),
        "cached_ts": ts,
        "narrative_strength": dict(narrative_strength),
        "is_copycat_vamp": dict(is_copycat_vamp),
    }
    if not mint or ttl_seconds <= 0 or max_size <= 0:
        return entry

    _prune_expired(ts, ttl_seconds)
    while len(_narrative_cache) >= max_size and mint not in _narrative_cache:
        oldest_mint = min(
            _narrative_cache.keys(),
            key=lambda k: float(_narrative_cache[k].get("cached_ts") or 0.0),
        )
        _narrative_cache.pop(oldest_mint, None)

    _narrative_cache[mint] = entry
    return entry
