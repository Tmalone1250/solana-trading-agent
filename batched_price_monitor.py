"""Batched price polling across up to 6 concurrent positions with on-demand Jupiter quotes."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase, utc_now
from executor_node import fetch_jupiter_order
from state import ActivePosition

logger = logging.getLogger(__name__)
EPS = 1e-9
MAX_PRICE_BACKOFF_ENTRIES = 32

# Per-endpoint backoff state on HTTP 429 (never hard-evicts tokens)
_price_backoff: dict[str, dict[str, float]] = {}


def clear_price_backoff() -> None:
    """Clear batched price backoff state (useful for tests)."""
    _price_backoff.clear()


def _prune_price_backoff(now_ts: float) -> None:
    expired = [k for k, v in list(_price_backoff.items()) if now_ts >= float(v.get("until") or 0.0)]
    for k in expired:
        _price_backoff.pop(k, None)
    if len(_price_backoff) > MAX_PRICE_BACKOFF_ENTRIES:
        oldest = sorted(_price_backoff.keys(), key=lambda k: float(_price_backoff[k].get("until") or 0.0))
        for k in oldest[: len(_price_backoff) - MAX_PRICE_BACKOFF_ENTRIES]:
            _price_backoff.pop(k, None)


def _record_429(key: str, now_ts: float) -> float:
    st = _price_backoff.get(key) or {"errors": 0.0, "until": 0.0}
    errors = int(st["errors"]) + 1
    delay = min(120.0, 5.0 * (2 ** (errors - 1)))
    _price_backoff[key] = {"errors": float(errors), "until": now_ts + delay}
    _prune_price_backoff(now_ts)
    logger.warning("[BatchedPrice] HTTP 429 on %s — backing off %.0fs", key, delay)
    return delay


def _clear_429(key: str) -> None:
    _price_backoff.pop(key, None)


def _in_backoff(key: str, now_ts: float) -> bool:
    st = _price_backoff.get(key)
    if st is None:
        return False
    if now_ts >= float(st.get("until") or 0.0):
        _price_backoff.pop(key, None)
        return False
    return True


async def fetch_batched_sol_prices(
    mints: list[str],
    config: AgentConfig,
    client: httpx.AsyncClient,
    *,
    now_ts: Optional[float] = None,
) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
    """Fetch USD-derived SOL prices for all active mints in 1 Jupiter /price/v3 + 1 DexScreener batch.

    Returns (sol_per_token_by_mint, dex_flow_by_mint).
    """
    unique_mints = list(dict.fromkeys(m.strip() for m in mints if m and m.strip()))
    if not unique_mints:
        return {}, {}

    ts = now_ts if now_ts is not None else time.time()
    sol_per_token: dict[str, float] = {}
    decimals_by_mint: dict[str, int] = {}
    dex_flow: dict[str, dict[str, Any]] = {}

    # 1. Single batched Jupiter Price API v3 call (always includes SOL_MINT + all active mints)
    if not _in_backoff("jup_price_v3", ts):
        ids_list = [SOL_MINT] + [m for m in unique_mints if m != SOL_MINT]
        ids_param = ",".join(ids_list)
        url = f"{config.JUPITER_BASE_URL}/price/v3"
        headers = {"x-api-key": config.JUPITER_API_KEY} if config.JUPITER_API_KEY else {}
        try:
            resp = await client.get(url, params={"ids": ids_param}, headers=headers, timeout=10.0)
            if resp.status_code == 429:
                _record_429("jup_price_v3", ts)
            else:
                resp.raise_for_status()
                _clear_429("jup_price_v3")
                data = resp.json()
                # Jupiter /price/v3 flat schema: {mint: {"usdPrice": float, "decimals": int, ...}}
                price_map = (
                    data["data"]
                    if isinstance(data, dict)
                    and SOL_MINT not in data
                    and isinstance(data.get("data"), dict)
                    else data
                )
                if isinstance(price_map, dict):
                    sol_entry = price_map.get(SOL_MINT)
                    sol_usd = (
                        float(sol_entry.get("usdPrice") or sol_entry.get("price") or 0.0)
                        if isinstance(sol_entry, dict)
                        else 0.0
                    )
                    if sol_usd > 0:
                        for m in unique_mints:
                            m_entry = price_map.get(m)
                            if not isinstance(m_entry, dict):
                                continue
                            if m_entry.get("decimals") is not None:
                                decimals_by_mint[m] = int(m_entry["decimals"])
                            tok_usd = float(m_entry.get("usdPrice") or m_entry.get("price") or 0.0)
                            if tok_usd > 0:
                                sol_per_token[m] = tok_usd / sol_usd
        except Exception as exc:
            logger.warning("[BatchedPrice] Jupiter /price/v3 failed: %s", exc)

    # 2. Single batched DexScreener lookup for 5m flow, liquidity & mandatory priceNative fallback
    if not _in_backoff("dex_batch", ts):
        for idx in range(0, len(unique_mints), 30):
            chunk = unique_mints[idx : idx + 30]
            url = f"{config.DEXSCREENER_BASE_URL}/tokens/v1/solana/{','.join(chunk)}"
            try:
                resp = await client.get(url, timeout=10.0)
                if resp.status_code == 429:
                    _record_429("dex_batch", ts)
                    break
                resp.raise_for_status()
                _clear_429("dex_batch")
                payload = resp.json()
                pairs = payload if isinstance(payload, list) else (payload.get("pairs") or [])
                best_by_mint: dict[str, dict[str, Any]] = {}
                for p in pairs:
                    if not isinstance(p, dict) or str(p.get("chainId") or "").lower() != "solana":
                        continue
                    m = str(
                        (p.get("baseToken") or {}).get("address")
                        or (chunk[0] if len(chunk) == 1 else "")
                    ).strip()
                    if not m:
                        continue
                    liq = float((p.get("liquidity") or {}).get("usd") or 0.0)
                    prev_liq = float((best_by_mint.get(m, {}).get("liquidity") or {}).get("usd") or 0.0)
                    if m not in best_by_mint or liq > prev_liq:
                        best_by_mint[m] = p
                for m, p in best_by_mint.items():
                    m5 = (p.get("txns") or {}).get("m5") or {}
                    native_p = float(p.get("priceNative") or 0.0)
                    dex_flow[m] = {
                        "buys_5m": int(m5.get("buys") or 0),
                        "sells_5m": int(m5.get("sells") or 0),
                        "liquidity_usd": float((p.get("liquidity") or {}).get("usd") or 0.0),
                        "price_native": native_p,
                        "decimals": decimals_by_mint.get(m),
                        "pair": p,
                    }
                    # Mandatory meme-coin fallback when Jupiter /price/v3 omits mint or fails
                    if m not in sol_per_token and native_p > 0:
                        sol_per_token[m] = native_p
            except Exception as exc:
                logger.warning("[BatchedPrice] DexScreener batch failed: %s", exc)

    for m, dec in decimals_by_mint.items():
        if m not in dex_flow:
            dex_flow[m] = {"decimals": dec}

    return sol_per_token, dex_flow


def estimate_position_sol_value(
    pos: ActivePosition,
    sol_per_token: Optional[float],
    *,
    token_decimals: Optional[int] = None,
) -> float:
    """Estimate current SOL value: (token_amount / (10 ** decimals)) * sol_per_token."""
    decimals = int(
        token_decimals
        if token_decimals is not None
        else (getattr(pos, "decimals", None) or 6)
    )
    tok_amt = pos.token_amount if pos.token_amount > 0 else pos.reference_token_amount
    if sol_per_token is not None and sol_per_token > 0 and tok_amt > 0:
        return (tok_amt / (10**decimals)) * sol_per_token
    return pos.current_sol_value or pos.entry_sol


def apply_dex_flow_and_pnl(
    pos: ActivePosition,
    *,
    current_sol_value: float,
    flow: Optional[dict[str, Any]],
    lane_label: str = "chart_slot",
) -> tuple[int, int, float, float]:
    """Update current/peak SOL, PnL, and DexScreener 5m flow/liquidity in-place and log 1-line status."""
    buys_5m = 0
    sells_5m = 0
    current_liq = float(pos.last_liquidity_usd or pos.entry_liquidity_usd or 0.0)
    buy_ratio_5m = float(pos.last_5m_buy_ratio or 0.0)

    if isinstance(flow, dict):
        if flow.get("decimals") is not None:
            pos.decimals = int(flow["decimals"])
        if "buys_5m" in flow or "sells_5m" in flow:
            buys_5m = int(flow.get("buys_5m") or 0)
            sells_5m = int(flow.get("sells_5m") or 0)
            total_5m = buys_5m + sells_5m
            buy_ratio_5m = round(buys_5m / total_5m, 4) if total_5m > 0 else 0.0
            sell_ratio_5m = round(sells_5m / total_5m, 4) if total_5m > 0 else 0.0
            pos.last_5m_buy_ratio = buy_ratio_5m
            pos.last_5m_sell_ratio = sell_ratio_5m
        if "liquidity_usd" in flow:
            current_liq = round(float(flow.get("liquidity_usd") or 0.0), 2)
            pos.last_liquidity_usd = current_liq

    pos.current_sol_value = round(current_sol_value, 6)
    if current_sol_value > pos.peak_sol_value:
        pos.peak_sol_value = round(current_sol_value, 6)
    pnl_sol = (current_sol_value - pos.entry_sol) if pos.entry_sol > 0 else 0.0
    pnl_pct = ((pnl_sol / pos.entry_sol) * 100.0) if pos.entry_sol > 0 else 0.0
    pos.pnl_sol = round(pnl_sol, 6)
    pos.pnl_pct = round(pnl_pct, 2)

    logger.info(
        "[Monitor] [%s] $%s (%s..): val=%.4f SOL (entry=%.4f, peak=%.4f, PnL=%+.4f SOL / %+.2f%%) | liq=$%.0f | 5m_buy=%.0f%%",
        lane_label,
        pos.symbol,
        pos.mint[:6],
        pos.current_sol_value,
        pos.entry_sol,
        pos.peak_sol_value,
        pos.pnl_sol,
        pos.pnl_pct,
        current_liq,
        buy_ratio_5m * 100.0,
    )
    return buys_5m, sells_5m, buy_ratio_5m, current_liq


def needs_exact_jupiter_quote(
    pos: ActivePosition,
    estimated_sol_value: float,
    config: AgentConfig,
    *,
    now_dt: Optional[datetime] = None,
    proximity_pct: float = 2.0,
) -> bool:
    """Return True only when estimated PnL is within 2% of Hard Stop, BE Shield, Trailing TP, or Whale exit."""
    if pos.entry_sol <= 0:
        return True
    pnl_pct_100 = ((estimated_sol_value - pos.entry_sol) / pos.entry_sol) * 100.0
    peak_sol = max(pos.peak_sol_value, estimated_sol_value)
    peak_pct_100 = ((peak_sol - pos.entry_sol) / pos.entry_sol) * 100.0
    is_whale = str(pos.strategy or "").upper() == "WHALE_SCALP"

    if is_whale:
        if pos.whale_shadow_exit_flag:
            return True
        # Near hard stop (-10% -> <= -8%)
        if pnl_pct_100 <= config.WHALE_HARD_STOP_PCT + proximity_pct:
            return True
        # Near Front-Run TP (+15% -> >= +13%)
        if pnl_pct_100 >= config.WHALE_TP_PCT - proximity_pct:
            return True
        # Near Whale Breakeven Shield (+1% floor when armed -> <= +3%)
        if (pos.breakeven_armed or peak_pct_100 >= config.WHALE_BREAKEVEN_ARM_PCT) and (
            pnl_pct_100 <= config.WHALE_BREAKEVEN_FLOOR_PCT + proximity_pct
        ):
            return True
        # Near 2-Tier Peak Trail drop
        if (pos.trail_armed or peak_pct_100 >= config.WHALE_TRAIL_ARM_PCT) and peak_sol > 0:
            drop_limit = (
                config.WHALE_TIGHT_TRAIL_DROP_PCT
                if peak_pct_100 >= config.WHALE_TIGHT_TRAIL_ARM_PCT
                else config.WHALE_TRAIL_DROP_PCT
            )
            drop_pct = ((peak_sol - estimated_sol_value) / peak_sol) * 100.0
            if drop_pct >= max(0.0, drop_limit - proximity_pct):
                return True
        # Near Time Stop
        if pos.opened_at:
            try:
                o_dt = datetime.fromisoformat(pos.opened_at)
                if o_dt.tzinfo is None:
                    o_dt = o_dt.replace(tzinfo=timezone.utc)
                held = ((now_dt or utc_now()) - o_dt).total_seconds()
                if held >= config.WHALE_TIME_STOP_SECONDS - 10 and pnl_pct_100 < config.WHALE_TIME_STOP_MIN_PNL_PCT + proximity_pct:
                    return True
            except ValueError:
                pass
        return False

    # Regular Chart position thresholds
    hard_stop_100 = -config.HARD_STOP_LOSS_PCT * 100.0
    if pnl_pct_100 <= hard_stop_100 + proximity_pct:
        return True
    be_floor_100 = config.BREAKEVEN_FLOOR_PCT * 100.0
    be_arm_100 = config.BREAKEVEN_ARM_PCT * 100.0
    if (pos.breakeven_armed or peak_pct_100 >= be_arm_100) and pnl_pct_100 <= be_floor_100 + proximity_pct:
        return True
    trail_arm_100 = config.TRAIL_ARM_PCT * 100.0
    trail_drop_100 = config.TRAIL_PULLBACK_PCT * 100.0
    if (pos.trail_armed or peak_pct_100 >= trail_arm_100) and peak_sol > 0:
        drop_pct = ((peak_sol - estimated_sol_value) / peak_sol) * 100.0
        if drop_pct >= max(0.0, trail_drop_100 - proximity_pct):
            return True
    return False


async def resolve_position_sol_value(
    pos: ActivePosition,
    *,
    estimated_sol_value: float,
    config: AgentConfig,
    client: httpx.AsyncClient,
    now_dt: Optional[datetime] = None,
    has_batched_price: bool = True,
    quote_amount: Optional[int] = None,
) -> tuple[float, bool]:
    """Return (sol_value, used_exact_quote), calling Jupiter Swap Quote only when near a threshold."""
    if has_batched_price and not needs_exact_jupiter_quote(pos, estimated_sol_value, config, now_dt=now_dt):
        return estimated_sol_value, False
    amt = quote_amount if quote_amount is not None and quote_amount > 0 else max(1, pos.token_amount or pos.reference_token_amount)
    try:
        quote_resp = await fetch_jupiter_order(
            input_mint=pos.mint,
            output_mint=SOL_MINT,
            amount_atomic=amt,
            config=config,
            client=client,
            taker=None,
        )
        return int(quote_resp["outAmount"]) / LAMPORTS_PER_SOL, True
    except Exception as exc:
        logger.warning("[BatchedPrice] On-demand Jupiter quote failed for %s (%s); using batched price %.4f SOL", pos.symbol, exc, estimated_sol_value)
        return estimated_sol_value, False
