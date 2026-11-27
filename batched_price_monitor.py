"""Batched price polling across up to 6 concurrent positions with on-demand Jupiter quotes."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase, utc_now
from executor_node import executor_node, fetch_jupiter_order
from state import ActivePosition, FocusState, PendingOrder, is_valid_pool_address

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


def select_pinned_dexscreener_pair(
    pairs: list[dict[str, Any]],
    *,
    expected_mint: str,
    expected_pair_address: Optional[str],
) -> dict[str, Any]:
    """Strictly match a DexScreener pair by (expected_pair_address, expected_mint).

    Never substitutes a different highest-liquidity pool. Distinguishes valid zero
    flow/liquidity from missing, migrated, incomplete, or mismatched provider responses.
    """
    exp_mint = str(expected_mint or "").strip()
    if not is_valid_pool_address(expected_pair_address, mint=exp_mint):
        return {
            "pool_status": "UNKNOWN_POOL_IDENTITY",
            "flow_available": False,
            "pair_address": None,
            "dex_id": None,
            "buys_5m": None,
            "sells_5m": None,
            "liquidity_usd": None,
            "price_native": 0.0,
            "pair": None,
        }

    exp_pair = str(expected_pair_address).strip()
    solana_pairs = [
        p for p in (pairs or [])
        if isinstance(p, dict) and str(p.get("chainId") or "").lower() == "solana"
    ]
    matched = next(
        (p for p in solana_pairs if str(p.get("pairAddress") or "").strip() == exp_pair),
        None,
    )
    if matched is not None:
        base_mint = str((matched.get("baseToken") or {}).get("address") or "").strip()
        if base_mint != exp_mint:
            logger.warning(
                "[BatchedPrice] Rejected DexScreener pool %s: baseToken mint %s != expected %s",
                exp_pair[:8], base_mint[:8], exp_mint[:8],
            )
            return {
                "pool_status": "MISMATCHED_MINT",
                "flow_available": False,
                "pair_address": exp_pair,
                "dex_id": None,
                "buys_5m": None,
                "sells_5m": None,
                "liquidity_usd": None,
                "price_native": 0.0,
                "pair": None,
            }

        dex_id = str(matched.get("dexId") or "").strip() or None
        native_p = float(matched.get("priceNative") or 0.0)
        txns = matched.get("txns")
        m5 = txns.get("m5") if isinstance(txns, dict) else None
        liq_obj = matched.get("liquidity")
        has_m5 = isinstance(m5, dict) and m5.get("buys") is not None and m5.get("sells") is not None
        has_liq = isinstance(liq_obj, dict) and liq_obj.get("usd") is not None
        if not (has_m5 and has_liq):
            return {
                "pool_status": "INCOMPLETE_DATA",
                "flow_available": False,
                "pair_address": exp_pair,
                "dex_id": dex_id,
                "buys_5m": None,
                "sells_5m": None,
                "liquidity_usd": float(liq_obj["usd"]) if has_liq else None,
                "price_native": native_p,
                "pair": matched,
            }

        buys_5m = int(m5["buys"])
        sells_5m = int(m5["sells"])
        liq_usd = float(liq_obj["usd"])
        status = "ZERO_LIQUIDITY" if liq_usd <= 0.0 else "MATCHED"
        return {
            "pool_status": status,
            "flow_available": True,
            "pair_address": exp_pair,
            "dex_id": dex_id,
            "buys_5m": buys_5m,
            "sells_5m": sells_5m,
            "liquidity_usd": liq_usd,
            "price_native": native_p,
            "pair": matched,
        }

    # Pinned pool was NOT returned: classify without substituting any other pool
    other_for_mint = [
        p for p in solana_pairs
        if str((p.get("baseToken") or {}).get("address") or "").strip() == exp_mint
    ]
    if other_for_mint:
        status = "POOL_MIGRATED_OR_MISSING"
    elif solana_pairs:
        status = "MISMATCHED_PAIR"
    else:
        status = "POOL_UNAVAILABLE"

    return {
        "pool_status": status,
        "flow_available": False,
        "pair_address": exp_pair,
        "dex_id": None,
        "buys_5m": None,
        "sells_5m": None,
        "liquidity_usd": None,
        "price_native": 0.0,
        "pair": None,
    }


async def fetch_batched_sol_prices(
    mints: list[str],
    config: AgentConfig,
    client: httpx.AsyncClient,
    *,
    now_ts: Optional[float] = None,
    pinned_pools: Optional[dict[str, Optional[str]]] = None,
) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
    """Fetch USD-derived SOL prices for all active mints in 1 Jupiter /price/v3 + 1 DexScreener batch.

    When `pinned_pools` is provided (`{mint: pair_address}`), DexScreener flow and liquidity
    observations are strictly pinned to each mint's stored `pair_address` without silent pool switching.
    Returns (sol_per_token_by_mint, dex_flow_by_mint).
    """
    unique_mints = list(dict.fromkeys(m.strip() for m in mints if m and m.strip()))
    if not unique_mints:
        return {}, {}

    ts = now_ts if now_ts is not None else time.time()
    sol_per_token: dict[str, float] = {}
    decimals_by_mint: dict[str, int] = {}
    price_source_by_mint: dict[str, str] = {}
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
                                price_source_by_mint[m] = "jupiter_v3"
        except Exception as exc:
            logger.warning("[BatchedPrice] Jupiter /price/v3 failed: %s", exc)

    # 2. Single batched DexScreener lookup for 5m flow, liquidity & priceNative fallback
    all_solana_pairs: list[dict[str, Any]] = []
    dex_fetch_ok = False
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
                dex_fetch_ok = True
                payload = resp.json()
                pairs = payload if isinstance(payload, list) else (payload.get("pairs") or [])
                for p in pairs:
                    if isinstance(p, dict) and str(p.get("chainId") or "").lower() == "solana":
                        all_solana_pairs.append(p)
            except Exception as exc:
                logger.warning("[BatchedPrice] DexScreener batch failed: %s", exc)

    if pinned_pools is not None:
        for m in unique_mints:
            exp_pair = pinned_pools.get(m)
            if not dex_fetch_ok and is_valid_pool_address(exp_pair, mint=m):
                obs = {
                    "pool_status": "POOL_UNAVAILABLE",
                    "flow_available": False,
                    "pair_address": str(exp_pair).strip(),
                    "dex_id": None,
                    "buys_5m": None,
                    "sells_5m": None,
                    "liquidity_usd": None,
                    "price_native": 0.0,
                    "pair": None,
                }
            else:
                obs = select_pinned_dexscreener_pair(
                    all_solana_pairs,
                    expected_mint=m,
                    expected_pair_address=exp_pair,
                )
            native_p = float(obs.get("price_native") or 0.0)
            if (
                m not in sol_per_token
                and native_p > 0
                and obs["pool_status"] in ("MATCHED", "ZERO_LIQUIDITY", "INCOMPLETE_DATA")
            ):
                sol_per_token[m] = native_p
                price_source_by_mint[m] = "dexscreener_native"
            dex_flow[m] = {
                **obs,
                "decimals": decimals_by_mint.get(m),
                "price_source": price_source_by_mint.get(m),
                "all_solana_pairs": all_solana_pairs,
            }
    else:
        best_by_mint: dict[str, dict[str, Any]] = {}
        for p in all_solana_pairs:
            m = str(
                (p.get("baseToken") or {}).get("address")
                or (unique_mints[0] if len(unique_mints) == 1 else "")
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
            if m not in sol_per_token and native_p > 0:
                sol_per_token[m] = native_p
                price_source_by_mint[m] = "dexscreener_native"
            dex_flow[m] = {
                "buys_5m": int(m5.get("buys") or 0),
                "sells_5m": int(m5.get("sells") or 0),
                "liquidity_usd": float((p.get("liquidity") or {}).get("usd") or 0.0),
                "price_native": native_p,
                "pair_address": str(p.get("pairAddress") or "").strip() or None,
                "dex_id": str(p.get("dexId") or "").strip() or None,
                "decimals": decimals_by_mint.get(m),
                "price_source": price_source_by_mint.get(m),
                "pair": p,
                "all_solana_pairs": all_solana_pairs,
            }
        for m in unique_mints:
            if m not in dex_flow:
                dex_flow[m] = {
                    "decimals": decimals_by_mint.get(m),
                    "price_source": price_source_by_mint.get(m),
                    "all_solana_pairs": all_solana_pairs,
                    "flow_available": False,
                    "pool_status": "POOL_UNAVAILABLE",
                }

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


def active_cost_basis_sol(pos: ActivePosition) -> float:
    """Return remaining_cost_basis_sol (when > 0), otherwise initial entry_sol."""
    rem = float(getattr(pos, "remaining_cost_basis_sol", 0.0) or 0.0)
    if rem > 0.0:
        return rem
    return float(pos.entry_sol)


def hard_stop_limit_pct_100(pos: ActivePosition, config: AgentConfig) -> float:
    """Return the negative hard stop threshold in percent (e.g., -10.0) for Chart or Whale position."""
    if str(pos.strategy or "").upper() == "WHALE_SCALP":
        raw_w = abs(float(config.WHALE_HARD_STOP_PCT))
        return -(raw_w * 100.0 if 0.0 < raw_w < 1.0 else raw_w)
    raw_c = abs(float(config.HARD_STOP_LOSS_PCT))
    return -(raw_c * 100.0 if 0.0 < raw_c < 1.0 else raw_c)


def is_hard_stop_hit(
    pos: ActivePosition,
    current_sol_value: float,
    config: AgentConfig,
) -> tuple[bool, float, float]:
    """Unconditionally evaluate HARD_STOP_LOSS against remaining_cost_basis_sol.

    Returns (is_hit, pnl_pct_100, stop_limit_pct_100). Never suppressed by partial_tp_taken,
    breakeven_armed, trail_armed, LEG_2_REENTRY, or any other flag.
    """
    basis = active_cost_basis_sol(pos)
    stop_limit_100 = hard_stop_limit_pct_100(pos, config)
    if pos.token_amount <= 0 or basis <= 0.0:
        return False, 0.0, stop_limit_100
    pnl_pct_100 = ((current_sol_value - basis) / basis) * 100.0
    return (pnl_pct_100 <= stop_limit_100 + 1e-6, pnl_pct_100, stop_limit_100)


def apply_dex_flow_and_pnl(
    pos: ActivePosition,
    *,
    current_sol_value: float,
    flow: Optional[dict[str, Any]],
    lane_label: str = "chart_slot",
    used_exact_quote: bool = False,
) -> tuple[int, int, float, float]:
    """Update current/peak SOL, PnL, and pool-pinned DexScreener 5m flow/liquidity in-place.

    Never silently substitutes another pool or retroactively assigns a pool identity to an
    unknown-pool position. When pinned pool data is unavailable/mismatched/unknown, returns
    (0, 0, last_buy_ratio, 0.0) so downstream exit checks do not fabricate a liquidity collapse
    or unrelated sell-flow exit.
    """
    buys_5m = 0
    sells_5m = 0
    current_liq = 0.0
    buy_ratio_5m = float(pos.last_5m_buy_ratio or 0.0)

    if isinstance(flow, dict):
        if flow.get("decimals") is not None:
            pos.decimals = int(flow["decimals"])
        if flow.get("price_source"):
            pos.last_price_source = str(flow["price_source"])
    if used_exact_quote:
        pos.last_price_source = "jupiter_exact_quote"

    if not is_valid_pool_address(pos.pair_address, mint=pos.mint):
        pos.pool_identity_status = "UNKNOWN"
        pos.pool_observation_status = "UNKNOWN_POOL_IDENTITY"
    else:
        pos.pool_identity_status = "PINNED"
        obs: Optional[dict[str, Any]] = None
        if isinstance(flow, dict):
            if isinstance(flow.get("all_solana_pairs"), list):
                obs = select_pinned_dexscreener_pair(
                    flow["all_solana_pairs"],
                    expected_mint=pos.mint,
                    expected_pair_address=pos.pair_address,
                )
            elif "pool_status" in flow:
                f_pair = str(flow.get("pair_address") or "").strip()
                if f_pair and f_pair != str(pos.pair_address).strip():
                    obs = {"pool_status": "MISMATCHED_PAIR", "flow_available": False}
                else:
                    obs = flow
            elif isinstance(flow.get("pair"), dict) and flow["pair"]:
                obs = select_pinned_dexscreener_pair(
                    [flow["pair"]],
                    expected_mint=pos.mint,
                    expected_pair_address=pos.pair_address,
                )
            elif ("buys_5m" in flow or "sells_5m" in flow) and flow.get("flow_available", True) is not False:
                obs = {
                    "pool_status": "ZERO_LIQUIDITY" if float(flow.get("liquidity_usd") or 0.0) <= 0.0 else "MATCHED",
                    "flow_available": True,
                    "buys_5m": int(flow.get("buys_5m") or 0),
                    "sells_5m": int(flow.get("sells_5m") or 0),
                    "liquidity_usd": float(flow.get("liquidity_usd") or 0.0),
                    "dex_id": flow.get("dex_id"),
                }

        if obs is not None and obs.get("flow_available") is True:
            buys_5m = int(obs.get("buys_5m") or 0)
            sells_5m = int(obs.get("sells_5m") or 0)
            total_5m = buys_5m + sells_5m
            buy_ratio_5m = round(buys_5m / total_5m, 4) if total_5m > 0 else 0.0
            sell_ratio_5m = round(sells_5m / total_5m, 4) if total_5m > 0 else 0.0
            pos.last_5m_buy_ratio = buy_ratio_5m
            pos.last_5m_sell_ratio = sell_ratio_5m
            if obs.get("liquidity_usd") is not None:
                current_liq = round(float(obs["liquidity_usd"]), 2)
                pos.last_liquidity_usd = current_liq
            if not pos.dex_id and isinstance(obs.get("dex_id"), str) and obs["dex_id"].strip():
                pos.dex_id = obs["dex_id"].strip()
            pos.pool_observation_status = str(obs.get("pool_status") or "MATCHED")
        else:
            status = str((obs or {}).get("pool_status") or "POOL_UNAVAILABLE")
            pos.pool_observation_status = status
            logger.warning(
                "[Monitor] [%s] $%s pinned pool %s observation=%s (preserving original pool identity; no substitution)",
                lane_label, pos.symbol, str(pos.pair_address)[:8], status,
            )

    basis = active_cost_basis_sol(pos)
    pos.current_sol_value = round(current_sol_value, 6)
    if current_sol_value > pos.peak_sol_value:
        pos.peak_sol_value = round(current_sol_value, 6)
    pnl_sol = (current_sol_value - basis) if basis > 0 else 0.0
    pnl_pct = ((pnl_sol / basis) * 100.0) if basis > 0 else 0.0
    pos.pnl_sol = round(pnl_sol, 6)
    pos.pnl_pct = round(pnl_pct, 2)
    pos.position_mfe_pct = pos.pnl_pct if pos.position_mfe_pct is None else max(pos.position_mfe_pct, pos.pnl_pct)
    pos.position_mae_pct = pos.pnl_pct if pos.position_mae_pct is None else min(pos.position_mae_pct, pos.pnl_pct)

    logger.info(
        "[Monitor] [%s] $%s (%s..): val=%.4f SOL (basis=%.4f, peak=%.4f, PnL=%+.4f SOL / %+.2f%%) | pool=%s(%s) | liq=$%.0f | 5m_buy=%.0f%%",
        lane_label,
        pos.symbol,
        pos.mint[:6],
        pos.current_sol_value,
        basis,
        pos.peak_sol_value,
        pos.pnl_sol,
        pos.pnl_pct,
        str(pos.pair_address)[:8] if pos.pair_address else "UNKNOWN",
        pos.pool_observation_status or pos.pool_identity_status,
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
    """Return True when estimated PnL is at/below HARD_STOP_LOSS or within 2% of any exit threshold."""
    basis = active_cost_basis_sol(pos)
    if basis <= 0:
        return True
    pnl_pct_100 = ((estimated_sol_value - basis) / basis) * 100.0
    stop_limit_100 = hard_stop_limit_pct_100(pos, config)

    # Unconditional Hard Stop Gate: ALWAYS fetch exact Jupiter exit quote near or below -HARD_STOP_LOSS_PCT
    if pnl_pct_100 <= stop_limit_100 + max(0.0, proximity_pct) + EPS:
        return True

    peak_sol = max(pos.peak_sol_value, estimated_sol_value)
    peak_pct_100 = ((peak_sol - basis) / basis) * 100.0
    is_whale = str(pos.strategy or "").upper() == "WHALE_SCALP"

    if is_whale:
        if pos.whale_shadow_exit_flag:
            return True
        whale_tp_pct = float(config.WHALE_PARTIAL_TP_TRIGGER_PCT or config.PARTIAL_TP_TRIGGER_PCT)
        if not pos.partial_tp_taken and pnl_pct_100 >= whale_tp_pct - proximity_pct:
            return True
        be_floor_100 = (
            max(float(config.WHALE_BREAKEVEN_FLOOR_PCT), float(config.BREAKEVEN_FLOOR_PCT) * 100.0)
            if pos.partial_tp_taken
            else float(config.WHALE_BREAKEVEN_FLOOR_PCT)
        )
        if (pos.breakeven_armed or peak_pct_100 >= config.WHALE_BREAKEVEN_ARM_PCT) and (
            pnl_pct_100 <= be_floor_100 + proximity_pct
        ):
            return True
        if (pos.trail_armed or pos.partial_tp_taken or peak_pct_100 >= config.WHALE_TRAIL_ARM_PCT) and peak_sol > 0:
            raw_w_runner = float(config.WHALE_RUNNER_TRAIL_PULLBACK_PCT or config.RUNNER_TRAIL_PULLBACK_PCT)
            w_runner_100 = raw_w_runner if raw_w_runner >= 1.0 else raw_w_runner * 100.0
            drop_limit = (
                w_runner_100
                if pos.partial_tp_taken
                else (
                    config.WHALE_TIGHT_TRAIL_DROP_PCT
                    if peak_pct_100 >= config.WHALE_TIGHT_TRAIL_ARM_PCT
                    else config.WHALE_TRAIL_DROP_PCT
                )
            )
            drop_pct = ((peak_sol - estimated_sol_value) / peak_sol) * 100.0
            if drop_pct >= max(0.0, drop_limit - proximity_pct):
                return True
        if not pos.partial_tp_taken and pos.opened_at:
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

    # Regular Chart position thresholds (+15% Partial TP & -10% Runner Trail)
    if not pos.partial_tp_taken and pnl_pct_100 >= config.PARTIAL_TP_TRIGGER_PCT - proximity_pct:
        return True
    be_floor_100 = config.BREAKEVEN_FLOOR_PCT * 100.0
    be_arm_100 = config.BREAKEVEN_ARM_PCT * 100.0
    if (pos.breakeven_armed or peak_pct_100 >= be_arm_100) and pnl_pct_100 <= be_floor_100 + proximity_pct:
        return True
    trail_arm_100 = config.TRAIL_ARM_PCT * 100.0
    raw_runner_drop = float(config.RUNNER_TRAIL_PULLBACK_PCT)
    runner_drop_100 = raw_runner_drop if raw_runner_drop >= 1.0 else raw_runner_drop * 100.0
    trail_drop_100 = runner_drop_100 if pos.partial_tp_taken else (config.TRAIL_PULLBACK_PCT * 100.0)
    if (pos.trail_armed or pos.partial_tp_taken or peak_pct_100 >= trail_arm_100) and peak_sol > 0:
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
    """Return (sol_value, used_exact_quote), calling Jupiter Swap Quote when near/below a threshold."""
    force_hard_stop_quote, _, _ = is_hard_stop_hit(pos, estimated_sol_value, config)
    if (
        has_batched_price
        and not force_hard_stop_quote
        and not needs_exact_jupiter_quote(pos, estimated_sol_value, config, now_dt=now_dt)
    ):
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
        quoted_sol = int(quote_resp["outAmount"]) / LAMPORTS_PER_SOL
        if quoted_sol > 0:
            return quoted_sol, True
        return estimated_sol_value, False
    except Exception as exc:
        logger.warning("[BatchedPrice] On-demand Jupiter quote failed for %s (%s); using batched price %.4f SOL", pos.symbol, exc, estimated_sol_value)
        return estimated_sol_value, False


async def enforce_hard_stop_if_hit(
    pos: ActivePosition,
    *,
    current_sol_value: float,
    config: AgentConfig,
    db: AgentDatabase,
    client: httpx.AsyncClient,
    lane: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Unconditionally execute a 100% HARD_STOP_LOSS SELL if pnl_pct <= -HARD_STOP_LOSS_PCT."""
    hit, pnl_pct_100, stop_limit_100 = is_hard_stop_hit(pos, current_sol_value, config)
    if not hit:
        return None

    is_whale = (lane == "whale") or (str(pos.strategy or "").upper() == "WHALE_SCALP")
    pos.current_sol_value = round(current_sol_value, 6)
    reason = f"HARD_STOP_LOSS ({pnl_pct_100:+.2f}% <= {stop_limit_100:.0f}%)"
    order = PendingOrder(
        side="SELL", mint=pos.mint, symbol=pos.symbol, opened_at=pos.opened_at, amount_atomic=pos.token_amount,
        reason=reason, target_state_after_fill=FocusState.SCANNING, apply_cooldown=True,
        strategy=pos.strategy or ("WHALE_SCALP" if is_whale else "SCALPER"),
        triggering_wallets=list(pos.triggering_wallets), entry_mc_usd=pos.entry_mc_usd,
        whale_sol_flow=pos.whale_sol_flow, breakout_telemetry=pos.breakout_telemetry,
        monitor_sol_value=round(current_sol_value, 6), price_source=pos.last_price_source,
        rationale=f"[HARD STOP LOSS] {pos.symbol}: {reason}",
    )
    st = db.get_state()
    exec_state: dict[str, Any] = {
        "focus_state": st["focus_state"],
        "sol_balance": float(st["sol_balance"]),
        "daily_pnl_sol": float(st["daily_pnl_sol"]),
        "active_position": st["active_position"] if not is_whale else pos.model_dump(),
        "chart_positions": list(st.get("chart_positions") or []),
        "whale_position": pos.model_dump() if is_whale else st.get("whale_position"),
        "whale_positions": list(st.get("whale_positions") or []),
        "pending_order": order.model_dump(),
        "slot": "whale_slot" if is_whale else "chart_slot",
    }
    if not is_whale:
        exec_state["active_position"] = pos.model_dump()
    out = await executor_node(exec_state, config=config, db=db, http_client=client)
    return {**out, "pending_order": order.model_dump(), "monitor_event": reason, "_hard_stop_already_executed": True}


