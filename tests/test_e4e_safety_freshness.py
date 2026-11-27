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
        # Unavailable API failure must NOT evict or refresh safety_checked_at
        row3 = db.get_watchlist_entry(MINT_E4E)
        assert row3 is not None and row3["safety_checked_at"] == t0.isoformat()
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
        if "rugcheck.xyz" in str(request.url) or "/ultra/v1/shield" in str(request.url):
            raise AssertionError("RugCheck/Shield should not be called when watchlist safety_checked_at is fresh!")
        if "/tokens/v2/search" in str(request.url):
            return httpx.Response(200, json=[{"id": mint2, "audit": {"isSus": False}}])
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


@pytest.mark.asyncio
async def test_e4e_corrective_no_unconfigured_memory_cache_bypass(tmp_path):
    """Blocker 1: In-memory cached pass is NEVER reused when SAFETY_CACHE_TTL_SECONDS is unset, expired, or has malformed/future timestamp."""
    from safety_gate_node import _safety_cache, cache_safety_report, get_cached_safety_report
    from state import SafetyReport

    clear_safety_cache()
    db = AgentDatabase(db_path=str(tmp_path / "e4e_mem_bypass.duckdb"), starting_sol=2.0)
    t0 = datetime(2026, 11, 27, 16, 0, 0, tzinfo=timezone.utc)
    calls = {"rug": 0, "shield": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "rugcheck.xyz" in str(request.url):
            calls["rug"] += 1
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        if "/ultra/v1/shield" in str(request.url):
            calls["shield"] += 1
            return httpx.Response(200, json={"warnings": {MINT_E4E: []}})
        return httpx.Response(404)

    # 1. TTL unset + pre-populated in-memory cache: must NOT authorize a pass without calling APIs
    _safety_cache[MINT_E4E] = {
        "fetched_at": t0.timestamp(),
        "report": SafetyReport(mint=MINT_E4E, passed=True, reasons=[]),
    }
    assert get_cached_safety_report(MINT_E4E, now_ts=t0.timestamp(), ttl_seconds=None) is None

    cfg_unset = AgentConfig(_env_file=None, EXECUTION_MODE="paper", SAFETY_CACHE_TTL_SECONDS=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await safety_gate_node(
            {"focus_state": FocusState.SCANNING.value, "raw_candidates": [{"mint": MINT_E4E, "symbol": "E4E"}]},
            config=cfg_unset, db=db, http_client=client, now=t0,
        )
    assert len(out["safe_candidates"]) == 1
    assert out["safe_candidates"][0]["safety_report"].get("cached") is not True
    assert calls == {"rug": 1, "shield": 1}

    # 2. Malformed / future cached fetched_at never authorizes a pass
    _safety_cache[MINT_E4E] = {
        "fetched_at": t0.timestamp() + 60.0,  # future
        "report": SafetyReport(mint=MINT_E4E, passed=True, reasons=[]),
    }
    assert get_cached_safety_report(MINT_E4E, now_ts=t0.timestamp(), ttl_seconds=120.0) is None
    _safety_cache[MINT_E4E] = {
        "fetched_at": "bad-ts",
        "report": SafetyReport(mint=MINT_E4E, passed=True, reasons=[]),
    }
    assert get_cached_safety_report(MINT_E4E, now_ts=t0.timestamp(), ttl_seconds=120.0) is None

    # 3. Valid configured TTL reuses cache within TTL and expires after TTL
    clear_safety_cache()
    cache_safety_report(MINT_E4E, SafetyReport(mint=MINT_E4E, passed=True, reasons=[]), now_ts=t0.timestamp(), ttl_seconds=60.0)
    assert get_cached_safety_report(MINT_E4E, now_ts=t0.timestamp() + 30.0, ttl_seconds=60.0) is not None
    assert get_cached_safety_report(MINT_E4E, now_ts=t0.timestamp() + 61.0, ttl_seconds=60.0) is None
    db.close()


@pytest.mark.asyncio
async def test_e4e_corrective_incomplete_evidence_fails_closed_without_eviction(tmp_path):
    """Blocker 2: Missing/malformed Token v2 audit.isSus, empty/malformed RugCheck, and empty/missing-mint Shield fail closed WITHOUT evicting or marking fresh."""
    clear_buy_reservations()
    clear_sell_idempotency_guard()
    kp = Keypair()
    cfg = _mk_live_e4e_cfg(kp, PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0)

    incomplete_scenarios = [
        # Missing isSus in audit
        ([{"id": MINT_E4E, "audit": {}}], {"rugged": False}, {"warnings": {MINT_E4E: []}}),
        # Null isSus
        ([{"id": MINT_E4E, "audit": {"isSus": None}}], {"rugged": False}, {"warnings": {MINT_E4E: []}}),
        # String isSus
        ([{"id": MINT_E4E, "audit": {"isSus": "false"}}], {"rugged": False}, {"warnings": {MINT_E4E: []}}),
        # Wrong-mint Token v2 result
        ([{"id": "WrongMint1111111111111111111111111111111111", "audit": {"isSus": False}}], {"rugged": False}, {"warnings": {MINT_E4E: []}}),
        # Empty RugCheck JSON
        ([{"id": MINT_E4E, "audit": {"isSus": False}}], {}, {"warnings": {MINT_E4E: []}}),
        # Malformed RugCheck JSON
        ([{"id": MINT_E4E, "audit": {"isSus": False}}], {"unexpected": True}, {"warnings": {MINT_E4E: []}}),
        # Empty Shield JSON
        ([{"id": MINT_E4E, "audit": {"isSus": False}}], {"rugged": False}, {}),
        # Missing-mint Shield JSON
        ([{"id": MINT_E4E, "audit": {"isSus": False}}], {"rugged": False}, {"warnings": {}}),
        # Malformed Shield warnings
        ([{"id": MINT_E4E, "audit": {"isSus": False}}], {"rugged": False}, {"warnings": {MINT_E4E: "bad"}}),
    ]

    for idx, (v2_body, rug_body, shield_body) in enumerate(incomplete_scenarios):
        clear_buy_reservations()
        db = AgentDatabase(db_path=str(tmp_path / f"e4e_inc_{idx}.duckdb"), starting_sol=5.0)
        db.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, candidate={"mint": MINT_E4E}, safety_passed=False)

        def handler(request: httpx.Request, vb=v2_body, rb=rug_body, sb=shield_body) -> httpx.Response:
            url = str(request.url)
            if url.startswith(RPC_URL):
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 5_000_000_000}})
            if "/tokens/v2/search" in url:
                return httpx.Response(200, json=vb)
            if "rugcheck.xyz" in url:
                return httpx.Response(200, json=rb)
            if "/ultra/v1/shield" in url:
                return httpx.Response(200, json=sb)
            raise AssertionError(f"Unexpected request after incomplete safety evidence: {url}")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            out = await executor_node(
                {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4e_order()},
                config=cfg, db=db, http_client=client,
            )
        assert out == {"pending_order": None}
        assert get_inflight_buy_reservations(str(db.db_path)) == []
        assert db.get_active_locking_intents() == []
        # Unavailable/malformed evidence must NOT evict or set cooldown or mark fresh
        row = db.get_watchlist_entry(MINT_E4E)
        assert row is not None and row["safety_checked_at"] is None
        assert db.is_on_cooldown(MINT_E4E) is False
        db.close()


