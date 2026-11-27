"""Directive 24 (E2B) Verification Suite: Fee Accounting & Net Breakeven."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import duckdb
import httpx
import pytest
from solders.keypair import Keypair

from batched_price_monitor import active_cost_basis_sol
from chart_exit_engine import (
    PRIO_BREAKEVEN,
    PRIO_EXODUS,
    PRIO_HARD_STOP,
    PRIO_HOLD,
    PRIO_TRAILING_TP,
    compute_net_lifecycle_pnl,
    evaluate_chart_position_exit,
)
from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase
from executor_node import clear_sell_idempotency_guard, executor_node
from state import ActivePosition, FocusState, PendingOrder
from state_ledger import clear_buy_reservations

POOL_PINNED = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"
MINT_E2B = "p2Yicb86aZig616Eav2VWG9vuXR5mEqhtzshZYBxzsV"


def _mk_paper_buy_order(mint: str = MINT_E2B, symbol: str = "E2B", reason: str = "DECISION_1_ENTRY") -> dict[str, Any]:
    return PendingOrder(
        side="BUY",
        mint=mint,
        symbol=symbol,
        pair_address=POOL_PINNED,
        dex_id="pumpswap",
        amount_atomic=int(0.50 * LAMPORTS_PER_SOL),
        reason=reason,
        target_state_after_fill=FocusState.IN_POSITION,
        entry_liquidity_usd=80_000.0,
        entry_mc_usd=500_000.0,
        strategy="BREAKOUT_IGNITION",
    ).model_dump()


@pytest.mark.asyncio
async def test_e2b_buy_to_sell_reconciles_account_and_lifecycle_pnl(tmp_path):
    """Test 1 & Invariants: BUY -> SELL counts entry & exit fees once and reconciles daily_pnl_sol."""
    clear_buy_reservations()
    clear_sell_idempotency_guard()
    db = AgentDatabase(db_path=str(tmp_path / "e2b_buy_sell.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper", PAPER_TX_FEE_SOL=0.001)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("inputMint") == SOL_MINT:
            return httpx.Response(200, json={"outAmount": "1000000", "transaction": None})
        return httpx.Response(200, json={"outAmount": str(int(0.55 * LAMPORTS_PER_SOL)), "transaction": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out_b = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "daily_pnl_sol": 0.0, "pending_order": _mk_paper_buy_order()},
            config=cfg, db=db, http_client=client,
        )
        pos = out_b["chart_positions"][0]
        assert pos["entry_fee_sol"] == pytest.approx(0.001)
        assert pos["accumulated_fees_sol"] == pytest.approx(0.001)
        assert pos["fee_accounting_status"] == "PAPER_ASSUMED"
        assert out_b["sol_balance"] == pytest.approx(2.0 - 0.50 - 0.001)
        assert out_b["daily_pnl_sol"] == pytest.approx(-0.001)

        sell_order = PendingOrder(
            side="SELL", mint=MINT_E2B, symbol="E2B", opened_at=pos["opened_at"],
            amount_atomic=pos["token_amount"], reason="TRAILING_TP",
            target_state_after_fill=FocusState.SCANNING, strategy="BREAKOUT_IGNITION",
        ).model_dump()
        out_s = await executor_node(
            {**db.get_state(), "pending_order": sell_order},
            config=cfg, db=db, http_client=client,
        )

    # Lifecycle net = 0.55 - 0.50 - 0.001 (buy) - 0.001 (sell) = +0.048 SOL
    assert out_s["daily_pnl_sol"] == pytest.approx(0.048)
    assert out_s["sol_balance"] == pytest.approx(2.048)

    trades = db.get_recent_trades(limit=5)
    assert all(t["tx_fee_sol"] == pytest.approx(0.001) and t["fee_status"] == "PAPER_ASSUMED" for t in trades)
    db.close()


@pytest.mark.asyncio
async def test_e2b_partial_sell_then_final_sell_trailing_and_reentry_scope(tmp_path):
    """Tests 2, 3, 4, 5 & Invariants: BUY -> PARTIAL_SELL -> SELL / TRAILING / WATCHING_REENTRY -> Re-entry BUY -> SELL."""
    clear_buy_reservations()
    clear_sell_idempotency_guard()
    db = AgentDatabase(db_path=str(tmp_path / "e2b_partial_reentry.duckdb"), starting_sol=3.0)
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper", PAPER_TX_FEE_SOL=0.001, PARTIAL_TP_SELL_FRACTION=0.50)

    sell_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        in_mint = request.url.params.get("inputMint")
        if in_mint == SOL_MINT:
            return httpx.Response(200, json={"outAmount": "1000000", "transaction": None})
        sell_calls["n"] += 1
        if sell_calls["n"] == 1:
            return httpx.Response(200, json={"outAmount": str(int(0.30 * LAMPORTS_PER_SOL)), "transaction": None})
        return httpx.Response(200, json={"outAmount": str(int(0.32 * LAMPORTS_PER_SOL)), "transaction": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # Leg 1 BUY
        await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 3.0, "daily_pnl_sol": 0.0, "pending_order": _mk_paper_buy_order()},
            config=cfg, db=db, http_client=client,
        )
        pos1 = db.get_state()["chart_positions"][0]

        # PARTIAL_SELL (50% for 0.30 SOL; cost_of_sold=0.25; fee=0.001; partial_pnl=+0.049)
        ps_order = PendingOrder(
            side="PARTIAL_SELL", mint=MINT_E2B, symbol="E2B", opened_at=pos1["opened_at"],
            amount_atomic=500_000, reason="PARTIAL_TP_50PCT (+15%)",
            target_state_after_fill=FocusState.IN_POSITION, strategy="BREAKOUT_IGNITION",
        ).model_dump()
        out_ps = await executor_node({**db.get_state(), "pending_order": ps_order}, config=cfg, db=db, http_client=client)
        runner = out_ps["chart_positions"][0]
        assert runner["remaining_cost_basis_sol"] == pytest.approx(0.25)
        assert runner["remaining_cost_basis_sol"] + 0.25 == pytest.approx(runner["initial_entry_sol"])
        assert runner["realized_partial_pnl_sol"] == pytest.approx(0.049)
        assert runner["entry_fee_sol"] == pytest.approx(0.001)
        assert runner["accumulated_fees_sol"] == pytest.approx(0.002)
        assert runner["fee_accounting_status"] == "PAPER_ASSUMED"
        assert runner["pair_address"] == POOL_PINNED

        # Final SELL -> WATCHING_REENTRY (0.32 SOL; basis=0.25; fee=0.001; sell_pnl=+0.069)
        # Total Leg 1 net = (0.30 + 0.32) - 0.50 - 0.003 = +0.117 SOL
        sell_watch = PendingOrder(
            side="SELL", mint=MINT_E2B, symbol="E2B", opened_at=runner["opened_at"],
            amount_atomic=runner["token_amount"], reason="TRAILING_TP",
            target_state_after_fill=FocusState.WATCHING_REENTRY, strategy="BREAKOUT_IGNITION",
        ).model_dump()
        out_w = await executor_node({**db.get_state(), "pending_order": sell_watch}, config=cfg, db=db, http_client=client)
        assert out_w["focus_state"] == FocusState.WATCHING_REENTRY.value
        assert out_w["daily_pnl_sol"] == pytest.approx(0.117)
        assert out_w["sol_balance"] == pytest.approx(3.117)

        # Re-entry BUY must start a fresh position fee scope (never inherit Leg 1's 0.002+ fees or partial PnL)
        clear_sell_idempotency_guard()
        out_rb = await executor_node(
            {**db.get_state(), "pending_order": _mk_paper_buy_order(reason="LEG_2_REENTRY")},
            config=cfg, db=db, http_client=client,
        )
        pos2 = out_rb["chart_positions"][0]
        assert pos2["reentry_count"] == 1
        assert pos2["entry_fee_sol"] == pytest.approx(0.001)
        assert pos2["accumulated_fees_sol"] == pytest.approx(0.001)
        assert pos2["realized_partial_pnl_sol"] == pytest.approx(0.0)
        assert pos2["remaining_cost_basis_sol"] == pytest.approx(0.50)

        # Re-entry SELL (0.32 SOL; basis=0.50; fee=0.001 -> leg2 net = 0.32 - 0.50 - 0.002 = -0.182)
        sell_leg2 = PendingOrder(
            side="SELL", mint=MINT_E2B, symbol="E2B", opened_at=pos2["opened_at"],
            amount_atomic=pos2["token_amount"], reason="HARD_STOP_LOSS",
            target_state_after_fill=FocusState.SCANNING, strategy="BREAKOUT_IGNITION",
        ).model_dump()
        out_final = await executor_node({**db.get_state(), "pending_order": sell_leg2}, config=cfg, db=db, http_client=client)
        assert out_final["daily_pnl_sol"] == pytest.approx(0.117 - 0.182)
        assert out_final["sol_balance"] == pytest.approx(3.0 + 0.117 - 0.182)
    db.close()


@pytest.mark.asyncio
async def test_e2b_live_unknown_fee_remains_null_and_historical_migration_preserves_unknown(tmp_path):
    """Tests 6 & 7: Unknown live fees stay NULL/UNKNOWN (never 0.0); pre-E2B DuckDB migrates with NULL/UNKNOWN."""
    # 1. Pre-E2B DuckDB migration check
    db_file = tmp_path / "legacy_pre_e2b.duckdb"
    conn = duckdb.connect(str(db_file))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY, timestamp VARCHAR NOT NULL, mode VARCHAR NOT NULL,
            side VARCHAR NOT NULL, mint VARCHAR NOT NULL, symbol VARCHAR NOT NULL,
            in_amount BIGINT NOT NULL, out_amount BIGINT NOT NULL, sol_value DOUBLE NOT NULL,
            pnl_sol DOUBLE, pnl_pct DOUBLE, reason VARCHAR NOT NULL, tx_signature VARCHAR,
            rationale VARCHAR, strategy VARCHAR DEFAULT 'SCALPER',
            triggering_wallets_json VARCHAR DEFAULT '[]', opened_at VARCHAR,
            jupiter_price_impact_raw VARCHAR, pair_address VARCHAR, dex_id VARCHAR
        )
    """)
    conn.execute(f"""
        INSERT INTO trades VALUES (
            1, '2026-11-26T05:00:00+00:00', 'paper', 'BUY', '{MINT_E2B}',
            'OLD', 500000000, 1000000, 0.5, NULL, NULL, 'DECISION_1_ENTRY', 'paper-buy-legacy',
            'old', 'BREAKOUT_IGNITION', '[]', '2026-11-26T05:00:00+00:00', '-0.01', '{POOL_PINNED}', 'pumpswap'
        )
    """)
    conn.close()

    db1 = AgentDatabase(db_path=str(db_file), starting_sol=1.0)
    db1.close()
    db2 = AgentDatabase(db_path=str(db_file), starting_sol=1.0)
    legacy_trades = db2.get_recent_trades(limit=5)
    assert len(legacy_trades) == 1
    assert legacy_trades[0]["tx_fee_sol"] is None
    assert legacy_trades[0]["fee_status"] is None
    db2.close()

    # Historical ActivePosition without fee fields defaults conservatively to UNKNOWN
    hist_pos = ActivePosition(mint=MINT_E2B, symbol="HIST", entry_sol=0.50, token_amount=1_000_000)
    assert hist_pos.entry_fee_sol is None
    assert hist_pos.accumulated_fees_sol == 0.0
    assert hist_pos.fee_accounting_status == "UNKNOWN"

    # 2. Live mode BUY without observed RPC fee records tx_fee_sol=NULL & fee_status='UNKNOWN'
    clear_buy_reservations()
    db_live = AgentDatabase(db_path=str(tmp_path / "e2b_live.duckdb"), starting_sol=2.0)
    kp = Keypair()
    cfg_live = AgentConfig(
        _env_file=None, EXECUTION_MODE="live", SOLANA_PRIVATE_KEY_BS58=str(kp),
    )

    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction
    import base64
    msg = MessageV0.try_compile(kp.pubkey(), [], [], kp.pubkey().__bytes__().ljust(32, b"\x00")[:32] if False else __import__("solders.hash", fromlist=["Hash"]).Hash.default())
    unsigned_tx = base64.b64encode(bytes(VersionedTransaction(msg, [kp]))).decode("ascii")

    def live_handler(request: httpx.Request) -> httpx.Response:
        if "/swap/v2/order" in str(request.url):
            return httpx.Response(200, json={"outAmount": "1000000", "transaction": unsigned_tx, "requestId": "req-1"})
        return httpx.Response(200, json={"signature": "LiveSig111", "outputAmountResult": "1000000"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(live_handler)) as client:
        out_live = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "daily_pnl_sol": 0.0, "pending_order": _mk_paper_buy_order()},
            config=cfg_live, db=db_live, http_client=client,
        )
    live_pos = out_live["chart_positions"][0]
    assert live_pos["entry_fee_sol"] is None
    assert live_pos["fee_accounting_status"] == "UNKNOWN"
    assert out_live["last_execution"]["tx_fee_sol"] is None
    assert out_live["last_execution"]["fee_status"] == "UNKNOWN"

    live_trade = db_live.get_recent_trades(limit=1)[0]
    assert live_trade["tx_fee_sol"] is None
    assert live_trade["fee_status"] == "UNKNOWN"
    db_live.close()


