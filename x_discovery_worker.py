"""Budgeted X (TwitterAPI.io) Discovery & Sentiment background worker."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from config import AgentConfig
from db import AgentDatabase, utc_now

logger = logging.getLogger(__name__)

# Known Solana base/stable/program mints to ignore when extracting memecoin CAs
IGNORED_MINTS: set[str] = {
    "So11111111111111111111111111111111111111112",  # Wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "11111111111111111111111111111111",  # System Program
}

_PUMP_URL_RE = re.compile(
    r"pump\.fun/(?:coin/)?([1-9A-HJ-NP-Za-km-z]{32,44})",
    re.IGNORECASE,
)
_DEX_URL_RE = re.compile(
    r"dexscreener\.com/solana/([1-9A-HJ-NP-Za-km-z]{32,44})",
    re.IGNORECASE,
)
_STANDALONE_B58_RE = re.compile(
    r"(?<![1-9A-HJ-NP-Za-km-z/])([1-9A-HJ-NP-Za-km-z]{32,44})(?![1-9A-HJ-NP-Za-km-z])"
)


def extract_solana_mints(text: str) -> list[str]:
    """Extract deduplicated Solana base58 mint addresses from links and plain text."""
    if not text:
        return []

    found: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        c = candidate.strip()
        if not c or c in IGNORED_MINTS or c in seen:
            return
        # Guard against long plain-English words: real Solana CAs contain digits
        if not any(ch.isdigit() for ch in c):
            return
        seen.add(c)
        found.append(c)

    for pattern in (_PUMP_URL_RE, _DEX_URL_RE, _STANDALONE_B58_RE):
        for match in pattern.finditer(text):
            _add(match.group(1))

    return found


def _extract_tweets_list(payload: Any) -> list[dict[str, Any]]:
    """Handle TwitterAPI.io response shapes ({tweets: [...]} or {data: {tweets: [...]}})."""
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("tweets"), list):
        return payload["tweets"]
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("tweets"), list):
        return data["tweets"]
    if isinstance(data, list):
        return data
    return []


def _parse_tweet(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Normalize TwitterAPI.io camelCase tweet payload."""
    tweet_id = str(raw.get("id") or "").strip()
    text = str(raw.get("text") or "").strip()
    if not tweet_id or not text:
        return None
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    return {
        "id": tweet_id,
        "text": text,
        "created_at": str(raw.get("createdAt") or ""),
        "like_count": int(raw.get("likeCount") or 0),
        "retweet_count": int(raw.get("retweetCount") or 0),
        "author_username": str(author.get("userName") or "unknown"),
        "author_followers": int(author.get("followers") or 0),
        "is_blue_verified": bool(author.get("isBlueVerified", False)),
    }


def _format_since_utc(dt: datetime) -> str:
    utc_dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return utc_dt.strftime("%Y-%m-%d_%H:%M:%S_UTC")


