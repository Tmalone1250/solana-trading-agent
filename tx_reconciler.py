"""Phase E4D: Durable Transaction Intent Attribution & Evidence-Based RPC Reconciliation."""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from solders.keypair import Keypair
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from state import ActivePosition, FocusState

logger = logging.getLogger(__name__)

SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
VALID_TOKEN_PROGRAMS = frozenset({SPL_TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID})
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
ZERO_SIGNATURE = "1" * 64
TERMINAL_COMMITMENTS = frozenset({"confirmed", "finalized"})
RFQ_RECONCILE_GRACE_SECONDS = 15.0


class AttributionAmbiguousError(RuntimeError):
    """Raised when RPC transaction metadata cannot deterministically prove swap attribution."""


class UnsupportedSigningFlowError(RuntimeError):
    """Raised when a Jupiter transaction uses gasless or multi-signer partial signing."""


def derive_and_validate_single_signer_tx(
    tx_base64: str,
    private_key_bs58: str,
    order_resp: dict[str, Any],
    taker_pubkey: str,
) -> tuple[str, str]:
    """Validate non-gasless single-signer VersionedTransaction, sign it, and return (signed_b64, tx_signature)."""
    if bool(order_resp.get("gasless")):
        raise UnsupportedSigningFlowError("BLOCKED: UNSUPPORTED_PARTIAL_SIGN_OR_GASLESS (gasless=True)")
    fee_payer = order_resp.get("signatureFeePayer")
    if fee_payer is not None and str(fee_payer).strip() != "" and str(fee_payer).strip() != taker_pubkey:
        raise UnsupportedSigningFlowError(
            f"BLOCKED: UNSUPPORTED_PARTIAL_SIGN_OR_GASLESS (signatureFeePayer={fee_payer} != taker={taker_pubkey})"
        )

    keypair = Keypair.from_base58_string(private_key_bs58)
    raw_tx = base64.b64decode(tx_base64)
    tx = VersionedTransaction.from_bytes(raw_tx)
    if int(tx.message.header.num_required_signatures) != 1:
        raise UnsupportedSigningFlowError(
            f"BLOCKED: UNSUPPORTED_PARTIAL_SIGN_OR_GASLESS (num_required_signatures={tx.message.header.num_required_signatures})"
        )
    if not tx.message.account_keys or str(tx.message.account_keys[0]) != taker_pubkey:
        raise UnsupportedSigningFlowError("BLOCKED: UNSUPPORTED_PARTIAL_SIGN_OR_GASLESS (fee payer != taker)")

    signed_tx = VersionedTransaction(tx.message, [keypair])
    sig_str = str(signed_tx.signatures[0]) if signed_tx.signatures else ""
    if not sig_str or sig_str == ZERO_SIGNATURE:
        raise UnsupportedSigningFlowError("BLOCKED: INVALID_DERIVED_SIGNATURE (empty or all-zero)")
    # Validate canonical 64-byte base58 signature
    Signature.from_string(sig_str)
    return base64.b64encode(bytes(signed_tx)).decode("ascii"), sig_str


def parse_json_parsed_account_keys(tx_obj: dict[str, Any]) -> list[str]:
    """Extract ordered account pubkeys from jsonParsed transaction.message.accountKeys.

    In Solana getTransaction(..., encoding='jsonParsed', maxSupportedTransactionVersion=0),
    message.accountKeys already includes ALT-loaded accounts and meta.loadedAddresses is omitted.
    Never append meta.loadedAddresses here.
    """
    tx = tx_obj.get("transaction")
    if not isinstance(tx, dict):
        raise AttributionAmbiguousError("Missing transaction object in getTransaction result")
    msg = tx.get("message")
    if not isinstance(msg, dict):
        raise AttributionAmbiguousError("Missing message object in getTransaction result")
    raw_keys = msg.get("accountKeys")
    if not isinstance(raw_keys, list) or not raw_keys:
        raise AttributionAmbiguousError("Missing or empty message.accountKeys")

    keys: list[str] = []
    for entry in raw_keys:
        if isinstance(entry, dict):
            pk = entry.get("pubkey")
        elif isinstance(entry, str):
            pk = entry
        else:
            pk = None
        if not isinstance(pk, str) or not pk.strip():
            raise AttributionAmbiguousError(f"Invalid accountKeys entry: {entry!r}")
        keys.append(pk.strip())
    return keys


