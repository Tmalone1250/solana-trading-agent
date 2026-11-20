"""Jupiter Swap API V2 Executor (executor_node.py)."""

from __future__ import annotations

import base64
import logging
from typing import Any, Optional

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase, utc_now_iso
from state import ActivePosition, AgentGraphState, FocusState, PendingOrder

logger = logging.getLogger(__name__)


def sign_jupiter_transaction(tx_base64: str, private_key_bs58: str) -> str:
    """Deserialize a base64 VersionedTransaction, sign with Keypair, return base64."""
    keypair = Keypair.from_base58_string(private_key_bs58)
    raw_tx = base64.b64decode(tx_base64)
    tx = VersionedTransaction.from_bytes(raw_tx)
    signed_tx = VersionedTransaction(tx.message, [keypair])
    return base64.b64encode(bytes(signed_tx)).decode("ascii")


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

    order = PendingOrder.model_validate(raw_order)
    is_buy = order.side.upper() == "BUY"

    input_mint = SOL_MINT if is_buy else order.mint
    output_mint = order.mint if is_buy else SOL_MINT

    taker: Optional[str] = None
    if config.EXECUTION_MODE == "live":
        keypair = Keypair.from_base58_string(config.SOLANA_PRIVATE_KEY_BS58)
        taker = str(keypair.pubkey())

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient()
    try:
        order_resp = await fetch_jupiter_order(
            input_mint=input_mint,
            output_mint=output_mint,
            amount_atomic=order.amount_atomic,
            config=config,
            client=client,
            taker=taker,
        )

        out_amount = int(order_resp["outAmount"])
        tx_signature: Optional[str] = None

        if config.EXECUTION_MODE == "live":
            tx_b64 = order_resp.get("transaction")
            request_id = str(order_resp.get("requestId") or "")
            if not tx_b64:
                raise RuntimeError("Jupiter /swap/v2/order returned no transaction in live mode")
            signed_b64 = sign_jupiter_transaction(tx_b64, config.SOLANA_PRIVATE_KEY_BS58)
            exec_resp = await execute_jupiter_signed_order(
                signed_tx_base64=signed_b64,
                request_id=request_id,
                config=config,
                client=client,
            )
            tx_signature = str(exec_resp.get("signature") or exec_resp.get("txid") or "")
            if exec_resp.get("outputAmountResult"):
                out_amount = int(exec_resp["outputAmountResult"])
        else:
            tx_signature = f"paper-{order.side.lower()}-{order.mint[:8]}"
    finally:
        if owns_client:
            await client.aclose()

    sol_balance = float(state.get("sol_balance") or 0.0)
    daily_pnl_sol = float(state.get("daily_pnl_sol") or 0.0)
    existing_pos_dict = state.get("active_position")
    new_pos_dict: Optional[dict[str, Any]] = None
    next_state = order.target_state_after_fill.value

    if is_buy:
        spent_sol = order.amount_atomic / LAMPORTS_PER_SOL
        sol_balance = max(0.0, sol_balance - spent_sol)
        pos = ActivePosition(
            mint=order.mint,
            symbol=order.symbol,
            entry_sol=spent_sol,
            token_amount=out_amount,
            entry_liquidity_usd=order.entry_liquidity_usd,
            current_sol_value=spent_sol,
            peak_sol_value=spent_sol,
            breakeven_armed=False,
            trail_armed=False,
            retrace_low_sol=None,
            run_peak_sol=None,
            watch_started_at=None,
            reference_token_amount=out_amount,
            last_liquidity_usd=order.entry_liquidity_usd,
        )
        new_pos_dict = pos.model_dump()
        db.remove_from_watchlist(order.mint)

        db.log_trade(
            mode=config.EXECUTION_MODE,
            side="BUY",
            mint=order.mint,
            symbol=order.symbol,
            in_amount=order.amount_atomic,
            out_amount=out_amount,
            sol_value=spent_sol,
            reason=order.reason,
            tx_signature=tx_signature,
            rationale=order.rationale,
        )
        logger.info(
            "[%s] BUY %s: %.4f SOL -> %d tokens (reason=%s)",
            config.EXECUTION_MODE.upper(),
            order.symbol,
            spent_sol,
            out_amount,
            order.reason,
        )
    else:
        received_sol = out_amount / LAMPORTS_PER_SOL
        sol_balance += received_sol
        entry_sol = (
            float(existing_pos_dict.get("entry_sol", 0.0))
            if isinstance(existing_pos_dict, dict)
            else 0.0
        )
        pnl_sol = received_sol - entry_sol if entry_sol > 0 else 0.0
        pnl_pct = (pnl_sol / entry_sol) if entry_sol > 0 else 0.0
        daily_pnl_sol += pnl_sol

        db.log_trade(
            mode=config.EXECUTION_MODE,
            side="SELL",
            mint=order.mint,
            symbol=order.symbol,
            in_amount=order.amount_atomic,
            out_amount=out_amount,
            sol_value=received_sol,
            pnl_sol=pnl_sol,
            pnl_pct=pnl_pct,
            reason=order.reason,
            tx_signature=tx_signature,
            rationale=order.rationale,
        )

        if order.target_state_after_fill == FocusState.WATCHING_REENTRY:
            prev_peak = order.run_peak_sol or (
                float(existing_pos_dict.get("peak_sol_value", received_sol))
                if isinstance(existing_pos_dict, dict)
                else received_sol
            )
            prev_liq = (
                float(existing_pos_dict.get("entry_liquidity_usd", 0.0))
                if isinstance(existing_pos_dict, dict)
                else 0.0
            )
            watch_pos = ActivePosition(
                mint=order.mint,
                symbol=order.symbol,
                entry_sol=entry_sol,
                token_amount=0,
                entry_liquidity_usd=prev_liq,
                current_sol_value=received_sol,
                peak_sol_value=prev_peak,
                breakeven_armed=False,
                trail_armed=False,
                retrace_low_sol=received_sol,
                run_peak_sol=prev_peak,
                watch_started_at=utc_now_iso(),
                reference_token_amount=order.amount_atomic,
                last_liquidity_usd=prev_liq,
            )
            new_pos_dict = watch_pos.model_dump()
        else:
            new_pos_dict = None
            if order.apply_cooldown:
                db.set_cooldown(order.mint, hours=config.COOLDOWN_HOURS, reason=order.reason)

        logger.info(
            "[%s] SELL %s: %d tokens -> %.4f SOL (PnL %+.4f SOL / %+.2f%%, reason=%s -> %s)",
            config.EXECUTION_MODE.upper(),
            order.symbol,
            order.amount_atomic,
            received_sol,
            pnl_sol,
            pnl_pct * 100.0,
            order.reason,
            next_state,
        )

    db.save_state(
        focus_state=next_state,
        sol_balance=sol_balance,
        daily_pnl_sol=daily_pnl_sol,
        active_position=new_pos_dict,
        last_decision_rationale=order.rationale or state.get("last_decision_rationale"),
    )

    return {
        "focus_state": next_state,
        "sol_balance": sol_balance,
        "daily_pnl_sol": daily_pnl_sol,
        "active_position": new_pos_dict,
        "pending_order": None,
        "last_execution": {
            "side": order.side,
            "mint": order.mint,
            "symbol": order.symbol,
            "in_amount": order.amount_atomic,
            "out_amount": out_amount,
            "tx_signature": tx_signature,
            "reason": order.reason,
        },
    }