async def monitor_and_enforce_hard_stops(
    *,
    config: AgentConfig,
    db: AgentDatabase,
    client: httpx.AsyncClient,
    now_dt: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Poll all active chart_positions and whale_positions and immediately execute HARD_STOP_LOSS on any <= -10%."""
    dt = now_dt or utc_now()
    st = db.get_state()
    chart_list = [p for p in (st.get("chart_positions") or []) if isinstance(p, dict) and p.get("mint")]
    whale_list = [p for p in (st.get("whale_positions") or []) if isinstance(p, dict) and p.get("mint")]
    all_mints = [str(p["mint"]) for p in chart_list + whale_list]
    if not all_mints:
        return []

    pinned_pools = {
        str(p["mint"]): p.get("pair_address")
        for p in chart_list + whale_list
        if isinstance(p, dict) and p.get("mint")
    }
    sol_prices, dex_flow = await fetch_batched_sol_prices(
        all_mints, config, client, now_ts=dt.timestamp(), pinned_pools=pinned_pools
    )
    executed: list[dict[str, Any]] = []

    for lane_name, raw_list in (("chart", chart_list), ("whale", whale_list)):
        lane_label = "whale_slot" if lane_name == "whale" else "chart_slot"
        for raw_p in raw_list:
            pos = ActivePosition.model_validate(raw_p)
            flow = dex_flow.get(pos.mint)
            if isinstance(flow, dict) and flow.get("decimals") is not None:
                pos.decimals = int(flow["decimals"])
            est_val = estimate_position_sol_value(pos, sol_prices.get(pos.mint), token_decimals=pos.decimals)
            curr_val, used_q = await resolve_position_sol_value(
                pos, estimated_sol_value=est_val, config=config, client=client,
                now_dt=dt, has_batched_price=(pos.mint in sol_prices),
            )
            apply_dex_flow_and_pnl(pos, current_sol_value=curr_val, flow=flow, lane_label=lane_label, used_exact_quote=used_q)
            hs_out = await enforce_hard_stop_if_hit(
                pos, current_sol_value=curr_val, config=config, db=db, client=client, lane=lane_name
            )
            if hs_out is not None:
                executed.append(hs_out)
    return executed
