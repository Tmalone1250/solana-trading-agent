"""Unit tests for Pure Decision 1 Strategist (alpha_node) and Jupiter V2 Executor."""

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
from executor_node import executor_node
from state import FocusState, PendingOrder


class MockDecisionClient:
    """Stub for GeminiDecisionClient recording single batched calls."""

    def __init__(self, responses: list[dict[str, dict[str, Any]]]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        self.calls.append({"input_text": input_text, "questions": questions})
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_alpha_node_single_batched_decision_1_call(tmp_path):
    from tests.test_scalper_entry_engine import make_sweep_reclaim_candles

    db = AgentDatabase(db_path=str(tmp_path / "alpha.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None)
    soaked_added = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    valid_candles = make_sweep_reclaim_candles()

    candidates = [
        {
            "mint": "MintVamp11111111111111111111111111111111111",
            "symbol": "VAMP",
            "liquidity_usd": 50_000,
            "buys_5m": 25,
            "sells_5m": 10,
            "added_at": soaked_added,
            "candles_5m": valid_candles,
        },
        {
            "mint": "MintAlpha1111111111111111111111111111111111",
            "symbol": "ALPHA",
            "liquidity_usd": 90_000,
            "buys_5m": 25,
            "sells_5m": 10,
            "added_at": soaked_added,
            "candles_5m": valid_candles,
        },
    ]
    mock_answers = {
        "t0_narrative_strength": {"type": "score", "score": 4.2, "confidence": 0.90},
        "t0_momentum_quality": {"type": "score", "score": 4.0, "confidence": 0.90},
        "t0_is_copycat_vamp": {"type": "noul", "noul": 0.60, "confidence": 0.92},  # fails <= 0.25
        "t0_action": {"type": "choice", "choice": "BUY", "confidence": 0.88},
        "t1_narrative_strength": {"type": "score", "score": 4.4, "confidence": 0.91},
        "t1_momentum_quality": {"type": "score", "score": 4.1, "confidence": 0.89},
        "t1_is_copycat_vamp": {"type": "noul", "noul": 0.12, "confidence": 0.94},
        "t1_action": {"type": "choice", "choice": "BUY", "confidence": 0.86},
    }
    decision_client = MockDecisionClient([mock_answers])

    out = await alpha_node(
        {
            "focus_state": FocusState.SCANNING.value,
            "sol_balance": 0.7,
            "safe_candidates": candidates,
        },
        config=cfg,
        db=db,
        decision_client=decision_client,  # type: ignore[arg-type]
    )

    # Must be exactly ONE batched call for all surviving tokens
    assert len(decision_client.calls) == 1
    assert set(decision_client.calls[0]["questions"].keys()) == set(mock_answers.keys())

    order = out["pending_order"]
    assert order is not None
    assert order["mint"] == "MintAlpha1111111111111111111111111111111111"
    assert order["side"] == "BUY"
    assert order["amount_atomic"] == int(0.5 * LAMPORTS_PER_SOL)
    assert "[DECISION-1 ENTRY] ALPHA" in out["last_decision_rationale"]
    assert "APPROVED -> ENTER IN_POSITION" in out["last_decision_rationale"]

    # Decisions logged to DuckDB
    logged = db.get_recent_decisions(limit=10)
    assert len(logged) == 2
    db.close()


@pytest.mark.asyncio
async def test_executor_paper_mode_omits_taker_and_logs_out_amount(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "exec_paper.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper", JUPITER_API_KEY="jup-secret")

    seen_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/swap/v2/order"
        assert request.headers.get("x-api-key") == "jup-secret"
        seen_params.update(dict(request.url.params))
        return httpx.Response(200, json={"outAmount": "123456789", "transaction": None})

    order = PendingOrder(
        side="BUY",
        mint="MintAlpha1111111111111111111111111111111111",
        symbol="ALPHA",
        amount_atomic=500_000_000,
        reason="DECISION_1_ENTRY",
        target_state_after_fill=FocusState.IN_POSITION,
        entry_liquidity_usd=90_000.0,
        rationale="[DECISION-1 ENTRY] ALPHA approved",
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await executor_node(
            {
                "focus_state": FocusState.SCANNING.value,
                "sol_balance": 0.7,
                "daily_pnl_sol": 0.0,
                "pending_order": order.model_dump(),
            },
            config=cfg,
            db=db,
            http_client=client,
        )

    assert "taker" not in seen_params
    assert seen_params["inputMint"] == SOL_MINT
    assert seen_params["outputMint"] == "MintAlpha1111111111111111111111111111111111"
    assert out["focus_state"] == FocusState.IN_POSITION.value
    assert pytest.approx(out["sol_balance"], rel=1e-6) == 0.2
    assert out["active_position"]["token_amount"] == 123_456_789

    trades = db.get_recent_trades()
    assert len(trades) == 1
    assert trades[0]["out_amount"] == 123_456_789
    assert trades[0]["mode"] == "paper"
    db.close()


@pytest.mark.asyncio
async def test_executor_live_mode_passes_taker_signs_and_executes(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "exec_live.duckdb"), starting_sol=0.7)
    kp = Keypair()
    cfg = AgentConfig(
        _env_file=None,
        EXECUTION_MODE="live",
        JUPITER_API_KEY="jup-live-key",
        SOLANA_PRIVATE_KEY_BS58=str(kp),
    )

    # Build a valid unsigned VersionedTransaction for the mock Jupiter response
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=kp.pubkey(), lamports=1))
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.default())
    unsigned_tx = VersionedTransaction.populate(msg, [kp.sign_message(b"0" * 32)])
    unsigned_b64 = base64.b64encode(bytes(unsigned_tx)).decode("ascii")

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/swap/v2/order":
            assert request.url.params.get("taker") == str(kp.pubkey())
            return httpx.Response(
                200,
                json={"outAmount": "987654321", "transaction": unsigned_b64, "requestId": "req-123"},
            )
        if request.url.path == "/swap/v2/execute":
            import json

            body = json.loads(request.content.decode("utf-8"))
            assert body["requestId"] == "req-123"
            # Verify signedTransaction is a valid signed VersionedTransaction
            signed_raw = base64.b64decode(body["signedTransaction"])
            signed_tx = VersionedTransaction.from_bytes(signed_raw)
            assert len(signed_tx.signatures) == 1
            return httpx.Response(200, json={"signature": "5LiveSigMock111111", "status": "Success"})
        return httpx.Response(404)

    order = PendingOrder(
        side="BUY",
        mint="MintLive11111111111111111111111111111111111",
        symbol="LIVE",
        amount_atomic=500_000_000,
        reason="DECISION_1_ENTRY",
        target_state_after_fill=FocusState.IN_POSITION,
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await executor_node(
            {
                "focus_state": FocusState.SCANNING.value,
                "sol_balance": 0.7,
                "daily_pnl_sol": 0.0,
                "pending_order": order.model_dump(),
            },
            config=cfg,
            db=db,
            http_client=client,
        )

    assert calls == ["GET /swap/v2/order", "POST /swap/v2/execute"]
    assert out["focus_state"] == FocusState.IN_POSITION.value
    assert out["last_execution"]["tx_signature"] == "5LiveSigMock111111"
    db.close()
