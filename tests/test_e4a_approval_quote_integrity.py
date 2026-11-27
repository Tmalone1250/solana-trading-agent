"""Phase E4A Tests: BREAKOUT_IGNITION Pre-Broadcast Approval & Quote Integrity."""

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

from alpha_node import alpha_node
from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase
from executor_node import (
    AUTHORITATIVE_APPROVAL_PRICE_SOURCE,
    E4AValidationError,
    _parse_e4a_price_impact_pct,
    _validate_e4a_quote_timing_and_expiry,
    clear_sell_idempotency_guard,
    executor_node,
)
from state import FocusState, PendingOrder
from state_ledger import clear_buy_reservations, get_inflight_buy_reservations
from tests.test_e3b_cache_provenance import make_verified_ohlcv_result
from tests.test_scalper_entry_engine import make_sweep_reclaim_candles

POOL_PINNED = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"
POOL_OTHER = "7qbRF6YsyGuLUVs6Y1q64bdVrfe4ZcUUz1JRdoVNUJnm"
MINT_E4A = "p2Yicb86aZig616Eav2VWG9vuXR5mEqhtzshZYBxzsV"
RPC_URL = "https://rpc.e4a.test"


def _mk_unsigned_tx(kp: Keypair) -> str:
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=kp.pubkey(), lamports=1))
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.default())
    return base64.b64encode(bytes(VersionedTransaction(msg, [kp]))).decode("ascii")


def _mk_live_e4a_cfg(kp: Keypair, **overrides: Any) -> AgentConfig:
    params: dict[str, Any] = {
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
    }
    params.update(overrides)
    return AgentConfig(**params)


def _mk_e4a_order(**overrides: Any) -> dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat()
    params: dict[str, Any] = {
        "side": "BUY",
        "mint": MINT_E4A,
        "symbol": "E4A",
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
        "approval_price_native": 0.0005,  # 0.50 SOL -> 1,000 tokens (1_000_000_000 at dec=6)
        "approval_price_source": AUTHORITATIVE_APPROVAL_PRICE_SOURCE,
        "token_decimals": 6,
    }
    params.update(overrides)
    return PendingOrder(**params).model_dump()