@pytest.mark.asyncio
async def test_e4e_safety_gate_unavailable_does_not_evict_while_confirmed_and_mixed_do(tmp_path):
    """Discovery-side API failure/malformed Shield rejects for cycle without eviction/cooldown/refresh; confirmed and mixed unsafe evict."""
    clear_safety_cache()
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper", SAFETY_CACHE_TTL_SECONDS=60.0)
    t0 = datetime(2026, 11, 27, 16, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=120)

    # 1. RugCheck HTTP 500 (unavailable only) -> rejected for cycle, NOT evicted, NO cooldown, NO timestamp refresh
    db1 = AgentDatabase(db_path=str(tmp_path / "sg_rug_500.duckdb"), starting_sol=2.0)
    db1.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, candidate={"mint": MINT_E4E}, added_at=t0.isoformat(), safety_passed=True)
    db1.mark_watchlist_safety_passed(MINT_E4E, True, checked_at=t0.isoformat())

    def h_rug_500(req: httpx.Request) -> httpx.Response:
        if "rugcheck.xyz" in str(req.url):
            return httpx.Response(500)
        return httpx.Response(200, json={"warnings": {MINT_E4E: []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(h_rug_500)) as client:
        out1 = await safety_gate_node(
            {"focus_state": FocusState.SCANNING.value, "raw_candidates": [{"mint": MINT_E4E, "symbol": "E4E"}]},
            config=cfg, db=db1, http_client=client, now=t1,
        )
    assert out1["safe_candidates"] == [] and len(out1["rejected_candidates"]) == 1
    assert db1.get_watchlist_entry(MINT_E4E)["safety_checked_at"] == t0.isoformat()
    assert db1.is_on_cooldown(MINT_E4E, now=t1) is False
    assert db1.get_action_log(limit=5, actions=["EVICT"]) == []
    db1.close()

    # 2. Malformed / missing-mint Shield response (unavailable only) -> rejected for cycle, NOT evicted, NO cooldown
    clear_safety_cache()
    db2 = AgentDatabase(db_path=str(tmp_path / "sg_shield_miss.duckdb"), starting_sol=2.0)
    db2.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, candidate={"mint": MINT_E4E}, added_at=t0.isoformat(), safety_passed=False)

    def h_sh_miss(req: httpx.Request) -> httpx.Response:
        if "rugcheck.xyz" in str(req.url):
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        return httpx.Response(200, json={"warnings": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(h_sh_miss)) as client:
        out2 = await safety_gate_node(
            {"focus_state": FocusState.SCANNING.value, "raw_candidates": [{"mint": MINT_E4E, "symbol": "E4E"}]},
            config=cfg, db=db2, http_client=client, now=t1,
        )
    assert out2["safe_candidates"] == [] and len(out2["rejected_candidates"]) == 1
    row2 = db2.get_watchlist_entry(MINT_E4E)
    assert row2 is not None and row2["safety_checked_at"] is None
    assert db2.is_on_cooldown(MINT_E4E, now=t1) is False
    assert db2.get_action_log(limit=5, actions=["EVICT"]) == []
    db2.close()

    # 3. Confirmed unsafe (RugCheck rugged=True) -> evicted + cooldown + EVICT log
    clear_safety_cache()
    db3 = AgentDatabase(db_path=str(tmp_path / "sg_unsafe.duckdb"), starting_sol=2.0)
    db3.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, candidate={"mint": MINT_E4E}, added_at=t0.isoformat(), safety_passed=False)

    def h_unsafe(req: httpx.Request) -> httpx.Response:
        if "rugcheck.xyz" in str(req.url):
            return httpx.Response(200, json={"rugged": True, "risks": []})
        return httpx.Response(200, json={"warnings": {MINT_E4E: []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(h_unsafe)) as client:
        out3 = await safety_gate_node(
            {"focus_state": FocusState.SCANNING.value, "raw_candidates": [{"mint": MINT_E4E, "symbol": "E4E"}]},
            config=cfg, db=db3, http_client=client, now=t1,
        )
    assert out3["safe_candidates"] == []
    assert db3.get_watchlist_entry(MINT_E4E) is None
    assert db3.is_on_cooldown(MINT_E4E, now=t1) is True
    assert len(db3.get_action_log(limit=5, actions=["EVICT"])) == 1
    db3.close()

    # 4. Mixed confirmed-unsafe (Shield HONEYPOT) + unavailable (RugCheck 500) -> evicted + cooldown + EVICT log
    clear_safety_cache()
    db4 = AgentDatabase(db_path=str(tmp_path / "sg_mixed.duckdb"), starting_sol=2.0)
    db4.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, candidate={"mint": MINT_E4E}, added_at=t0.isoformat(), safety_passed=False)

    def h_mixed(req: httpx.Request) -> httpx.Response:
        if "rugcheck.xyz" in str(req.url):
            return httpx.Response(500)
        return httpx.Response(200, json={"warnings": {MINT_E4E: [{"type": "HONEYPOT", "severity": "critical", "message": "Honeypot"}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(h_mixed)) as client:
        out4 = await safety_gate_node(
            {"focus_state": FocusState.SCANNING.value, "raw_candidates": [{"mint": MINT_E4E, "symbol": "E4E"}]},
            config=cfg, db=db4, http_client=client, now=t1,
        )
    assert out4["safe_candidates"] == []
    assert db4.get_watchlist_entry(MINT_E4E) is None
    assert db4.is_on_cooldown(MINT_E4E, now=t1) is True
    assert len(db4.get_action_log(limit=5, actions=["EVICT"])) == 1
    db4.close()


@pytest.mark.asyncio
async def test_e4e_corrective_paper_and_non_breakout_live_require_ttl_and_sell_unaffected(tmp_path):
    """Blocker 3: Unset PRE_EXECUTION_SAFETY_TTL_SECONDS blocks BUY in BOTH paper and live (including SCALPER), leaving SELL unaltered."""
    clear_buy_reservations()
    clear_sell_idempotency_guard()
    kp = Keypair()
    db = AgentDatabase(db_path=str(tmp_path / "e4e_ttl_both.duckdb"), starting_sol=5.0)
    db.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, candidate={"mint": MINT_E4E}, safety_passed=True)
    db.mark_watchlist_safety_passed(MINT_E4E, True, checked_at=datetime.now(timezone.utc).isoformat())

    jup_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(RPC_URL):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 5_000_000_000}})
        if "/swap/v2/" in str(request.url):
            jup_calls["n"] += 1
            return httpx.Response(200, json={"outAmount": str(int(0.50 * LAMPORTS_PER_SOL)), "transaction": None})
        return httpx.Response(404)

    # 1. Paper BUY with unset TTL -> blocked before Jupiter
    cfg_paper_unset = AgentConfig(_env_file=None, EXECUTION_MODE="paper", PRE_EXECUTION_SAFETY_TTL_SECONDS=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out_p = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4e_order(strategy="SCALPER")},
            config=cfg_paper_unset, db=db, http_client=client,
        )
    assert out_p == {"pending_order": None}
    assert jup_calls["n"] == 0
    assert get_inflight_buy_reservations(str(db.db_path)) == []
    assert db.get_active_locking_intents() == []

    # 2. Live SCALPER BUY with unset TTL -> blocked before Jupiter
    cfg_live_unset = _mk_live_e4e_cfg(kp, PRE_EXECUTION_SAFETY_TTL_SECONDS=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out_l = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4e_order(strategy="SCALPER")},
            config=cfg_live_unset, db=db, http_client=client,
        )
    assert out_l == {"pending_order": None}
    assert jup_calls["n"] == 0
    assert get_inflight_buy_reservations(str(db.db_path)) == []
    assert db.get_active_locking_intents() == []

    # 3. Paper SELL with unset TTL -> unaltered (succeeds)
    from state import ActivePosition
    pos = ActivePosition(mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, dex_id="pumpswap", entry_sol=0.50, token_amount=1_000_000).model_dump()
    sell_order = PendingOrder(
        side="SELL", mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, dex_id="pumpswap",
        amount_atomic=1_000_000, reason="TRAILING_TP", target_state_after_fill=FocusState.SCANNING,
    ).model_dump()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out_sell = await executor_node(
            {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 4.5, "daily_pnl_sol": 0.0, "active_position": pos, "chart_positions": [pos], "pending_order": sell_order},
            config=cfg_paper_unset, db=db, http_client=client,
        )
    assert out_sell["last_execution"]["side"] == "SELL"
    db.close()


