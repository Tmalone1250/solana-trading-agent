"""Jupiter Swap API V2 Executor (executor_node.py)."""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase, utc_now, utc_now_iso
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
    is_whale_slot = (
        str(state.get("slot") or "").lower() == "whale_slot"
        or str(order.strategy or "").upper() == "WHALE_SCALP"
    )

    # Shared Guards on BUY: No Duplicate Mint, 3+3 Lane Capacity, Global Cap (6), and Free SOL Check
    st_live = db.get_state()
    chart_positions: list[dict[str, Any]] = list(state.get("chart_positions") if "chart_positions" in state else (st_live.get("chart_positions") or []))
    whale_positions: list[dict[str, Any]] = list(state.get("whale_positions") if "whale_positions" in state else (st_live.get("whale_positions") or []))
    if is_buy:
        active_pos_for_reentry = state.get("active_position") or st_live.get("active_position")
        is_reentry_same_mint = (
            not is_whale_slot
            and order.reason == "LEG_2_REENTRY"
            and isinstance(active_pos_for_reentry, dict)
            and active_pos_for_reentry.get("mint") == order.mint
            and state.get("focus_state") == FocusState.WATCHING_REENTRY.value
        )
        open_mints = db.get_all_open_mints()
        for p in chart_positions + whale_positions + [state.get("active_position"), state.get("whale_position")]:
            if isinstance(p, dict) and p.get("mint"):
                open_mints.add(str(p["mint"]))
        if order.mint in open_mints and not is_reentry_same_mint:
            logger.warning("[Executor] Duplicate mint guard blocked %s BUY of %s", "whale_slot" if is_whale_slot else "chart_slot", order.symbol)
            return {"pending_order": None}

        lane_count = len(whale_positions) if is_whale_slot else len(chart_positions)
        lane_cap = config.MAX_WHALE_POSITIONS if is_whale_slot else config.MAX_CHART_POSITIONS
        total_open = len(chart_positions) + len(whale_positions)
        if lane_count >= lane_cap or total_open >= config.MAX_OPEN_POSITIONS:
            logger.warning("[Executor] Lane capacity reached (chart=%d/%d, whale=%d/%d, total=%d/%d) — skipping %s", len(chart_positions), config.MAX_CHART_POSITIONS, len(whale_positions), config.MAX_WHALE_POSITIONS, total_open, config.MAX_OPEN_POSITIONS, order.symbol)
            return {"pending_order": None}

        req_sol = order.amount_atomic / LAMPORTS_PER_SOL
        avail_sol = float(state.get("sol_balance") or 0.0)
        if avail_sol + 1e-9 < req_sol:
            logger.warning("[Executor] Insufficient free SOL (%.4f < %.4f) for %s BUY", avail_sol, req_sol, order.symbol)
            return {"pending_order": None}

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
    existing_pos_dict = (
        state.get("whale_position") if is_whale_slot else state.get("active_position")
    )
    if existing_pos_dict is None and is_whale_slot:
        existing_pos_dict = state.get("active_position")
    new_pos_dict: Optional[dict[str, Any]] = None
    next_state = order.target_state_after_fill.value

    if is_buy:
        spent_sol = order.amount_atomic / LAMPORTS_PER_SOL
        sol_balance = max(0.0, sol_balance - spent_sol)
        pos_strategy = order.strategy or "SCALPER"
        prev_reentries = (
            int(existing_pos_dict.get("reentry_count") or 0)
            if isinstance(existing_pos_dict, dict) and existing_pos_dict.get("mint") == order.mint
            else 0
        )
        next_reentry_count = (prev_reentries + 1) if order.reason == "LEG_2_REENTRY" else 0
        pos = ActivePosition(
            mint=order.mint,
            symbol=order.symbol,
            entry_sol=spent_sol,
            token_amount=out_amount,
            strategy=pos_strategy,
            triggering_wallets=list(order.triggering_wallets),
            whale_shadow_exit_flag=False,
            opened_at=utc_now_iso(),
            entry_liquidity_usd=order.entry_liquidity_usd,
            entry_mc_usd=order.entry_mc_usd,
            whale_sol_flow=order.whale_sol_flow,
            breakout_telemetry=order.breakout_telemetry,
            current_sol_value=spent_sol,
            peak_sol_value=spent_sol,
            breakeven_armed=False,
            trail_armed=False,
            retrace_low_sol=None,
            run_peak_sol=None,
            watch_started_at=None,
            reference_token_amount=out_amount,
            reentry_count=next_reentry_count,
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
            strategy=pos_strategy,
            triggering_wallets=list(order.triggering_wallets),
        )
        action_type = "REENTRY_BUY" if order.reason == "LEG_2_REENTRY" else "BUY"
        bt = order.breakout_telemetry or {}
        db.log_action(
            mode=config.EXECUTION_MODE,
            action=action_type,
            symbol=order.symbol,
            asset_id=order.mint,
            qty=float(out_amount),
            notional=round(spent_sol, 6),
            price_or_value=round(spent_sol, 6),
            strategy=pos_strategy,
            reason=order.reason,
            sol_equity=round(sol_balance, 6),
            daily_realized_sol=round(daily_pnl_sol, 6),
            mc_usd=order.entry_mc_usd,
            liq_usd=round(order.entry_liquidity_usd, 2) if order.entry_liquidity_usd else None,
            triggering_whales=",".join(order.triggering_wallets) if order.triggering_wallets else None,
            whale_sol_flow=order.whale_sol_flow,
            breakout_score=bt.get("score"),
            breakout_state=bt.get("state"),
            breakout_timeframe=bt.get("timeframe"),
            breakout_vol_ratio=bt.get("vol_ratio"),
            breakout_ema_ext_pct=bt.get("ema_ext_pct"),
            breakout_failed_gates="; ".join(bt.get("failed_gates") or []) if bt else None,
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

        existing_strat = (
            str(existing_pos_dict.get("strategy") or "").strip()
            if isinstance(existing_pos_dict, dict)
            else ""
        )
        db_buy_strat = db.get_latest_buy_strategy(order.mint)
        if order.strategy and order.strategy != "SCALPER":
            pos_strategy = order.strategy
        elif existing_strat and existing_strat != "SCALPER":
            pos_strategy = existing_strat
        elif db_buy_strat:
            pos_strategy = db_buy_strat
        else:
            pos_strategy = existing_strat or order.strategy or "SCALPER"

        pos_trig_wallets = (
            list(existing_pos_dict.get("triggering_wallets") or order.triggering_wallets or [])
            if isinstance(existing_pos_dict, dict)
            else list(order.triggering_wallets or [])
        )
        pos_opened_at = (
            existing_pos_dict.get("opened_at")
            if isinstance(existing_pos_dict, dict)
            else None
        )
        peak_sol_val = (
            float(existing_pos_dict.get("peak_sol_value") or 0.0)
            if isinstance(existing_pos_dict, dict)
            else 0.0
        )
        peak_pnl_pct_val = (
            round(((peak_sol_val - entry_sol) / entry_sol) * 100.0, 2)
            if entry_sol > 0 and peak_sol_val > 0
            else None
        )
        hold_sec_val: Optional[float] = None
        if pos_opened_at:
            try:
                o_dt = datetime.fromisoformat(str(pos_opened_at))
                if o_dt.tzinfo is None:
                    o_dt = o_dt.replace(tzinfo=timezone.utc)
                hold_sec_val = round(max(0.0, (utc_now() - o_dt).total_seconds()), 1)
            except ValueError:
                hold_sec_val = None
        pos_liq_usd = (
            float(existing_pos_dict.get("last_liquidity_usd") or existing_pos_dict.get("entry_liquidity_usd") or 0.0)
            if isinstance(existing_pos_dict, dict)
            else 0.0
        )
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
            strategy=pos_strategy,
            triggering_wallets=pos_trig_wallets,
            opened_at=pos_opened_at,
        )
        pos_mc_usd = (
            order.entry_mc_usd
            if order.entry_mc_usd is not None
            else (
                existing_pos_dict.get("entry_mc_usd")
                if isinstance(existing_pos_dict, dict)
                else None
            )
        )
        pos_whale_flow = (
            order.whale_sol_flow
            if order.whale_sol_flow is not None
            else (
                existing_pos_dict.get("whale_sol_flow")
                if isinstance(existing_pos_dict, dict)
                else None
            )
        )
        sell_bt = (
            order.breakout_telemetry
            or (existing_pos_dict.get("breakout_telemetry") if isinstance(existing_pos_dict, dict) else None)
            or {}
        )
        db.log_action(
            mode=config.EXECUTION_MODE,
            action="SELL",
            symbol=order.symbol,
            asset_id=order.mint,
            qty=float(order.amount_atomic),
            notional=round(received_sol, 6),
            price_or_value=round(received_sol, 6),
            pnl_amount=round(pnl_sol, 6),
            pnl_pct=round(pnl_pct * 100.0, 2),
            strategy=pos_strategy,
            reason=order.reason,
            sol_equity=round(sol_balance, 6),
            daily_realized_sol=round(daily_pnl_sol, 6),
            mc_usd=pos_mc_usd,
            liq_usd=round(pos_liq_usd, 2) if pos_liq_usd else None,
            peak_pnl_pct=peak_pnl_pct_val,
            hold_seconds=hold_sec_val,
            triggering_whales=",".join(pos_trig_wallets) if pos_trig_wallets else None,
            whale_sol_flow=pos_whale_flow,
            breakout_score=sell_bt.get("score"),
            breakout_state=sell_bt.get("state"),
            breakout_timeframe=sell_bt.get("timeframe"),
            breakout_vol_ratio=sell_bt.get("vol_ratio"),
            breakout_ema_ext_pct=sell_bt.get("ema_ext_pct"),
            breakout_failed_gates="; ".join(sell_bt.get("failed_gates") or []) if sell_bt else None,
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
            prev_reentry_count = (
                int(existing_pos_dict.get("reentry_count") or 0)
                if isinstance(existing_pos_dict, dict)
                else 0
            )
            watch_pos = ActivePosition(
                mint=order.mint,
                symbol=order.symbol,
                entry_sol=entry_sol,
                token_amount=0,
                strategy=pos_strategy,
                entry_liquidity_usd=prev_liq,
                current_sol_value=received_sol,
                peak_sol_value=prev_peak,
                breakeven_armed=False,
                trail_armed=False,
                retrace_low_sol=received_sol,
                run_peak_sol=prev_peak,
                watch_started_at=utc_now_iso(),
                reference_token_amount=order.amount_atomic,
                reentry_count=prev_reentry_count,
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

    last_exec = {
        "side": order.side, "mint": order.mint, "symbol": order.symbol,
        "in_amount": order.amount_atomic, "out_amount": out_amount,
        "tx_signature": tx_signature, "reason": order.reason,
    }
    if is_whale_slot:
        updated_whales = [p for p in whale_positions if isinstance(p, dict) and p.get("mint") != order.mint]
        if is_buy and new_pos_dict is not None:
            updated_whales.append(new_pos_dict)
        db.save_lane_positions(
            lane="whale", positions=updated_whales, sol_balance=sol_balance,
            daily_pnl_sol=daily_pnl_sol, last_decision_rationale=order.rationale or state.get("last_decision_rationale"),
        )
        return {
            "focus_state": state.get("focus_state", FocusState.SCANNING.value),
            "sol_balance": sol_balance, "daily_pnl_sol": daily_pnl_sol,
            "whale_position": new_pos_dict if is_buy else (updated_whales[0] if updated_whales else None),
            "whale_positions": updated_whales, "pending_order": None, "last_execution": last_exec,
        }

    updated_charts = [p for p in chart_positions if isinstance(p, dict) and p.get("mint") != order.mint]
    if is_buy and new_pos_dict is not None:
        updated_charts.append(new_pos_dict)
    db.save_lane_positions(
        lane="chart", positions=updated_charts, sol_balance=sol_balance, daily_pnl_sol=daily_pnl_sol,
        focus_state=next_state, active_position=new_pos_dict,
        last_decision_rationale=order.rationale or state.get("last_decision_rationale"),
    )
    return {
        "focus_state": next_state, "sol_balance": sol_balance, "daily_pnl_sol": daily_pnl_sol,
        "active_position": new_pos_dict, "chart_positions": updated_charts,
        "pending_order": None, "last_execution": last_exec,
    }