async def poll_x_once(
    config: AgentConfig,
    db: AgentDatabase,
    *,
    http_client: Optional[httpx.AsyncClient] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Execute one budgeted poll cycle against TwitterAPI.io (max 1 page = 20 tweets per query)."""
    now_dt = now or utc_now()
    now_iso = now_dt.isoformat()

    if not config.TWITTERAPI_IO_KEY.strip():
        logger.warning("[XDiscovery] TWITTERAPI_IO_KEY is empty; skipping poll.")
        return {"status": "missing_api_key", "new_tweets": 0, "mints_found": 0}

    tweets_today = db.count_x_tweets_today(now=now_dt)
    if tweets_today >= config.X_DAILY_TWEET_CAP:
        logger.info(
            "[XDiscovery] Daily tweet cap reached (%d/%d); pausing until UTC midnight.",
            tweets_today,
            config.X_DAILY_TWEET_CAP,
        )
        return {
            "status": "daily_cap_reached",
            "tweets_today": tweets_today,
            "new_tweets": 0,
            "mints_found": 0,
        }

    last_poll_str = db.get_x_last_poll_utc()
    last_poll_dt: Optional[datetime] = None
    if last_poll_str:
        try:
            last_poll_dt = datetime.fromisoformat(last_poll_str)
            if last_poll_dt.tzinfo is None:
                last_poll_dt = last_poll_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            last_poll_dt = None

    headers = {"X-API-Key": config.TWITTERAPI_IO_KEY.strip()}
    list_ids = config.x_list_id_list()
    search_query = config.X_SEARCH_QUERY.strip()

    raw_tweets: list[dict[str, Any]] = []
    errors: list[str] = []

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(follow_redirects=False)
    try:
        # 1. List timeline polls (if X_LIST_IDS configured)
        for list_id in list_ids:
            if db.count_x_tweets_today(now=now_dt) + len(raw_tweets) >= config.X_DAILY_TWEET_CAP:
                break
            params: dict[str, Any] = {
                "listId": list_id,
                "includeReplies": "false",
            }
            if last_poll_dt is not None:
                params["sinceTime"] = str(int(last_poll_dt.timestamp()))
            url = f"{config.TWITTERAPI_IO_BASE_URL}/twitter/list/tweets"
            try:
                resp = await client.get(url, params=params, headers=headers, timeout=15.0)
                if resp.status_code in (401, 402, 429):
                    msg = f"HTTP {resp.status_code} on list {list_id}"
                    logger.warning("[XDiscovery] %s; skipping gracefully.", msg)
                    errors.append(msg)
                    continue
                resp.raise_for_status()
                page = _extract_tweets_list(resp.json())[:20]
                raw_tweets.extend(page)
            except Exception as exc:
                msg = f"list {list_id} error: {exc}"
                logger.warning("[XDiscovery] %s", msg)
                errors.append(msg)

        # 2. Advanced search poll (when no lists configured, or in addition when X_SEARCH_QUERY is set)
        if search_query and (
            db.count_x_tweets_today(now=now_dt) + len(raw_tweets) < config.X_DAILY_TWEET_CAP
        ):
            full_query = search_query
            if last_poll_dt is not None and "since:" not in full_query:
                full_query = f"{full_query} since:{_format_since_utc(last_poll_dt)}"
            params = {
                "query": full_query,
                "queryType": "Latest",
            }
            url = f"{config.TWITTERAPI_IO_BASE_URL}/twitter/tweet/advanced_search"
            try:
                resp = await client.get(url, params=params, headers=headers, timeout=15.0)
                if resp.status_code in (401, 402, 429):
                    msg = f"HTTP {resp.status_code} on advanced_search"
                    logger.warning("[XDiscovery] %s; skipping gracefully.", msg)
                    errors.append(msg)
                else:
                    resp.raise_for_status()
                    page = _extract_tweets_list(resp.json())[:20]
                    raw_tweets.extend(page)
            except Exception as exc:
                msg = f"advanced_search error: {exc}"
                logger.warning("[XDiscovery] %s", msg)
                errors.append(msg)
    finally:
        if owns_client:
            await client.aclose()

    new_tweets = 0
    mints_seen: set[str] = set()
    remaining_cap = max(0, config.X_DAILY_TWEET_CAP - tweets_today)

    for raw in raw_tweets:
        if new_tweets >= remaining_cap:
            break
        parsed = _parse_tweet(raw)
        if parsed is None:
            continue
        mints = extract_solana_mints(parsed["text"])
        inserted = db.record_x_tweet(
            tweet_id=parsed["id"],
            fetched_at_utc=now_iso,
            author_username=parsed["author_username"],
            author_followers=parsed["author_followers"],
            like_count=parsed["like_count"],
            text=parsed["text"],
            extracted_mints=mints,
        )
        if not inserted:
            continue
        new_tweets += 1
        for mint in mints:
            mints_seen.add(mint)
            db.upsert_social_signal(
                mint=mint,
                author_username=parsed["author_username"],
                author_followers=parsed["author_followers"],
                like_count=parsed["like_count"],
                retweet_count=parsed["retweet_count"],
                is_blue_verified=parsed["is_blue_verified"],
                text=parsed["text"],
                seen_at_utc=now_iso,
            )

    db.set_x_last_poll_utc(now_iso)
    logger.info(
        "[XDiscovery] Poll complete | new_tweets=%d | mints_found=%d | today=%d/%d | errors=%d",
        new_tweets,
        len(mints_seen),
        tweets_today + new_tweets,
        config.X_DAILY_TWEET_CAP,
        len(errors),
    )
    return {
        "status": "ok" if not errors else "partial_error",
        "new_tweets": new_tweets,
        "mints_found": len(mints_seen),
        "mints": sorted(mints_seen),
        "tweets_today": tweets_today + new_tweets,
        "errors": errors,
    }


async def run_x_discovery_loop(config: AgentConfig, db: AgentDatabase) -> None:
    """Long-running background asyncio task polling TwitterAPI.io on X_POLL_INTERVAL_MINUTES."""
    logger.info(
        "[XDiscovery] Background worker started | interval=%dm | daily_cap=%d | ttl=%dm",
        config.X_POLL_INTERVAL_MINUTES,
        config.X_DAILY_TWEET_CAP,
        config.X_SIGNAL_TTL_MINUTES,
    )
    while True:
        try:
            await poll_x_once(config, db)
        except Exception as exc:
            logger.exception("[XDiscovery] Unexpected worker error: %s", exc)
        await asyncio.sleep(config.X_POLL_INTERVAL_MINUTES * 60)
