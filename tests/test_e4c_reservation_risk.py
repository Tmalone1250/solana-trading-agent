"""Phase E4C Tests: Reservation, Spendable Balance & Risk Guard Integrity."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase
from executor_node import clear_sell_idempotency_guard, executor_node
from state import FocusState, PendingOrder
from state_ledger import (
    RESERVATION_TTL_SEC,
    _inflight_buy_reservations,
    clear_buy_reservations,
    get_inflight_buy_reservations,
)

POOL_A = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"
POOL_B = "7qbRF6YsyGuLUVs6Y1q64bdVrfe4ZcUUz1JRdoVNUJnm"
MINT_A = "p2Yicb86aZig616Eav2VWG9vuXR5mEqhtzshZYBxzsV"
MINT_B = "So11111111111111111111111111111111111111113"
RPC_URL = "https://rpc.e4c.test"


def _mk_buy_order(
    mint: str = MINT_A,
    symbol: str = "E4C",
    pair_address: str = POOL_A,
    amount_sol: float = 0.50,
    strategy: str = "BREAKOUT_IGNITION",
) -> dict[str, Any]:
    return PendingOrder(
        side="BUY",
        mint=mint,
        symbol=symbol,
        pair_address=pair_address,
        dex_id="pumpswap",
        amount_atomic=int(round(amount_sol * LAMPORTS_PER_SOL)),
        reason="DECISION_1_ENTRY",
        target_state_after_fill=FocusState.IN_POSITION,
        entry_liquidity_usd=80_000.0,
        entry_mc_usd=500_000.0,
        strategy=strategy,
    ).model_dump()


def _mk_unsigned_tx(kp: Keypair) -> str:
    msg = MessageV0.try_compile(kp.pubkey(), [], [], Hash.default())
    return base64.b64encode(bytes(VersionedTransaction(msg, [kp]))).decode("ascii")


def test_e4c_config_validates_live_rpc_url():
    """Live mode requires a valid http(s):// SOLANA_RPC_URL; malformed URL fails validation."""
    kp = Keypair()
    with pytest.raises(ValueError, match="SOLANA_RPC_URL"):
        AgentConfig(_env_file=None, EXECUTION_MODE="live", SOLANA_PRIVATE_KEY_BS58=str(kp), SOLANA_RPC_URL="")
    with pytest.raises(ValueError, match="SOLANA_RPC_URL"):
        AgentConfig(_env_file=None, EXECUTION_MODE="live", SOLANA_PRIVATE_KEY_BS58=str(kp), SOLANA_RPC_URL="wss://bad.url")
    with pytest.raises(ValueError, match="SOLANA_RPC_URL"):
        AgentConfig(_env_file=None, EXECUTION_MODE="paper", SOLANA_RPC_URL="ftp://bad.url")

    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="live", SOLANA_PRIVATE_KEY_BS58=str(kp), SOLANA_RPC_URL=RPC_URL)
    assert cfg.SOLANA_RPC_URL == RPC_URL
    assert cfg.LIVE_MIN_SOL_RESERVE >= 0.0
    assert cfg.LIVE_FEE_RESERVATION_BUFFER_SOL >= 0.0


def test_e4c_paper_fee_aware_reservation_enforces_required_plus_fee(tmp_path):
    """Paper reservation rejects when avail_sol < required_sol + PAPER_TX_FEE_SOL and reserves full sum."""
    clear_buy_reservations()
    db = AgentDatabase(db_path=str(tmp_path / "e4c_paper_fee.duckdb"), starting_sol=0.5005)
    res_id, rej = db.reserve_buy_slot(
        lane="chart", mint=MINT_A, required_sol=0.50, tx_fee_sol=0.001,
        max_lane_positions=3, max_open_positions=6,
    )
    assert res_id is None
    assert rej is not None and "INSUFFICIENT_SOL" in rej

    db2 = AgentDatabase(db_path=str(tmp_path / "e4c_paper_fee_ok.duckdb"), starting_sol=0.501)
    res_ok, rej_ok = db2.reserve_buy_slot(
        lane="chart", mint=MINT_A, required_sol=0.50, tx_fee_sol=0.001,
        max_lane_positions=3, max_open_positions=6,
    )
    assert res_ok is not None and rej_ok is None
    inflight = get_inflight_buy_reservations(str(db2.db_path))
    assert len(inflight) == 1
    assert inflight[0]["sol_amount"] == pytest.approx(0.501)
    assert inflight[0]["active_coroutine"] is True
    assert inflight[0]["owner_task_id"]
    db.close()
    db2.close()