@pytest.mark.asyncio
async def test_e4e_corrective_pinned_pool_required_before_any_safety_or_quote(tmp_path):
    """Blocker 4: Missing watchlist row, empty/invalid watchlist pair_address, or mismatch blocks BREAKOUT_IGNITION BUY before safety API or Jupiter."""
    clear_buy_reservations()
    clear_sell_idempotency_guard()
    kp = Keypair()
    cfg = _mk_live_e4e_cfg(kp, PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0)
    api_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(RPC_URL):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 5_000_000_000}})
        api_calls["n"] += 1
        return httpx.Response(500)

    # 1. Missing watchlist row
    db1 = AgentDatabase(db_path=str(tmp_path / "e4e_nowl.duckdb"), starting_sol=5.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out1 = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4e_order()},
            config=cfg, db=db1, http_client=client,
        )
    assert out1 == {"pending_order": None}
    assert api_calls["n"] == 0
    assert get_inflight_buy_reservations(str(db1.db_path)) == []
    db1.close()

    # 2. Empty watchlist pair_address
    db2 = AgentDatabase(db_path=str(tmp_path / "e4e_emptypool.duckdb"), starting_sol=5.0)
    db2.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address="", candidate={"mint": MINT_E4E}, safety_passed=True)
    db2.mark_watchlist_safety_passed(MINT_E4E, True, checked_at=datetime.now(timezone.utc).isoformat())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out2 = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4e_order()},
            config=cfg, db=db2, http_client=client,
        )
    assert out2 == {"pending_order": None}
    assert api_calls["n"] == 0
    assert get_inflight_buy_reservations(str(db2.db_path)) == []
    db2.close()


