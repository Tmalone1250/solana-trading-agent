"""Phase E4B regression tests: Execution Pool Integrity (Router-Specific Contract)."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

import executor_node as ex_mod
from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase
from executor_node import clear_sell_idempotency_guard, executor_node
from state import FocusState, PendingOrder
from state_ledger import clear_buy_reservations, get_inflight_buy_reservations

POOL_PINNED = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"
POOL_OTHER = "7qbRF6YsyGuLUVs6Y1q64bdVrfe4ZcUUz1JRdoVNUJnm"
MINT_E4B = "p2Yicb86aZig616Eav2VWG9vuXR5mEqhtzshZYBxzsV"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RPC_URL = "https://rpc.e4b.test"


def _mk_unsigned_tx(kp: Keypair) -> str:
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=kp.pubkey(), lamports=1))
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.default())
    return base64.b64encode(bytes(VersionedTransaction(msg, [kp]))).decode("ascii")


def _mk_live_e4b_cfg(kp: Keypair, **overrides: Any) -> AgentConfig:
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
        "MAX_QUOTE_AGE_SECONDS": 10.0,
        "MAX_ACCEPTED_SLIPPAGE_BPS": 300,
        "MAX_ENTRY_PRICE_IMPACT_PCT": 2.5,
        "MIN_BLOCK_HEIGHT_HEADROOM": 5,
        "MIN_RFQ_EXPIRY_HEADROOM_SECONDS": 5.0,
        "PRE_EXECUTION_SAFETY_TTL_SECONDS": 60.0,
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _mk_e4b_order(mint: str = MINT_E4B, **overrides: Any) -> dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat()
    defaults: dict[str, Any] = {
        "side": "BUY",
        "mint": mint,
        "symbol": "E4B",
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


def _mk_hop(in_mint: str, out_mint: str, amm_key: Any, label: str = "PumpSwap") -> dict[str, Any]:
    swap_info: dict[str, Any] = {
        "label": label,
        "inputMint": in_mint,
        "outputMint": out_mint,
        "inAmount": "500000000",
        "outAmount": "1000000000",
    }
    if amm_key is not None:
        swap_info["ammKey"] = amm_key
    return {"swapInfo": swap_info, "percent": 100}


@pytest.mark.asyncio
async def test_e4b_live_metis_verified_pinned_pool_and_multi_hop_continuity(tmp_path):
    """Valid Metis route matching pinned pool succeeds and preserves pair_address, dex_id, and PINNED status."""
    clear_buy_reservations()
    clear_sell_idempotency_guard()
    kp = Keypair()
    unsigned_tx = _mk_unsigned_tx(kp)
    db = AgentDatabase(db_path=str(tmp_path / "e4b_ok.duckdb"), starting_sol=5.0)
    db.upsert_watchlist_entry(mint=MINT_E4B, symbol="E4B", pair_address=POOL_PINNED, candidate={"mint": MINT_E4B}, safety_passed=True)
    db.mark_watchlist_safety_passed(MINT_E4B, True, checked_at=datetime.now(timezone.utc).isoformat())
    cfg = _mk_live_e4b_cfg(kp)

    # Multi-hop: SOL -> USDC (POOL_OTHER) then USDC -> MINT_E4B (POOL_PINNED)
    # Also label is "Raydium CLMM" to prove swapInfo.label never overwrites pinned dex_id
    route_plan = [
        _mk_hop(SOL_MINT, USDC_MINT, POOL_OTHER, label="Orca"),
        _mk_hop(USDC_MINT, MINT_E4B, POOL_PINNED, label="Raydium CLMM"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(RPC_URL):
            body = json.loads(request.content.decode("utf-8"))
            if body["method"] == "getBalance":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 5_000_000_000}})
            if body["method"] == "getBlockHeight":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 990})
        if "/swap/v2/order" in str(request.url):
            return httpx.Response(200, json={
                "outAmount": "1000000000",
                "otherAmountThreshold": "980000000",
                "slippageBps": 200,
                "priceImpact": -0.5,
                "router": "metis",
                "lastValidBlockHeight": 1000,
                "routePlan": route_plan,
                "transaction": unsigned_tx,
                "requestId": "req-e4b-ok",
            })
        if "/swap/v2/execute" in str(request.url):
            return httpx.Response(200, json={"signature": "SigE4BOk111", "outputAmountResult": "1000000000"})
        if "/tokens/v2/search" in str(request.url):
            return httpx.Response(200, json=[{"id": request.url.params.get("query", MINT_E4B), "audit": {"isSus": False}}])
        if "rugcheck.xyz" in str(request.url):
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        if "/ultra/v1/shield" in str(request.url):
            return httpx.Response(200, json={"warnings": {}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4b_order()},
            config=cfg, db=db, http_client=client,
        )

    assert len(out["chart_positions"]) == 1
    pos = out["chart_positions"][0]
    assert pos["pair_address"] == POOL_PINNED
    assert pos["dex_id"] == "pumpswap"
    assert pos["pool_identity_status"] == "PINNED"
    assert out["last_execution"]["router"] == "metis"
    assert out["last_execution"]["pool_route_status"] == "VERIFIED_PINNED_POOL"
    assert out["last_execution"]["route_pool_keys"] == [POOL_OTHER, POOL_PINNED]

    rows = db.get_action_log(limit=5, actions=["BUY"])
    assert len(rows) == 1
    assert rows[0]["router"] == "metis"
    assert rows[0]["pool_route_status"] == "VERIFIED_PINNED_POOL"
    assert json.loads(rows[0]["route_pool_keys"]) == [POOL_OTHER, POOL_PINNED]
    db.close()


@pytest.mark.asyncio
async def test_e4b_live_rejects_mismatched_missing_and_unverifiable_routes_without_signing(tmp_path, monkeypatch):
    """Mismatched hops, split-hop mismatch, missing/empty routePlan, missing ammKey, no target hop, jupiterz/dflow/okx, and unknown routers all fail closed before signing."""
    clear_buy_reservations()
    clear_sell_idempotency_guard()
    kp = Keypair()
    unsigned_tx = _mk_unsigned_tx(kp)
    db = AgentDatabase(db_path=str(tmp_path / "e4b_rej.duckdb"), starting_sol=5.0)
    db.upsert_watchlist_entry(mint=MINT_E4B, symbol="E4B", pair_address=POOL_PINNED, candidate={"mint": MINT_E4B}, safety_passed=True)
    db.mark_watchlist_safety_passed(MINT_E4B, True, checked_at=datetime.now(timezone.utc).isoformat())
    cfg = _mk_live_e4b_cfg(kp)

    base_quote = {
        "outAmount": "1000000000",
        "otherAmountThreshold": "980000000",
        "slippageBps": 200,
        "priceImpact": -0.5,
        "router": "metis",
        "lastValidBlockHeight": 1000,
        "transaction": unsigned_tx,
        "requestId": "req-e4b-rej",
    }

    future_exp = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    bad_cases: list[tuple[dict[str, Any], str]] = [
        # Mismatched target-mint hop (even with matching label!)
        ({**base_quote, "routePlan": [_mk_hop(SOL_MINT, MINT_E4B, POOL_OTHER, label="pumpswap")]}, "POOL_MISMATCH"),
        # Multiple target-mint hops where one matches and one mismatches
        ({**base_quote, "routePlan": [_mk_hop(SOL_MINT, MINT_E4B, POOL_PINNED), _mk_hop(SOL_MINT, MINT_E4B, POOL_OTHER)]}, "POOL_MISMATCH"),
        # Missing or empty routePlan
        ({**base_quote}, "MISSING_ROUTE_PLAN"),
        ({**base_quote, "routePlan": []}, "MISSING_ROUTE_PLAN"),
        # Missing ammKey on target-mint hop
        ({**base_quote, "routePlan": [_mk_hop(SOL_MINT, MINT_E4B, None)]}, "MISSING_ROUTE_PLAN"),
        # Route plan with no target-mint hop
        ({**base_quote, "routePlan": [_mk_hop(SOL_MINT, USDC_MINT, POOL_PINNED)]}, "MISSING_ROUTE_PLAN"),
        # Unverifiable routers
        ({**base_quote, "router": "jupiterz", "expireAt": future_exp, "routePlan": [_mk_hop(SOL_MINT, MINT_E4B, POOL_PINNED)]}, "UNVERIFIABLE_ROUTER"),
        ({**base_quote, "router": "dflow", "routePlan": [_mk_hop(SOL_MINT, MINT_E4B, POOL_PINNED)]}, "UNVERIFIABLE_ROUTER"),
        ({**base_quote, "router": "okx", "routePlan": [_mk_hop(SOL_MINT, MINT_E4B, POOL_PINNED)]}, "UNVERIFIABLE_ROUTER"),
        # Unknown / missing / malformed routers
        ({**base_quote, "router": "raydium"}, "UNVERIFIABLE_ROUTER"),
        ({**base_quote, "router": ""}, "UNVERIFIABLE_ROUTER"),
        ({k: v for k, v in base_quote.items() if k != "router"}, "UNVERIFIABLE_ROUTER"),
    ]

    sign_calls = {"n": 0}
    exec_calls = {"n": 0}
    orig_derive = ex_mod.derive_and_validate_single_signer_tx

    def spy_derive(*args: Any, **kwargs: Any):
        sign_calls["n"] += 1
        return orig_derive(*args, **kwargs)

    monkeypatch.setattr(ex_mod, "derive_and_validate_single_signer_tx", spy_derive)
    curr_quote = {"val": dict(base_quote)}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(RPC_URL):
            body = json.loads(request.content.decode("utf-8"))
            if body["method"] == "getBalance":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 5_000_000_000}})
            if body["method"] == "getBlockHeight":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 990})
        if "/swap/v2/order" in str(request.url):
            return httpx.Response(200, json=curr_quote["val"])
        if "/swap/v2/execute" in str(request.url):
            exec_calls["n"] += 1
            return httpx.Response(200, json={"signature": "ShouldNeverBroadcast"})
        if "/tokens/v2/search" in str(request.url):
            return httpx.Response(200, json=[{"id": request.url.params.get("query", MINT_E4B), "audit": {"isSus": False}}])
        if "rugcheck.xyz" in str(request.url):
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        if "/ultra/v1/shield" in str(request.url):
            return httpx.Response(200, json={"warnings": {}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for bq, expected_status in bad_cases:
            curr_quote["val"] = bq
            out = await executor_node(
                {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4b_order()},
                config=cfg, db=db, http_client=client,
            )
            assert out == {"pending_order": None}
            assert sign_calls["n"] == 0
            assert exec_calls["n"] == 0
            assert get_inflight_buy_reservations(str(db.db_path)) == []
            assert db.get_active_locking_intents() == []
            latest = db.get_action_log(limit=1)[0]
            assert latest["pool_route_status"] == expected_status
    db.close()


@pytest.mark.asyncio
async def test_e4b_paper_telemetry_all_statuses_and_opt_in_enforcement(tmp_path):
    """Paper mode persists router/route_pool_keys/pool_route_status for all 4 statuses; opt-in flag enforces rejection."""
    clear_buy_reservations()
    clear_sell_idempotency_guard()

    paper_cases = [
        ({"outAmount": "1000000000", "router": "metis", "routePlan": [_mk_hop(SOL_MINT, MINT_E4B, POOL_PINNED)], "transaction": None}, "VERIFIED_PINNED_POOL", [POOL_PINNED], True),
        ({"outAmount": "1000000000", "router": "dflow", "routePlan": [_mk_hop(SOL_MINT, MINT_E4B, POOL_PINNED)], "transaction": None}, "UNVERIFIABLE_ROUTER", [POOL_PINNED], False),
        ({"outAmount": "1000000000", "router": "metis", "routePlan": [_mk_hop(SOL_MINT, MINT_E4B, POOL_OTHER)], "transaction": None}, "POOL_MISMATCH", [POOL_OTHER], False),
        ({"outAmount": "1000000000", "router": "metis", "routePlan": [], "transaction": None}, "MISSING_ROUTE_PLAN", [], False),
    ]

    # 1. Default paper mode (REQUIRE_PINNED_POOL_ROUTE_IN_PAPER=False): all 4 execute & persist telemetry
    cfg_obs = AgentConfig(_env_file=None, EXECUTION_MODE="paper", PAPER_TX_FEE_SOL=0.001, PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0)
    assert cfg_obs.REQUIRE_PINNED_POOL_ROUTE_IN_PAPER is False

    for idx, (quote_payload, expected_status, expected_keys, _allowed_enforced) in enumerate(paper_cases):
        clear_buy_reservations()
        db = AgentDatabase(db_path=str(tmp_path / f"e4b_paper_obs_{idx}.duckdb"), starting_sol=5.0)
        mint_i = MINT_E4B
        db.upsert_watchlist_entry(mint=mint_i, symbol="E4B", pair_address=POOL_PINNED, candidate={"mint": mint_i}, safety_passed=True)
        db.mark_watchlist_safety_passed(mint_i, True, checked_at=datetime.now(timezone.utc).isoformat())
        order_i = _mk_e4b_order(mint=mint_i)

        def handler_p(req: httpx.Request, qp: dict[str, Any] = quote_payload) -> httpx.Response:
            if "/tokens/v2/search" in str(req.url):
                return httpx.Response(200, json=[{"id": MINT_E4B, "audit": {"isSus": False}}])
            return httpx.Response(200, json=qp)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler_p)) as client:
            out = await executor_node(
                {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": order_i},
                config=cfg_obs, db=db, http_client=client,
            )
        assert len(out["chart_positions"]) == 1
        assert out["last_execution"]["pool_route_status"] == expected_status
        assert out["last_execution"]["route_pool_keys"] == expected_keys
        row = db.get_action_log(limit=1, actions=["BUY"])[0]
        assert row["pool_route_status"] == expected_status
        assert json.loads(row["route_pool_keys"]) == expected_keys
        assert row["router"] == quote_payload["router"]
        db.close()

    # 2. Paper enforcement enabled (REQUIRE_PINNED_POOL_ROUTE_IN_PAPER=True): non-verified statuses reject cleanly
    cfg_enf = AgentConfig(
        _env_file=None, EXECUTION_MODE="paper", PAPER_TX_FEE_SOL=0.001,
        REQUIRE_PINNED_POOL_ROUTE_IN_PAPER=True, PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0,
    )
    for idx, (quote_payload, expected_status, _expected_keys, allowed_enforced) in enumerate(paper_cases):
        clear_buy_reservations()
        db = AgentDatabase(db_path=str(tmp_path / f"e4b_paper_enf_{idx}.duckdb"), starting_sol=5.0)
        db.upsert_watchlist_entry(mint=MINT_E4B, symbol="E4B", pair_address=POOL_PINNED, candidate={"mint": MINT_E4B}, safety_passed=True)
        db.mark_watchlist_safety_passed(MINT_E4B, True, checked_at=datetime.now(timezone.utc).isoformat())
        order_i = _mk_e4b_order(mint=MINT_E4B)

        def handler_e(req: httpx.Request, qp: dict[str, Any] = quote_payload) -> httpx.Response:
            if "/tokens/v2/search" in str(req.url):
                return httpx.Response(200, json=[{"id": MINT_E4B, "audit": {"isSus": False}}])
            return httpx.Response(200, json=qp)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler_e)) as client:
            out = await executor_node(
                {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": order_i},
                config=cfg_enf, db=db, http_client=client,
            )
        if allowed_enforced:
            assert len(out["chart_positions"]) == 1
        else:
            assert out == {"pending_order": None}
            assert get_inflight_buy_reservations(str(db.db_path)) == []
            row = db.get_action_log(limit=1)[0]
            assert row["pool_route_status"] == expected_status
        db.close()