@pytest.mark.asyncio
async def test_e4c_live_rpc_balance_success_insufficient_timeout_and_error(tmp_path):
    """Live BUY verifies RPC getBalance, enforces reserve+buffer, never stores buffer as fee, fails closed on timeout/error."""
    clear_buy_reservations()
    clear_sell_idempotency_guard()
    kp = Keypair()
    unsigned_tx = _mk_unsigned_tx(kp)
    cfg = AgentConfig(
        _env_file=None, EXECUTION_MODE="live", SOLANA_PRIVATE_KEY_BS58=str(kp),
        SOLANA_RPC_URL=RPC_URL, LIVE_MIN_SOL_RESERVE=0.05, LIVE_FEE_RESERVATION_BUFFER_SOL=0.01,
    )

    # 1. Success: wallet has 0.56 SOL -> 0.56 - 0.05 reserve = 0.51 >= 0.50 + 0.01 buffer
    db = AgentDatabase(db_path=str(tmp_path / "e4c_live_ok.duckdb"), starting_sol=1.0)
    rpc_mode = {"mode": "ok", "lamports": int(0.56 * LAMPORTS_PER_SOL)}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(RPC_URL):
            if rpc_mode["mode"] == "timeout":
                raise httpx.ReadTimeout("RPC timed out")
            if rpc_mode["mode"] == "rpc_error":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "unhealthy"}})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": rpc_mode["lamports"]}})
        if "/swap/v2/order" in str(request.url):
            return httpx.Response(200, json={"outAmount": "1000000", "transaction": unsigned_tx, "requestId": "r1"})
        return httpx.Response(200, json={"signature": "SigOk111", "outputAmountResult": "1000000"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # Insufficient wallet funds (0.555 - 0.05 = 0.505 < 0.51) -> blocked before Jupiter quote
        rpc_mode["lamports"] = int(0.555 * LAMPORTS_PER_SOL)
        out_insuff = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 1.0, "daily_pnl_sol": 0.0, "pending_order": _mk_buy_order()},
            config=cfg, db=db, http_client=client,
        )
        assert out_insuff == {"pending_order": None}
        assert get_inflight_buy_reservations(str(db.db_path)) == []

        # RPC timeout -> fail closed
        rpc_mode["mode"] = "timeout"
        out_to = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 1.0, "daily_pnl_sol": 0.0, "pending_order": _mk_buy_order()},
            config=cfg, db=db, http_client=client,
        )
        assert out_to == {"pending_order": None}

        # RPC error -> fail closed
        rpc_mode["mode"] = "rpc_error"
        out_err = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 1.0, "daily_pnl_sol": 0.0, "pending_order": _mk_buy_order()},
            config=cfg, db=db, http_client=client,
        )
        assert out_err == {"pending_order": None}

        # Valid sufficient balance (0.56 SOL) -> succeeds and NEVER persists fee buffer as observed fee
        rpc_mode["mode"] = "ok"
        rpc_mode["lamports"] = int(0.56 * LAMPORTS_PER_SOL)
        out_ok = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 1.0, "daily_pnl_sol": 0.0, "pending_order": _mk_buy_order()},
            config=cfg, db=db, http_client=client,
        )
        assert len(out_ok["chart_positions"]) == 1
        pos = out_ok["chart_positions"][0]
        assert pos["entry_fee_sol"] is None
        assert pos["fee_accounting_status"] == "UNKNOWN"
        assert out_ok["last_execution"]["tx_fee_sol"] is None
        assert out_ok["last_execution"]["fee_status"] == "UNKNOWN"
        assert get_inflight_buy_reservations(str(db.db_path)) == []
    db.close()