@pytest.mark.asyncio
async def test_e4e_fresh_watchlist_still_requires_explicit_token_v2_is_sus(tmp_path):
    """Finding 1: Fresh safety_checked_at skips RugCheck/Shield but NEVER bypasses Token v2 audit.isSus."""
    clear_buy_reservations()
    clear_sell_idempotency_guard()
    kp = Keypair()
    unsigned_tx = _mk_unsigned_tx(kp)
    cfg = _mk_live_e4e_cfg(kp, PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0)
    fresh_ts = datetime.now(timezone.utc).isoformat()

    # 1. Fresh watchlist + missing/malformed Token v2 -> fails closed WITHOUT evicting
    for bad_v2 in (
        [],
        [{"id": MINT_E4E, "audit": {}}],
        [{"id": MINT_E4E, "audit": {"isSus": "false"}}],
    ):
        clear_buy_reservations()
        db = AgentDatabase(db_path=str(tmp_path / "e4e_f1_bad.duckdb"), starting_sol=5.0)
        db.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, candidate={"mint": MINT_E4E}, safety_passed=True)
        db.mark_watchlist_safety_passed(MINT_E4E, True, checked_at=fresh_ts)

        def handler_bad_v2(request: httpx.Request, payload=bad_v2) -> httpx.Response:
            url = str(request.url)
            if url.startswith(RPC_URL):
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 5_000_000_000}})
            if "/tokens/v2/search" in url:
                return httpx.Response(200, json=payload)
            raise AssertionError(f"Unexpected call: {url}")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler_bad_v2)) as client:
            out = await executor_node(
                {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4e_order()},
                config=cfg, db=db, http_client=client,
            )
        assert out == {"pending_order": None}
        assert get_inflight_buy_reservations(str(db.db_path)) == []
        assert db.get_watchlist_entry(MINT_E4E) is not None
        assert db.is_on_cooldown(MINT_E4E) is False
        db.close()

    # 2. Fresh watchlist + isSus=True -> confirmed unsafe: evicts + sets cooldown + aborts
    db_sus = AgentDatabase(db_path=str(tmp_path / "e4e_f1_sus.duckdb"), starting_sol=5.0)
    db_sus.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, candidate={"mint": MINT_E4E}, safety_passed=True)
    db_sus.mark_watchlist_safety_passed(MINT_E4E, True, checked_at=fresh_ts)

    def handler_sus_v2(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(RPC_URL):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 5_000_000_000}})
        if "/tokens/v2/search" in url:
            return httpx.Response(200, json=[{"id": MINT_E4E, "audit": {"isSus": True}}])
        raise AssertionError(f"Unexpected call: {url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_sus_v2)) as client:
        out_sus = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4e_order()},
            config=cfg, db=db_sus, http_client=client,
        )
    assert out_sus == {"pending_order": None}
    assert db_sus.get_watchlist_entry(MINT_E4E) is None
    assert db_sus.is_on_cooldown(MINT_E4E) is True
    db_sus.close()

    # 3. Fresh watchlist + explicit isSus=False -> skips RugCheck/Shield and succeeds
    db_ok = AgentDatabase(db_path=str(tmp_path / "e4e_f1_ok.duckdb"), starting_sol=5.0)
    db_ok.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, candidate={"mint": MINT_E4E}, safety_passed=True)
    db_ok.mark_watchlist_safety_passed(MINT_E4E, True, checked_at=fresh_ts)

    def handler_ok_v2(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "rugcheck.xyz" in url or "/ultra/v1/shield" in url:
            raise AssertionError("RugCheck/Shield should be skipped when fresh!")
        if url.startswith(RPC_URL):
            body = json.loads(request.content.decode("utf-8"))
            if body["method"] == "getBalance":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 5_000_000_000}})
            if body["method"] == "getBlockHeight":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 990})
        if "/tokens/v2/search" in url:
            return httpx.Response(200, json=[{"id": MINT_E4E, "audit": {"isSus": False}}])
        if "/swap/v2/order" in url:
            return httpx.Response(200, json={
                "outAmount": "1000000000", "otherAmountThreshold": "980000000",
                "slippageBps": 200, "priceImpact": -0.5, "router": "metis", "lastValidBlockHeight": 1000,
                "routePlan": [{"swapInfo": {"ammKey": POOL_PINNED, "inputMint": SOL_MINT, "outputMint": MINT_E4E, "label": "PumpSwap"}, "percent": 100}],
                "transaction": unsigned_tx, "requestId": "req-f1-ok",
            })
        if "/swap/v2/execute" in url:
            return httpx.Response(200, json={"signature": "SigF1Ok", "outputAmountResult": "1000000000"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_ok_v2)) as client:
        out_ok = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4e_order()},
            config=cfg, db=db_ok, http_client=client,
        )
    assert len(out_ok["chart_positions"]) == 1
    db_ok.close()