def test_e2b_net_breakeven_accounts_for_fees_and_suppresses_when_unknown():
    """Net breakeven prevents net-negative closures, credits partial PnL once, and suppresses on UNKNOWN fees."""
    now_dt = datetime(2026, 11, 26, 16, 0, 0, tzinfo=timezone.utc)
    warmed = (now_dt - timedelta(minutes=10)).isoformat()

    # 1. Paper mode with high PAPER_TX_FEE_SOL=0.006 (0.012 round-trip = 2.4% on 0.50 SOL):
    # Gross +2% (0.510 SOL) would be net-negative (-0.002 SOL) and MUST NOT arm or claim +2% net breakeven!
    cfg_hi = AgentConfig(
        _env_file=None, EXECUTION_MODE="paper", PAPER_TX_FEE_SOL=0.006,
        BREAKEVEN_ARM_PCT=0.08, BREAKEVEN_FLOOR_PCT=0.02,
    )
    pos = ActivePosition(
        mint=MINT_E2B, symbol="NET", pair_address=POOL_PINNED, dex_id="pumpswap",
        entry_sol=0.50, initial_entry_sol=0.50, remaining_cost_basis_sol=0.50,
        token_amount=1_000_000, current_sol_value=0.555, peak_sol_value=0.555,
        entry_fee_sol=0.006, accumulated_fees_sol=0.006, fee_accounting_status="PAPER_ASSUMED",
        opened_at=warmed,
    )
    # Peak 0.555 gross (+11%) -> net = 0.555 - 0.50 - 0.012 = +0.043 (+8.6% >= +8%) -> arms net breakeven
    o_arm, _, _ = evaluate_chart_position_exit(
        pos, current_sol_value=0.555, buys_5m=15, sells_5m=5, current_liquidity_usd=80_000.0,
        config=cfg_hi, now_dt=now_dt,
    )
    assert o_arm is None
    assert pos.breakeven_armed is True
    assert pos.last_breakeven_status == "NET_BREAKEVEN_ARMED"

    # At 0.522 SOL -> net = 0.522 - 0.50 - 0.012 = +0.010 (+2.0% net!) -> triggers BREAKEVEN_SHIELD
    # Note: at 0.510 SOL (+2% gross, -0.4% net), it would ALSO trigger protective exit because net <= +2%,
    # whereas at 0.523 SOL (+4.6% gross, +2.2% net), it HOLDS above the +2% net floor!
    o_hold, _, p_hold = evaluate_chart_position_exit(
        pos, current_sol_value=0.523, buys_5m=15, sells_5m=5, current_liquidity_usd=80_000.0,
        config=cfg_hi, now_dt=now_dt,
    )
    assert o_hold is None and p_hold == PRIO_HOLD

    o_be, ev_be, p_be = evaluate_chart_position_exit(
        pos, current_sol_value=0.521, buys_5m=15, sells_5m=5, current_liquidity_usd=80_000.0,
        config=cfg_hi, now_dt=now_dt,
    )
    assert o_be is not None and p_be == PRIO_BREAKEVEN
    assert "BREAKEVEN_SHIELD" in ev_be
    assert pos.last_breakeven_status == "NET_BREAKEVEN_TRIGGERED"
    _, net_sol, _, _, _ = compute_net_lifecycle_pnl(pos, 0.521, cfg_hi)
    assert net_sol == pytest.approx(0.009)  # strictly net-positive after all fees

    # 2. Partial sale lifecycle: 50% sold for +0.049 realized_partial_pnl, remaining_cost_basis=0.25
    # Net = 0.049 + (current - 0.25 - 0.001) - 0.001
    cfg_p = AgentConfig(_env_file=None, EXECUTION_MODE="paper", PAPER_TX_FEE_SOL=0.001)
    pos_part = ActivePosition(
        mint=MINT_E2B, symbol="PART", pair_address=POOL_PINNED, dex_id="pumpswap",
        entry_sol=0.50, initial_entry_sol=0.50, remaining_cost_basis_sol=0.25,
        partial_tp_taken=True, realized_partial_pnl_sol=0.049,
        token_amount=500_000, current_sol_value=0.25, peak_sol_value=0.30,
        entry_fee_sol=0.001, accumulated_fees_sol=0.002, fee_accounting_status="PAPER_ASSUMED",
        breakeven_armed=True, opened_at=warmed,
    )
    assert active_cost_basis_sol(pos_part) == pytest.approx(0.25)
    # At runner=0.213 -> net = 0.049 + (0.213 - 0.25 - 0.001) - 0.001 = +0.010 (+2.0% of 0.50 initial)
    _, net_p, net_p_pct, _, _ = compute_net_lifecycle_pnl(pos_part, 0.213, cfg_p)
    assert net_p == pytest.approx(0.010)
    assert net_p_pct == pytest.approx(0.02)

    # 3. UNKNOWN fees (historical or live): suppresses breakeven, but preserves HARD_STOP_LOSS, EXODUS, and TRAILING_TP
    pos_unk = ActivePosition(
        mint=MINT_E2B, symbol="UNK", pair_address=POOL_PINNED, dex_id="pumpswap",
        entry_sol=0.50, token_amount=1_000_000, current_sol_value=0.55, peak_sol_value=0.55,
        opened_at=warmed,
    )
    assert pos_unk.fee_accounting_status == "UNKNOWN"
    o_u1, _, _ = evaluate_chart_position_exit(
        pos_unk, current_sol_value=0.505, buys_5m=15, sells_5m=5, current_liquidity_usd=80_000.0,
        config=cfg_p, now_dt=now_dt,
    )
    assert o_u1 is None
    assert pos_unk.last_breakeven_status == "BREAKEVEN_SUPPRESSED_UNKNOWN_FEES"

    # HARD_STOP_LOSS still fires on UNKNOWN fees
    o_hs, _, p_hs = evaluate_chart_position_exit(
        pos_unk, current_sol_value=0.44, buys_5m=15, sells_5m=5, current_liquidity_usd=80_000.0,
        config=cfg_p, now_dt=now_dt,
    )
    assert o_hs is not None and p_hs == PRIO_HARD_STOP

    # TRAILING_TP still fires on UNKNOWN fees
    pos_unk.trail_armed = True
    pos_unk.peak_sol_value = 0.60
    o_tr, _, p_tr = evaluate_chart_position_exit(
        pos_unk, current_sol_value=0.55, buys_5m=15, sells_5m=5, current_liquidity_usd=80_000.0,
        config=cfg_p, now_dt=now_dt,
    )
    assert o_tr is not None and p_tr == PRIO_TRAILING_TP

    # EXODUS still fires on UNKNOWN fees
    pos_unk.exodus_confirm_count = 1
    pos_unk.exodus_last_confirmed_at = (now_dt - timedelta(seconds=10)).isoformat()
    o_ex, _, p_ex = evaluate_chart_position_exit(
        pos_unk, current_sol_value=0.58, buys_5m=2, sells_5m=18, current_liquidity_usd=80_000.0,
        config=cfg_p, now_dt=now_dt,
    )
    assert o_ex is not None and p_ex == PRIO_EXODUS