def resolve_account_index(idx: Any, all_keys: list[str]) -> str:
    """Strictly validate and resolve an integer accountIndex against all_keys."""
    if isinstance(idx, bool) or not isinstance(idx, int):
        raise AttributionAmbiguousError(f"Non-integer accountIndex: {idx!r}")
    if idx < 0 or idx >= len(all_keys):
        raise AttributionAmbiguousError(f"Out-of-range accountIndex {idx} for {len(all_keys)} keys")
    return all_keys[idx]


def _iter_all_instructions(tx_obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect top-level and inner parsed/unparsed instructions from a jsonParsed getTransaction result."""
    out: list[dict[str, Any]] = []
    tx = tx_obj.get("transaction") or {}
    msg = tx.get("message") or {}
    for ix in msg.get("instructions") or []:
        if isinstance(ix, dict):
            out.append(ix)
    meta = tx_obj.get("meta") or {}
    for group in meta.get("innerInstructions") or []:
        if isinstance(group, dict):
            for ix in group.get("instructions") or []:
                if isinstance(ix, dict):
                    out.append(ix)
    return out


def _collect_proven_ata_creations(
    tx_obj: dict[str, Any],
    *,
    taker_pubkey: str,
    target_mint: str,
) -> set[str]:
    """Identify token account pubkeys proven by parsed instructions to be newly initialized for (taker, mint)."""
    proven: set[str] = set()
    for ix in _iter_all_instructions(tx_obj):
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict):
            continue
        ix_type = str(parsed.get("type") or "")
        info = parsed.get("info")
        if not isinstance(info, dict):
            continue
        prog_id = str(ix.get("programId") or "")
        prog_name = str(ix.get("program") or "")
        if ix_type in ("create", "createIdempotent") and (
            prog_id == ASSOCIATED_TOKEN_PROGRAM_ID or prog_name == "spl-associated-token-account"
        ):
            wallet = str(info.get("wallet") or info.get("source") or "")
            mint = str(info.get("mint") or "")
            acct = str(info.get("account") or "")
            if wallet == taker_pubkey and mint == target_mint and acct:
                proven.add(acct)
        elif ix_type in ("initializeAccount", "initializeAccount2", "initializeAccount3") and (
            prog_id in VALID_TOKEN_PROGRAMS or prog_name == "spl-token"
        ):
            owner = str(info.get("owner") or "")
            mint = str(info.get("mint") or "")
            acct = str(info.get("account") or "")
            if owner == taker_pubkey and mint == target_mint and acct:
                proven.add(acct)
    return proven


def _resolve_token_entry_owner_and_program(
    entry: dict[str, Any],
    acct_pubkey: str,
    *,
    known_owners_by_acct: dict[str, str],
    known_progs_by_acct: dict[str, str],
    proven_created_accts: set[str],
    taker_pubkey: str,
) -> tuple[str, Optional[str]]:
    """Resolve owner and token program without assuming ownership when omitted."""
    owner = str(entry.get("owner") or "").strip()
    if not owner:
        owner = known_owners_by_acct.get(acct_pubkey, "")
    if not owner and acct_pubkey in proven_created_accts:
        owner = taker_pubkey
    if not owner:
        raise AttributionAmbiguousError(f"Missing owner for token account {acct_pubkey}")

    prog_id = str(entry.get("programId") or "").strip() or known_progs_by_acct.get(acct_pubkey)
    if prog_id is not None and prog_id not in VALID_TOKEN_PROGRAMS:
        raise AttributionAmbiguousError(f"Unexpected token program {prog_id} for {acct_pubkey}")
    return owner, prog_id


def _extract_raw_token_amount(entry: dict[str, Any]) -> tuple[int, int]:
    ui = entry.get("uiTokenAmount")
    if not isinstance(ui, dict):
        raise AttributionAmbiguousError("Missing uiTokenAmount in token balance entry")
    raw_amt = ui.get("amount")
    dec = ui.get("decimals")
    if raw_amt is None or isinstance(dec, bool) or not isinstance(dec, int) or not (0 <= dec <= 18):
        raise AttributionAmbiguousError(f"Invalid uiTokenAmount: {ui!r}")
    try:
        amt_int = int(str(raw_amt))
    except (ValueError, TypeError) as exc:
        raise AttributionAmbiguousError(f"Non-integer token amount {raw_amt!r}") from exc
    if amt_int < 0:
        raise AttributionAmbiguousError(f"Negative token amount {amt_int}")
    return amt_int, dec


def attribute_token_mint_delta(
    tx_obj: dict[str, Any],
    all_keys: list[str],
    *,
    taker_pubkey: str,
    target_mint: str,
    expected_decimals: Optional[int] = None,
) -> tuple[int, Optional[int]]:
    """Deterministically attribute net raw token delta for (taker_pubkey, target_mint) across all owned accounts."""
    meta = tx_obj.get("meta")
    if not isinstance(meta, dict):
        raise AttributionAmbiguousError("Missing meta in getTransaction result")

    pre_list = meta.get("preTokenBalances")
    post_list = meta.get("postTokenBalances")
    if not isinstance(pre_list, list) or not isinstance(post_list, list):
        raise AttributionAmbiguousError("Missing preTokenBalances or postTokenBalances")

    proven_created = _collect_proven_ata_creations(tx_obj, taker_pubkey=taker_pubkey, target_mint=target_mint)
    known_owners: dict[str, str] = {}
    known_progs: dict[str, str] = {}
    for entry in [*pre_list, *post_list]:
        if not isinstance(entry, dict) or str(entry.get("mint") or "") != target_mint:
            continue
        acct = resolve_account_index(entry.get("accountIndex"), all_keys)
        if entry.get("owner"):
            known_owners[acct] = str(entry["owner"]).strip()
        if entry.get("programId"):
            known_progs[acct] = str(entry["programId"]).strip()

    pre_by_acct: dict[str, tuple[int, int]] = {}
    post_by_acct: dict[str, tuple[int, int]] = {}
    seen_decimals: set[int] = set()

    for entry in pre_list:
        if not isinstance(entry, dict) or str(entry.get("mint") or "") != target_mint:
            continue
        acct = resolve_account_index(entry.get("accountIndex"), all_keys)
        owner, _ = _resolve_token_entry_owner_and_program(
            entry, acct,
            known_owners_by_acct=known_owners, known_progs_by_acct=known_progs,
            proven_created_accts=proven_created, taker_pubkey=taker_pubkey,
        )
        if owner != taker_pubkey:
            continue
        amt, dec = _extract_raw_token_amount(entry)
        seen_decimals.add(dec)
        pre_by_acct[acct] = (amt, dec)

    for entry in post_list:
        if not isinstance(entry, dict) or str(entry.get("mint") or "") != target_mint:
            continue
        acct = resolve_account_index(entry.get("accountIndex"), all_keys)
        owner, _ = _resolve_token_entry_owner_and_program(
            entry, acct,
            known_owners_by_acct=known_owners, known_progs_by_acct=known_progs,
            proven_created_accts=proven_created, taker_pubkey=taker_pubkey,
        )
        if owner != taker_pubkey:
            continue
        amt, dec = _extract_raw_token_amount(entry)
        seen_decimals.add(dec)
        if acct not in pre_by_acct and acct not in proven_created:
            # New post-balance without pre-balance is only treated as zero pre-balance with creation proof
            raise AttributionAmbiguousError(
                f"Account {acct} missing preTokenBalances without creation/initialization proof"
            )
        post_by_acct[acct] = (amt, dec)

    if len(seen_decimals) > 1:
        raise AttributionAmbiguousError(f"Inconsistent decimals for {target_mint}: {sorted(seen_decimals)}")
    resolved_dec = next(iter(seen_decimals), expected_decimals)
    if expected_decimals is not None and resolved_dec is not None and resolved_dec != expected_decimals:
        raise AttributionAmbiguousError(
            f"Decimals mismatch for {target_mint}: tx={resolved_dec} != expected={expected_decimals}"
        )

    all_accts = set(pre_by_acct) | set(post_by_acct)
    total_delta = 0
    for acct in all_accts:
        pre_amt = pre_by_acct[acct][0] if acct in pre_by_acct else 0
        post_amt = post_by_acct[acct][0] if acct in post_by_acct else 0
        total_delta += post_amt - pre_amt
    return total_delta, resolved_dec


def _attribute_swap_principal_and_costs(
    tx_obj: dict[str, Any],
    all_keys: list[str],
    *,
    taker_pubkey: str,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Separate swap input principal (wSOL/swap funding) from network fee and rent/creation costs.

    Never returns total wallet lamport delta (preBalances - postBalances) as swap principal.
    Returns (swap_principal_sol, network_fee_sol, rent_lamports_sol).
    """
    meta = tx_obj.get("meta") or {}
    fee_lamports = meta.get("fee")
    network_fee_sol: Optional[float] = None
    if (
        all_keys and all_keys[0] == taker_pubkey
        and not isinstance(fee_lamports, bool)
        and isinstance(fee_lamports, int)
        and fee_lamports >= 0
    ):
        network_fee_sol = fee_lamports / LAMPORTS_PER_SOL

    # 1. Preferred: verified wSOL token-account net outflow owned by taker (including newly created & closed temp wSOL)
    swap_principal_sol: Optional[float] = None
    try:
        wsol_delta, _ = attribute_token_mint_delta(
            tx_obj, all_keys, taker_pubkey=taker_pubkey, target_mint=SOL_MINT, expected_decimals=9
        )
        if wsol_delta < 0:
            swap_principal_sol = abs(wsol_delta) / LAMPORTS_PER_SOL
    except AttributionAmbiguousError:
        wsol_delta = 0

    # 2. If wSOL was funded via SystemProgram transfer into a taker-owned wSOL account and closed in same tx
    rent_lamports = 0
    wsol_funded_lamports = 0
    proven_wsol_accts = _collect_proven_ata_creations(tx_obj, taker_pubkey=taker_pubkey, target_mint=SOL_MINT)
    for ix in _iter_all_instructions(tx_obj):
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict):
            continue
        ix_type = str(parsed.get("type") or "")
        info = parsed.get("info")
        if not isinstance(info, dict):
            continue
        prog = str(ix.get("program") or "")
        prog_id = str(ix.get("programId") or "")
        if prog == "system" or prog_id == SYSTEM_PROGRAM_ID:
            src = str(info.get("source") or "")
            dst = str(info.get("destination") or info.get("newAccount") or "")
            lam = info.get("lamports")
            if isinstance(lam, bool) or not isinstance(lam, int) or lam < 0:
                continue
            if ix_type == "createAccount" and src == taker_pubkey:
                rent_lamports += lam
            elif ix_type == "transfer" and src == taker_pubkey:
                if dst in proven_wsol_accts:
                    wsol_funded_lamports += lam

    if swap_principal_sol is None and wsol_funded_lamports > 0:
        swap_principal_sol = wsol_funded_lamports / LAMPORTS_PER_SOL

    rent_sol = (rent_lamports / LAMPORTS_PER_SOL) if rent_lamports > 0 else None
    return swap_principal_sol, network_fee_sol, rent_sol