@pytest.mark.asyncio
async def test_e4e_nested_empty_jupiter_shield_rejected_without_eviction(tmp_path):
    """Finding 2: Empty/incomplete nested Shield shapes fail closed without eviction; valid shapes pass."""
    from safety_gate_node import validate_jupiter_shield_structure

    # Direct structure validation checks
    assert validate_jupiter_shield_structure({"mints": {MINT_E4E: {}}}, MINT_E4E) is not None
    assert validate_jupiter_shield_structure({"mints": {MINT_E4E: {"shield": {}}}}, MINT_E4E) is not None
    assert validate_jupiter_shield_structure([{"mint": MINT_E4E}], MINT_E4E) is not None
    assert validate_jupiter_shield_structure([{"mint": MINT_E4E, "shield": {}}], MINT_E4E) is not None

    assert validate_jupiter_shield_structure({"warnings": {MINT_E4E: []}}, MINT_E4E) is None
    assert validate_jupiter_shield_structure({"mints": {MINT_E4E: {"shield": {"warnings": []}}}}, MINT_E4E) is None
    assert validate_jupiter_shield_structure([{"mint": MINT_E4E, "shield": {"warnings": []}}], MINT_E4E) is None

    # End-to-end executor rejection on {"mints": {mint: {}}} and {"mints": {mint: {"shield": {}}}}
    clear_buy_reservations()
    clear_sell_idempotency_guard()
    kp = Keypair()
    cfg = _mk_live_e4e_cfg(kp, PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0)

    for idx, shield_payload in enumerate((
        {"mints": {MINT_E4E: {}}},
        {"mints": {MINT_E4E: {"shield": {}}}},
    )):
        clear_buy_reservations()
        db = AgentDatabase(db_path=str(tmp_path / f"e4e_sh_empty_{idx}.duckdb"), starting_sol=5.0)
        db.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, candidate={"mint": MINT_E4E}, safety_passed=False)

        def handler_sh(request: httpx.Request, sp=shield_payload) -> httpx.Response:
            url = str(request.url)
            if url.startswith(RPC_URL):
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 5_000_000_000}})
            if "/tokens/v2/search" in url:
                return httpx.Response(200, json=[{"id": MINT_E4E, "audit": {"isSus": False}}])
            if "rugcheck.xyz" in url:
                return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
            if "/ultra/v1/shield" in url:
                return httpx.Response(200, json=sp)
            raise AssertionError(f"Unexpected call: {url}")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler_sh)) as client:
            out = await executor_node(
                {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4e_order()},
                config=cfg, db=db, http_client=client,
            )
        assert out == {"pending_order": None}
        assert get_inflight_buy_reservations(str(db.db_path)) == []
        row = db.get_watchlist_entry(MINT_E4E)
        assert row is not None and row["safety_checked_at"] is None
        assert db.is_on_cooldown(MINT_E4E) is False
        db.close()