@pytest.mark.asyncio
async def test_e4a_alpha_node_selects_exact_pinned_pool_price_and_decimals(tmp_path):
    """alpha_node selects DexScreener priceNative matched strictly to winner_pair and explicit decimals."""
    db = AgentDatabase(db_path=str(tmp_path / "e4a_alpha.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper", TRADE_SIZE_SOL=0.50)
    now_dt = datetime(2026, 11, 27, 16, 0, 0, tzinfo=timezone.utc)
    soaked = (now_dt - timedelta(minutes=5)).isoformat()
    candles = make_sweep_reclaim_candles()
    prov = make_verified_ohlcv_result(candles, last_ok_at=now_dt.timestamp())

    cand = {
        "mint": MINT_E4A,
        "symbol": "E4A",
        "pair_address": POOL_PINNED,
        "dex_id": "pumpswap",
        "market_cap_usd": 500_000.0,
        "liquidity_usd": 80_000.0,
        "volume_1h_usd": 120_000.0,
        "volume_5m_usd": 20_000.0,
        "price_change_5m_pct": 8.0,
        "buys_5m": 35,
        "sells_5m": 10,
        "added_at": soaked,
        "candles_5m": candles,
        "_ohlcv_5m_result": prov,
    }
    db.upsert_watchlist_entry(
        mint=MINT_E4A,
        symbol="E4A",
        pair_address=POOL_PINNED,
        candidate={k: v for k, v in cand.items() if k != "_ohlcv_5m_result"},
        added_at=soaked,
        safety_passed=True,
    )

    class _StubDec:
        def __init__(self, answers: dict[str, Any]) -> None:
            self._answers = answers

        async def decide(self, **_kwargs: Any) -> dict[str, Any]:
            return self._answers

    dec = _StubDec({
        "t0_narrative_strength": {"type": "score", "score": 4.4, "confidence": 0.90},
        "t0_momentum_quality": {"type": "score", "score": 4.3, "confidence": 0.90},
        "t0_is_copycat_vamp": {"type": "noul", "noul": 0.10, "confidence": 0.95},
        "t0_action": {"type": "choice", "choice": "BUY", "confidence": 0.90},
    })

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/tokens/v1/solana/" in url:
            # First pool is POOL_OTHER with different price; second is POOL_PINNED
            return httpx.Response(200, json=[
                {"chainId": "solana", "pairAddress": POOL_OTHER, "priceNative": "0.999"},
                {"chainId": "solana", "pairAddress": POOL_PINNED, "priceNative": "0.0005"},
            ])
        if "/price/v3" in url:
            return httpx.Response(200, json={MINT_E4A: {"usdPrice": 0.10, "decimals": 8}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await alpha_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "safe_candidates": [cand]},
            config=cfg, db=db, decision_client=dec, http_client=client, now=now_dt,  # type: ignore[arg-type]
        )

    po = out["pending_order"]
    assert po is not None
    assert po["approved_at"] == now_dt.isoformat()
    assert po["approval_pair_address"] == POOL_PINNED
    assert po["approval_price_native"] == pytest.approx(0.0005)
    assert po["approval_price_source"] == AUTHORITATIVE_APPROVAL_PRICE_SOURCE
    assert po["approval_price_observed_at"] is not None
    assert po["token_decimals"] == 8
    assert po["approval_candle_ts"] is not None
    assert po["approval_candle_close_usd"] is not None
    db.close()


@pytest.mark.asyncio
async def test_e4a_approval_provenance_rejects_wrong_pool_invalid_price_decimals_and_stale(tmp_path):
    """Wrong pool, missing/non-finite/non-positive price, invalid decimals, and stale approval/price all fail closed."""
    clear_buy_reservations()
    clear_sell_idempotency_guard()
    kp = Keypair()
    cfg = _mk_live_e4a_cfg(kp)
    db = AgentDatabase(db_path=str(tmp_path / "e4a_prov.duckdb"), starting_sol=2.0)

    bad_orders = [
        _mk_e4a_order(approval_pair_address=POOL_OTHER),
        _mk_e4a_order(approval_price_source="jupiter_mint_quote"),
        _mk_e4a_order(approval_price_native=None),
        _mk_e4a_order(approval_price_native=0.0),
        _mk_e4a_order(approval_price_native=-0.001),
        _mk_e4a_order(approval_price_native=float("nan")),
        _mk_e4a_order(token_decimals=None),
        _mk_e4a_order(token_decimals=19),
        _mk_e4a_order(approved_at=(datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()),
        _mk_e4a_order(approval_price_observed_at=(datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()),
    ]
    jup_called = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        jup_called["n"] += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for bad in bad_orders:
            out = await executor_node(
                {"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "daily_pnl_sol": 0.0, "pending_order": bad},
                config=cfg, db=db, http_client=client,
            )
            assert out == {"pending_order": None}
            assert get_inflight_buy_reservations(str(db.db_path)) == []
    assert jup_called["n"] == 0  # Rejected BEFORE calling RPC or Jupiter!
    db.close()


@pytest.mark.asyncio
async def test_e4a_unset_live_thresholds_fail_closed_and_paper_allows_unset_but_blocks_zero_output(tmp_path):
    """Live mode fails closed when E4A thresholds are unset; paper mode allows unset thresholds but rejects out_amount <= 0."""
    clear_buy_reservations()
    clear_sell_idempotency_guard()
    kp = Keypair()

    # Live with unset E4A thresholds -> fail closed
    cfg_live_unset = AgentConfig(_env_file=None, EXECUTION_MODE="live", SOLANA_PRIVATE_KEY_BS58=str(kp), SOLANA_RPC_URL=RPC_URL)
    db1 = AgentDatabase(db_path=str(tmp_path / "e4a_unset_live.duckdb"), starting_sol=2.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(500))) as client:
        out = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4a_order()},
            config=cfg_live_unset, db=db1, http_client=client,
        )
        assert out == {"pending_order": None}
    db1.close()

    # Paper with unset E4A thresholds -> rejects out_amount <= 0 cleanly without reservation leak
    cfg_paper_unset = AgentConfig(_env_file=None, EXECUTION_MODE="paper", PAPER_TX_FEE_SOL=0.001)
    db2 = AgentDatabase(db_path=str(tmp_path / "e4a_paper_zero.duckdb"), starting_sol=2.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"outAmount": "0"}))) as client:
        out_z = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4a_order()},
            config=cfg_paper_unset, db=db2, http_client=client,
        )
        assert out_z == {"pending_order": None}
        assert get_inflight_buy_reservations(str(db2.db_path)) == []
        assert db2.get_state()["chart_positions"] == []
    db2.close()