def attribute_transaction_balances(
    tx_obj: dict[str, Any],
    *,
    taker_pubkey: str,
    output_mint: str,
    expected_decimals: Optional[int] = None,
) -> dict[str, Any]:
    """Perform complete jsonParsed transaction-specific balance attribution."""
    meta = tx_obj.get("meta")
    if not isinstance(meta, dict):
        raise AttributionAmbiguousError("Missing meta in getTransaction result")
    if meta.get("err") is not None:
        return {"err": meta.get("err"), "output_token_delta": 0}

    all_keys = parse_json_parsed_account_keys(tx_obj)
    out_delta, resolved_dec = attribute_token_mint_delta(
        tx_obj, all_keys, taker_pubkey=taker_pubkey, target_mint=output_mint, expected_decimals=expected_decimals
    )
    principal_sol, network_fee_sol, rent_sol = _attribute_swap_principal_and_costs(
        tx_obj, all_keys, taker_pubkey=taker_pubkey
    )
    return {
        "err": None,
        "output_token_delta": out_delta,
        "decimals": resolved_dec,
        "swap_principal_sol": principal_sol,
        "network_fee_sol": network_fee_sol,
        "rent_sol": rent_sol,
    }


async def _rpc_call(
    method: str,
    params: list[Any],
    *,
    config: AgentConfig,
    client: httpx.AsyncClient,
) -> Any:
    rpc_url = config.SOLANA_RPC_URL.strip()
    if not rpc_url:
        raise RuntimeError("SOLANA_RPC_URL is not configured")
    resp = await client.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=config.LIVE_RPC_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or data.get("error") is not None:
        raise RuntimeError(f"RPC {method} error: {data.get('error') if isinstance(data, dict) else data}")
    return data.get("result")