@pytest.mark.asyncio
async def test_e4e_safety_gate_unavailable_does_not_evict_while_confirmed_and_mixed_do(tmp_path):
    """Discovery-side API failure/malformed Shield rejects for cycle without eviction/cooldown/refresh; confirmed and mixed unsafe evict."""
    clear_safety_cache()
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper", SAFETY_CACHE_TTL_SECONDS=60.0)
    t0 = datetime(2026, 11, 27, 16, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=120)

    # 1. RugCheck HTTP 500 (unavailable only) -> rejected for cycle, NOT evicted, NO cooldown, NO timestamp refresh
    db1 = AgentDatabase(db_path=str(tmp_path / "sg_rug_500.duckdb"), starting_sol=2.0)
    db1.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, candidate={"mint": MINT_E4E}, added_at=t0.isoformat(), safety_passed=True)
    db1.mark_watchlist_safety_passed(MINT_E4E, True, checked_at=t0.isoformat())

    def h_rug_500(req: httpx.Request) -> httpx.Response:
        if "rugcheck.xyz" in str(req.url):
            return httpx.Response(500)
        return httpx.Response(200, json={"warnings": {MINT_E4E: []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(h_rug_500)) as client:
        out1 = await safety_gate_node(
            {"focus_state": FocusState.SCANNING.value, "raw_candidates": [{"mint": MINT_E4E, "symbol": "E4E"}]},
            config=cfg, db=db1, http_client=client, now=t1,
        )
    assert out1["safe_candidates"] == [] and len(out1["rejected_candidates"]) == 1
    assert db1.get_watchlist_entry(MINT_E4E)["safety_checked_at"] == t0.isoformat()
    assert db1.is_on_cooldown(MINT_E4E, now=t1) is False
    assert db1.get_action_log(limit=5, actions=["EVICT"]) == []
    db1.close()

    # 2. Malformed / missing-mint Shield response (unavailable only) -> rejected for cycle, NOT evicted, NO cooldown
    clear_safety_cache()
    db2 = AgentDatabase(db_path=str(tmp_path / "sg_shield_miss.duckdb"), starting_sol=2.0)
    db2.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, candidate={"mint": MINT_E4E}, added_at=t0.isoformat(), safety_passed=False)

    def h_sh_miss(req: httpx.Request) -> httpx.Response:
        if "rugcheck.xyz" in str(req.url):
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        return httpx.Response(200, json={"warnings": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(h_sh_miss)) as client:
        out2 = await safety_gate_node(
            {"focus_state": FocusState.SCANNING.value, "raw_candidates": [{"mint": MINT_E4E, "symbol": "E4E"}]},
            config=cfg, db=db2, http_client=client, now=t1,
        )
    assert out2["safe_candidates"] == [] and len(out2["rejected_candidates"]) == 1
    row2 = db2.get_watchlist_entry(MINT_E4E)
    assert row2 is not None and row2["safety_checked_at"] is None
    assert db2.is_on_cooldown(MINT_E4E, now=t1) is False
    assert db2.get_action_log(limit=5, actions=["EVICT"]) == []
    db2.close()

    # 3. Confirmed unsafe (RugCheck rugged=True) -> evicted + cooldown + EVICT log
    clear_safety_cache()
    db3 = AgentDatabase(db_path=str(tmp_path / "sg_unsafe.duckdb"), starting_sol=2.0)
    db3.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, candidate={"mint": MINT_E4E}, added_at=t0.isoformat(), safety_passed=False)

    def h_unsafe(req: httpx.Request) -> httpx.Response:
        if "rugcheck.xyz" in str(req.url):
            return httpx.Response(200, json={"rugged": True, "risks": []})
        return httpx.Response(200, json={"warnings": {MINT_E4E: []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(h_unsafe)) as client:
        out3 = await safety_gate_node(
            {"focus_state": FocusState.SCANNING.value, "raw_candidates": [{"mint": MINT_E4E, "symbol": "E4E"}]},
            config=cfg, db=db3, http_client=client, now=t1,
        )
    assert out3["safe_candidates"] == []
    assert db3.get_watchlist_entry(MINT_E4E) is None
    assert db3.is_on_cooldown(MINT_E4E, now=t1) is True
    assert len(db3.get_action_log(limit=5, actions=["EVICT"])) == 1
    db3.close()

    # 4. Mixed confirmed-unsafe (Shield HONEYPOT) + unavailable (RugCheck 500) -> evicted + cooldown + EVICT log
    clear_safety_cache()
    db4 = AgentDatabase(db_path=str(tmp_path / "sg_mixed.duckdb"), starting_sol=2.0)
    db4.upsert_watchlist_entry(mint=MINT_E4E, symbol="E4E", pair_address=POOL_PINNED, candidate={"mint": MINT_E4E}, added_at=t0.isoformat(), safety_passed=False)

    def h_mixed(req: httpx.Request) -> httpx.Response:
        if "rugcheck.xyz" in str(req.url):
            return httpx.Response(500)
        return httpx.Response(200, json={"warnings": {MINT_E4E: [{"type": "HONEYPOT", "severity": "critical", "message": "Honeypot"}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(h_mixed)) as client:
        out4 = await safety_gate_node(
            {"focus_state": FocusState.SCANNING.value, "raw_candidates": [{"mint": MINT_E4E, "symbol": "E4E"}]},
            config=cfg, db=db4, http_client=client, now=t1,
        )
    assert out4["safe_candidates"] == []
    assert db4.get_watchlist_entry(MINT_E4E) is None
    assert db4.is_on_cooldown(MINT_E4E, now=t1) is True
    assert len(db4.get_action_log(limit=5, actions=["EVICT"])) == 1
    db4.close()
