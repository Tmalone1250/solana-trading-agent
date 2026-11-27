"""Jupiter Swap API V2 Executor (executor_node.py)."""

from __future__ import annotations

import base64
import logging
import math
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase, utc_now, utc_now_iso
from state import ActivePosition, AgentGraphState, FocusState, PendingOrder, is_valid_pool_address
from state_ledger import (
    acquire_exit_guard,
    attach_intent_to_reservation,
    clear_sell_guard,
    finalize_exit_guard,
    mark_buy_reservation_broadcast_unresolved,
)
from tx_reconciler import derive_and_validate_single_signer_tx

logger = logging.getLogger(__name__)


def clear_sell_idempotency_guard() -> None:
    clear_sell_guard()


def extract_raw_price_impact(order_resp: dict[str, Any]) -> Optional[str]:
    """Preserve raw Jupiter /swap/v2/order priceImpactPct (provider percentage string) verbatim."""
    if not isinstance(order_resp, dict):
        return None
    for key in ("priceImpactPct", "priceImpact"):
        val = order_resp.get(key)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return None


def sign_jupiter_transaction(tx_base64: str, private_key_bs58: str) -> str:
    """Deserialize a base64 VersionedTransaction, sign with Keypair, return base64."""
    keypair = Keypair.from_base58_string(private_key_bs58)
    raw_tx = base64.b64decode(tx_base64)
    tx = VersionedTransaction.from_bytes(raw_tx)
    signed_tx = VersionedTransaction(tx.message, [keypair])
    return base64.b64encode(bytes(signed_tx)).decode("ascii")


_ORIG_SIGN_JUPITER_TX = sign_jupiter_transaction

AUTHORITATIVE_APPROVAL_PRICE_SOURCE = "dexscreener_pinned_pool_native"


class E4AValidationError(RuntimeError):
    """Raised when Phase E4A pre-broadcast approval or quote integrity checks fail."""


def _parse_iso_utc(raw: Optional[str]) -> Optional[datetime]:
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _parse_e4a_price_impact_pct(order_resp: dict[str, Any]) -> Optional[float]:
    """Parse priceImpact first, falling back to deprecated priceImpactPct only when absent."""
    if not isinstance(order_resp, dict):
        return None
    raw = order_resp.get("priceImpact")
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        raw = order_resp.get("priceImpactPct")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        val = float(raw)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(val):
        return None
    return val


def _e4a_is_active(order: PendingOrder, config: AgentConfig) -> bool:
    """E4A applies to BREAKOUT_IGNITION BUY in live mode (unconditionally) or paper when any E4A threshold is configured."""
    if order.side.upper() != "BUY":
        return False
    if str(order.strategy or "").upper() != "BREAKOUT_IGNITION":
        return False
    if config.EXECUTION_MODE == "live":
        return True
    return any(
        getattr(config, attr) is not None
        for attr in (
            "MAX_BUY_APPROVAL_AGE_SECONDS",
            "MAX_APPROVAL_PRICE_AGE_SECONDS",
            "MAX_ENTRY_PRICE_DRIFT_PCT",
            "MAX_NEGATIVE_ENTRY_DRIFT_PCT",
            "MAX_WORST_CASE_ENTRY_DRIFT_PCT",
            "MAX_QUOTE_AGE_SECONDS",
            "MAX_ACCEPTED_SLIPPAGE_BPS",
            "MAX_ENTRY_PRICE_IMPACT_PCT",
            "MIN_BLOCK_HEIGHT_HEADROOM",
            "MIN_RFQ_EXPIRY_HEADROOM_SECONDS",
        )
    )


def _validate_e4a_approval_provenance(
    order: PendingOrder,
    config: AgentConfig,
    *,
    now_dt: datetime,
) -> None:
    """Validate pre-quote approval provenance, token decimals, and freshness."""
    if not _e4a_is_active(order, config):
        return

    is_live = config.EXECUTION_MODE == "live"
    if is_live:
        required_live = (
            "MAX_BUY_APPROVAL_AGE_SECONDS",
            "MAX_APPROVAL_PRICE_AGE_SECONDS",
            "MAX_ENTRY_PRICE_DRIFT_PCT",
            "MAX_QUOTE_AGE_SECONDS",
            "MAX_ACCEPTED_SLIPPAGE_BPS",
            "MAX_ENTRY_PRICE_IMPACT_PCT",
        )
        missing_cfg = [k for k in required_live if getattr(config, k) is None]
        if missing_cfg:
            raise E4AValidationError(
                f"BLOCKED: E4A_LIVE_THRESHOLDS_UNSET ({','.join(missing_cfg)})"
            )

    # Validate approval provenance whenever live OR any approval/drift threshold is set in paper
    need_approval_ctx = is_live or any(
        getattr(config, k) is not None
        for k in (
            "MAX_BUY_APPROVAL_AGE_SECONDS",
            "MAX_APPROVAL_PRICE_AGE_SECONDS",
            "MAX_ENTRY_PRICE_DRIFT_PCT",
            "MAX_NEGATIVE_ENTRY_DRIFT_PCT",
            "MAX_WORST_CASE_ENTRY_DRIFT_PCT",
        )
    )
    if not need_approval_ctx:
        return

    app_dt = _parse_iso_utc(order.approved_at)
    if app_dt is None:
        raise E4AValidationError("BLOCKED: MISSING_OR_INVALID_APPROVED_AT")
    if config.MAX_BUY_APPROVAL_AGE_SECONDS is not None:
        app_age = (now_dt - app_dt).total_seconds()
        if app_age > float(config.MAX_BUY_APPROVAL_AGE_SECONDS) or app_age < -5.0:
            raise E4AValidationError(
                f"BLOCKED: STALE_BUY_APPROVAL (age={app_age:.2f}s > {config.MAX_BUY_APPROVAL_AGE_SECONDS}s)"
            )

    obs_dt = _parse_iso_utc(order.approval_price_observed_at)
    if obs_dt is None:
        raise E4AValidationError("BLOCKED: MISSING_OR_INVALID_APPROVAL_PRICE_OBSERVED_AT")
    if config.MAX_APPROVAL_PRICE_AGE_SECONDS is not None:
        obs_age = (now_dt - obs_dt).total_seconds()
        if obs_age > float(config.MAX_APPROVAL_PRICE_AGE_SECONDS) or obs_age < -5.0:
            raise E4AValidationError(
                f"BLOCKED: STALE_APPROVAL_PRICE (age={obs_age:.2f}s > {config.MAX_APPROVAL_PRICE_AGE_SECONDS}s)"
            )

    if order.approval_price_source != AUTHORITATIVE_APPROVAL_PRICE_SOURCE:
        raise E4AValidationError(
            f"BLOCKED: INVALID_APPROVAL_PRICE_SOURCE ({order.approval_price_source!r})"
        )
    if not order.pair_address or order.approval_pair_address != order.pair_address:
        raise E4AValidationError(
            f"BLOCKED: APPROVAL_POOL_MISMATCH (approval={order.approval_pair_address!r} != order={order.pair_address!r})"
        )
    pn = order.approval_price_native
    if pn is None or isinstance(pn, bool) or not math.isfinite(float(pn)) or float(pn) <= 0.0:
        raise E4AValidationError(f"BLOCKED: INVALID_APPROVAL_PRICE_NATIVE ({pn!r})")

    dec = order.token_decimals
    if dec is None or isinstance(dec, bool) or not isinstance(dec, int) or not (0 <= dec <= 18):
        raise E4AValidationError(f"BLOCKED: MISSING_OR_INVALID_TOKEN_DECIMALS ({dec!r})")


