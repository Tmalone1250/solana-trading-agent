"""On-demand Insider Wallet Scanner (insider_scanner.py) via DexScreener, GeckoTerminal & Helius."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

from config import AgentConfig
from helius_webhook import (
    IGNORED_BASE_MINTS,
    _estimate_sol_flow,
    fetch_helius_tracked_wallets,
)
from ohlcv_client import parse_geckoterminal_ohlcv

logger = logging.getLogger(__name__)

MAX_HELIUS_PAGES = 5
MAX_CANDIDATES_RETURNED = 25
POST_BREAKOUT_BUFFER_SECONDS = 120


class InsiderScanRequest(BaseModel):
    """Request body for POST /api/insider-scan."""

    mint: str
    breakout_ts: Optional[int] = None
    lookback_mins: int = Field(default=30, ge=1, le=240)
    min_sol: float = Field(default=1.0, gt=0.0)


async def resolve_pool_and_symbol(
    mint_or_pool: str,
    config: AgentConfig,
    client: httpx.AsyncClient,
) -> tuple[str, str, str]:
    """Resolve (token_mint, pool_address, symbol) for a Solana token CA or pool address via DexScreener."""
    target = (mint_or_pool or "").strip()
    if not target:
        raise ValueError("Token mint or pool address is required.")

    url = f"{config.DEXSCREENER_BASE_URL}/tokens/v1/solana/{target}"
    pairs: list[dict[str, Any]] = []
    try:
        resp = await client.get(url, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        pairs = data if isinstance(data, list) else (data.get("pairs") or [])
    except Exception as exc:
        logger.warning("[InsiderScanner] DexScreener tokens lookup failed for %s: %s", target[:8], exc)

    sol_pairs = [
        p for p in pairs if isinstance(p, dict) and str(p.get("chainId") or "").lower() == "solana"
    ]
    if not sol_pairs:
        # Fallback: check if user pasted a pair/pool address directly
        pair_url = f"{config.DEXSCREENER_BASE_URL}/latest/dex/pairs/solana/{target}"
        try:
            resp2 = await client.get(pair_url, timeout=10.0)
            if resp2.status_code == 200:
                d2 = resp2.json()
                p_list = d2.get("pairs") or ([d2["pair"]] if isinstance(d2.get("pair"), dict) else [])
                sol_pairs = [p for p in p_list if isinstance(p, dict)]
        except Exception:
            pass

    if not sol_pairs:
        short = f"{target[:6]}..{target[-4:]}" if len(target) > 10 else target
        return target, target, short

    best = max(sol_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0))
    base_tok = best.get("baseToken") or {}
    resolved_mint = str(base_tok.get("address") or target).strip()
    pool_address = str(best.get("pairAddress") or target).strip()
    symbol = str(base_tok.get("symbol") or f"{resolved_mint[:6]}..{resolved_mint[-4:]}").strip()
    return resolved_mint, pool_address, symbol


async def auto_detect_breakout_ts(
    pool_address: str,
    config: AgentConfig,
    client: httpx.AsyncClient,
) -> int:
    """Fetch recent 5m OHLCV candles from GeckoTerminal and pick the highest-volume green candle."""
    url = f"{config.GECKOTERMINAL_BASE_URL}/networks/solana/pools/{pool_address}/ohlcv/minute"
    params = {"aggregate": "5", "limit": "48", "currency": "usd"}
    try:
        resp = await client.get(url, params=params, headers={"Accept": "application/json"}, timeout=10.0)
        resp.raise_for_status()
        candles = parse_geckoterminal_ohlcv(resp.json())
        if candles:
            green = [c for c in candles if float(c.get("close") or 0.0) >= float(c.get("open") or 0.0)]
            pool = green if green else candles
            best = max(pool, key=lambda c: (float(c.get("volume") or 0.0), float(c.get("timestamp") or 0.0)))
            ts_val = int(float(best["timestamp"]))
            if ts_val > 0:
                return ts_val
    except Exception as exc:
        logger.warning("[InsiderScanner] GeckoTerminal OHLCV auto-detect failed for %s: %s", pool_address[:8], exc)
    return int(time.time())


def _extract_buy_swap_for_mint(
    tx: dict[str, Any],
    *,
    target_mint: str,
    pool_address: str,
    min_sol: float,
) -> Optional[dict[str, Any]]:
    """Extract a qualifying BUY swap (sol_spent >= min_sol) acquiring `target_mint`."""
    if not isinstance(tx, dict):
        return None
    if str(tx.get("type") or "").upper() != "SWAP":
        return None

    wallet = str(tx.get("feePayer") or "").strip()
    if not wallet or wallet == pool_address or wallet in IGNORED_BASE_MINTS:
        return None

    # Calculate net tokens of target_mint (or primary non-base mint if target_mint is pool address)
    tokens_received = 0.0
    for tf in tx.get("tokenTransfers") or []:
        if not isinstance(tf, dict):
            continue
        m = str(tf.get("mint") or "").strip()
        if not m or m in IGNORED_BASE_MINTS:
            continue
        if target_mint != pool_address and m != target_mint:
            continue
        amt = float(tf.get("tokenAmount") or 0.0)
        if amt <= 0:
            continue
        if tf.get("toUserAccount") == wallet:
            tokens_received += amt
        elif tf.get("fromUserAccount") == wallet:
            tokens_received -= amt

    if tokens_received <= 0:
        return None

    sol_spent = _estimate_sol_flow(tx, wallet, "BUY")
    if sol_spent + 1e-9 < min_sol:
        return None

    raw_ts = tx.get("timestamp")
    tx_ts = int(float(raw_ts)) if isinstance(raw_ts, (int, float)) and raw_ts > 0 else int(time.time())
    return {
        "signature": str(tx.get("signature") or ""),
        "wallet": wallet,
        "sol_spent": float(sol_spent),
        "token_amount": float(tokens_received),
        "timestamp": tx_ts,
    }


async def run_insider_scan(
    req: InsiderScanRequest,
    *,
    config: AgentConfig,
    http_client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """Execute on-demand Insider Wallet Scan across DexScreener, GeckoTerminal & Helius (max 5 pages)."""
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(follow_redirects=True, timeout=15.0)
    try:
        token_mint, pool_address, symbol = await resolve_pool_and_symbol(req.mint, config, client)
        breakout_ts = (
            int(req.breakout_ts)
            if req.breakout_ts is not None and int(req.breakout_ts) > 0
            else await auto_detect_breakout_ts(pool_address, config, client)
        )
        start_ts = int(breakout_ts) - int(req.lookback_mins) * 60
        end_ts = int(breakout_ts) + POST_BREAKOUT_BUFFER_SECONDS

        tracked_wallets = set(await fetch_helius_tracked_wallets(config, http_client=client)) | config.helius_tracked_wallet_set()
        api_key = config.HELIUS_API_KEY.strip()
        helius_url = f"{config.HELIUS_API_BASE_URL}/addresses/{pool_address}/transactions"

        seen_sigs: set[str] = set()
        by_wallet: dict[str, dict[str, Any]] = {}
        pages_scanned = 0
        cursor_sig: Optional[str] = None

        for _page in range(MAX_HELIUS_PAGES):
            params: dict[str, Any] = {
                "api-key": api_key,
                "type": "SWAP",
                "gte-time": str(start_ts),
                "lte-time": str(end_ts),
                "sort-order": "asc",
            }
            if cursor_sig:
                params["before"] = cursor_sig

            resp = await client.get(helius_url, params=params, timeout=15.0)
            resp.raise_for_status()
            tx_list = resp.json()
            if not isinstance(tx_list, list) or not tx_list:
                break

            pages_scanned += 1
            new_in_page = 0
            last_sig_in_page: Optional[str] = None

            for tx in tx_list:
                if not isinstance(tx, dict):
                    continue
                sig = str(tx.get("signature") or "").strip()
                if sig:
                    last_sig_in_page = sig
                    if sig in seen_sigs:
                        continue
                    seen_sigs.add(sig)
                new_in_page += 1

                tx_ts_raw = tx.get("timestamp")
                if isinstance(tx_ts_raw, (int, float)) and tx_ts_raw > 0:
                    if int(tx_ts_raw) < start_ts or int(tx_ts_raw) > end_ts:
                        continue

                buy_ev = _extract_buy_swap_for_mint(
                    tx,
                    target_mint=token_mint,
                    pool_address=pool_address,
                    min_sol=float(req.min_sol),
                )
                if buy_ev is None:
                    continue

                w = buy_ev["wallet"]
                b_ts = int(buy_ev["timestamp"])
                agg = by_wallet.get(w)
                if agg is None:
                    by_wallet[w] = {
                        "wallet": w,
                        "total_sol_spent": buy_ev["sol_spent"],
                        "buy_count": 1,
                        "first_buy_ts": b_ts,
                    }
                else:
                    agg["total_sol_spent"] += buy_ev["sol_spent"]
                    agg["buy_count"] += 1
                    if b_ts < agg["first_buy_ts"]:
                        agg["first_buy_ts"] = b_ts

            if new_in_page == 0 or not last_sig_in_page:
                break
            cursor_sig = last_sig_in_page

        candidates: list[dict[str, Any]] = []
        for w, agg in by_wallet.items():
            first_ts = int(agg["first_buy_ts"])
            first_utc = datetime.fromtimestamp(first_ts, tz=timezone.utc).isoformat()
            mins_before = round((float(breakout_ts) - float(first_ts)) / 60.0, 2)
            candidates.append(
                {
                    "wallet": w,
                    "wallet_label": config.resolve_wallet_label(w),
                    "total_sol_spent": round(float(agg["total_sol_spent"]), 4),
                    "buy_count": int(agg["buy_count"]),
                    "first_buy_ts": first_ts,
                    "first_buy_utc": first_utc,
                    "mins_before_breakout": mins_before,
                    "already_in_roster": w in tracked_wallets,
                    "gmgn_url": f"https://gmgn.ai/sol/address/{w}",
                    "solscan_url": f"https://solscan.io/account/{w}",
                }
            )

        candidates.sort(key=lambda c: (c["first_buy_ts"], -c["total_sol_spent"]))
        top_candidates = candidates[:MAX_CANDIDATES_RETURNED]
        breakout_utc = datetime.fromtimestamp(int(breakout_ts), tz=timezone.utc).isoformat()

        return {
            "mint": token_mint,
            "symbol": symbol,
            "pool_address": pool_address,
            "breakout_ts": int(breakout_ts),
            "breakout_utc": breakout_utc,
            "lookback_mins": int(req.lookback_mins),
            "min_sol": float(req.min_sol),
            "pages_scanned": pages_scanned,
            "total_candidates": len(candidates),
            "candidates": top_candidates,
        }
    finally:
        if owns_client:
            await client.aclose()
