"""Unit tests for DuckDB action_log ledger and GET /api/export/actions.csv."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from alpha_node import alpha_node
from config import AgentConfig
from db import ACTION_LOG_COLUMNS, AgentDatabase
from executor_node import executor_node
from safety_gate_node import safety_gate_node
from scanner_node import scanner_node
from state import ActivePosition, FocusState, PendingOrder
from telemetry_api import create_telemetry_app


class StubDecisionClient:
    def __init__(self, answers: dict[str, dict[str, Any]]):
        self.answers = answers

    async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return self.answers


@pytest.mark.asyncio
async def test_action_ledger_logs_buy_sell_evict_circuit_breaker_and_csv_export(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "action_ledger.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper")
    now_dt = datetime(2026, 11, 19, 20, 0, 0, tzinfo=timezone.utc)

    # 1. Safety Gate EVICT
    def safety_handler(request: httpx.Request) -> httpx.Response:
        if "rugcheck.xyz" in str(request.url):
            return httpx.Response(200, json={"rugged": True, "risks": []})
        return httpx.Response(200, json={"warnings": {"MintRug": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(safety_handler)) as client:
        await safety_gate_node(
            {"focus_state": FocusState.SCANNING.value, "raw_candidates": [{"mint": "MintRug", "symbol": "RUG"}]},
            config=cfg,
            db=db,
            http_client=client,
            now=now_dt,
        )

    # 2. Alpha EVICT (vamp >= 0.85)
    dec_vamp = StubDecisionClient(
        {
            "t0_narrative_strength": {"type": "score", "score": 3.0, "confidence": 0.80},
            "t0_momentum_quality": {"type": "score", "score": 3.0, "confidence": 0.80},
            "t0_is_copycat_vamp": {"type": "noul", "noul": 0.90, "confidence": 0.90},
            "t0_action": {"type": "choice", "choice": "REJECT", "confidence": 0.85},
        }
    )
    await alpha_node(
        {
            "focus_state": FocusState.SCANNING.value,
            "sol_balance": 0.7,
            "safe_candidates": [{"mint": "MintVamp", "symbol": "VAMP", "added_at": now_dt.isoformat()}],
        },
        config=cfg,
        db=db,
        decision_client=dec_vamp,  # type: ignore[arg-type]
        now=now_dt,
    )

    # 3. Executor BUY + SELL + REENTRY_BUY
    def jup_handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("inputMint") == "MintWin":
            return httpx.Response(200, json={"outAmount": "575000000", "transaction": None})
        return httpx.Response(200, json={"outAmount": "1000000", "transaction": None})

    buy_order = PendingOrder(
        side="BUY",
        mint="MintWin",
        symbol="WIN",
        amount_atomic=500_000_000,
        reason="DECISION_1_ENTRY",
        target_state_after_fill=FocusState.IN_POSITION,
        strategy="SWEEP_RECLAIM",
    )
    sell_order = PendingOrder(
        side="SELL",
        mint="MintWin",
        symbol="WIN",
        amount_atomic=1_000_000,
        reason="TRAILING_TP",
        target_state_after_fill=FocusState.WATCHING_REENTRY,
        strategy="SWEEP_RECLAIM",
    )
    reentry_order = PendingOrder(
        side="BUY",
        mint="MintWin",
        symbol="WIN",
        amount_atomic=500_000_000,
        reason="LEG_2_REENTRY",
        target_state_after_fill=FocusState.IN_POSITION,
        strategy="LEG2_ENTRY",
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(jup_handler)) as client:
        await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 0.7, "daily_pnl_sol": 0.0, "pending_order": buy_order.model_dump()},
            config=cfg,
            db=db,
            http_client=client,
        )
        await executor_node(
            {
                "focus_state": FocusState.IN_POSITION.value,
                "sol_balance": 0.2,
                "daily_pnl_sol": 0.0,
                "active_position": ActivePosition(mint="MintWin", symbol="WIN", entry_sol=0.5, token_amount=1_000_000).model_dump(),
                "pending_order": sell_order.model_dump(),
            },
            config=cfg,
            db=db,
            http_client=client,
        )
        await executor_node(
            {"focus_state": FocusState.WATCHING_REENTRY.value, "sol_balance": 0.775, "daily_pnl_sol": 0.075, "pending_order": reentry_order.model_dump()},
            config=cfg,
            db=db,
            http_client=client,
        )

    # 4. Circuit breaker logs once (never spams on repeated halted ticks)
    for _ in range(2):
        await scanner_node(
            {"focus_state": FocusState.SCANNING.value, "daily_start_sol": 0.7, "daily_pnl_sol": -0.20},
            config=cfg,
            db=db,
            now=now_dt,
        )

    all_actions = db.get_action_log(limit=100)
    action_types = [r["action"] for r in all_actions]
    assert action_types.count("EVICT") == 2
    assert action_types.count("BUY") == 1
    assert action_types.count("SELL") == 1
    assert action_types.count("REENTRY_BUY") == 1
    assert action_types.count("CIRCUIT_BREAKER") == 1

    # 5. Verify GET /api/export/actions.csv (headers, rows, CORS, Content-Disposition, filters)
    app = create_telemetry_app(config=cfg, db=db)
    tc = TestClient(app)

    resp_all = tc.get("/api/export/actions.csv", headers={"Origin": "http://localhost:3000"})
    assert resp_all.status_code == 200
    assert resp_all.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="solana_actions.csv"' in resp_all.headers["content-disposition"]
    assert resp_all.headers.get("access-control-allow-origin") == "*"

    parsed_all = list(csv.DictReader(io.StringIO(resp_all.text)))
    assert list(parsed_all[0].keys()) == ACTION_LOG_COLUMNS
    assert len(parsed_all) == 6
    assert all(r["bot"] == "solana_meme" and r["unit"] == "SOL" for r in parsed_all)

    # Filtered export: ?actions=BUY,SELL&limit=10
    resp_filtered = tc.get("/api/export/actions.csv?actions=BUY,SELL&limit=10")
    assert resp_filtered.status_code == 200
    parsed_filtered = list(csv.DictReader(io.StringIO(resp_filtered.text)))
    assert len(parsed_filtered) == 2
    assert {r["action"] for r in parsed_filtered} == {"BUY", "SELL"}
    sell_row = next(r for r in parsed_filtered if r["action"] == "SELL")
    assert float(sell_row["pnl_amount"]) == pytest.approx(0.075, rel=1e-4)
    assert float(sell_row["pnl_pct"]) == pytest.approx(15.0, rel=1e-2)
    assert sell_row["strategy"] == "SWEEP_RECLAIM"

    db.close()