@pytest.mark.asyncio
async def test_e4a_drift_worst_case_slippage_impact_rtse_and_expiry_gates(tmp_path):
    """Test adverse/favorable drift, otherAmountThreshold drift, slippage, priceImpact/Pct, RTSE preservation, and expiry."""
    clear_buy_reservations()
    clear_sell_idempotency_guard()
    kp = Keypair()
    unsigned_tx = _mk_unsigned_tx(kp)
    cfg = _mk_live_e4a_cfg(kp)
    db = AgentDatabase(db_path=str(tmp_path / "e4a_gates.duckdb"), starting_sol=5.0)

    # Verify helper parses priceImpact first, falls back to priceImpactPct, rejects missing/malformed
    assert _parse_e4a_price_impact_pct({"priceImpact": -0.4, "priceImpactPct": "-9.9"}) == pytest.approx(-0.4)
    assert _parse_e4a_price_impact_pct({"priceImpactPct": "-1.2"}) == pytest.approx(-1.2)
    assert _parse_e4a_price_impact_pct({}) is None
    assert _parse_e4a_price_impact_pct({"priceImpact": "nan"}) is None

    # Base quote for 0.50 SOL at approval_price_native=0.0005 (1000 tokens = 1_000_000_000 at dec=6)
    base_quote = {
        "outAmount": "1000000000",
        "otherAmountThreshold": "980000000",  # +2.04% worst-case drift (< 7%)
        "slippageBps": 200,
        "priceImpact": -0.5,
        "router": "metis",
        "lastValidBlockHeight": 1000,
        "transaction": unsigned_tx,
        "requestId": "req-e4a",
    }

    bad_quotes = [
        {**base_quote, "outAmount": "900000000"},        # +11.11% adverse drift > 5%
        {**base_quote, "outAmount": "1200000000"},       # -16.67% negative drift < -10%
        {**base_quote, "otherAmountThreshold": "900000000"},  # +11.11% worst-case drift > 7%
        {**base_quote, "slippageBps": 400},              # > 300 bps
        {**base_quote, "priceImpact": -3.0},             # |-3.0| > 2.5%
        {**base_quote, "priceImpact": None, "priceImpactPct": "-3.0"},  # fallback > 2.5%
        {**base_quote, "priceImpact": None},             # missing impact -> fail closed
        {**base_quote, "lastValidBlockHeight": 995},     # curr=990 >= 995 - 5 headroom -> expired
        {**base_quote, "lastValidBlockHeight": None},    # missing lvbh -> fail closed
        {**base_quote, "router": "jupiterz", "expireAt": (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()},  # < 5s headroom
        {**base_quote, "router": "jupiterz", "expireAt": None},  # missing expireAt
    ]

    curr_quote = {"val": dict(base_quote)}
    seen_order_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(RPC_URL):
            body = __import__("json").loads(request.content.decode("utf-8"))
            if body["method"] == "getBalance":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 5_000_000_000}})
            if body["method"] == "getBlockHeight":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 990})
        if "/swap/v2/order" in str(request.url):
            seen_order_params.append(dict(request.url.params))
            return httpx.Response(200, json=curr_quote["val"])
        if "/swap/v2/execute" in str(request.url):
            return httpx.Response(200, json={"signature": "SigE4AOk111", "outputAmountResult": "1000000000"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for bq in bad_quotes:
            curr_quote["val"] = bq
            out = await executor_node(
                {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4a_order()},
                config=cfg, db=db, http_client=client,
            )
            assert out == {"pending_order": None}
            assert get_inflight_buy_reservations(str(db.db_path)) == []
            assert db.get_active_locking_intents() == []

        # Valid quote passes all E4A gates & preserves RTSE (no slippageBps sent in request!)
        curr_quote["val"] = dict(base_quote)
        out_ok = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 5.0, "daily_pnl_sol": 0.0, "pending_order": _mk_e4a_order()},
            config=cfg, db=db, http_client=client,
        )
        assert len(out_ok["chart_positions"]) == 1
        assert all("slippageBps" not in p for p in seen_order_params)
    db.close()


@pytest.mark.asyncio
async def test_e4a_quote_age_enforced_at_all_three_checkpoints():
    """Quote age check rejects stale quotes at RECEIPT, PRE_SIGN, and PRE_SUBMIT checkpoints."""
    kp = Keypair()
    cfg = _mk_live_e4a_cfg(kp, MAX_QUOTE_AGE_SECONDS=5.0)
    resp = {"router": "metis", "lastValidBlockHeight": 1000}

    def rpc_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 900})

    stale_received = datetime.now(timezone.utc) - timedelta(seconds=10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(rpc_handler)) as client:
        for stage in ("RECEIPT", "PRE_SIGN", "PRE_SUBMIT"):
            with pytest.raises(E4AValidationError, match=f"STALE_QUOTE_{stage}"):
                await _validate_e4a_quote_timing_and_expiry(
                    resp, cfg, quote_received_at=stale_received, client=client, stage=stage, is_live=True,
                )
