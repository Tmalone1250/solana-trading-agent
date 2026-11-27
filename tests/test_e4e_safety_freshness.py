"""Phase E4E regression tests: Persistent Watchlist Safety TTL & Pre-Execution Safety Boundary."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import duckdb
import httpx
import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase
from executor_node import clear_sell_idempotency_guard, executor_node
from safety_gate_node import clear_safety_cache, is_safety_timestamp_fresh, safety_gate_node
from state import FocusState, PendingOrder
from state_ledger import clear_buy_reservations, get_inflight_buy_reservations

POOL_PINNED = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"
POOL_OTHER = "7qbRF6YsyGuLUVs6Y1q64bdVrfe4ZcUUz1JRdoVNUJnm"
MINT_E4E = "p2Yicb86aZig616Eav2VWG9vuXR5mEqhtzshZYBxzsV"
RPC_URL = "https://rpc.e4e.test"


def _mk_unsigned_tx(kp: Keypair) -> str:
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=kp.pubkey(), lamports=1))
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.default())
    return base64.b64encode(bytes(VersionedTransaction(msg, [kp]))).decode("ascii")


def _mk_live_e4e_cfg(kp: Keypair, **overrides: Any) -> AgentConfig:
    defaults: dict[str, Any] = {
        "_env_file": None,
        "EXECUTION_MODE": "live",
        "SOLANA_PRIVATE_KEY_BS58": str(kp),
        "SOLANA_RPC_URL": RPC_URL,
        "MAX_BUY_APPROVAL_AGE_SECONDS": 30.0,
        "MAX_APPROVAL_PRICE_AGE_SECONDS": 30.0,
        "MAX_ENTRY_PRICE_DRIFT_PCT": 5.0,
        "MAX_NEGATIVE_ENTRY_DRIFT_PCT": 10.0,
        "MAX_WORST_CASE_ENTRY_DRIFT_PCT": 7.0,
        "MAX_QUOTE_AGE_SECONDS": 15.0,
        "MAX_ACCEPTED_SLIPPAGE_BPS": 300,
        "MAX_ENTRY_PRICE_IMPACT_PCT": 2.5,
        "MIN_BLOCK_HEIGHT_HEADROOM": 5,
        "MIN_RFQ_EXPIRY_HEADROOM_SECONDS": 5.0,
        "SAFETY_CACHE_TTL_SECONDS": 120.0,
        "PRE_EXECUTION_SAFETY_TTL_SECONDS": 60.0,
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _mk_e4e_order(mint: str = MINT_E4E, **overrides: Any) -> dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat()
    defaults: dict[str, Any] = {
        "side": "BUY",
        "mint": mint,
        "symbol": "E4E",
        "pair_address": POOL_PINNED,
        "dex_id": "pumpswap",
        "amount_atomic": int(0.50 * LAMPORTS_PER_SOL),
        "reason": "DECISION_1_ENTRY",
        "target_state_after_fill": FocusState.IN_POSITION,
        "entry_liquidity_usd": 80_000.0,
        "entry_mc_usd": 500_000.0,
        "strategy": "BREAKOUT_IGNITION",
        "approved_at": now_iso,
        "approval_price_observed_at": now_iso,
        "approval_pair_address": POOL_PINNED,
        "approval_price_native": 0.0005,
        "approval_price_source": "dexscreener_pinned_pool_native",
        "token_decimals": 6,
    }
    defaults.update(overrides)
    return PendingOrder(**defaults).model_dump()


def test_e4e_additive_migration_and_null_timestamp_treated_stale(tmp_path):
    """Pre-E4E active_watchlist migrates with safety_checked_at VARCHAR; existing NULL rows are stale."""
    db_file = tmp_path / "legacy_pre_e4e.duckdb"
    conn = duckdb.connect(str(db_file))
    conn.execute("""
        CREATE TABLE active_watchlist (
            mint VARCHAR PRIMARY KEY, symbol VARCHAR NOT NULL, pair_address VARCHAR DEFAULT '',
            dex_id VARCHAR, added_at VARCHAR NOT NULL, safety_passed BOOLEAN DEFAULT FALSE,
            entry_liquidity_usd DOUBLE DEFAULT 0.0, peak_mc DOUBLE DEFAULT 0.0, low_mc DOUBLE DEFAULT 0.0,
            leg2_score DOUBLE DEFAULT 0.0, leg2_breakdown_json VARCHAR, vamp_strikes INTEGER DEFAULT 0,
            rsi_5m DOUBLE, setup_type VARCHAR DEFAULT 'NONE', scalper_eligible BOOLEAN DEFAULT FALSE,
            dist_from_1h_high_pct DOUBLE, discovery_source VARCHAR DEFAULT 'DEXSCREENER',
            jup_organic_score DOUBLE, jup_meta_json VARCHAR, last_evaluated_at VARCHAR,
            latest_scores_json VARCHAR, candidate_json VARCHAR
        )
    """)
    conn.execute(f"""
        INSERT INTO active_watchlist (mint, symbol, pair_address, added_at, safety_passed)
        VALUES ('{MINT_E4E}', 'LEGACY', '{POOL_PINNED}', '2026-11-27T00:00:00+00:00', TRUE)
    """)
    conn.close()

    db = AgentDatabase(db_path=str(db_file), starting_sol=2.0)
    rows = db.get_watchlist()
    assert len(rows) == 1
    assert rows[0]["safety_passed"] is True
    assert rows[0]["safety_checked_at"] is None  # never backfilled as fresh!
    assert is_safety_timestamp_fresh(rows[0]["safety_checked_at"], 300.0) is False
    db.close()


def test_e4e_timestamp_validation_edges():
    """Malformed, naive, non-UTC, future, non-finite, and expired timestamps are rejected."""
    now_dt = datetime(2026, 11, 27, 16, 0, 0, tzinfo=timezone.utc)
    assert is_safety_timestamp_fresh((now_dt - timedelta(seconds=30)).isoformat(), 60.0, now_dt=now_dt) is True
    assert is_safety_timestamp_fresh((now_dt - timedelta(seconds=61)).isoformat(), 60.0, now_dt=now_dt) is False
    assert is_safety_timestamp_fresh((now_dt + timedelta(seconds=5)).isoformat(), 60.0, now_dt=now_dt) is False
    assert is_safety_timestamp_fresh("2026-11-27T16:00:00", 60.0, now_dt=now_dt) is False  # naive
    assert is_safety_timestamp_fresh("2026-11-27T16:00:00-05:00", 60.0, now_dt=now_dt) is False  # non-UTC
    assert is_safety_timestamp_fresh("not-a-ts", 60.0, now_dt=now_dt) is False
    assert is_safety_timestamp_fresh(None, 60.0, now_dt=now_dt) is False
    assert is_safety_timestamp_fresh(now_dt.isoformat(), None, now_dt=now_dt) is False
    assert is_safety_timestamp_fresh(now_dt.isoformat(), float("nan"), now_dt=now_dt) is False


@pytest.mark.asyncio
async def test_e4e_watchlist_ttl_reuse_expiry_and_no_refresh_on_failure(tmp_path):
    """SafetyGate reuses persisted pass only within SAFETY_CACHE_TTL_SECONDS; expired/failed checks do not refresh timestamp."""
    clear_safety_cache()
    db = AgentDatabase(db_path=str(tmp_path / "e4e_sg.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper", SAFETY_CACHE_TTL_SECONDS=60.0)
    t0 = datetime(2026, 11, 27, 16, 0, 0, tzinfo=timezone.utc)

    db.upsert_watchlist_entry(
        mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED,
        candidate={"mint": MINT_E4E, "symbol": "E4E", "pair_address": POOL_PINNED},
        added_at=t0.isoformat(), safety_passed=True,
    )
    # NULL safety_checked_at -> must re-check even though safety_passed=True
    calls = {"rug": 0, "shield": 0}
    fail_mode = {"on": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if "rugcheck.xyz" in str(request.url):
            calls["rug"] += 1
            if fail_mode["on"]:
                return httpx.Response(500)
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        if "/ultra/v1/shield" in str(request.url):
            calls["shield"] += 1
            return httpx.Response(200, json={"warnings": {MINT_E4E: []}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # Cycle 1 at t0: NULL timestamp -> calls APIs, updates safety_checked_at = t0
        out1 = await safety_gate_node(
            {"focus_state": FocusState.SCANNING.value, "raw_candidates": [{"mint": MINT_E4E, "symbol": "E4E", "safety_passed": True}]},
            config=cfg, db=db, http_client=client, now=t0,
        )
        assert len(out1["safe_candidates"]) == 1
        assert calls == {"rug": 1, "shield": 1}
        assert db.get_watchlist_entry(MINT_E4E)["safety_checked_at"] == t0.isoformat()

        # Cycle 2 at t0 + 30s (within 60s TTL): reuses cache, does NOT call APIs, does NOT refresh safety_checked_at
        clear_safety_cache()  # force persisted DB check rather than in-memory cache
        out2 = await safety_gate_node(
            {"focus_state": FocusState.SCANNING.value, "raw_candidates": [{"mint": MINT_E4E, "symbol": "E4E"}]},
            config=cfg, db=db, http_client=client, now=t0 + timedelta(seconds=30),
        )
        assert len(out2["safe_candidates"]) == 1
        assert calls == {"rug": 1, "shield": 1}
        assert db.get_watchlist_entry(MINT_E4E)["safety_checked_at"] == t0.isoformat()

        # Cycle 3 at t0 + 90s (expired > 60s TTL) with failing RugCheck -> fails closed, does NOT update timestamp
        clear_safety_cache()
        fail_mode["on"] = True
        out3 = await safety_gate_node(
            {"focus_state": FocusState.SCANNING.value, "raw_candidates": [{"mint": MINT_E4E, "symbol": "E4E"}]},
            config=cfg, db=db, http_client=client, now=t0 + timedelta(seconds=90),
        )
        assert out3["safe_candidates"] == []
        assert calls == {"rug": 2, "shield": 2}
        assert db.get_watchlist_entry(MINT_E4E) is None  # evicted on failed safety
    db.close()


@pytest.mark.asyncio
async def test_e4e_pre_execution_safety_unset_fresh_stale_and_lock_freedom(tmp_path):
    """Pre-execution BUY boundary: fails closed if TTL unset; skips network if fresh; refreshes Shield+isSus+RugCheck when stale without holding db._lock."""
    clear_buy_reservations()
    clear_sell_idempotency_guard()
    kp = Keypair()
    unsigned_tx = _mk_unsigned_tx(kp)
    db = AgentDatabase(db_path=str(tmp_path / "e4e_exec.duckdb"), starting_sol=5.0)

    # 1. Unset PRE_EXECUTION_SAFETY_TTL_SECONDS fails closed in live BREAKOUT_IGNITION
    cfg_unset = _mk_live_e4e_cfg(kp, PRE_EXECUTION_SAFETY_TTL_SECONDS=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 5_000_000_000}}))) as client:
        out_unset = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4e_order()},
            config=cfg_unset, db=db, http_client=client,
        )
        assert out_unset == {"pending_order": None}
        assert get_inflight_buy_reservations(str(db.db_path)) == []
        assert db.get_active_locking_intents() == []

    # 2. Stale watchlist entry -> runs Token v2 isSus + RugCheck + Shield WITHOUT holding db._lock, updates safety_checked_at
    cfg = _mk_live_e4e_cfg(kp, PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0)
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    db.upsert_watchlist_entry(
        mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED,
        candidate={"mint": MINT_E4E, "symbol": "E4E", "pair_address": POOL_PINNED},
        added_at=stale_ts, safety_passed=True,
    )
    db.mark_watchlist_safety_passed(MINT_E4E, True, checked_at=stale_ts)

    lock_free_during_network = {"ok": False}
    safety_calls = {"v2": 0, "rug": 0, "shield": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if str(request.url).startswith(RPC_URL):
            body = json.loads(request.content.decode("utf-8"))
            if body["method"] == "getBalance":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 5_000_000_000}})
            if body["method"] == "getBlockHeight":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 990})
        if "/tokens/v2/search" in url:
            safety_calls["v2"] += 1
            # Verify db._lock is NOT held during network I/O
            acquired = db._lock.acquire(blocking=False)
            if acquired:
                lock_free_during_network["ok"] = True
                db._lock.release()
            return httpx.Response(200, json=[{"id": MINT_E4E, "audit": {"isSus": False}}])
        if "rugcheck.xyz" in url:
            safety_calls["rug"] += 1
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        if "/ultra/v1/shield" in url:
            safety_calls["shield"] += 1
            return httpx.Response(200, json={"warnings": {MINT_E4E: []}})
        if "/swap/v2/order" in url:
            return httpx.Response(200, json={
                "outAmount": "1000000000", "otherAmountThreshold": "980000000",
                "slippageBps": 200, "priceImpact": -0.5, "router": "metis", "lastValidBlockHeight": 1000,
                "routePlan": [{"swapInfo": {"ammKey": POOL_PINNED, "inputMint": SOL_MINT, "outputMint": MINT_E4E, "label": "PumpSwap"}, "percent": 100}],
                "transaction": unsigned_tx, "requestId": "req-e4e-1",
            })
        if "/swap/v2/execute" in url:
            return httpx.Response(200, json={"signature": "SigE4EOk1", "outputAmountResult": "1000000000"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out_ok = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4e_order()},
            config=cfg, db=db, http_client=client,
        )
    assert len(out_ok["chart_positions"]) == 1
    assert lock_free_during_network["ok"] is True
    assert safety_calls == {"v2": 1, "rug": 1, "shield": 1}

    # 3. Fresh watchlist entry (within 60s TTL) -> skips safety network calls on next BUY
    mint2 = str(Keypair().pubkey())
    db.upsert_watchlist_entry(
        mint=mint2, symbol="E4E2", pair_address=POOL_PINNED,
        candidate={"mint": mint2, "symbol": "E4E2", "pair_address": POOL_PINNED},
        added_at=datetime.now(timezone.utc).isoformat(), safety_passed=True,
    )
    db.mark_watchlist_safety_passed(mint2, True, checked_at=datetime.now(timezone.utc).isoformat())

    def handler_fresh(request: httpx.Request) -> httpx.Response:
        if "/tokens/v2/search" in str(request.url) or "rugcheck.xyz" in str(request.url) or "/ultra/v1/shield" in str(request.url):
            raise AssertionError("Safety API should not be called when watchlist safety_checked_at is fresh!")
        if str(request.url).startswith(RPC_URL):
            body = json.loads(request.content.decode("utf-8"))
            if body["method"] == "getBalance":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 5_000_000_000}})
            if body["method"] == "getBlockHeight":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 990})
        if "/swap/v2/order" in str(request.url):
            return httpx.Response(200, json={
                "outAmount": "1000000000", "otherAmountThreshold": "980000000",
                "slippageBps": 200, "priceImpact": -0.5, "router": "metis", "lastValidBlockHeight": 1000,
                "routePlan": [{"swapInfo": {"ammKey": POOL_PINNED, "inputMint": SOL_MINT, "outputMint": mint2, "label": "PumpSwap"}, "percent": 100}],
                "transaction": unsigned_tx, "requestId": "req-e4e-2",
            })
        if "/swap/v2/execute" in str(request.url):
            return httpx.Response(200, json={"signature": "SigE4EOk2", "outputAmountResult": "1000000000"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_fresh)) as client:
        out_fresh = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4e_order(mint=mint2)},
            config=cfg, db=db, http_client=client,
        )
    assert len(out_fresh["chart_positions"]) == 2
    db.close()


@pytest.mark.asyncio
async def test_e4e_pre_execution_rejects_shield_issus_api_error_and_pool_mismatch(tmp_path):
    """Shield rejection, Token v2 isSus, API timeout/error, and pinned-pool mismatch fail closed, release reservation, and evict/cooldown on confirmed unsafe."""
    clear_buy_reservations()
    clear_sell_idempotency_guard()
    kp = Keypair()
    cfg = _mk_live_e4e_cfg(kp, PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0)

    # Case A: Token v2 isSus=True -> evict + cooldown + release reservation + no Jupiter order/execute
    db_a = AgentDatabase(db_path=str(tmp_path / "e4e_sus.duckdb"), starting_sol=5.0)
    db_a.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, candidate={"mint": MINT_E4E}, safety_passed=False)

    def handler_sus(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if str(request.url).startswith(RPC_URL):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 5_000_000_000}})
        if "/tokens/v2/search" in url:
            return httpx.Response(200, json=[{"id": MINT_E4E, "audit": {"isSus": True}}])
        if "rugcheck.xyz" in url:
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        if "/ultra/v1/shield" in url:
            return httpx.Response(200, json={"warnings": {MINT_E4E: []}})
        raise AssertionError(f"Unexpected call after unsafe detection: {url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_sus)) as client:
        out_a = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4e_order()},
            config=cfg, db=db_a, http_client=client,
        )
    assert out_a == {"pending_order": None}
    assert get_inflight_buy_reservations(str(db_a.db_path)) == []
    assert db_a.get_active_locking_intents() == []
    assert db_a.get_watchlist_entry(MINT_E4E) is None
    assert db_a.is_on_cooldown(MINT_E4E) is True
    db_a.close()

    # Case B: Jupiter Shield HONEYPOT -> evict + cooldown + release reservation
    db_b = AgentDatabase(db_path=str(tmp_path / "e4e_shield.duckdb"), starting_sol=5.0)
    db_b.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, candidate={"mint": MINT_E4E}, safety_passed=False)

    def handler_shield(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if str(request.url).startswith(RPC_URL):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 5_000_000_000}})
        if "/tokens/v2/search" in url:
            return httpx.Response(200, json=[{"id": MINT_E4E, "audit": {"isSus": False}}])
        if "rugcheck.xyz" in url:
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        if "/ultra/v1/shield" in url:
            return httpx.Response(200, json={"warnings": {MINT_E4E: [{"type": "HONEYPOT", "severity": "critical", "message": "Honeypot"}]}})
        raise AssertionError(f"Unexpected call: {url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_shield)) as client:
        out_b = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4e_order()},
            config=cfg, db=db_b, http_client=client,
        )
    assert out_b == {"pending_order": None}
    assert get_inflight_buy_reservations(str(db_b.db_path)) == []
    assert db_b.is_on_cooldown(MINT_E4E) is True
    db_b.close()

    # Case C: API timeout -> fail closed, release reservation, do NOT mark safety_checked_at fresh
    db_c = AgentDatabase(db_path=str(tmp_path / "e4e_err.duckdb"), starting_sol=5.0)
    db_c.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, candidate={"mint": MINT_E4E}, safety_passed=False)

    def handler_err(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(RPC_URL):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 5_000_000_000}})
        raise httpx.ReadTimeout("safety API timeout")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_err)) as client:
        out_c = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4e_order()},
            config=cfg, db=db_c, http_client=client,
        )
    assert out_c == {"pending_order": None}
    assert get_inflight_buy_reservations(str(db_c.db_path)) == []
    assert db_c.get_active_locking_intents() == []
    assert db_c.get_watchlist_entry(MINT_E4E)["safety_checked_at"] is None
    db_c.close()

    # Case D: Pinned pool mismatch between order and watchlist fails closed
    db_d = AgentDatabase(db_path=str(tmp_path / "e4e_pool.duckdb"), starting_sol=5.0)
    db_d.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address=POOL_OTHER, candidate={"mint": MINT_E4E}, safety_passed=True)
    db_d.mark_watchlist_safety_passed(MINT_E4E, True, checked_at=datetime.now(timezone.utc).isoformat())
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 5_000_000_000}}))) as client:
        out_d = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4e_order()},
            config=cfg, db=db_d, http_client=client,
        )
    assert out_d == {"pending_order": None}
    assert get_inflight_buy_reservations(str(db_d.db_path)) == []
    db_d.close()