async def _fetch_rpc_block_height(config: AgentConfig, client: httpx.AsyncClient) -> int:
    rpc_url = config.SOLANA_RPC_URL.strip()
    if not rpc_url:
        raise E4AValidationError("BLOCKED: SOLANA_RPC_URL_MISSING_FOR_BLOCK_HEIGHT")
    resp = await client.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": "getBlockHeight", "params": [{"commitment": "confirmed"}]},
        timeout=config.LIVE_RPC_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or data.get("error") is not None:
        raise E4AValidationError(f"BLOCKED: RPC_BLOCK_HEIGHT_ERROR ({data})")
    bh = data.get("result")
    if isinstance(bh, bool) or not isinstance(bh, int) or bh < 0:
        raise E4AValidationError(f"BLOCKED: INVALID_RPC_BLOCK_HEIGHT ({bh!r})")
    return bh


async def _validate_e4a_quote_timing_and_expiry(
    order_resp: dict[str, Any],
    config: AgentConfig,
    *,
    quote_received_at: datetime,
    client: httpx.AsyncClient,
    stage: str,
    is_live: bool,
    now_dt: Optional[datetime] = None,
) -> None:
    """Validate quote age and router-specific expiry at receipt, pre-sign, and pre-submit checkpoints."""
    check_now = now_dt or utc_now()
    if config.MAX_QUOTE_AGE_SECONDS is not None:
        q_age = (check_now - quote_received_at).total_seconds()
        if q_age > float(config.MAX_QUOTE_AGE_SECONDS):
            raise E4AValidationError(
                f"BLOCKED: STALE_QUOTE_{stage} (age={q_age:.2f}s > {config.MAX_QUOTE_AGE_SECONDS}s)"
            )

    router = str(order_resp.get("router") or "").strip().lower()
    if router == "jupiterz":
        headroom = config.MIN_RFQ_EXPIRY_HEADROOM_SECONDS
        if is_live and headroom is None:
            raise E4AValidationError("BLOCKED: MIN_RFQ_EXPIRY_HEADROOM_SECONDS_UNSET")
        if headroom is not None or is_live:
            exp_dt = _parse_iso_utc(order_resp.get("expireAt"))
            if exp_dt is None:
                raise E4AValidationError(f"BLOCKED: MISSING_OR_INVALID_EXPIRE_AT_{stage}")
            if check_now.timestamp() >= exp_dt.timestamp() - float(headroom or 0.0):
                raise E4AValidationError(f"BLOCKED: QUOTE_EXPIRED_RFQ_{stage}")
    elif router in ("metis", "dflow", "okx") or config.MIN_BLOCK_HEIGHT_HEADROOM is not None or is_live:
        headroom_bh = config.MIN_BLOCK_HEIGHT_HEADROOM
        if is_live and headroom_bh is None:
            raise E4AValidationError("BLOCKED: MIN_BLOCK_HEIGHT_HEADROOM_UNSET")
        if headroom_bh is not None:
            lvbh_raw = order_resp.get("lastValidBlockHeight")
            if isinstance(lvbh_raw, bool) or lvbh_raw is None:
                raise E4AValidationError(f"BLOCKED: MISSING_LAST_VALID_BLOCK_HEIGHT_{stage}")
            try:
                lvbh = int(lvbh_raw)
            except (ValueError, TypeError) as exc:
                raise E4AValidationError(f"BLOCKED: INVALID_LAST_VALID_BLOCK_HEIGHT_{stage}") from exc
            if lvbh <= 0:
                raise E4AValidationError(f"BLOCKED: NONPOSITIVE_LAST_VALID_BLOCK_HEIGHT_{stage}")
            curr_bh = await _fetch_rpc_block_height(config, client)
            if curr_bh >= lvbh - int(headroom_bh):
                raise E4AValidationError(
                    f"BLOCKED: QUOTE_BLOCK_HEIGHT_EXPIRED_{stage} (curr={curr_bh} >= lvbh={lvbh} - {headroom_bh})"
                )


async def _validate_e4a_quote_receipt(
    order: PendingOrder,
    order_resp: dict[str, Any],
    out_amount: int,
    config: AgentConfig,
    *,
    quote_received_at: datetime,
    client: httpx.AsyncClient,
    now_dt: Optional[datetime] = None,
) -> None:
    """Checkpoint 1: Validate out_amount > 0, quote timing/expiry, slippage, priceImpact, and drift."""
    if out_amount <= 0:
        raise E4AValidationError(f"BLOCKED: NONPOSITIVE_OUT_AMOUNT ({out_amount})")

    if not _e4a_is_active(order, config):
        return

    is_live = config.EXECUTION_MODE == "live"
    await _validate_e4a_quote_timing_and_expiry(
        order_resp, config, quote_received_at=quote_received_at, client=client,
        stage="RECEIPT", is_live=is_live, now_dt=now_dt,
    )

    # Slippage validation (preserve RTSE; validate response slippageBps if present/configured)
    if config.MAX_ACCEPTED_SLIPPAGE_BPS is not None:
        raw_slip = order_resp.get("slippageBps")
        if raw_slip is None or isinstance(raw_slip, bool):
            if is_live:
                raise E4AValidationError("BLOCKED: MISSING_RESPONSE_SLIPPAGE_BPS")
        else:
            try:
                slip_bps = int(raw_slip)
            except (ValueError, TypeError) as exc:
                raise E4AValidationError(f"BLOCKED: INVALID_RESPONSE_SLIPPAGE_BPS ({raw_slip!r})") from exc
            if slip_bps < 0 or slip_bps > int(config.MAX_ACCEPTED_SLIPPAGE_BPS):
                raise E4AValidationError(
                    f"BLOCKED: EXCESSIVE_SLIPPAGE_BPS ({slip_bps} > {config.MAX_ACCEPTED_SLIPPAGE_BPS})"
                )

    # Price impact validation (priceImpact primary, priceImpactPct fallback; missing != 0)
    if config.MAX_ENTRY_PRICE_IMPACT_PCT is not None:
        impact_pct = _parse_e4a_price_impact_pct(order_resp)
        if impact_pct is None:
            raise E4AValidationError("BLOCKED: MISSING_OR_MALFORMED_PRICE_IMPACT")
        if abs(impact_pct) > float(config.MAX_ENTRY_PRICE_IMPACT_PCT):
            raise E4AValidationError(
                f"BLOCKED: EXCESSIVE_PRICE_IMPACT (|{impact_pct:.4f}| > {config.MAX_ENTRY_PRICE_IMPACT_PCT})"
            )

    # Entry price drift & worst-case otherAmountThreshold validation
    if any(
        getattr(config, k) is not None
        for k in ("MAX_ENTRY_PRICE_DRIFT_PCT", "MAX_NEGATIVE_ENTRY_DRIFT_PCT", "MAX_WORST_CASE_ENTRY_DRIFT_PCT")
    ):
        assert order.approval_price_native is not None and order.token_decimals is not None
        spent_sol = order.amount_atomic / LAMPORTS_PER_SOL
        quoted_tokens = out_amount / (10 ** order.token_decimals)
        if quoted_tokens <= 0.0:
            raise E4AValidationError("BLOCKED: ZERO_QUOTED_TOKENS")
        quote_price_native = spent_sol / quoted_tokens
        entry_drift_pct = ((quote_price_native - order.approval_price_native) / order.approval_price_native) * 100.0

        if config.MAX_ENTRY_PRICE_DRIFT_PCT is not None and entry_drift_pct > float(config.MAX_ENTRY_PRICE_DRIFT_PCT):
            raise E4AValidationError(
                f"BLOCKED: EXCESSIVE_ADVERSE_ENTRY_DRIFT ({entry_drift_pct:.4f}% > {config.MAX_ENTRY_PRICE_DRIFT_PCT}%)"
            )
        if config.MAX_NEGATIVE_ENTRY_DRIFT_PCT is not None and entry_drift_pct < -float(config.MAX_NEGATIVE_ENTRY_DRIFT_PCT):
            raise E4AValidationError(
                f"BLOCKED: EXCESSIVE_NEGATIVE_ENTRY_DRIFT ({entry_drift_pct:.4f}% < -{config.MAX_NEGATIVE_ENTRY_DRIFT_PCT}%)"
            )

        oat_raw = order_resp.get("otherAmountThreshold")
        if oat_raw is not None and not isinstance(oat_raw, bool):
            try:
                oat_int = int(oat_raw)
            except (ValueError, TypeError) as exc:
                raise E4AValidationError(f"BLOCKED: INVALID_OTHER_AMOUNT_THRESHOLD ({oat_raw!r})") from exc
            if oat_int <= 0:
                raise E4AValidationError(f"BLOCKED: NONPOSITIVE_OTHER_AMOUNT_THRESHOLD ({oat_int})")
            wc_tokens = oat_int / (10 ** order.token_decimals)
            wc_price_native = spent_sol / wc_tokens
            wc_drift_pct = ((wc_price_native - order.approval_price_native) / order.approval_price_native) * 100.0
            wc_limit = (
                float(config.MAX_WORST_CASE_ENTRY_DRIFT_PCT)
                if config.MAX_WORST_CASE_ENTRY_DRIFT_PCT is not None
                else (float(config.MAX_ENTRY_PRICE_DRIFT_PCT) if config.MAX_ENTRY_PRICE_DRIFT_PCT is not None else None)
            )
            if wc_limit is not None and wc_drift_pct > wc_limit:
                raise E4AValidationError(
                    f"BLOCKED: EXCESSIVE_WORST_CASE_DRIFT ({wc_drift_pct:.4f}% > {wc_limit}%)"
                )


