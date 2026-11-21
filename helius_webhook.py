"""Helius Enhanced Webhook parser and ingestion handler for whale tracking."""

from __future__ import annotations

import hmac
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase, utc_now

logger = logging.getLogger(__name__)

# In-memory cache for dynamically synced Helius webhook accountAddresses
_helius_roster_cache: dict[str, Any] = {
    "fetched_at": None,
    "wallets": [],
}


def clear_helius_roster_cache() -> None:
    """Clear the in-memory Helius tracked wallet roster cache (useful for tests)."""
    _helius_roster_cache["fetched_at"] = None
    _helius_roster_cache["wallets"] = []


def _extract_account_addresses(webhook_obj: dict[str, Any]) -> list[str]:
    raw = webhook_obj.get("accountAddresses") or []
    result: list[str] = []
    seen: set[str] = set()
    for addr in raw:
        cleaned = str(addr or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


async def fetch_helius_tracked_wallets(
    config: AgentConfig,
    *,
    force_refresh: bool = False,
    http_client: Optional[httpx.AsyncClient] = None,
    now_ts: Optional[float] = None,
) -> list[str]:
    """Dynamically fetch accountAddresses from Helius Webhooks API with 5-minute cache."""
    current_ts = now_ts if now_ts is not None else time.time()
    cached_at = _helius_roster_cache.get("fetched_at")
    if (
        not force_refresh
        and cached_at is not None
        and (current_ts - float(cached_at)) < config.HELIUS_ROSTER_CACHE_SECONDS
    ):
        return list(_helius_roster_cache["wallets"])

    api_key = config.HELIUS_API_KEY.strip()
    if not api_key:
        fallback = sorted(config.helius_tracked_wallet_set())
        return list(_helius_roster_cache["wallets"]) or fallback

    webhook_id = config.HELIUS_WEBHOOK_ID.strip()
    if webhook_id:
        url = f"{config.HELIUS_API_BASE_URL}/webhooks/{webhook_id}"
    else:
        url = f"{config.HELIUS_API_BASE_URL}/webhooks"

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient()
    try:
        resp = await client.get(url, params={"api-key": api_key}, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()

        selected: Optional[dict[str, Any]] = None
        if isinstance(data, dict):
            selected = data
        elif isinstance(data, list) and data:
            for hook in data:
                if isinstance(hook, dict) and "solana-api.projectkami.net" in str(
                    hook.get("webhookURL") or ""
                ).lower():
                    selected = hook
                    break
            if selected is None and isinstance(data[0], dict):
                selected = data[0]

        wallets = _extract_account_addresses(selected) if selected else []
        _helius_roster_cache["fetched_at"] = current_ts
        _helius_roster_cache["wallets"] = wallets
        logger.info(
            "[WhaleRoster] Synced %d tracked wallets from Helius Webhooks API",
            len(wallets),
        )
        return list(wallets)
    except Exception as exc:
        logger.warning(
            "[WhaleRoster] Helius webhook roster sync failed (%s); using cached/fallback wallets.",
            exc,
        )
        if _helius_roster_cache["wallets"]:
            return list(_helius_roster_cache["wallets"])
        return sorted(config.helius_tracked_wallet_set())
    finally:
        if owns_client:
            await client.aclose()

IGNORED_BASE_MINTS: set[str] = {
    SOL_MINT,  # Wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "11111111111111111111111111111111",  # System Program
}


def verify_helius_auth(incoming_header: Optional[str], expected_header: str) -> bool:
    """Constant-time verification of the Helius webhook Authorization header."""
    expected = (expected_header or "").strip()
    incoming = (incoming_header or "").strip()
    if not expected or not incoming:
        return False
    return hmac.compare_digest(incoming.encode("utf-8"), expected.encode("utf-8"))


def _resolve_actor_wallet(tx: dict[str, Any], tracked_wallets: set[str]) -> Optional[str]:
    """Identify the tracked wallet involved in the tx (or feePayer if no filter list)."""
    fee_payer = str(tx.get("feePayer") or "").strip()
    candidates: list[str] = []
    if fee_payer:
        candidates.append(fee_payer)

    for tf in tx.get("tokenTransfers") or []:
        if not isinstance(tf, dict):
            continue
        for key in ("fromUserAccount", "toUserAccount"):
            acct = str(tf.get(key) or "").strip()
            if acct and acct not in candidates:
                candidates.append(acct)

    for nf in tx.get("nativeTransfers") or []:
        if not isinstance(nf, dict):
            continue
        for key in ("fromUserAccount", "toUserAccount"):
            acct = str(nf.get(key) or "").strip()
            if acct and acct not in candidates:
                candidates.append(acct)

    if tracked_wallets:
        for acct in candidates:
            if acct in tracked_wallets:
                return acct
        return None

    return candidates[0] if candidates else None


def _parse_tx_timestamp(raw_ts: Any, fallback_dt: datetime) -> str:
    if isinstance(raw_ts, (int, float)) and raw_ts > 0:
        return datetime.fromtimestamp(float(raw_ts), tz=timezone.utc).isoformat()
    if isinstance(raw_ts, str) and raw_ts.strip():
        try:
            dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            pass
    return fallback_dt.isoformat()


def _estimate_sol_flow(tx: dict[str, Any], wallet: str, side: str) -> float:
    """Estimate SOL spent (BUY) or received (SELL) from nativeTransfers and WSOL transfers."""
    native_out = 0.0
    native_in = 0.0
    for nf in tx.get("nativeTransfers") or []:
        if not isinstance(nf, dict):
            continue
        amt_sol = float(nf.get("amount") or 0.0) / LAMPORTS_PER_SOL
        if amt_sol <= 0:
            continue
        if nf.get("fromUserAccount") == wallet:
            native_out += amt_sol
        if nf.get("toUserAccount") == wallet:
            native_in += amt_sol

    wsol_out = 0.0
    wsol_in = 0.0
    for tf in tx.get("tokenTransfers") or []:
        if not isinstance(tf, dict):
            continue
        if tf.get("mint") != SOL_MINT:
            continue
        amt = float(tf.get("tokenAmount") or 0.0)
        if amt <= 0:
            continue
        if tf.get("fromUserAccount") == wallet:
            wsol_out += amt
        if tf.get("toUserAccount") == wallet:
            wsol_in += amt

    # Prefer WSOL if present and larger, otherwise native SOL (avoid double-counting wrap+swap)
    if side == "BUY":
        sol_out = max(native_out, wsol_out)
        sol_in = max(native_in, wsol_in)
        net = sol_out - sol_in
        return round(net if net > 0 else sol_out, 6)
    else:
        sol_in = max(native_in, wsol_in)
        sol_out = max(native_out, wsol_out)
        net = sol_in - sol_out
        return round(net if net > 0 else sol_in, 6)


def parse_helius_swap_event(
    tx: dict[str, Any],
    *,
    tracked_wallets: set[str],
    min_sol_spent: float,
    now_dt: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """Parse a single Helius Enhanced Webhook SWAP transaction into a whale event."""
    if not isinstance(tx, dict):
        return None
    if str(tx.get("type") or "").upper() != "SWAP":
        return None

    signature = str(tx.get("signature") or "").strip()
    if not signature:
        return None

    wallet = _resolve_actor_wallet(tx, tracked_wallets)
    if not wallet:
        return None

    # Aggregate net SPL token flows for non-base mints involving this wallet
    net_by_mint: dict[str, float] = {}
    for tf in tx.get("tokenTransfers") or []:
        if not isinstance(tf, dict):
            continue
        mint = str(tf.get("mint") or "").strip()
        if not mint or mint in IGNORED_BASE_MINTS:
            continue
        amt = float(tf.get("tokenAmount") or 0.0)
        if amt <= 0:
            continue
        if tf.get("toUserAccount") == wallet:
            net_by_mint[mint] = net_by_mint.get(mint, 0.0) + amt
        if tf.get("fromUserAccount") == wallet:
            net_by_mint[mint] = net_by_mint.get(mint, 0.0) - amt

    if not net_by_mint:
        return None

    # Pick the primary non-base SPL mint with the largest absolute token delta
    primary_mint, net_tokens = max(net_by_mint.items(), key=lambda kv: abs(kv[1]))
    if net_tokens == 0.0:
        return None

    side = "BUY" if net_tokens > 0 else "SELL"
    token_amount = abs(net_tokens)
    sol_amount = _estimate_sol_flow(tx, wallet, side)

    if side == "BUY" and sol_amount < min_sol_spent:
        return None

    ts_utc = _parse_tx_timestamp(tx.get("timestamp"), now_dt or utc_now())
    return {
        "signature": signature,
        "wallet": wallet,
        "mint": primary_mint,
        "side": side,
        "sol_amount": sol_amount,
        "token_amount": token_amount,
        "timestamp_utc": ts_utc,
    }


def process_helius_webhook_payload(
    payload: Any,
    *,
    config: AgentConfig,
    db: AgentDatabase,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Ingest a Helius Enhanced Webhook JSON array payload idempotently into DuckDB."""
    now_dt = now or utc_now()
    items = payload if isinstance(payload, list) else ([payload] if isinstance(payload, dict) else [])
    tracked_wallets = config.helius_tracked_wallet_set()

    recorded = 0
    duplicates = 0
    ignored = 0
    touched_mints: set[str] = set()

    for tx in items:
        ev = parse_helius_swap_event(
            tx,
            tracked_wallets=tracked_wallets,
            min_sol_spent=config.WHALE_MIN_SOL_SPENT,
            now_dt=now_dt,
        )
        if ev is None:
            ignored += 1
            continue

        inserted = db.record_whale_event(
            signature=ev["signature"],
            wallet=ev["wallet"],
            mint=ev["mint"],
            side=ev["side"],
            sol_amount=ev["sol_amount"],
            token_amount=ev["token_amount"],
            timestamp_utc=ev["timestamp_utc"],
        )
        if not inserted:
            duplicates += 1
            continue

        recorded += 1
        touched_mints.add(ev["mint"])
        if ev["side"] == "SELL":
            shadow_triggered = db.flag_whale_shadow_exit(ev["mint"], ev["wallet"])
            if shadow_triggered:
                logger.warning(
                    "[WhaleShadow] Triggering wallet %s.. SOLD %s.. -> flagging WHALE_SHADOW_EXIT!",
                    ev["wallet"][:6],
                    ev["mint"][:6],
                )
        logger.info(
            "[WhaleWebhook] %s | wallet=%s.. | mint=%s.. | %.3f SOL",
            ev["side"],
            ev["wallet"][:6],
            ev["mint"][:6],
            ev["sol_amount"],
        )

    for mint in touched_mints:
        db.recompute_whale_signal(
            mint,
            ttl_minutes=config.WHALE_SIGNAL_TTL_MINUTES,
            now=now_dt,
        )

    return {
        "status": "ok",
        "received": len(items),
        "recorded": recorded,
        "duplicates": duplicates,
        "ignored": ignored,
    }