async def _is_intent_expired_on_chain(
    intent: dict[str, Any],
    *,
    config: AgentConfig,
    client: httpx.AsyncClient,
) -> bool:
    """Verify router-specific expiry using a healthy RPC response before classifying missing sig as FAILED."""
    router = str(intent.get("router") or "").lower()
    if router == "jupiterz":
        exp_raw = intent.get("expire_at")
        if not exp_raw:
            return False
        try:
            exp_dt = datetime.fromisoformat(str(exp_raw).replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) > (exp_dt + timedelta(seconds=RFQ_RECONCILE_GRACE_SECONDS))
        except ValueError:
            return False

    lvbh = intent.get("last_valid_block_height")
    if lvbh is None:
        return False
    try:
        lvbh_int = int(lvbh)
    except (ValueError, TypeError):
        return False
    block_height = await _rpc_call("getBlockHeight", [{"commitment": "finalized"}], config=config, client=client)
    if isinstance(block_height, bool) or not isinstance(block_height, int):
        return False
    return block_height > lvbh_int


async def reconcile_single_intent(
    db: Any,
    intent: dict[str, Any],
    *,
    config: AgentConfig,
    client: httpx.AsyncClient,
) -> str:
    """Reconcile one durable order_intent outside db._lock for RPC calls and atomically under db._lock for DB writes."""
    intent_id = str(intent["intent_id"])
    status = str(intent["status"])
    mint = str(intent["mint"])
    res_id = intent.get("reservation_id")

    if status in ("CONFIRMED", "FAILED", "RECONCILED"):
        return status

    # 1. PREPARED (process died before broadcast): safe to mark FAILED and release reservation
    if status == "PREPARED":
        db.transition_order_intent(intent_id, new_status="FAILED", error_detail="ABORTED_BEFORE_BROADCAST")
        if res_id:
            db.release_buy_reservation(res_id)
        return "FAILED"

    snap = json.loads(intent.get("order_snapshot_json") or "{}") if intent.get("order_snapshot_json") else {}

    def _settle_confirmed_intent(
        *,
        out_tokens: int,
        entry_sol: float,
        tx_sig: Optional[str],
        decimals: Optional[int],
    ) -> str:
        opened_at = snap.get("opened_at") or intent.get("created_at")
        pos = ActivePosition(
            mint=mint,
            symbol=str(intent["symbol"]),
            entry_sol=entry_sol,
            token_amount=out_tokens,
            decimals=decimals if decimals is not None else 6,
            pair_address=intent.get("pair_address"),
            dex_id=intent.get("dex_id"),
            strategy=str(intent.get("strategy") or "SCALPER"),
            triggering_wallets=list(snap.get("triggering_wallets") or []),
            whale_shadow_exit_flag=False,
            opened_at=opened_at,
            entry_liquidity_usd=float(snap.get("entry_liquidity_usd") or 0.0),
            entry_mc_usd=snap.get("entry_mc_usd"),
            whale_sol_flow=snap.get("whale_sol_flow"),
            breakout_telemetry=snap.get("breakout_telemetry"),
            current_sol_value=entry_sol,
            peak_sol_value=entry_sol,
            partial_tp_taken=False,
            initial_entry_sol=entry_sol,
            remaining_cost_basis_sol=entry_sol,
            realized_partial_pnl_sol=0.0,
            entry_fee_sol=None,
            accumulated_fees_sol=0.0,
            fee_accounting_status="UNKNOWN",
            reference_token_amount=out_tokens,
            last_liquidity_usd=float(snap.get("entry_liquidity_usd") or 0.0),
        )
        try:
            db.remove_from_watchlist(mint)
            db.apply_trade_settlement(
                lane=str(intent.get("lane") or "chart"),
                side="BUY",
                mint=mint,
                opened_at=pos.opened_at,
                new_pos_dict=pos.model_dump(),
                sol_delta=-entry_sol,
                pnl_delta=0.0,
                focus_state=FocusState.IN_POSITION.value,
                last_decision_rationale=snap.get("rationale"),
                reservation_id=res_id,
                intent_id=intent_id,
                intent_final_status="RECONCILED",
            )
            db.log_trade(
                mode=config.EXECUTION_MODE,
                side="BUY",
                mint=mint,
                symbol=str(intent["symbol"]),
                in_amount=int(intent["amount_atomic"]),
                out_amount=out_tokens,
                sol_value=entry_sol,
                reason=str(snap.get("reason") or "RECONCILED_BUY"),
                tx_signature=tx_sig,
                rationale=str(snap.get("rationale") or ""),
                strategy=str(intent.get("strategy") or "SCALPER"),
                triggering_wallets=list(snap.get("triggering_wallets") or []),
                opened_at=pos.opened_at,
                pair_address=intent.get("pair_address"),
                dex_id=intent.get("dex_id"),
                tx_fee_sol=None,
                fee_status="UNKNOWN",
            )
            return "RECONCILED"
        except Exception as exc:
            db.transition_order_intent(
                intent_id,
                new_status="SETTLEMENT_PENDING_PERSISTENCE",
                error_detail=f"settlement_retry_required: {exc}",
            )
            return "SETTLEMENT_PENDING_PERSISTENCE"

    # 2. SETTLEMENT_PENDING_PERSISTENCE: retry settlement with persisted reconciled values (no duplicate RPC needed if stored)
    if status == "SETTLEMENT_PENDING_PERSISTENCE":
        rec_out = snap.get("reconciled_out_tokens")
        rec_sol = snap.get("reconciled_entry_sol")
        if rec_out and int(rec_out) > 0 and rec_sol and float(rec_sol) > 0:
            return _settle_confirmed_intent(
                out_tokens=int(rec_out),
                entry_sol=float(rec_sol),
                tx_sig=intent.get("tx_signature"),
                decimals=intent.get("token_decimals"),
            )

    tx_sig = str(intent.get("tx_signature") or "").strip()
    taker_pubkey = str(intent.get("taker_pubkey") or "").strip()
    if not tx_sig or not taker_pubkey:
        return status

    # 3. Query RPC getSignatureStatuses outside db._lock
    try:
        sig_res = await _rpc_call(
            "getSignatureStatuses",
            [[tx_sig], {"searchTransactionHistory": True}],
            config=config,
            client=client,
        )
        statuses = (sig_res or {}).get("value") if isinstance(sig_res, dict) else None
        sig_status = statuses[0] if isinstance(statuses, list) and statuses else None
    except Exception as exc:
        logger.warning("[Reconcile] getSignatureStatuses failed for %s (%s): %s", intent_id, mint, exc)
        db.transition_order_intent(intent_id, new_status="UNRESOLVED", error_detail=f"rpc_status_error: {exc}")
        return "UNRESOLVED"

    # 3a. Missing status -> check router-specific expiry on healthy RPC before FAILED
    if sig_status is None:
        try:
            expired = await _is_intent_expired_on_chain(intent, config=config, client=client)
        except Exception as exc:
            logger.warning("[Reconcile] expiry check failed for %s: %s", intent_id, exc)
            expired = False
        if expired:
            db.transition_order_intent(intent_id, new_status="FAILED", error_detail="EXPIRED_WITHOUT_ONCHAIN_STATUS")
            if res_id:
                db.release_buy_reservation(res_id)
            return "FAILED"
        db.transition_order_intent(intent_id, new_status="UNRESOLVED", error_detail="STATUS_NULL_NOT_EXPIRED")
        return "UNRESOLVED"

    conf_status = str(sig_status.get("confirmationStatus") or "").lower()
    if conf_status not in TERMINAL_COMMITMENTS:
        # Processed-only or unknown commitment must remain UNRESOLVED
        db.transition_order_intent(
            intent_id,
            new_status="UNRESOLVED",
            error_detail=f"NON_TERMINAL_COMMITMENT:{conf_status or 'none'}",
        )
        return "UNRESOLVED"

    # 3b. Query getTransaction(jsonParsed) outside db._lock
    try:
        tx_obj = await _rpc_call(
            "getTransaction",
            [tx_sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": conf_status}],
            config=config,
            client=client,
        )
    except Exception as exc:
        logger.warning("[Reconcile] getTransaction failed for %s: %s", intent_id, exc)
        db.transition_order_intent(intent_id, new_status="UNRESOLVED", error_detail=f"rpc_get_tx_error: {exc}")
        return "UNRESOLVED"

    sig_err = sig_status.get("err")
    if sig_err is not None:
        # Cross-check getTransaction.meta.err when available
        if isinstance(tx_obj, dict) and isinstance(tx_obj.get("meta"), dict):
            if tx_obj["meta"].get("err") is None:
                db.transition_order_intent(
                    intent_id, new_status="UNRESOLVED", error_detail="STATUS_AND_GET_TX_ERR_DISAGREE"
                )
                return "UNRESOLVED"
        db.transition_order_intent(intent_id, new_status="FAILED", error_detail=f"ONCHAIN_ERR:{sig_err}")
        if res_id:
            db.release_buy_reservation(res_id)
        return "FAILED"

    if not isinstance(tx_obj, dict):
        db.transition_order_intent(intent_id, new_status="UNRESOLVED", error_detail="GET_TRANSACTION_NULL")
        return "UNRESOLVED"

    try:
        attr = attribute_transaction_balances(
            tx_obj,
            taker_pubkey=taker_pubkey,
            output_mint=mint,
            expected_decimals=intent.get("token_decimals"),
        )
    except AttributionAmbiguousError as exc:
        logger.warning("[Reconcile] Attribution ambiguous for %s (%s): %s", intent_id, mint, exc)
        db.transition_order_intent(intent_id, new_status="UNRESOLVED", error_detail=f"ATTRIBUTION_AMBIGUOUS: {exc}")
        return "UNRESOLVED"

    if attr.get("err") is not None:
        # getTransaction reports meta.err != None while status had err is None -> disagreement, keep UNRESOLVED
        db.transition_order_intent(intent_id, new_status="UNRESOLVED", error_detail="STATUS_AND_GET_TX_ERR_DISAGREE")
        return "UNRESOLVED"

    out_tokens = int(attr.get("output_token_delta") or 0)
    principal_sol = attr.get("swap_principal_sol")
    if out_tokens <= 0 or principal_sol is None or principal_sol <= 0.0:
        db.transition_order_intent(
            intent_id,
            new_status="UNRESOLVED",
            error_detail=f"INCOMPLETE_ATTRIBUTION: out_tokens={out_tokens}, principal_sol={principal_sol}",
        )
        return "UNRESOLVED"

    snap["reconciled_out_tokens"] = out_tokens
    snap["reconciled_entry_sol"] = principal_sol
    db.update_order_intent_snapshot(intent_id, snap)
    return _settle_confirmed_intent(
        out_tokens=out_tokens,
        entry_sol=float(principal_sol),
        tx_sig=tx_sig,
        decimals=attr.get("decimals"),
    )


async def reconcile_unresolved_intents(
    db: Any,
    *,
    config: AgentConfig,
    client: httpx.AsyncClient,
) -> list[tuple[str, str]]:
    """Reconcile all active locking order_intents on startup or periodic recovery."""
    intents = db.get_active_locking_intents()
    results: list[tuple[str, str]] = []
    for intent in intents:
        new_st = await reconcile_single_intent(db, intent, config=config, client=client)
        results.append((str(intent["intent_id"]), new_st))
    return results