def test_e4c_balance_divergence_warning_and_no_double_counting_inflight_reservations(tmp_path, caplog):
    """Divergence logs BALANCE_DIVERGENCE without altering PnL; in-flight reservations are counted exactly once."""
    clear_buy_reservations()
    db = AgentDatabase(db_path=str(tmp_path / "e4c_div.duckdb"), starting_sol=1.50)

    # Wallet has 1.20 SOL vs DuckDB 1.50 SOL (diff=-0.30 > 0.02 warn)
    with caplog.at_level(logging.WARNING):
        res1, rej1 = db.reserve_buy_slot(
            lane="chart", mint=MINT_A, required_sol=0.50, tx_fee_sol=0.01,
            max_lane_positions=3, max_open_positions=6,
            live_wallet_sol=1.20, live_min_sol_reserve=0.05, balance_divergence_warn_sol=0.02,
        )
    assert res1 is not None and rej1 is None
    assert "BALANCE_DIVERGENCE" in caplog.text
    assert db.get_state()["daily_pnl_sol"] == pytest.approx(0.0)  # historical PnL untouched

    # Second reservation sees wallet_avail = 1.20 - 0.05 - 0.51 = 0.64 >= 0.51 (no double counting of 0.51!)
    res2, rej2 = db.reserve_buy_slot(
        lane="chart", mint=MINT_B, required_sol=0.50, tx_fee_sol=0.01,
        max_lane_positions=3, max_open_positions=6,
        live_wallet_sol=1.20, live_min_sol_reserve=0.05, balance_divergence_warn_sol=0.02,
    )
    assert res2 is not None and rej2 is None

    # Third reservation sees wallet_avail = 1.20 - 0.05 - 1.02 = 0.13 < 0.51 -> rejected
    res3, rej3 = db.reserve_buy_slot(
        lane="chart", mint="MintC11111111111111111111111111111111111111", required_sol=0.50, tx_fee_sol=0.01,
        max_lane_positions=3, max_open_positions=6,
        live_wallet_sol=1.20, live_min_sol_reserve=0.05, balance_divergence_warn_sol=0.02,
    )
    assert res3 is None and rej3 is not None and "INSUFFICIENT_SOL" in rej3
    db.close()


def test_e4c_atomic_cooldown_and_daily_max_loss_guards(tmp_path):
    """reserve_buy_slot enforces cooldowns and DAILY_MAX_LOSS_PCT atomically under db._lock."""
    clear_buy_reservations()
    db = AgentDatabase(db_path=str(tmp_path / "e4c_risk.duckdb"), starting_sol=2.0)
    now = datetime(2026, 11, 27, 15, 0, 0, tzinfo=timezone.utc)

    # 1. Active cooldown blocks reservation
    db.set_cooldown(MINT_A, hours=1.0, reason="HARD_STOP_LOSS", now=now)
    r_cd, rej_cd = db.reserve_buy_slot(
        lane="chart", mint=MINT_A, required_sol=0.50, tx_fee_sol=0.001,
        max_lane_positions=3, max_open_positions=6, daily_max_loss_pct=0.20, now_dt=now + timedelta(minutes=10),
    )
    assert r_cd is None and rej_cd is not None and "ON_COOLDOWN" in rej_cd

    # Expired cooldown allows reservation
    r_exp, rej_exp = db.reserve_buy_slot(
        lane="chart", mint=MINT_A, required_sol=0.50, tx_fee_sol=0.001,
        max_lane_positions=3, max_open_positions=6, daily_max_loss_pct=0.20, now_dt=now + timedelta(hours=2),
    )
    assert r_exp is not None and rej_exp is None
    db.release_buy_reservation(r_exp)

    # 2. DAILY_MAX_LOSS_PCT breach (-0.40 on 2.0 start = -20%) blocks reservation
    db.save_lane_positions(lane="chart", positions=[], sol_balance=1.60, daily_pnl_sol=-0.40)
    r_dl, rej_dl = db.reserve_buy_slot(
        lane="chart", mint=MINT_B, required_sol=0.50, tx_fee_sol=0.001,
        max_lane_positions=3, max_open_positions=6, daily_max_loss_pct=0.20, now_dt=now,
    )
    assert r_dl is None and rej_dl is not None and "DAILY_MAX_LOSS_HIT" in rej_dl
    db.close()


