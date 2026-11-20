"""Scanner Node: Polls DexScreener feeds and maintains the 2-slot rotating watchlist."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from config import AgentConfig
from db import AgentDatabase, utc_now
from leg2_predictor import compute_leg2_score
from state import AgentGraphState, CandidateToken, FocusState

logger = logging.getLogger(__name__)

# In-memory 60s rate-limit cache for GeckoTerminal trending pools
_geckoterminal_cache: dict[str, Any] = {
    "fetched_at": None,
    "mints": [],
}


def clear_geckoterminal_cache() -> None:
    """Reset the in-memory GeckoTerminal cache (useful for tests)."""
    _geckoterminal_cache["fetched_at"] = None
    _geckoterminal_cache["mints"] = []


def parse_geckoterminal_trending_mints(payload: dict[str, Any]) -> list[str]:
    """Extract deduplicated Solana base token mint addresses from GeckoTerminal JSON:API."""
    mints: list[str] = []
    seen: set[str] = set()

    def _add(addr: str) -> None:
        cleaned = addr.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            mints.append(cleaned)

    # Map included token objects by id (e.g. "solana_<mint>" -> attributes.address)
    included_addr_by_id: dict[str, str] = {}
    for item in payload.get("included") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "token":
            tid = str(item.get("id") or "")
            addr = str((item.get("attributes") or {}).get("address") or "").strip()
            if tid and addr:
                included_addr_by_id[tid] = addr

    # Walk pools in trending order so highest-trending pools come first
    for pool in payload.get("data") or []:
        if not isinstance(pool, dict):
            continue
        rel = (pool.get("relationships") or {}).get("base_token") or {}
        base_id = str((rel.get("data") or {}).get("id") or "").strip()
        if not base_id:
            continue
        if base_id in included_addr_by_id:
            _add(included_addr_by_id[base_id])
        elif base_id.startswith("solana_"):
            _add(base_id[len("solana_") :])

    # Fallback: any remaining included tokens if data lacked relationships
    if not mints:
        for tid, addr in included_addr_by_id.items():
            if tid.startswith("solana_"):
                _add(addr)

    return mints


async def fetch_geckoterminal_trending_solana(
    config: AgentConfig,
    client: httpx.AsyncClient,
    now_ts: Optional[float] = None,
) -> list[str]:
    """Fetch GeckoTerminal Solana trending pools with a 60s in-memory rate-limit cache."""
    current_ts = now_ts if now_ts is not None else time.time()
    cached_at = _geckoterminal_cache.get("fetched_at")
    if cached_at is not None and (current_ts - float(cached_at)) < config.GECKOTERMINAL_CACHE_SECONDS:
        return list(_geckoterminal_cache["mints"])

    url = f"{config.GECKOTERMINAL_BASE_URL}/networks/solana/trending_pools"
    try:
        resp = await client.get(
            url,
            params={"include": "base_token"},
            headers={"Accept": "application/json"},
            timeout=10.0,
        )
        if resp.status_code == 429:
            logger.warning(
                "GeckoTerminal rate-limited (429); backing off %ds and using cached mints (%d).",
                int(config.GECKOTERMINAL_CACHE_SECONDS),
                len(_geckoterminal_cache["mints"]),
            )
            _geckoterminal_cache["fetched_at"] = current_ts
            return list(_geckoterminal_cache["mints"])
        resp.raise_for_status()
        mints = parse_geckoterminal_trending_mints(resp.json())
        _geckoterminal_cache["fetched_at"] = current_ts
        _geckoterminal_cache["mints"] = mints
        logger.info("[Scanner] GeckoTerminal trending pools fetched: %d Solana mints (60s cache)", len(mints))
        return list(mints)
    except Exception as exc:
        logger.warning(
            "GeckoTerminal trending fetch failed (%s); falling back to cached mints (%d).",
            exc,
            len(_geckoterminal_cache["mints"]),
        )
        return list(_geckoterminal_cache["mints"])


def _parse_pair_to_candidate(
    pair: dict[str, Any],
    boost_map: dict[str, float],
    now_ms: int,
) -> Optional[CandidateToken]:
    """Convert a DexScreener pair payload into a CandidateToken."""
    if str(pair.get("chainId", "")).lower() != "solana":
        return None
    base = pair.get("baseToken") or {}
    mint = str(base.get("address") or "").strip()
    symbol = str(base.get("symbol") or "UNKNOWN").strip()
    name = str(base.get("name") or symbol).strip()
    if not mint:
        return None

    liquidity_usd = float((pair.get("liquidity") or {}).get("usd") or 0.0)
    volume = pair.get("volume") or {}
    volume_1h = float(volume.get("h1") or 0.0)
    volume_5m = float(volume.get("m5") or 0.0)

    mc = float(pair.get("marketCap") or pair.get("fdv") or 0.0)
    price_usd = float(pair.get("priceUsd") or 0.0)

    txns = pair.get("txns") or {}
    m5 = txns.get("m5") or {}
    h1 = txns.get("h1") or {}
    buys_5m = int(m5.get("buys") or 0)
    sells_5m = int(m5.get("sells") or 0)
    buys_1h = int(h1.get("buys") or 0)
    sells_1h = int(h1.get("sells") or 0)

    price_change = pair.get("priceChange") or {}
    pc_5m = float(price_change.get("m5") or 0.0)
    pc_1h = float(price_change.get("h1") or 0.0)

    created_ms = pair.get("pairCreatedAt")
    age_seconds = max(0.0, (now_ms - int(created_ms)) / 1000.0) if created_ms else 0.0

    info = pair.get("info") or {}
    socials = [
        str(s.get("type") or s.get("url") or "")
        for s in (info.get("socials") or [])
        if isinstance(s, dict)
    ]

    return CandidateToken(
        mint=mint,
        symbol=symbol,
        name=name,
        pair_address=str(pair.get("pairAddress") or ""),
        dex_id=str(pair.get("dexId") or ""),
        price_usd=price_usd,
        market_cap_usd=mc,
        liquidity_usd=liquidity_usd,
        volume_1h_usd=volume_1h,
        volume_5m_usd=volume_5m,
        buys_5m=buys_5m,
        sells_5m=sells_5m,
        buys_1h=buys_1h,
        sells_1h=sells_1h,
        price_change_5m_pct=pc_5m,
        price_change_1h_pct=pc_1h,
        pair_created_at_ms=int(created_ms) if created_ms else None,
        age_seconds=age_seconds,
        boost_amount=float(boost_map.get(mint, 0.0)),
        socials=socials,
    )


def filter_candidates(
    candidates: list[CandidateToken],
    config: AgentConfig,
    db: AgentDatabase,
    now_dt: Optional[datetime] = None,
) -> list[CandidateToken]:
    """Apply hunting lane, liquidity, 1h volume, and cooldown filters."""
    min_mc, max_mc, min_age_sec = config.lane_bounds()
    best_by_mint: dict[str, CandidateToken] = {}

    for c in candidates:
        if db.is_on_cooldown(c.mint, now=now_dt):
            continue
        if not (min_mc <= c.market_cap_usd <= max_mc):
            continue
        if c.age_seconds < min_age_sec:
            continue
        if c.liquidity_usd < config.MIN_LIQUIDITY_USD:
            continue
        if c.volume_1h_usd < config.MIN_VOL_1H_USD:
            continue

        # Keep the highest-liquidity pool per mint
        existing = best_by_mint.get(c.mint)
        if existing is None or c.liquidity_usd > existing.liquidity_usd:
            best_by_mint[c.mint] = c

    ranked = sorted(
        best_by_mint.values(),
        key=lambda x: (x.volume_1h_usd, x.boost_amount),
        reverse=True,
    )
    return ranked


async def fetch_dexscreener_trending_solana(
    config: AgentConfig,
    client: httpx.AsyncClient,
    extra_mints: Optional[list[str]] = None,
    now_ts: Optional[float] = None,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Merge GeckoTerminal trending + DexScreener boosts/profiles/CTOs and batch-fetch pairs."""
    boost_map: dict[str, float] = {}
    mints: list[str] = []
    seen: set[str] = set()

    def _add_mint(addr: str) -> None:
        cleaned = addr.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            mints.append(cleaned)

    for m in extra_mints or []:
        _add_mint(m)

    # 1. Organic trending discovery from GeckoTerminal (60s cached)
    gecko_mints = await fetch_geckoterminal_trending_solana(config, client, now_ts=now_ts)
    for m in gecko_mints:
        _add_mint(m)

    # 2. DexScreener boosts, profiles, and community takeovers (CTOs)
    endpoints = [
        f"{config.DEXSCREENER_BASE_URL}/token-boosts/latest/v1",
        f"{config.DEXSCREENER_BASE_URL}/token-boosts/top/v1",
        f"{config.DEXSCREENER_BASE_URL}/token-profiles/latest/v1",
        f"{config.DEXSCREENER_BASE_URL}/community-takeovers/latest/v1",
    ]
    for url in endpoints:
        try:
            resp = await client.get(url, timeout=10.0)
            resp.raise_for_status()
            items = resp.json()
            if not isinstance(items, list):
                continue
            for item in items:
                if str(item.get("chainId", "")).lower() != "solana":
                    continue
                addr = str(item.get("tokenAddress") or "").strip()
                if addr:
                    boost_map[addr] = max(
                        boost_map.get(addr, 0.0),
                        float(item.get("totalAmount") or item.get("amount") or 0.0),
                    )
                    _add_mint(addr)
        except Exception as exc:
            logger.warning("DexScreener discovery fetch failed (%s): %s", url, exc)

    pairs: list[dict[str, Any]] = []
    # DexScreener supports up to 30 comma-separated mints per batch call
    for idx in range(0, len(mints), 30):
        chunk = mints[idx : idx + 30]
        batch_url = f"{config.DEXSCREENER_BASE_URL}/tokens/v1/solana/{','.join(chunk)}"
        try:
            resp = await client.get(batch_url, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                pairs.extend(data)
            elif isinstance(data, dict) and isinstance(data.get("pairs"), list):
                pairs.extend(data["pairs"])
        except Exception as exc:
            logger.warning("DexScreener batch pair fetch failed: %s", exc)

    return pairs, boost_map


def _evict_stale_or_cooled_watchlist(
    *,
    config: AgentConfig,
    db: AgentDatabase,
    now_dt: datetime,
) -> list[dict[str, Any]]:
    """Evict stale (>15m) or cooled-down entries from active_watchlist; return survivors."""
    survivors: list[dict[str, Any]] = []
    for entry in db.get_watchlist():
        mint = entry["mint"]
        symbol = entry["symbol"]
        if db.is_on_cooldown(mint, now=now_dt):
            db.remove_from_watchlist(mint)
            logger.info("[Scanner] $%s (%s..) -> EVICTED (on cooldown)", symbol, mint[:6])
            continue

        added_dt = datetime.fromisoformat(entry["added_at"])
        if added_dt.tzinfo is None:
            added_dt = added_dt.replace(tzinfo=timezone.utc)
        elapsed_min = (now_dt - added_dt).total_seconds() / 60.0

        if elapsed_min > config.STALE_WATCH_TIMEOUT_MINUTES:
            db.remove_from_watchlist(mint)
            db.set_cooldown_minutes(
                mint,
                minutes=config.STALE_WATCH_COOLDOWN_MINUTES,
                reason=f"STALE_WATCH ({elapsed_min:.1f}m > {config.STALE_WATCH_TIMEOUT_MINUTES}m)",
                now=now_dt,
            )
            logger.info(
                "[Scanner] $%s | in_slot=%.1fm > %dm -> EVICTED (stale watch, %dm cooldown)",
                symbol,
                elapsed_min,
                config.STALE_WATCH_TIMEOUT_MINUTES,
                config.STALE_WATCH_COOLDOWN_MINUTES,
            )
            continue

        survivors.append(entry)
    return survivors


async def scanner_node(
    state: AgentGraphState,
    *,
    config: AgentConfig,
    db: AgentDatabase,
    http_client: Optional[httpx.AsyncClient] = None,
    now_ms: Optional[int] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """LangGraph node: runs ONLY in SCANNING state, maintaining a 2-slot rotating watchlist."""
    if state.get("focus_state") != FocusState.SCANNING.value:
        return {"raw_candidates": [], "active_watchlist": db.get_watchlist()}

    # Daily max loss circuit breaker
    daily_start = float(state.get("daily_start_sol") or config.PAPER_STARTING_SOL)
    daily_pnl = float(state.get("daily_pnl_sol") or 0.0)
    if daily_start > 0 and (-daily_pnl / daily_start) >= config.DAILY_MAX_LOSS_PCT:
        logger.warning(
            "Daily max loss limit reached (%.2f%%). Halting scans.",
            (-daily_pnl / daily_start) * 100.0,
        )
        return {
            "raw_candidates": [],
            "active_watchlist": db.get_watchlist(),
            "halted_daily_loss": True,
        }

    now_dt = now or utc_now()
    current_ms = now_ms if now_ms is not None else int(now_dt.timestamp() * 1000)

    # 1. Evict stale (>15m) or cooled-down watchlist entries
    kept_entries = _evict_stale_or_cooled_watchlist(config=config, db=db, now_dt=now_dt)
    kept_mints = [e["mint"] for e in kept_entries]

    # 2. Fetch widened DexScreener discovery feeds + refresh kept watchlist mints
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient()
    try:
        pairs, boost_map = await fetch_dexscreener_trending_solana(
            config, client, extra_mints=kept_mints, now_ts=now_dt.timestamp()
        )
    finally:
        if owns_client:
            await client.aclose()

    parsed = [
        c
        for p in pairs
        if (c := _parse_pair_to_candidate(p, boost_map, current_ms)) is not None
    ]
    eligible = filter_candidates(parsed, config, db, now_dt=now_dt)
    eligible_by_mint = {c.mint: c for c in eligible}

    # 3. Keep existing valid watchlist mints (updating live metrics, peak_mc/low_mc, leg2_score)
    active_mints: set[str] = set()
    for entry in kept_entries:
        mint = entry["mint"]
        if mint in eligible_by_mint:
            fresh = eligible_by_mint[mint]
            prev_peak = float(entry.get("peak_mc") or 0.0)
            prev_low = float(entry.get("low_mc") or 0.0)
            peak_mc = max(prev_peak, fresh.market_cap_usd)
            low_mc = (
                min(prev_low, fresh.market_cap_usd)
                if prev_low > 0 and fresh.market_cap_usd > 0
                else (fresh.market_cap_usd or prev_low)
            )
            entry_liq = float(entry.get("entry_liquidity_usd") or fresh.liquidity_usd)
            leg2 = compute_leg2_score(
                fresh.model_dump(),
                tracked_peak_mc=peak_mc,
                tracked_low_mc=low_mc,
                entry_liquidity_usd=entry_liq,
            )
            db.upsert_watchlist_entry(
                mint=mint,
                symbol=fresh.symbol,
                pair_address=fresh.pair_address,
                candidate=fresh.model_dump(),
                added_at=entry["added_at"],
                entry_liquidity_usd=entry_liq,
                peak_mc=peak_mc,
                low_mc=low_mc,
                leg2_score=leg2.score,
                leg2_breakdown=leg2.model_dump(),
            )
            active_mints.add(mint)
        else:
            # No longer meets lane/liquidity/volume criteria -> free slot
            db.remove_from_watchlist(mint)
            logger.info(
                "[Scanner] $%s | no longer meets lane/liq/vol filters -> EVICTED (slot freed)",
                entry["symbol"],
            )

    # 4. Backfill any open slots (len < MAX_WATCH_BATCH) with highest-volume eligible candidates
    for cand in eligible:
        if len(active_mints) >= config.MAX_WATCH_BATCH:
            break
        if cand.mint in active_mints:
            continue
        leg2 = compute_leg2_score(
            cand.model_dump(),
            tracked_peak_mc=cand.market_cap_usd,
            tracked_low_mc=cand.market_cap_usd,
            entry_liquidity_usd=cand.liquidity_usd,
        )
        db.upsert_watchlist_entry(
            mint=cand.mint,
            symbol=cand.symbol,
            pair_address=cand.pair_address,
            candidate=cand.model_dump(),
            added_at=now_dt.isoformat(),
            safety_passed=False,
            entry_liquidity_usd=cand.liquidity_usd,
            peak_mc=cand.market_cap_usd,
            low_mc=cand.market_cap_usd,
            leg2_score=leg2.score,
            leg2_breakdown=leg2.model_dump(),
        )
        active_mints.add(cand.mint)
        logger.info(
            "[Scanner] +SLOT $%s (vol_1h=$%s, liq=$%s, mc=$%s, leg2=%.0f/100) -> added to watchlist (%d/%d)",
            cand.symbol,
            f"{cand.volume_1h_usd:,.0f}",
            f"{cand.liquidity_usd:,.0f}",
            f"{cand.market_cap_usd:,.0f}",
            leg2.score,
            len(active_mints),
            config.MAX_WATCH_BATCH,
        )

    final_watchlist = db.get_watchlist()[: config.MAX_WATCH_BATCH]
    batch_candidates: list[dict[str, Any]] = []
    for entry in final_watchlist:
        cand_dict = dict(entry.get("candidate") or {})
        cand_dict["pair_address"] = entry.get("pair_address") or cand_dict.get("pair_address") or ""
        cand_dict["added_at"] = entry["added_at"]
        cand_dict["safety_passed"] = bool(entry.get("safety_passed", False))
        cand_dict["entry_liquidity_usd"] = float(entry.get("entry_liquidity_usd") or 0.0)
        cand_dict["peak_mc"] = float(entry.get("peak_mc") or 0.0)
        cand_dict["low_mc"] = float(entry.get("low_mc") or 0.0)
        cand_dict["leg2_score"] = float(entry.get("leg2_score") or 0.0)
        cand_dict["leg2_breakdown"] = entry.get("leg2_breakdown")
        batch_candidates.append(cand_dict)

    logger.info(
        "Scanner [%s]: %d eligible -> %d/%d active watchlist slots (%s)",
        config.HUNTING_LANE,
        len(eligible),
        len(batch_candidates),
        config.MAX_WATCH_BATCH,
        ", ".join(f"${c.get('symbol')}" for c in batch_candidates) or "empty",
    )
    return {
        "raw_candidates": batch_candidates,
        "active_watchlist": final_watchlist,
        "halted_daily_loss": False,
    }
