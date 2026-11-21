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

# Per-endpoint backoff state on HTTP 429 (never hard-evicts tokens)
_price_backoff: dict[str, dict[str, float]] = {}


def clear_price_backoff() -> None:
    """Clear batched price backoff state (useful for tests)."""
    _price_backoff.clear()


def _record_429(key: str, now_ts: float) -> float:
    st = _price_backoff.get(key) or {"errors": 0.0, "until": 0.0}
    errors = int(st["errors"]) + 1
    delay = min(120.0, 5.0 * (2 ** (errors - 1)))
    _price_backoff[key] = {"errors": float(errors), "until": now_ts + delay}
    logger.warning("[BatchedPrice] HTTP 429 on %s — backing off %.0fs", key, delay)
    return delay


def _clear_429(key: str) -> None:
    _price_backoff.pop(key, None)


def _in_backoff(key: str, now_ts: float) -> bool:
    st = _price_backoff.get(key)
    return st is not None and now_ts < float(st.get("until") or 0.0)


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
    dex_flow: dict[str, dict[str, Any]] = {}

    # 1. Single batched Jupiter Price API v3 call (SOL + all active mints)
    if not _in_backoff("jup_price_v3", ts):
        ids_param = ",".join([SOL_MINT, *unique_mints])
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
                # Jupiter /price/v3 returns {mint: {"usdPrice": ...}} (or {"data": {mint: {"price": ...}}})
                price_map = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
                if isinstance(price_map, dict):
                    sol_entry = price_map.get(SOL_MINT) or {}
                    sol_usd = float(sol_entry.get("usdPrice") or sol_entry.get("price") or 0.0)
                    if sol_usd > 0:
                        for m in unique_mints:
                            m_entry = price_map.get(m) or {}
                            tok_usd = float(m_entry.get("usdPrice") or m_entry.get("price") or 0.0)
                            if tok_usd > 0:
                                sol_per_token[m] = tok_usd / sol_usd
        except Exception as exc:
            logger.warning("[BatchedPrice] Jupiter /price/v3 failed: %s", exc)

    # 2. Single batched DexScreener lookup for 5m flow, liquidity & fallback priceNative
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
                    m = str((p.get("baseToken") or {}).get("address") or "").strip()
                    if not m:
                        continue
                    liq = float((p.get("liquidity") or {}).get("usd") or 0.0)
                    prev_liq = float((best_by_mint.get(m, {}).get("liquidity") or {}).get("usd") or 0.0)
                    if m not in best_by_mint or liq > prev_liq:
                        best_by_mint[m] = p
                for m, p in best_by_mint.items():
                    m5 = (p.get("txns") or {}).get("m5") or {}
                    dex_flow[m] = {
                        "buys_5m": int(m5.get("buys") or 0),
                        "sells_5m": int(m5.get("sells") or 0),
                        "liquidity_usd": float((p.get("liquidity") or {}).get("usd") or 0.0),
                        "pair": p,
                    }
                    if m not in sol_per_token:
                        native_p = float(p.get("priceNative") or 0.0)
                        if native_p > 0:
                            sol_per_token[m] = native_p
            except Exception as exc:
                logger.warning("[BatchedPrice] DexScreener batch failed: %s", exc)

    return sol_per_token, dex_flow


def estimate_position_sol_value(
    pos: ActivePosition,
    sol_per_token: Optional[float],
    *,
    token_decimals: int = 6,
) -> float:
    """Estimate current SOL value from batched price, falling back to last known value."""
    if sol_per_token is not None and sol_per_token > 0 and pos.token_amount > 0:
        # Derive effective scale from entry if reference ratio is known, else standard SPL decimals
        return (pos.token_amount / (10**token_decimals)) * sol_per_token
    return pos.current_sol_value or pos.entry_sol


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
) -> tuple[float, bool]:
    """Return (sol_value, used_exact_quote), calling Jupiter Swap Quote only when near a threshold."""
    if not needs_exact_jupiter_quote(pos, estimated_sol_value, config, now_dt=now_dt):
        return estimated_sol_value, False
    quote_resp = await fetch_jupiter_order(
        input_mint=pos.mint,
        output_mint=SOL_MINT,
        amount_atomic=max(1, pos.token_amount),
        config=config,
        client=client,
        taker=None,
    )
    return int(quote_resp["outAmount"]) / LAMPORTS_PER_SOL, True