def test_e4c_active_coroutine_reservation_survives_ttl_eviction(tmp_path):
    """Active coroutine reservations are never pruned when RESERVATION_TTL_SEC elapses."""
    clear_buy_reservations()
    db = AgentDatabase(db_path=str(tmp_path / "e4c_ttl.duckdb"), starting_sol=2.0)

    res1, rej1 = db.reserve_buy_slot(
        lane="chart", mint=MINT_A, required_sol=0.50, tx_fee_sol=0.001,
        max_lane_positions=1, max_open_positions=6,
    )
    assert res1 is not None and rej1 is None

    # Simulate > RESERVATION_TTL_SEC elapsed while coroutine is still active
    _inflight_buy_reservations[str(db.db_path)][0]["created_ts"] = time.monotonic() - (RESERVATION_TTL_SEC + 30.0)

    # Concurrent attempt must NOT prune res1 and must be blocked by LANE_CAPACITY
    res2, rej2 = db.reserve_buy_slot(
        lane="chart", mint=MINT_B, required_sol=0.50, tx_fee_sol=0.001,
        max_lane_positions=1, max_open_positions=6,
    )
    assert res2 is None and rej2 is not None and "LANE_CAPACITY" in rej2
    assert len(get_inflight_buy_reservations(str(db.db_path))) == 1
    db.release_buy_reservation(res1)
    assert get_inflight_buy_reservations(str(db.db_path)) == []
    db.close()


@pytest.mark.asyncio
async def test_e4c_concurrent_buys_rpc_outside_lock_and_release_on_prebroadcast_abort(tmp_path):
    """Concurrent BUYs serialize atomically; RPC getBalance executes outside db._lock; abort releases reservation."""
    clear_buy_reservations()
    clear_sell_idempotency_guard()
    kp = Keypair()
    unsigned_tx = _mk_unsigned_tx(kp)
    db = AgentDatabase(db_path=str(tmp_path / "e4c_conc.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(
        _env_file=None, EXECUTION_MODE="live", SOLANA_PRIVATE_KEY_BS58=str(kp),
        SOLANA_RPC_URL=RPC_URL, MAX_CHART_POSITIONS=1,
    )

    lock_was_free_during_rpc: list[bool] = []
    fail_quote = {"flag": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(RPC_URL):
            # Verify db._lock is NOT held during the RPC call (check from another thread to defeat RLock re-entrancy)
            import threading

            other_thread_acquired: list[bool] = []

            def _probe() -> None:
                got = db._lock.acquire(blocking=False)
                if got:
                    db._lock.release()
                other_thread_acquired.append(got)

            t = threading.Thread(target=_probe)
            t.start()
            t.join()
            lock_was_free_during_rpc.append(other_thread_acquired[0])
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 2_000_000_000}})
        if "/swap/v2/order" in str(request.url):
            if fail_quote["flag"]:
                return httpx.Response(500, json={"error": "quote down"})
            return httpx.Response(200, json={"outAmount": "1000000", "transaction": unsigned_tx, "requestId": "r-conc"})
        return httpx.Response(200, json={"signature": "SigConc111", "outputAmountResult": "1000000"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # 1. Pre-broadcast quote failure releases reservation cleanly
        fail_quote["flag"] = True
        with pytest.raises(httpx.HTTPStatusError):
            await executor_node(
                {"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "daily_pnl_sol": 0.0, "pending_order": _mk_buy_order()},
                config=cfg, db=db, http_client=client,
            )
        assert get_inflight_buy_reservations(str(db.db_path)) == []
        assert all(lock_was_free_during_rpc)

        # 2. Two concurrent BUYs with MAX_CHART_POSITIONS=1 -> exactly one succeeds
        fail_quote["flag"] = False
        r1, r2 = await asyncio.gather(
            executor_node(
                {"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "daily_pnl_sol": 0.0, "pending_order": _mk_buy_order(mint=MINT_A, symbol="A", pair_address=POOL_A)},
                config=cfg, db=db, http_client=client,
            ),
            executor_node(
                {"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "daily_pnl_sol": 0.0, "pending_order": _mk_buy_order(mint=MINT_B, symbol="B", pair_address=POOL_B)},
                config=cfg, db=db, http_client=client,
            ),
        )
        assert len(db.get_state()["chart_positions"]) == 1
        assert get_inflight_buy_reservations(str(db.db_path)) == []
        assert (r1 == {"pending_order": None}) ^ (r2 == {"pending_order": None})
    db.close()