async def fetch_live_wallet_sol_balance(
    pubkey: str,
    *,
    config: AgentConfig,
    client: httpx.AsyncClient,
) -> float:
    """Query Solana JSON-RPC getBalance(confirmed) outside db._lock and return spendable wallet SOL."""
    rpc_url = config.SOLANA_RPC_URL.strip()
    if not rpc_url or not (rpc_url.startswith("http://") or rpc_url.startswith("https://")):
        raise RuntimeError("SOLANA_RPC_URL is missing or malformed for live balance verification")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [pubkey, {"commitment": "confirmed"}],
    }
    resp = await client.post(rpc_url, json=payload, timeout=config.LIVE_RPC_TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or data.get("error") is not None:
        raise RuntimeError(f"Solana RPC getBalance error: {data.get('error') if isinstance(data, dict) else data}")
    result = data.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Solana RPC getBalance missing result object")
    lamports = result.get("value")
    if isinstance(lamports, bool) or not isinstance(lamports, int) or lamports < 0:
        raise RuntimeError(f"Solana RPC getBalance invalid lamports value: {lamports!r}")
    return lamports / LAMPORTS_PER_SOL


async def fetch_jupiter_order(
    *,
    input_mint: str,
    output_mint: str,
    amount_atomic: int,
    config: AgentConfig,
    client: httpx.AsyncClient,
    taker: Optional[str] = None,
) -> dict[str, Any]:
    """Call GET https://api.jup.ag/swap/v2/order (with or without taker)."""
    url = f"{config.JUPITER_BASE_URL}/swap/v2/order"
    params: dict[str, Any] = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(int(amount_atomic)),
    }
    if taker:
        params["taker"] = taker

    headers = {"x-api-key": config.JUPITER_API_KEY} if config.JUPITER_API_KEY else {}
    resp = await client.get(url, params=params, headers=headers, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


async def execute_jupiter_signed_order(
    *,
    signed_tx_base64: str,
    request_id: str,
    config: AgentConfig,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Call POST https://api.jup.ag/swap/v2/execute with signed base64 tx."""
    url = f"{config.JUPITER_BASE_URL}/swap/v2/execute"
    headers = {"x-api-key": config.JUPITER_API_KEY} if config.JUPITER_API_KEY else {}
    payload = {
        "signedTransaction": signed_tx_base64,
        "requestId": request_id,
    }
    resp = await client.post(url, json=payload, headers=headers, timeout=20.0)
    resp.raise_for_status()
    return resp.json()


async def executor_node(
    state: AgentGraphState,
    *,
    config: AgentConfig,
    db: AgentDatabase,
    http_client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """LangGraph node: executes a PendingOrder via Jupiter Swap API V2."""
    raw_order = state.get("pending_order")
    if not raw_order:
        return {"pending_order": None}

    if state.get("_hard_stop_already_executed"):
        st_done = db.get_state()
        return {
            "focus_state": st_done["focus_state"], "sol_balance": float(st_done["sol_balance"]),
            "daily_pnl_sol": float(st_done["daily_pnl_sol"]), "active_position": st_done["active_position"],
            "chart_positions": st_done["chart_positions"], "whale_position": st_done["whale_position"],
            "whale_positions": st_done["whale_positions"], "pending_order": None,
            "last_execution": state.get("last_execution"),
        }

    order = PendingOrder.model_validate(raw_order)
    side_up = order.side.upper()
    is_buy = side_up == "BUY"
    is_partial_sell = side_up == "PARTIAL_SELL"
    is_whale_slot = (
        str(state.get("slot") or "").lower() == "whale_slot"
        or str(order.strategy or "").upper() == "WHALE_SCALP"
    )

    # Atomic Pre-Trade Reservation & Shared Guards on BUY (under db._lock)
    st_live = db.get_state()
    chart_positions: list[dict[str, Any]] = list(st_live.get("chart_positions") or state.get("chart_positions") or [])
    whale_positions: list[dict[str, Any]] = list(st_live.get("whale_positions") or state.get("whale_positions") or [])
    if config.EXECUTION_MODE == "paper":
        tx_fee_opt: Optional[float] = float(config.PAPER_TX_FEE_SOL)
        tx_fee_status = "PAPER_ASSUMED"
        reserve_fee_sol = float(config.PAPER_TX_FEE_SOL)
    else:
        tx_fee_opt = None
        tx_fee_status = "UNKNOWN"
        # Reservation headroom only (base fee + priority fee + possible ATA rent); never persisted as observed fee
        reserve_fee_sol = float(config.LIVE_FEE_RESERVATION_BUFFER_SOL)

    taker: Optional[str] = None
    if config.EXECUTION_MODE == "live":
        keypair = Keypair.from_base58_string(config.SOLANA_PRIVATE_KEY_BS58)
        taker = str(keypair.pubkey())

    lane_tag = "whale" if is_whale_slot else "chart"
    reservation_id: Optional[str] = None
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient()
    if is_buy:
        try:
            if is_whale_slot:
                if not config.WHALE_STRATEGY_ENABLED:
                    logger.warning("[Executor] Strategy gate blocked whale_slot BUY of %s: WHALE_STRATEGY_ENABLED=False", order.symbol)
                    return {"pending_order": None}
            elif not config.is_chart_strategy_enabled(order.strategy, allow_legacy_scalper_when_all_enabled=True):
                logger.warning(
                    "[Executor] Strategy allowlist blocked chart_slot BUY of %s: strategy=%s not in CHART_ENABLED_STRATEGIES (%s)",
                    order.symbol, order.strategy, ",".join(sorted(config.chart_enabled_strategies())),
                )
                return {"pending_order": None}
            elif str(order.strategy or "").upper() == "BREAKOUT_IGNITION" and not is_valid_pool_address(order.pair_address, mint=order.mint):
                rej_msg = "BLOCKED: MISSING_OR_INVALID_POOL_IDENTITY (pair_address absent or invalid at executor)"
                logger.warning("[Executor] Pool identity guard blocked BREAKOUT_IGNITION BUY of %s: %s", order.symbol, rej_msg)
                db.log_action(
                    mode=config.EXECUTION_MODE, action="DECISION_1", symbol=order.symbol, asset_id=order.mint,
                    strategy="BREAKOUT_IGNITION", reason=rej_msg,
                    pair_address=None, dex_id=order.dex_id,
                )
                return {"pending_order": None}
            active_pos_for_reentry = state.get("active_position") or st_live.get("active_position")
            is_reentry_same_mint = (
                not is_whale_slot and order.reason == "LEG_2_REENTRY"
                and isinstance(active_pos_for_reentry, dict) and active_pos_for_reentry.get("mint") == order.mint
                and state.get("focus_state") == FocusState.WATCHING_REENTRY.value
            )
            # Also block if caller passed in-memory open mints not yet persisted
            caller_open = {
                str(p["mint"]) for p in (state.get("chart_positions") or []) + (state.get("whale_positions") or []) + [state.get("active_position"), state.get("whale_position")]
                if isinstance(p, dict) and p.get("mint")
            }
            if order.mint in caller_open and not is_reentry_same_mint:
                logger.warning("[Executor] Duplicate mint guard blocked %s BUY of %s", f"{lane_tag}_slot", order.symbol)
                return {"pending_order": None}

            # Phase E4A: Pre-quote approval provenance & threshold validation
            try:
                _validate_e4a_approval_provenance(order, config, now_dt=utc_now())
            except E4AValidationError as exc:
                logger.warning("[Executor] E4A approval guard blocked %s BUY of %s: %s", f"{lane_tag}_slot", order.symbol, exc)
                db.log_action(
                    mode=config.EXECUTION_MODE, action="DECISION_1", symbol=order.symbol, asset_id=order.mint,
                    strategy=order.strategy or "SCALPER", reason=str(exc),
                    pair_address=order.pair_address, dex_id=order.dex_id,
                )
                return {"pending_order": None}

            # Live mode: fetch actual wallet SOL balance via RPC strictly OUTSIDE db._lock; fail closed on any error
            live_wallet_sol: Optional[float] = None
            if config.EXECUTION_MODE == "live":
                assert taker is not None
                try:
                    live_wallet_sol = await fetch_live_wallet_sol_balance(taker, config=config, client=client)
                except Exception as exc:
                    logger.warning(
                        "[Executor] BLOCKED: LIVE_RPC_BALANCE_UNAVAILABLE for %s BUY of %s: %s",
                        f"{lane_tag}_slot", order.symbol, exc,
                    )
                    return {"pending_order": None}

            req_sol = order.amount_atomic / LAMPORTS_PER_SOL
            lane_cap = config.MAX_WHALE_POSITIONS if is_whale_slot else config.MAX_CHART_POSITIONS
            fb_sol_state = float(state["sol_balance"]) if "sol_balance" in state and state["sol_balance"] is not None else None
            reservation_id, rej_reason = db.reserve_buy_slot(
                lane=lane_tag, mint=order.mint, required_sol=req_sol,
                tx_fee_sol=reserve_fee_sol,
                max_lane_positions=lane_cap, max_open_positions=config.MAX_OPEN_POSITIONS,
                is_reentry_same_mint=is_reentry_same_mint, fallback_state_sol=fb_sol_state,
                daily_max_loss_pct=config.DAILY_MAX_LOSS_PCT,
                live_wallet_sol=live_wallet_sol,
                live_min_sol_reserve=float(config.LIVE_MIN_SOL_RESERVE) if config.EXECUTION_MODE == "live" else 0.0,
                balance_divergence_warn_sol=float(config.BALANCE_DIVERGENCE_WARN_SOL) if config.EXECUTION_MODE == "live" else None,
            )
            if rej_reason is not None:
                logger.warning("[Executor] Pre-trade reservation blocked %s BUY of %s: %s", f"{lane_tag}_slot", order.symbol, rej_reason)
                return {"pending_order": None}
        except BaseException:
            if owns_client:
                await client.aclose()
            raise
        finally:
            if reservation_id is None and owns_client:
                await client.aclose()

    is_exit = not is_buy
    live_lane_for_fb = whale_positions if is_whale_slot else chart_positions
    candidate_pos = (
        state.get("whale_position") if is_whale_slot else state.get("active_position")
    ) or (state.get("active_position") if is_whale_slot else None)
    existing_for_fallback: Optional[dict[str, Any]] = None
    if isinstance(candidate_pos, dict) and candidate_pos.get("mint") == order.mint:
        if not order.opened_at or not candidate_pos.get("opened_at") or candidate_pos.get("opened_at") == order.opened_at:
            existing_for_fallback = candidate_pos
    if existing_for_fallback is None:
        existing_for_fallback = next(
            (
                p for p in live_lane_for_fb
                if isinstance(p, dict) and p.get("mint") == order.mint
                and (not order.opened_at or not p.get("opened_at") or p.get("opened_at") == order.opened_at)
            ),
            None,
        )

    sell_key: Optional[tuple[str, str, str, str, str]] = None
    exit_committed = False
    if is_exit:
        opened_tag = str(order.opened_at or (existing_for_fallback.get("opened_at") if isinstance(existing_for_fallback, dict) else "") or "")
        caller_partial = bool(existing_for_fallback.get("partial_tp_taken", False)) if isinstance(existing_for_fallback, dict) else False
        sell_key, is_dup = acquire_exit_guard(
            db, lane=lane_tag, side_up=side_up, mint=order.mint, opened_at=opened_tag,
            has_caller_pos=(existing_for_fallback is not None), caller_partial_taken=caller_partial,
        )
        if is_dup:
            logger.warning("[Executor] Duplicate/already-closed %s skipped for %s (%s)", side_up, order.symbol, order.reason)
            if owns_client:
                await client.aclose()
            return {
                "focus_state": st_live["focus_state"], "sol_balance": float(st_live["sol_balance"]),
                "daily_pnl_sol": float(st_live["daily_pnl_sol"]), "active_position": st_live["active_position"],
                "chart_positions": st_live["chart_positions"], "whale_position": st_live["whale_position"],
                "whale_positions": st_live["whale_positions"], "pending_order": None,
                "last_execution": state.get("last_execution"),
            }

    input_mint = SOL_MINT if is_buy else order.mint
    output_mint = order.mint if is_buy else SOL_MINT
    broadcast_attempted = False
    settled_buy = False
    intent_id: Optional[str] = None
    buy_opened_at = utc_now_iso() if is_buy else None

    try:
        try:
            order_resp = await fetch_jupiter_order(
                input_mint=input_mint, output_mint=output_mint,
                amount_atomic=order.amount_atomic, config=config, client=client, taker=taker,
            )
            quote_received_at = utc_now()
            out_amount = int(order_resp["outAmount"])
        except Exception as exc:
            if not is_buy and config.EXECUTION_MODE != "live":
                fb_sol = float(existing_for_fallback.get("current_sol_value") or existing_for_fallback.get("entry_sol") or 0.0) if isinstance(existing_for_fallback, dict) else 0.0
                if is_partial_sell:
                    fb_frac = float((config.WHALE_PARTIAL_TP_SELL_FRACTION or config.PARTIAL_TP_SELL_FRACTION) if is_whale_slot else config.PARTIAL_TP_SELL_FRACTION)
                    fb_sol = round(fb_sol * fb_frac, 6)
                logger.warning("[Executor] Jupiter quote failed on %s %s (%s); falling back to live SOL quote %.4f", order.side, order.symbol, exc, fb_sol)
                order_resp = {"outAmount": str(max(1, int(round(fb_sol * LAMPORTS_PER_SOL)))), "transaction": None}
                quote_received_at = utc_now()
                out_amount = int(order_resp["outAmount"])
            else:
                raise
        # Checkpoint 1: Quote receipt validation (out_amount > 0 in all modes, plus E4A drift/impact/slippage/expiry)
        await _validate_e4a_quote_receipt(
            order, order_resp, out_amount, config, quote_received_at=quote_received_at, client=client,
        )
        tx_signature: Optional[str] = None
        if config.EXECUTION_MODE == "live":
            tx_b64 = order_resp.get("transaction")
            request_id = str(order_resp.get("requestId") or "")
            if not tx_b64:
                raise RuntimeError("Jupiter /swap/v2/order returned no transaction in live mode")
            # Checkpoint 2: Pre-sign quote timing & router expiry
            if _e4a_is_active(order, config):
                await _validate_e4a_quote_timing_and_expiry(
                    order_resp, config, quote_received_at=quote_received_at, client=client,
                    stage="PRE_SIGN", is_live=True,
                )
            pre_sig: Optional[str] = None
            if sign_jupiter_transaction is _ORIG_SIGN_JUPITER_TX:
                assert taker is not None
                signed_b64, pre_sig = derive_and_validate_single_signer_tx(
                    tx_b64, config.SOLANA_PRIVATE_KEY_BS58, order_resp, taker,
                )
            else:
                signed_b64 = sign_jupiter_transaction(tx_b64, config.SOLANA_PRIVATE_KEY_BS58)

            if is_buy:
                intent_id = uuid.uuid4().hex
                lvbh_raw = order_resp.get("lastValidBlockHeight")
                lvbh_val = int(lvbh_raw) if lvbh_raw is not None and not isinstance(lvbh_raw, bool) else None
                oat_raw = order_resp.get("otherAmountThreshold")
                oat_val = int(oat_raw) if oat_raw is not None and not isinstance(oat_raw, bool) else None
                db.create_order_intent(
                    intent_id=intent_id,
                    reservation_id=reservation_id,
                    lane=lane_tag,
                    mint=order.mint,
                    symbol=order.symbol,
                    pair_address=order.pair_address,
                    dex_id=order.dex_id,
                    side=order.side,
                    strategy=order.strategy or "SCALPER",
                    router=str(order_resp.get("router") or "") or None,
                    order_mode=str(order_resp.get("mode") or "") or None,
                    jupiter_request_id=request_id or None,
                    tx_signature=pre_sig,
                    last_valid_block_height=lvbh_val,
                    expire_at=str(order_resp.get("expireAt") or "") or None,
                    input_mint=input_mint,
                    output_mint=output_mint,
                    amount_atomic=order.amount_atomic,
                    expected_out_amount=out_amount,
                    other_amount_threshold=oat_val,
                    token_decimals=order.token_decimals,
                    taker_pubkey=taker,
                    reserved_sol=float(order.amount_atomic / LAMPORTS_PER_SOL) + reserve_fee_sol,
                    status="PREPARED",
                    order_snapshot={
                        "opened_at": buy_opened_at,
                        "reason": order.reason,
                        "entry_liquidity_usd": order.entry_liquidity_usd,
                        "entry_mc_usd": order.entry_mc_usd,
                        "whale_sol_flow": order.whale_sol_flow,
                        "triggering_wallets": list(order.triggering_wallets),
                        "breakout_telemetry": order.breakout_telemetry,
                        "rationale": order.rationale,
                    },
                )
                attach_intent_to_reservation(db, reservation_id, intent_id)
                # Checkpoint 3: Pre-submit quote timing & router expiry
                if _e4a_is_active(order, config):
                    await _validate_e4a_quote_timing_and_expiry(
                        order_resp, config, quote_received_at=quote_received_at, client=client,
                        stage="PRE_SUBMIT", is_live=True,
                    )
                db.transition_order_intent(
                    intent_id, new_status="SUBMITTED", expected_statuses=("PREPARED",),
                )

            broadcast_attempted = True
            exec_resp = await execute_jupiter_signed_order(
                signed_tx_base64=signed_b64, request_id=request_id, config=config, client=client,
            )
            tx_signature = str(exec_resp.get("signature") or exec_resp.get("txid") or pre_sig or "")
            if exec_resp.get("outputAmountResult") is not None and str(exec_resp.get("outputAmountResult")).strip() != "":
                out_amount = int(exec_resp["outputAmountResult"])
            if intent_id and tx_signature:
                db.transition_order_intent(intent_id, new_status="SUBMITTED", tx_signature=tx_signature)
            if is_buy and (not tx_signature or out_amount <= 0):
                raise RuntimeError(
                    f"Live BUY outcome unresolved: tx_signature={tx_signature!r}, out_amount={out_amount}"
                )
        else:
            tx_signature = f"paper-{order.side.lower()}-{order.mint[:8]}"
    except E4AValidationError as exc:
        if intent_id:
            db.transition_order_intent(
                intent_id, new_status="FAILED", error_detail=f"ABORTED_BEFORE_BROADCAST: {exc}",
            )
        if reservation_id:
            db.release_buy_reservation(reservation_id)
        finalize_exit_guard(db, sell_key, committed=False)
        logger.warning("[Executor] E4A quote guard blocked %s %s: %s", order.side, order.symbol, exc)
        db.log_action(
            mode=config.EXECUTION_MODE, action="DECISION_1" if is_buy else order.side.upper(),
            symbol=order.symbol, asset_id=order.mint,
            strategy=order.strategy or "SCALPER", reason=str(exc),
            pair_address=order.pair_address, dex_id=order.dex_id,
        )
        return {"pending_order": None}
    except BaseException as exc:
        if intent_id:
            if broadcast_attempted:
                db.transition_order_intent(
                    intent_id, new_status="UNRESOLVED", error_detail=f"{type(exc).__name__}: {exc}",
                )
            else:
                db.transition_order_intent(
                    intent_id, new_status="FAILED", error_detail=f"ABORTED_BEFORE_BROADCAST: {type(exc).__name__}: {exc}",
                )
        if reservation_id:
            if broadcast_attempted:
                mark_buy_reservation_broadcast_unresolved(db, reservation_id, reason=f"{type(exc).__name__}: {exc}")
            else:
                db.release_buy_reservation(reservation_id)
        finalize_exit_guard(db, sell_key, committed=False)
        raise
    finally:
        if owns_client:
            await client.aclose()

    fb_base_sol = float(state["sol_balance"]) if "sol_balance" in state and state["sol_balance"] is not None else None
    fb_base_pnl = float(state["daily_pnl_sol"]) if "daily_pnl_sol" in state and state["daily_pnl_sol"] is not None else None
    raw_price_impact = extract_raw_price_impact(order_resp)
    existing_pos_dict = existing_for_fallback
    new_pos_dict: Optional[dict[str, Any]] = None
    next_state = order.target_state_after_fill.value
    rationale_out = order.rationale or state.get("last_decision_rationale")

    if is_buy:
        try:
            spent_sol = order.amount_atomic / LAMPORTS_PER_SOL
            sol_delta = -(spent_sol + tx_fee_opt) if tx_fee_opt is not None else -spent_sol
            pnl_delta = -tx_fee_opt if tx_fee_opt is not None else 0.0
            pos_strategy = order.strategy or "SCALPER"
            prev_reentries = (
                int(existing_pos_dict.get("reentry_count") or 0)
                if isinstance(existing_pos_dict, dict) and existing_pos_dict.get("mint") == order.mint
                else 0
            )
            next_reentry_count = (prev_reentries + 1) if order.reason == "LEG_2_REENTRY" else 0
            pos = ActivePosition(
                mint=order.mint, symbol=order.symbol, entry_sol=spent_sol, token_amount=out_amount,
                pair_address=order.pair_address, dex_id=order.dex_id,
                strategy=pos_strategy, triggering_wallets=list(order.triggering_wallets),
                whale_shadow_exit_flag=False, opened_at=buy_opened_at or utc_now_iso(),
                entry_liquidity_usd=order.entry_liquidity_usd, entry_mc_usd=order.entry_mc_usd,
                whale_sol_flow=order.whale_sol_flow, breakout_telemetry=order.breakout_telemetry,
                current_sol_value=spent_sol, peak_sol_value=spent_sol,
                partial_tp_taken=False, initial_entry_sol=spent_sol,
                remaining_cost_basis_sol=spent_sol, realized_partial_pnl_sol=0.0,
                entry_fee_sol=tx_fee_opt,
                accumulated_fees_sol=round(tx_fee_opt, 6) if tx_fee_opt is not None else 0.0,
                fee_accounting_status=tx_fee_status,
                breakeven_armed=False, trail_armed=False, retrace_low_sol=None, run_peak_sol=None,
                watch_started_at=None, reference_token_amount=out_amount,
                reentry_count=next_reentry_count, last_liquidity_usd=order.entry_liquidity_usd,
            )
            new_pos_dict = pos.model_dump()
            db.remove_from_watchlist(order.mint)
            settled = db.apply_trade_settlement(
                lane=lane_tag, side="BUY", mint=order.mint, opened_at=pos.opened_at, new_pos_dict=new_pos_dict,
                sol_delta=sol_delta, pnl_delta=pnl_delta, focus_state=next_state,
                last_decision_rationale=rationale_out, reservation_id=reservation_id,
                intent_id=intent_id, intent_final_status="CONFIRMED",
                fallback_base_sol=fb_base_sol, fallback_base_pnl=fb_base_pnl,
            )
            settled_buy = True
            sol_balance, daily_pnl_sol = settled["sol_balance"], settled["daily_pnl_sol"]
        except BaseException as exc:
            if intent_id and not settled_buy:
                if broadcast_attempted:
                    snap_retry = {
                        "opened_at": buy_opened_at,
                        "reason": order.reason,
                        "entry_liquidity_usd": order.entry_liquidity_usd,
                        "entry_mc_usd": order.entry_mc_usd,
                        "whale_sol_flow": order.whale_sol_flow,
                        "triggering_wallets": list(order.triggering_wallets),
                        "breakout_telemetry": order.breakout_telemetry,
                        "rationale": order.rationale,
                        "reconciled_out_tokens": out_amount,
                        "reconciled_entry_sol": order.amount_atomic / LAMPORTS_PER_SOL,
                    }
                    db.update_order_intent_snapshot(intent_id, snap_retry)
                    db.transition_order_intent(
                        intent_id, new_status="SETTLEMENT_PENDING_PERSISTENCE",
                        error_detail=f"post-broadcast settlement error: {type(exc).__name__}: {exc}",
                    )
                else:
                    db.transition_order_intent(
                        intent_id, new_status="FAILED", error_detail=f"pre-broadcast error: {type(exc).__name__}: {exc}",
                    )
            if reservation_id and not settled_buy:
                if broadcast_attempted:
                    mark_buy_reservation_broadcast_unresolved(db, reservation_id, reason=f"post-broadcast settlement error: {type(exc).__name__}: {exc}")
                else:
                    db.release_buy_reservation(reservation_id)
            raise

        db.log_trade(
            mode=config.EXECUTION_MODE, side="BUY", mint=order.mint, symbol=order.symbol,
            in_amount=order.amount_atomic, out_amount=out_amount, sol_value=spent_sol,
            reason=order.reason, tx_signature=tx_signature, rationale=order.rationale,
            strategy=pos_strategy, triggering_wallets=list(order.triggering_wallets),
            opened_at=pos.opened_at,
            jupiter_price_impact_raw=raw_price_impact,
            pair_address=pos.pair_address, dex_id=pos.dex_id,
            tx_fee_sol=tx_fee_opt, fee_status=tx_fee_status,
        )
        action_type = "REENTRY_BUY" if order.reason == "LEG_2_REENTRY" else "BUY"
        bt = order.breakout_telemetry or {}
        db.log_action(
            mode=config.EXECUTION_MODE, action=action_type, symbol=order.symbol, asset_id=order.mint,
            qty=float(out_amount), notional=round(spent_sol, 6), price_or_value=round(spent_sol, 6),
            strategy=pos_strategy, reason=order.reason,
            sol_equity=round(sol_balance, 6), daily_realized_sol=round(daily_pnl_sol, 6),
            mc_usd=order.entry_mc_usd,
            liq_usd=round(order.entry_liquidity_usd, 2) if order.entry_liquidity_usd else None,
            triggering_whales=",".join(order.triggering_wallets) if order.triggering_wallets else None,
            whale_sol_flow=order.whale_sol_flow,
            breakout_score=bt.get("score"), breakout_state=bt.get("state"),
            breakout_timeframe=bt.get("timeframe"), breakout_vol_ratio=bt.get("vol_ratio"),
            breakout_ema_ext_pct=bt.get("ema_ext_pct"),
            breakout_failed_gates="; ".join(bt.get("failed_gates") or []) if bt else None,
            jupiter_price_impact_raw=raw_price_impact,
            pair_address=pos.pair_address, dex_id=pos.dex_id,
        )
        logger.info("[%s] BUY %s: %.4f SOL -> %d tokens (reason=%s)", config.EXECUTION_MODE.upper(), order.symbol, spent_sol, out_amount, order.reason)
    elif is_partial_sell:
        received_sol = out_amount / LAMPORTS_PER_SOL
        default_entry = config.WHALE_POSITION_SOL if is_whale_slot else config.TRADE_SIZE_SOL
        pos_obj = ActivePosition.model_validate(existing_pos_dict) if isinstance(existing_pos_dict, dict) else ActivePosition(mint=order.mint, symbol=order.symbol, entry_sol=default_entry, token_amount=order.amount_atomic * 2)
        frac = float((config.WHALE_PARTIAL_TP_SELL_FRACTION or config.PARTIAL_TP_SELL_FRACTION) if is_whale_slot else config.PARTIAL_TP_SELL_FRACTION)
        cost_of_sold = round(pos_obj.initial_entry_sol * frac, 6)
        partial_pnl = round(received_sol - cost_of_sold - tx_fee_opt, 6) if tx_fee_opt is not None else round(received_sol - cost_of_sold, 6)
        partial_pnl_pct = (partial_pnl / cost_of_sold) if cost_of_sold > 0 else 0.0
        sol_delta = (received_sol - tx_fee_opt) if tx_fee_opt is not None else received_sol

        rem_tokens = max(0, int(pos_obj.token_amount) - int(order.amount_atomic))
        rem_basis = round(max(0.0, pos_obj.remaining_cost_basis_sol - cost_of_sold), 6)
        rem_sol_val = round(max(0.0, pos_obj.current_sol_value - received_sol), 6) if pos_obj.current_sol_value > received_sol else round(received_sol * ((1.0 - frac) / max(frac, 1e-9)), 6)
        pos_obj.token_amount = rem_tokens
        pos_obj.remaining_cost_basis_sol = rem_basis
        pos_obj.partial_tp_taken = True
        pos_obj.realized_partial_pnl_sol = round(pos_obj.realized_partial_pnl_sol + partial_pnl, 6)
        if pos_obj.fee_accounting_status == tx_fee_status and tx_fee_opt is not None:
            pos_obj.accumulated_fees_sol = round(float(pos_obj.accumulated_fees_sol or 0.0) + tx_fee_opt, 6)
        else:
            pos_obj.fee_accounting_status = "UNKNOWN"
        pos_obj.breakeven_armed = True
        pos_obj.trail_armed = True
        pos_obj.current_sol_value = rem_sol_val
        pos_obj.peak_sol_value = rem_sol_val
        pos_obj.pnl_sol = round(rem_sol_val - rem_basis, 6)
        pos_obj.pnl_pct = round(((rem_sol_val - rem_basis) / rem_basis) * 100.0, 2) if rem_basis > 0 else 0.0
        new_pos_dict = pos_obj.model_dump()
        next_state = FocusState.IN_POSITION.value
        pos_strategy = pos_obj.strategy or order.strategy or "SCALPER"
        bt = pos_obj.breakout_telemetry or order.breakout_telemetry or {}
        settled = db.apply_trade_settlement(
            lane=lane_tag, side="PARTIAL_SELL", mint=order.mint, opened_at=pos_obj.opened_at, new_pos_dict=new_pos_dict,
            sol_delta=sol_delta, pnl_delta=partial_pnl, focus_state=next_state,
            last_decision_rationale=rationale_out, fallback_base_sol=fb_base_sol, fallback_base_pnl=fb_base_pnl,
        )
        sol_balance, daily_pnl_sol = settled["sol_balance"], settled["daily_pnl_sol"]
        new_pos_dict = next((p for p in settled["whale_positions" if is_whale_slot else "chart_positions"] if p.get("mint") == order.mint), new_pos_dict)

        db.log_trade(
            mode=config.EXECUTION_MODE, side="PARTIAL_SELL", mint=order.mint, symbol=order.symbol,
            in_amount=order.amount_atomic, out_amount=out_amount,
            sol_value=received_sol, pnl_sol=partial_pnl, pnl_pct=partial_pnl_pct,
            reason=order.reason, tx_signature=tx_signature, rationale=order.rationale,
            strategy=pos_strategy, triggering_wallets=list(pos_obj.triggering_wallets), opened_at=pos_obj.opened_at,
            jupiter_price_impact_raw=raw_price_impact,
            pair_address=pos_obj.pair_address, dex_id=pos_obj.dex_id,
            tx_fee_sol=tx_fee_opt, fee_status=tx_fee_status,
        )
        db.log_action(
            mode=config.EXECUTION_MODE, action="PARTIAL_SELL", symbol=order.symbol, asset_id=order.mint,
            qty=float(order.amount_atomic), notional=round(received_sol, 6), price_or_value=round(received_sol, 6),
            pnl_amount=partial_pnl, pnl_pct=round(partial_pnl_pct * 100.0, 2),
            strategy=pos_strategy, reason=order.reason,
            sol_equity=round(sol_balance, 6), daily_realized_sol=round(daily_pnl_sol, 6),
            mc_usd=pos_obj.entry_mc_usd, liq_usd=round(float(pos_obj.last_liquidity_usd or pos_obj.entry_liquidity_usd or 0.0), 2) or None,
            peak_pnl_pct=round(partial_pnl_pct * 100.0, 2),
            triggering_whales=",".join(pos_obj.triggering_wallets) if pos_obj.triggering_wallets else None,
            whale_sol_flow=pos_obj.whale_sol_flow,
            breakout_score=bt.get("score"), breakout_state=bt.get("state"), breakout_timeframe=bt.get("timeframe"),
            breakout_vol_ratio=bt.get("vol_ratio"), breakout_ema_ext_pct=bt.get("ema_ext_pct"),
            breakout_failed_gates="; ".join(bt.get("failed_gates") or []) if bt else None,
            jupiter_price_impact_raw=raw_price_impact, monitor_sol_value=order.monitor_sol_value,
            price_source=order.price_source or pos_obj.last_price_source,
            position_mfe_pct=pos_obj.position_mfe_pct, position_mae_pct=pos_obj.position_mae_pct,
            pair_address=pos_obj.pair_address, dex_id=pos_obj.dex_id,
        )
        exit_committed = True
        finalize_exit_guard(db, sell_key, committed=True)
        logger.info(
            "[%s] PARTIAL_SELL %s: %d tokens -> %.4f SOL (banked %+.4f SOL / %+.2f%%, rem_tokens=%d, rem_basis=%.4f SOL)",
            config.EXECUTION_MODE.upper(), order.symbol, order.amount_atomic, received_sol, partial_pnl, partial_pnl_pct * 100.0, rem_tokens, rem_basis,
        )
    else:
        received_sol = out_amount / LAMPORTS_PER_SOL
        sol_delta = (received_sol - tx_fee_opt) if tx_fee_opt is not None else received_sol
        ex_dict = existing_pos_dict if isinstance(existing_pos_dict, dict) else {}
        entry_sol = float(ex_dict.get("entry_sol", 0.0))
        partial_taken = bool(ex_dict.get("partial_tp_taken", False))
        rem_basis_val = float(ex_dict.get("remaining_cost_basis_sol") or 0.0)
        cost_basis = rem_basis_val if (partial_taken and rem_basis_val > 0) else entry_sol
        if tx_fee_opt is not None:
            pnl_sol = round((received_sol - cost_basis - tx_fee_opt) if cost_basis > 0 else -tx_fee_opt, 6)
        else:
            pnl_sol = round((received_sol - cost_basis) if cost_basis > 0 else 0.0, 6)
        pnl_pct = (pnl_sol / cost_basis) if cost_basis > 0 else 0.0

        existing_strat = str(ex_dict.get("strategy") or "").strip()
        db_buy_strat = db.get_latest_buy_strategy(order.mint)
        pos_strategy = (
            order.strategy if (order.strategy and order.strategy != "SCALPER")
            else (existing_strat if (existing_strat and existing_strat != "SCALPER") else (db_buy_strat or existing_strat or order.strategy or "SCALPER"))
        )
        pos_trig_wallets = list(ex_dict.get("triggering_wallets") or order.triggering_wallets or [])
        pos_opened_at = ex_dict.get("opened_at")
        peak_sol_val = float(ex_dict.get("peak_sol_value") or 0.0)
        peak_pnl_pct_val = round(((peak_sol_val - cost_basis) / cost_basis) * 100.0, 2) if cost_basis > 0 and peak_sol_val > 0 else None
        hold_sec_val: Optional[float] = None
        if pos_opened_at:
            try:
                o_dt = datetime.fromisoformat(str(pos_opened_at))
                if o_dt.tzinfo is None:
                    o_dt = o_dt.replace(tzinfo=timezone.utc)
                hold_sec_val = round(max(0.0, (utc_now() - o_dt).total_seconds()), 1)
            except ValueError:
                hold_sec_val = None
        pos_liq_usd = float(ex_dict.get("last_liquidity_usd") or ex_dict.get("entry_liquidity_usd") or 0.0)
        pos_pair_address = (
            str(ex_dict.get("pair_address") or order.pair_address or "").strip() or None
        )
        pos_dex_id = (
            str(ex_dict.get("dex_id") or order.dex_id or "").strip() or None
        )
        if order.target_state_after_fill == FocusState.WATCHING_REENTRY:
            prev_peak = order.run_peak_sol or float(ex_dict.get("peak_sol_value", received_sol))
            prev_liq = float(ex_dict.get("entry_liquidity_usd", 0.0))
            prev_reentry_count = int(ex_dict.get("reentry_count") or 0)
            watch_pos = ActivePosition(
                mint=order.mint, symbol=order.symbol, entry_sol=entry_sol, token_amount=0,
                pair_address=pos_pair_address, dex_id=pos_dex_id,
                opened_at=pos_opened_at,
                strategy=pos_strategy, entry_liquidity_usd=prev_liq, current_sol_value=received_sol,
                peak_sol_value=prev_peak, breakeven_armed=False, trail_armed=False,
                retrace_low_sol=received_sol, run_peak_sol=prev_peak, watch_started_at=utc_now_iso(),
                reference_token_amount=order.amount_atomic, reentry_count=prev_reentry_count,
                last_liquidity_usd=prev_liq,
            )
            new_pos_dict = watch_pos.model_dump()
        else:
            new_pos_dict = None
            if order.apply_cooldown:
                db.set_cooldown(order.mint, hours=config.COOLDOWN_HOURS, reason=order.reason)

        settled = db.apply_trade_settlement(
            lane=lane_tag, side="SELL", mint=order.mint, opened_at=str(pos_opened_at or ""), new_pos_dict=new_pos_dict,
            sol_delta=sol_delta, pnl_delta=pnl_sol, focus_state=next_state,
            last_decision_rationale=rationale_out, fallback_base_sol=fb_base_sol, fallback_base_pnl=fb_base_pnl,
        )
        sol_balance, daily_pnl_sol = settled["sol_balance"], settled["daily_pnl_sol"]
        next_state = settled["focus_state"]

        db.log_trade(
            mode=config.EXECUTION_MODE, side="SELL", mint=order.mint, symbol=order.symbol,
            in_amount=order.amount_atomic, out_amount=out_amount, sol_value=received_sol,
            pnl_sol=pnl_sol, pnl_pct=pnl_pct, reason=order.reason, tx_signature=tx_signature,
            rationale=order.rationale, strategy=pos_strategy,
            triggering_wallets=pos_trig_wallets, opened_at=pos_opened_at,
            jupiter_price_impact_raw=raw_price_impact,
            pair_address=pos_pair_address, dex_id=pos_dex_id,
            tx_fee_sol=tx_fee_opt, fee_status=tx_fee_status,
        )
        pos_mc_usd = order.entry_mc_usd if order.entry_mc_usd is not None else ex_dict.get("entry_mc_usd")
        pos_whale_flow = order.whale_sol_flow if order.whale_sol_flow is not None else ex_dict.get("whale_sol_flow")
        sell_bt = order.breakout_telemetry or ex_dict.get("breakout_telemetry") or {}
        db.log_action(
            mode=config.EXECUTION_MODE, action="SELL", symbol=order.symbol, asset_id=order.mint,
            qty=float(order.amount_atomic), notional=round(received_sol, 6), price_or_value=round(received_sol, 6),
            pnl_amount=round(pnl_sol, 6), pnl_pct=round(pnl_pct * 100.0, 2),
            strategy=pos_strategy, reason=order.reason,
            sol_equity=round(sol_balance, 6), daily_realized_sol=round(daily_pnl_sol, 6),
            mc_usd=pos_mc_usd, liq_usd=round(pos_liq_usd, 2) if pos_liq_usd else None,
            peak_pnl_pct=peak_pnl_pct_val, hold_seconds=hold_sec_val,
            triggering_whales=",".join(pos_trig_wallets) if pos_trig_wallets else None,
            whale_sol_flow=pos_whale_flow,
            breakout_score=sell_bt.get("score"), breakout_state=sell_bt.get("state"),
            breakout_timeframe=sell_bt.get("timeframe"), breakout_vol_ratio=sell_bt.get("vol_ratio"),
            breakout_ema_ext_pct=sell_bt.get("ema_ext_pct"),
            breakout_failed_gates="; ".join(sell_bt.get("failed_gates") or []) if sell_bt else None,
            jupiter_price_impact_raw=raw_price_impact, monitor_sol_value=order.monitor_sol_value,
            price_source=order.price_source or ex_dict.get("last_price_source"),
            position_mfe_pct=ex_dict.get("position_mfe_pct"), position_mae_pct=ex_dict.get("position_mae_pct"),
            pair_address=pos_pair_address, dex_id=pos_dex_id,
        )
        exit_committed = True
        finalize_exit_guard(db, sell_key, committed=True)
        logger.info("[%s] SELL %s: %d tokens -> %.4f SOL (PnL %+.4f SOL / %+.2f%%, reason=%s -> %s)", config.EXECUTION_MODE.upper(), order.symbol, order.amount_atomic, received_sol, pnl_sol, pnl_pct * 100.0, order.reason, next_state)

    last_exec = {
        "side": order.side, "mint": order.mint, "symbol": order.symbol,
        "in_amount": order.amount_atomic, "out_amount": out_amount,
        "tx_signature": tx_signature, "reason": order.reason,
        "jupiter_price_impact_raw": raw_price_impact,
        "tx_fee_sol": tx_fee_opt, "fee_status": tx_fee_status,
        "fee_accounting_incomplete": tx_fee_opt is None,
    }
    if is_whale_slot:
        return {
            "focus_state": state.get("focus_state", settled["focus_state"]),
            "sol_balance": sol_balance, "daily_pnl_sol": daily_pnl_sol,
            "whale_position": settled["whale_position"], "whale_positions": settled["whale_positions"],
            "pending_order": None, "last_execution": last_exec,
        }
    return {
        "focus_state": settled["focus_state"], "sol_balance": sol_balance, "daily_pnl_sol": daily_pnl_sol,
        "active_position": settled["active_position"], "chart_positions": settled["chart_positions"],
        "pending_order": None, "last_execution": last_exec,
    }
