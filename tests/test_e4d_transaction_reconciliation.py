"""Phase E4D Tests: Durable Transaction Intent & Evidence-Based RPC Reconciliation."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Any

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
from state import FocusState, PendingOrder
from state_ledger import (
    RESERVATION_STATE_BROADCAST_UNRESOLVED,
    clear_buy_reservations,
    get_inflight_buy_reservations,
)
from tx_reconciler import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    SPL_TOKEN_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    AttributionAmbiguousError,
    UnsupportedSigningFlowError,
    attribute_transaction_balances,
    derive_and_validate_single_signer_tx,
    parse_json_parsed_account_keys,
    reconcile_unresolved_intents,
)

POOL_A = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"
MINT_A = "p2Yicb86aZig616Eav2VWG9vuXR5mEqhtzshZYBxzsV"
MINT_B = "So11111111111111111111111111111111111111113"
RPC_URL = "https://rpc.e4d.test"


def _mk_buy_order(mint: str = MINT_A, symbol: str = "E4D", amount_sol: float = 0.50) -> dict[str, Any]:
    return PendingOrder(
        side="BUY",
        mint=mint,
        symbol=symbol,
        pair_address=POOL_A,
        dex_id="pumpswap",
        amount_atomic=int(round(amount_sol * LAMPORTS_PER_SOL)),
        reason="DECISION_1_ENTRY",
        target_state_after_fill=FocusState.IN_POSITION,
        entry_liquidity_usd=80_000.0,
        entry_mc_usd=500_000.0,
        strategy="BREAKOUT_IGNITION",
    ).model_dump()


def _mk_unsigned_tx(kp: Keypair) -> str:
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=kp.pubkey(), lamports=1))
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.default())
    return base64.b64encode(bytes(VersionedTransaction(msg, [kp]))).decode("ascii")


def _mk_json_parsed_swap_tx(
    *,
    taker: str,
    mint: str,
    ata1: str,
    ata2: str,
    wsol_ata: str,
    loaded_extra: str = "LookupLoaded111111111111111111111111111111",
    ata1_pre: int = 100_000,
    ata1_post: int = 350_000,
    ata2_post: int = 750_000,
    wsol_pre: int = 500_000_000,
    wsol_post: int = 0,
    fee_lamports: int = 15_000,
    rent_lamports: int = 2_039_280,
    tip_lamports: int = 100_000,
    err: Any = None,
    include_ata2_create: bool = True,
    omit_ata2_owner: bool = False,
) -> dict[str, Any]:
    # Index 0: taker (fee payer), 1: ata1, 2: wsol_ata, 3: tip_dest, 4 (ALT-loaded): ata2
    account_keys = [
        {"pubkey": taker, "signer": True, "writable": True, "source": "transaction"},
        {"pubkey": ata1, "signer": False, "writable": True, "source": "transaction"},
        {"pubkey": wsol_ata, "signer": False, "writable": True, "source": "transaction"},
        {"pubkey": loaded_extra, "signer": False, "writable": True, "source": "lookupTable"},
        {"pubkey": ata2, "signer": False, "writable": True, "source": "lookupTable"},
    ]
    instructions: list[dict[str, Any]] = [
        {
            "program": "system",
            "programId": SYSTEM_PROGRAM_ID,
            "parsed": {"type": "createAccount", "info": {"source": taker, "newAccount": wsol_ata, "lamports": rent_lamports}},
        },
        {
            "program": "system",
            "programId": SYSTEM_PROGRAM_ID,
            "parsed": {"type": "transfer", "info": {"source": taker, "destination": loaded_extra, "lamports": tip_lamports}},
        },
    ]
    if include_ata2_create:
        instructions.append(
            {
                "program": "spl-associated-token-account",
                "programId": ASSOCIATED_TOKEN_PROGRAM_ID,
                "parsed": {"type": "createIdempotent", "info": {"wallet": taker, "mint": mint, "account": ata2}},
            }
        )
    ata2_post_entry: dict[str, Any] = {
        "accountIndex": 4,  # ALT-loaded key in message.accountKeys
        "mint": mint,
        "programId": SPL_TOKEN_PROGRAM_ID,
        "uiTokenAmount": {"amount": str(ata2_post), "decimals": 6},
    }
    if not omit_ata2_owner:
        ata2_post_entry["owner"] = taker

    return {
        "transaction": {"message": {"accountKeys": account_keys, "instructions": instructions}},
        "meta": {
            "err": err,
            "fee": fee_lamports,
            # Total wallet delta includes principal + fee + rent + tip (e.g. 0.50215428 SOL), NOT just principal!
            "preBalances": [1_000_000_000, 0, 0, 0, 0],
            "postBalances": [1_000_000_000 - 500_000_000 - fee_lamports - rent_lamports - tip_lamports, 0, 0, tip_lamports, 0],
            # Deliberately include loadedAddresses to verify jsonParsed NEVER double-appends it
            "loadedAddresses": {"writable": [ata2], "readonly": []},
            "preTokenBalances": [
                {"accountIndex": 1, "mint": mint, "owner": taker, "programId": SPL_TOKEN_PROGRAM_ID, "uiTokenAmount": {"amount": str(ata1_pre), "decimals": 6}},
                {"accountIndex": 2, "mint": SOL_MINT, "owner": taker, "programId": SPL_TOKEN_PROGRAM_ID, "uiTokenAmount": {"amount": str(wsol_pre), "decimals": 9}},
            ],
            "postTokenBalances": [
                {"accountIndex": 1, "mint": mint, "owner": taker, "programId": SPL_TOKEN_PROGRAM_ID, "uiTokenAmount": {"amount": str(ata1_post), "decimals": 6}},
                ata2_post_entry,
                {"accountIndex": 2, "mint": SOL_MINT, "owner": taker, "programId": SPL_TOKEN_PROGRAM_ID, "uiTokenAmount": {"amount": str(wsol_post), "decimals": 9}},
            ],
        },
    }


def test_e4d_single_signer_validation_rejects_gasless_and_multi_signer():
    """Gasless or multi-signer transactions are rejected before broadcast; single-signer derives valid signature."""
    kp = Keypair()
    other = Keypair()
    tx_b64 = _mk_unsigned_tx(kp)

    with pytest.raises(UnsupportedSigningFlowError, match="gasless"):
        derive_and_validate_single_signer_tx(tx_b64, str(kp), {"gasless": True}, str(kp.pubkey()))

    with pytest.raises(UnsupportedSigningFlowError, match="signatureFeePayer"):
        derive_and_validate_single_signer_tx(
            tx_b64, str(kp), {"gasless": False, "signatureFeePayer": str(other.pubkey())}, str(kp.pubkey())
        )

    signed_b64, sig = derive_and_validate_single_signer_tx(
        tx_b64, str(kp), {"gasless": False, "signatureFeePayer": str(kp.pubkey()), "router": "metis"}, str(kp.pubkey())
    )
    assert signed_b64 and sig and len(sig) > 32


@pytest.mark.asyncio
async def test_e4d_intent_persisted_before_broadcast_and_timeout_restores_block_across_restart(tmp_path):
    """Persist PREPARED->SUBMITTED before broadcast; timeout leaves UNRESOLVED; new DB instance blocks duplicate BUY."""
    clear_buy_reservations()
    clear_sell_idempotency_guard()
    kp = Keypair()
    unsigned_tx = _mk_unsigned_tx(kp)
    db_path = str(tmp_path / "e4d_restart.duckdb")
    db1 = AgentDatabase(db_path=db_path, starting_sol=2.0)
    cfg = AgentConfig(
        _env_file=None, EXECUTION_MODE="live", SOLANA_PRIVATE_KEY_BS58=str(kp),
        SOLANA_RPC_URL=RPC_URL, MAX_CHART_POSITIONS=3,
    )

    seen_pre_broadcast_intent: list[dict[str, Any]] = []

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(RPC_URL):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 2_000_000_000}})
        if "/swap/v2/order" in str(request.url):
            return httpx.Response(200, json={
                "outAmount": "1000000", "transaction": unsigned_tx, "requestId": "req-e4d",
                "router": "metis", "mode": "ultra", "lastValidBlockHeight": 12345, "otherAmountThreshold": "990000",
            })
        # Inside /swap/v2/execute: durable SUBMITTED intent MUST already exist in DuckDB without secrets!
        intents = db1.get_active_locking_intents(mint=MINT_A)
        seen_pre_broadcast_intent.extend(intents)
        raise httpx.ReadTimeout("timeout during broadcast")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler)) as client:
        with pytest.raises(httpx.ReadTimeout):
            await executor_node(
                {"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "daily_pnl_sol": 0.0, "pending_order": _mk_buy_order()},
                config=cfg, db=db1, http_client=client,
            )

    assert len(seen_pre_broadcast_intent) == 1
    pre = seen_pre_broadcast_intent[0]
    assert pre["status"] == "SUBMITTED"
    assert pre["tx_signature"]
    assert pre["jupiter_request_id"] == "req-e4d"
    assert str(kp) not in str(pre)  # never store secrets

    # After timeout, durable intent is UNRESOLVED and reservation is BROADCAST_UNRESOLVED
    after = db1.get_active_locking_intents(mint=MINT_A)
    assert len(after) == 1 and after[0]["status"] == "UNRESOLVED"
    assert get_inflight_buy_reservations(db_path)[0]["state"] == RESERVATION_STATE_BROADCAST_UNRESOLVED
    db1.close()

    # Simulate process restart: clear in-memory reservations & reopen DuckDB
    clear_buy_reservations()
    db2 = AgentDatabase(db_path=db_path, starting_sol=2.0)
    res_dup, rej_dup = db2.reserve_buy_slot(
        lane="chart", mint=MINT_A, required_sol=0.50, tx_fee_sol=0.01,
        max_lane_positions=3, max_open_positions=6,
    )
    assert res_dup is None and rej_dup is not None and "DUPLICATE_MINT" in rej_dup
    db2.close()


@pytest.mark.asyncio
async def test_e4d_reconciliation_states_and_idempotency(tmp_path):
    """Test processed stays UNRESOLVED, null/RPC error stay UNRESOLVED, confirmed success settles idempotently, terminal fail releases."""
    clear_buy_reservations()
    kp = Keypair()
    taker = str(kp.pubkey())
    db = AgentDatabase(db_path=str(tmp_path / "e4d_rec.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(
        _env_file=None, EXECUTION_MODE="live", SOLANA_PRIVATE_KEY_BS58=str(kp), SOLANA_RPC_URL=RPC_URL,
    )

    # 1. PREPARED intent on startup -> FAILED + released
    res_prep, _ = db.reserve_buy_slot(lane="chart", mint=MINT_B, required_sol=0.25, tx_fee_sol=0.01, max_lane_positions=3, max_open_positions=6)
    db.create_order_intent(
        intent_id="int-prep", reservation_id=res_prep, lane="chart", mint=MINT_B, symbol="B",
        pair_address=POOL_A, dex_id="pumpswap", side="BUY", strategy="BREAKOUT_IGNITION",
        router="metis", order_mode="ultra", jupiter_request_id="rp", tx_signature="SigPrep111",
        last_valid_block_height=100, expire_at=None, input_mint=SOL_MINT, output_mint=MINT_B,
        amount_atomic=250_000_000, expected_out_amount=500_000, other_amount_threshold=495_000,
        token_decimals=6, taker_pubkey=taker, reserved_sol=0.26, status="PREPARED",
    )

    # 2. UNRESOLVED intent for MINT_A
    res_a, _ = db.reserve_buy_slot(lane="chart", mint=MINT_A, required_sol=0.50, tx_fee_sol=0.01, max_lane_positions=3, max_open_positions=6)
    db.create_order_intent(
        intent_id="int-a", reservation_id=res_a, lane="chart", mint=MINT_A, symbol="E4D",
        pair_address=POOL_A, dex_id="pumpswap", side="BUY", strategy="BREAKOUT_IGNITION",
        router="metis", order_mode="ultra", jupiter_request_id="ra", tx_signature="SigA111",
        last_valid_block_height=100, expire_at=None, input_mint=SOL_MINT, output_mint=MINT_A,
        amount_atomic=500_000_000, expected_out_amount=1_000_000, other_amount_threshold=990_000,
        token_decimals=6, taker_pubkey=taker, reserved_sol=0.51, status="UNRESOLVED",
        order_snapshot={"opened_at": "2026-11-27T16:00:00+00:00", "reason": "DECISION_1_ENTRY", "entry_liquidity_usd": 80_000.0},
    )

    rpc_scenario = {"mode": "processed"}
    swap_tx = _mk_json_parsed_swap_tx(
        taker=taker, mint=MINT_A, ata1="Ata111", ata2="Ata222", wsol_ata="Wsol111",
        ata1_pre=100_000, ata1_post=350_000, ata2_post=750_000, wsol_pre=500_000_000, wsol_post=0,
    )

    def rpc_handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content.decode("utf-8"))
        m = body["method"]
        if rpc_scenario["mode"] == "rpc_error":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "rpc lag"}})
        if m == "getSignatureStatuses":
            if rpc_scenario["mode"] == "processed":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": [{"confirmationStatus": "processed", "err": None}]}})
            if rpc_scenario["mode"] == "processed_err":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": [{"confirmationStatus": "processed", "err": {"InstructionError": [0, "Custom"]}}]}})
            if rpc_scenario["mode"] == "null_not_expired":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": [None]}})
            if rpc_scenario["mode"] == "get_tx_null":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": [{"confirmationStatus": "confirmed", "err": None}]}})
            if rpc_scenario["mode"] == "confirmed_success":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": [{"confirmationStatus": "finalized", "err": None}]}})
        if m == "getBlockHeight":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 90})  # <= 100 (not expired)
        if m == "getTransaction":
            if rpc_scenario["mode"] == "get_tx_null":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": swap_tx})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(rpc_handler)) as client:
        # First call reconciles int-prep -> FAILED, and int-a (processed) -> UNRESOLVED
        res_1 = await reconcile_unresolved_intents(db, config=cfg, client=client)
        assert ("int-prep", "FAILED") in res_1
        assert ("int-a", "UNRESOLVED") in res_1
        assert db.get_order_intent("int-prep")["status"] == "FAILED"

        # processed error, null status (not expired), rpc_error, and getTransaction=None all remain UNRESOLVED
        for mode in ("processed_err", "null_not_expired", "rpc_error", "get_tx_null"):
            rpc_scenario["mode"] = mode
            res_m = await reconcile_unresolved_intents(db, config=cfg, client=client)
            assert res_m == [("int-a", "UNRESOLVED")]

        # Confirmed/finalized success settles aggregated multi-ATA tokens (250k + 750k = 1,000,000) & exact 0.50 SOL principal!
        rpc_scenario["mode"] = "confirmed_success"
        res_ok = await reconcile_unresolved_intents(db, config=cfg, client=client)
        assert res_ok == [("int-a", "RECONCILED")]
        st = db.get_state()
        assert len(st["chart_positions"]) == 1
        pos = st["chart_positions"][0]
        assert pos["token_amount"] == 1_000_000
        assert pos["entry_sol"] == pytest.approx(0.50)  # NOT 0.50215428 (rent/fee/tip excluded)
        assert pos["fee_accounting_status"] == "UNKNOWN"
        assert st["sol_balance"] == pytest.approx(1.50)

        # Repeated reconciliation is idempotent (no active intents left; no double settlement)
        res_again = await reconcile_unresolved_intents(db, config=cfg, client=client)
        assert res_again == []
        assert db.get_state()["sol_balance"] == pytest.approx(1.50)
        assert len(db.get_recent_trades(limit=10)) == 1
    db.close()


@pytest.mark.asyncio
async def test_e4d_terminal_failure_and_expired_missing_sig_release_reservation(tmp_path):
    """Committed err != None (agreeing with getTransaction) or missing sig + expired blockhash -> FAILED & release."""
    clear_buy_reservations()
    kp = Keypair()
    taker = str(kp.pubkey())
    db = AgentDatabase(db_path=str(tmp_path / "e4d_fail.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="live", SOLANA_PRIVATE_KEY_BS58=str(kp), SOLANA_RPC_URL=RPC_URL)

    r1, _ = db.reserve_buy_slot(lane="chart", mint=MINT_A, required_sol=0.50, tx_fee_sol=0.01, max_lane_positions=3, max_open_positions=6)
    db.create_order_intent(
        intent_id="int-err", reservation_id=r1, lane="chart", mint=MINT_A, symbol="A",
        pair_address=POOL_A, dex_id="pumpswap", side="BUY", strategy="BREAKOUT_IGNITION",
        router="metis", order_mode="ultra", jupiter_request_id="r1", tx_signature="SigErr1",
        last_valid_block_height=100, expire_at=None, input_mint=SOL_MINT, output_mint=MINT_A,
        amount_atomic=500_000_000, expected_out_amount=1_000_000, other_amount_threshold=990_000,
        token_decimals=6, taker_pubkey=taker, reserved_sol=0.51, status="UNRESOLVED",
    )
    r2, _ = db.reserve_buy_slot(lane="chart", mint=MINT_B, required_sol=0.50, tx_fee_sol=0.01, max_lane_positions=3, max_open_positions=6)
    db.create_order_intent(
        intent_id="int-exp", reservation_id=r2, lane="chart", mint=MINT_B, symbol="B",
        pair_address=POOL_A, dex_id="pumpswap", side="BUY", strategy="BREAKOUT_IGNITION",
        router="metis", order_mode="ultra", jupiter_request_id="r2", tx_signature="SigExp2",
        last_valid_block_height=100, expire_at=None, input_mint=SOL_MINT, output_mint=MINT_B,
        amount_atomic=500_000_000, expected_out_amount=1_000_000, other_amount_threshold=990_000,
        token_decimals=6, taker_pubkey=taker, reserved_sol=0.51, status="UNRESOLVED",
    )

    def rpc_handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content.decode("utf-8"))
        m = body["method"]
        if m == "getSignatureStatuses":
            sig = body["params"][0][0]
            if sig == "SigErr1":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": [{"confirmationStatus": "finalized", "err": {"InstructionError": [0, "Custom"]}}]}})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": [None]}})
        if m == "getTransaction":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"meta": {"err": {"InstructionError": [0, "Custom"]}}}})
        if m == "getBlockHeight":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 105})  # > 100 expired!
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(rpc_handler)) as client:
        res = await reconcile_unresolved_intents(db, config=cfg, client=client)
        assert ("int-err", "FAILED") in res
        assert ("int-exp", "FAILED") in res
    assert db.get_active_locking_intents() == []
    assert get_inflight_buy_reservations(str(db.db_path)) == []
    db.close()


def test_e4d_attribution_rejects_bad_indices_missing_owner_and_does_not_double_append_loaded_addresses():
    """Validate jsonParsed accountKeys, invalid indices, missing owner/creation proof, and wrong programId."""
    taker = "Taker11111111111111111111111111111111111111"
    tx_ok = _mk_json_parsed_swap_tx(
        taker=taker, mint=MINT_A, ata1="Ata1", ata2="Ata2", wsol_ata="Wsol1",
    )
    keys = parse_json_parsed_account_keys(tx_ok)
    assert len(keys) == 5  # NOT 6 (loadedAddresses was NOT appended)

    # Invalid / out-of-range accountIndex -> AttributionAmbiguousError
    tx_bad_idx = _mk_json_parsed_swap_tx(taker=taker, mint=MINT_A, ata1="Ata1", ata2="Ata2", wsol_ata="Wsol1")
    tx_bad_idx["meta"]["postTokenBalances"][0]["accountIndex"] = 99
    with pytest.raises(AttributionAmbiguousError, match="Out-of-range"):
        attribute_transaction_balances(tx_bad_idx, taker_pubkey=taker, output_mint=MINT_A)

    # Missing ATA2 creation proof when preTokenBalances lacks Ata2 -> AttributionAmbiguousError
    tx_no_create = _mk_json_parsed_swap_tx(
        taker=taker, mint=MINT_A, ata1="Ata1", ata2="Ata2", wsol_ata="Wsol1", include_ata2_create=False,
    )
    with pytest.raises(AttributionAmbiguousError, match="creation/initialization proof"):
        attribute_transaction_balances(tx_no_create, taker_pubkey=taker, output_mint=MINT_A)

    # Missing owner resolved via proven ATA creation succeeds; missing owner WITHOUT creation proof fails
    tx_omit_owner = _mk_json_parsed_swap_tx(
        taker=taker, mint=MINT_A, ata1="Ata1", ata2="Ata2", wsol_ata="Wsol1", omit_ata2_owner=True,
    )
    attr = attribute_transaction_balances(tx_omit_owner, taker_pubkey=taker, output_mint=MINT_A)
    assert attr["output_token_delta"] == 1_000_000
    assert attr["swap_principal_sol"] == pytest.approx(0.50)

    # Unexpected programId fails closed
    tx_bad_prog = _mk_json_parsed_swap_tx(taker=taker, mint=MINT_A, ata1="Ata1", ata2="Ata2", wsol_ata="Wsol1")
    tx_bad_prog["meta"]["postTokenBalances"][0]["programId"] = "MaliciousProg1111111111111111111111111111"
    with pytest.raises(AttributionAmbiguousError, match="Unexpected token program"):
        attribute_transaction_balances(tx_bad_prog, taker_pubkey=taker, output_mint=MINT_A)


@pytest.mark.asyncio
async def test_e4d_atomic_trade_insert_rollback_and_decimal_preservation_on_retry(tmp_path, monkeypatch):
    """Findings 1 & 3: Trade-insert failure rolls back position/SOL/trade/intent; retry preserves non-6 decimals & settles once."""
    clear_buy_reservations()
    kp = Keypair()
    taker = str(kp.pubkey())
    db = AgentDatabase(db_path=str(tmp_path / "e4d_atomic.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="live", SOLANA_PRIVATE_KEY_BS58=str(kp), SOLANA_RPC_URL=RPC_URL)

    res_a, _ = db.reserve_buy_slot(
        lane="chart", mint=MINT_A, required_sol=0.50, tx_fee_sol=0.01, max_lane_positions=3, max_open_positions=6,
    )
    db.create_order_intent(
        intent_id="int-dec9", reservation_id=res_a, lane="chart", mint=MINT_A, symbol="DEC9",
        pair_address=POOL_A, dex_id="pumpswap", side="BUY", strategy="BREAKOUT_IGNITION",
        router="metis", order_mode="ultra", jupiter_request_id="r9", tx_signature="SigDec9",
        last_valid_block_height=100, expire_at=None, input_mint=SOL_MINT, output_mint=MINT_A,
        amount_atomic=500_000_000, expected_out_amount=1_000_000, other_amount_threshold=990_000,
        token_decimals=None,  # Deliberately None so retry must use reconciled_decimals=9!
        taker_pubkey=taker, reserved_sol=0.51, status="UNRESOLVED",
        order_snapshot={"opened_at": "2026-11-27T16:00:00+00:00", "reason": "DECISION_1_ENTRY", "entry_liquidity_usd": 80_000.0},
    )

    swap_tx_dec9 = _mk_json_parsed_swap_tx(
        taker=taker, mint=MINT_A, ata1="Ata1", ata2="Ata2", wsol_ata="Wsol1",
    )
    for b in [*swap_tx_dec9["meta"]["preTokenBalances"], *swap_tx_dec9["meta"]["postTokenBalances"]]:
        if b["mint"] == MINT_A:
            b["uiTokenAmount"]["decimals"] = 9

    def rpc_handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content.decode("utf-8"))
        m = body["method"]
        if m == "getSignatureStatuses":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": [{"confirmationStatus": "finalized", "err": None}]}})
        if m == "getTransaction":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": swap_tx_dec9})
        return httpx.Response(404)

    import state_ledger
    orig_insert = state_ledger._insert_reconciled_trade_locked

    def _boom_trade_insert(conn, row):
        raise RuntimeError("simulated trades insert failure")

    monkeypatch.setattr(state_ledger, "_insert_reconciled_trade_locked", _boom_trade_insert)

    async with httpx.AsyncClient(transport=httpx.MockTransport(rpc_handler)) as client:
        res1 = await reconcile_unresolved_intents(db, config=cfg, client=client)
        assert res1 == [("int-dec9", "SETTLEMENT_PENDING_PERSISTENCE")]

        # Everything rolled back atomically: 0 positions, 2.0 SOL untouched, 0 trades, reservation still locked!
        st_fail = db.get_state()
        assert st_fail["chart_positions"] == []
        assert st_fail["sol_balance"] == pytest.approx(2.0)
        assert db.get_recent_trades(limit=10) == []
        assert len(get_inflight_buy_reservations(str(db.db_path))) == 1
        res_dup, rej_dup = db.reserve_buy_slot(
            lane="chart", mint=MINT_A, required_sol=0.50, tx_fee_sol=0.01, max_lane_positions=3, max_open_positions=6,
        )
        assert res_dup is None and rej_dup is not None and "DUPLICATE_MINT" in rej_dup

        # Restore trade insert & retry -> commits 1 position with decimals=9, 1 SOL debit, 1 trade, RECONCILED, reservation released
        monkeypatch.setattr(state_ledger, "_insert_reconciled_trade_locked", orig_insert)
        res2 = await reconcile_unresolved_intents(db, config=cfg, client=client)
        assert res2 == [("int-dec9", "RECONCILED")]

    st_ok = db.get_state()
    assert len(st_ok["chart_positions"]) == 1
    assert st_ok["chart_positions"][0]["decimals"] == 9
    assert st_ok["chart_positions"][0]["token_amount"] == 1_000_000
    assert st_ok["sol_balance"] == pytest.approx(1.50)
    assert len(db.get_recent_trades(limit=10)) == 1
    assert get_inflight_buy_reservations(str(db.db_path)) == []
    assert db.get_order_intent("int-dec9")["status"] == "RECONCILED"
    # Terminal RECONCILED cannot transition backward to SETTLEMENT_PENDING_PERSISTENCE
    assert db.transition_order_intent("int-dec9", new_status="SETTLEMENT_PENDING_PERSISTENCE") is False
    assert db.get_order_intent("int-dec9")["status"] == "RECONCILED"
    db.close()


@pytest.mark.asyncio
async def test_e4d_terminal_status_error_with_null_missing_or_malformed_get_transaction_remains_unresolved(tmp_path):
    """Finding 2: Terminal status err with null/missing/malformed/disagreeing getTransaction stays UNRESOLVED."""
    clear_buy_reservations()
    kp = Keypair()
    taker = str(kp.pubkey())
    db = AgentDatabase(db_path=str(tmp_path / "e4d_f2.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="live", SOLANA_PRIVATE_KEY_BS58=str(kp), SOLANA_RPC_URL=RPC_URL)

    r1, _ = db.reserve_buy_slot(lane="chart", mint=MINT_A, required_sol=0.50, tx_fee_sol=0.01, max_lane_positions=3, max_open_positions=6)
    db.create_order_intent(
        intent_id="int-f2", reservation_id=r1, lane="chart", mint=MINT_A, symbol="F2",
        pair_address=POOL_A, dex_id="pumpswap", side="BUY", strategy="BREAKOUT_IGNITION",
        router="metis", order_mode="ultra", jupiter_request_id="rf2", tx_signature="SigF2",
        last_valid_block_height=100, expire_at=None, input_mint=SOL_MINT, output_mint=MINT_A,
        amount_atomic=500_000_000, expected_out_amount=1_000_000, other_amount_threshold=990_000,
        token_decimals=6, taker_pubkey=taker, reserved_sol=0.51, status="UNRESOLVED",
    )

    tx_payloads: list[Any] = [None, {}, {"meta": None}, {"meta": "bad"}, {"meta": {"err": None}}]
    idx = {"i": 0}

    def rpc_handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content.decode("utf-8"))
        if body["method"] == "getSignatureStatuses":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": [{"confirmationStatus": "finalized", "err": {"InstructionError": [0, "Custom"]}}]}})
        if body["method"] == "getTransaction":
            val = tx_payloads[idx["i"]]
            idx["i"] += 1
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": val})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(rpc_handler)) as client:
        for _ in tx_payloads:
            res = await reconcile_unresolved_intents(db, config=cfg, client=client)
            assert res == [("int-f2", "UNRESOLVED")]
            assert len(get_inflight_buy_reservations(str(db.db_path))) == 1
    db.close()
