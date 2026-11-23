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
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper", PAPER_TX_FEE_SOL=0.0)
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
        # Intentionally leave strategy at default "SCALPER" to verify SELL inherits entry strategy "SWEEP_RECLAIM"
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

    # 5. Whale board events: WHALE_WEBHOOK, WHALE_CONFLUENCE, WHALE_DISCOVERY
    from helius_webhook import process_helius_webhook_payload

    cfg_wh = AgentConfig(_env_file=None, EXECUTION_MODE="paper", ENABLE_HELIUS_WHALE_WEBHOOK=True)
    process_helius_webhook_payload(
        [
            {
                "signature": "sig-wh-csv-1",
                "type": "SWAP",
                "timestamp": int(now_dt.timestamp()),
                "feePayer": "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk",
                "nativeTransfers": [
                    {
                        "fromUserAccount": "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk",
                        "toUserAccount": "Pool111",
                        "amount": 2_500_000_000,
                    }
                ],
                "tokenTransfers": [
                    {
                        "mint": "MintWhaleCsv",
                        "fromUserAccount": "Pool111",
                        "toUserAccount": "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk",
                        "tokenAmount": 500_000.0,
                    }
                ],
            }
        ],
        config=cfg_wh,
        db=db,
        now=now_dt,
    )
    db.log_action(
        mode="paper",
        action="WHALE_CONFLUENCE",
        symbol="WCOIN",
        asset_id="MintWhaleCsv",
        notional=0.25,
        strategy="WHALE_SCALP",
        reason="WHALE_SCALP_SOLO",
        mc_usd=120_000.0,
        liq_usd=45_000.0,
        triggering_whales="Jijo",
        whale_sol_flow=2.5,
    )
    db.log_action(
        mode="paper",
        action="WHALE_DISCOVERY",
        symbol="ROSTER",
        asset_id="HELIUS_WEBHOOK",
        qty=12.0,
        strategy="WHALE_DISCOVERY",
        reason="ROSTER_SYNC (12 wallets)",
        triggering_whales="Jijo",
    )

    all_actions = db.get_action_log(limit=100)
    action_types = [r["action"] for r in all_actions]
    assert action_types.count("EVICT") == 2
    assert action_types.count("DECISION_1") == 1
    assert action_types.count("BUY") == 1
    assert action_types.count("SELL") == 1
    assert action_types.count("REENTRY_BUY") == 1
    assert action_types.count("CIRCUIT_BREAKER") == 1
    assert action_types.count("WHALE_WEBHOOK") == 1
    assert action_types.count("WHALE_CONFLUENCE") == 1
    assert action_types.count("WHALE_DISCOVERY") == 1

    # 6. Verify GET /api/export/csv & /api/export/actions.csv (30 columns + ?type= filters)
    app = create_telemetry_app(config=cfg, db=db)
    tc = TestClient(app)

    resp_all = tc.get("/api/export/csv", headers={"Origin": "http://localhost:3000"})
    assert resp_all.status_code == 200
    assert resp_all.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="solana_actions.csv"' in resp_all.headers["content-disposition"]
    assert resp_all.headers.get("access-control-allow-origin") == "*"

    parsed_all = list(csv.DictReader(io.StringIO(resp_all.text)))
    assert list(parsed_all[0].keys()) == ACTION_LOG_COLUMNS
    assert len(ACTION_LOG_COLUMNS) == 37
    assert len(parsed_all) == 10
    assert all(r["bot"] == "solana_meme" and r["unit"] == "SOL" for r in parsed_all)
    # Every row has enriched equity & daily realized SOL populated
    assert all(r["sol_equity"] != "" and r["daily_realized_sol"] != "" for r in parsed_all)

    # ?type=trades returns only BUY / SELL / REENTRY_BUY, and SELL inherits SWEEP_RECLAIM
    resp_trades = tc.get("/api/export/csv?type=trades")
    assert resp_trades.status_code == 200
    parsed_trades = list(csv.DictReader(io.StringIO(resp_trades.text)))
    assert {r["action"] for r in parsed_trades} == {"BUY", "SELL", "REENTRY_BUY"}
    sell_row = next(r for r in parsed_trades if r["action"] == "SELL")
    assert float(sell_row["pnl_amount"]) == pytest.approx(0.075, rel=1e-4)
    assert float(sell_row["pnl_pct"]) == pytest.approx(15.0, rel=1e-2)
    assert sell_row["strategy"] == "SWEEP_RECLAIM"

    # ?type=whales returns only WHALE_WEBHOOK / WHALE_CONFLUENCE / WHALE_DISCOVERY
    resp_whales = tc.get("/api/export/csv?type=whales")
    assert resp_whales.status_code == 200
    parsed_whales = list(csv.DictReader(io.StringIO(resp_whales.text)))
    assert {r["action"] for r in parsed_whales} == {"WHALE_WEBHOOK", "WHALE_CONFLUENCE", "WHALE_DISCOVERY"}
    wh_row = next(r for r in parsed_whales if r["action"] == "WHALE_WEBHOOK")
    assert wh_row["triggering_whales"] == "Jijo"
    assert float(wh_row["whale_sol_flow"]) == pytest.approx(2.5, rel=1e-4)

    # ?type=watchlist returns DECISION_1 / EVICT / CIRCUIT_BREAKER with Decision 1 scores
    resp_watch = tc.get("/api/export/csv?type=watchlist")
    assert resp_watch.status_code == 200
    parsed_watch = list(csv.DictReader(io.StringIO(resp_watch.text)))
    assert {r["action"] for r in parsed_watch} == {"DECISION_1", "EVICT", "CIRCUIT_BREAKER"}
    d1_row = next(r for r in parsed_watch if r["action"] == "DECISION_1")
    assert float(d1_row["narrative_score"]) == pytest.approx(3.0, rel=1e-2)
    assert float(d1_row["vamp_score"]) == pytest.approx(0.9, rel=1e-2)

    # Legacy alias /api/export/actions.csv?actions=BUY,SELL still works
    resp_filtered = tc.get("/api/export/actions.csv?actions=BUY,SELL&limit=10")
    assert resp_filtered.status_code == 200
    parsed_filtered = list(csv.DictReader(io.StringIO(resp_filtered.text)))
    assert len(parsed_filtered) == 2
    assert {r["action"] for r in parsed_filtered} == {"BUY", "SELL"}

    db.close()


def test_closed_trades_summary_all_wins_mixed_breakeven_and_empty(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "summary.duckdb"), starting_sol=1.0)
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper")
    tc = TestClient(create_telemetry_app(config=cfg, db=db))

    # 1. Empty (0 trades) -> no division by zero
    empty = db.get_closed_trades_summary()
    assert empty == {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate_pct": 0.0,
        "net_pnl_sol": 0.0,
    }
    tel_empty = tc.get("/api/telemetry").json()
    assert tel_empty["closed_trades_summary"] == empty

    # 2. All wins (2 SELLs with positive PnL; BUY rows ignored)
    db.log_trade(mode="paper", side="BUY", mint="M1", symbol="T1", in_amount=500, out_amount=100, sol_value=0.5, reason="ENTRY")
    db.log_trade(mode="paper", side="SELL", mint="M1", symbol="T1", in_amount=100, out_amount=600, sol_value=0.6, pnl_sol=0.10, pnl_pct=0.20, reason="TP")
    db.log_trade(mode="paper", side="SELL", mint="M2", symbol="T2", in_amount=100, out_amount=550, sol_value=0.55, pnl_sol=0.05, pnl_pct=0.10, reason="TP")
    all_wins = db.get_closed_trades_summary()
    assert all_wins["total_trades"] == 2
    assert all_wins["wins"] == 2
    assert all_wins["losses"] == 0
    assert all_wins["win_rate_pct"] == 100.0
    assert all_wins["net_pnl_sol"] == pytest.approx(0.15, rel=1e-4)

    # 3. Breakeven 0.0 PnL counts as loss (pnl <= 0)
    db.log_trade(mode="paper", side="SELL", mint="M3", symbol="T3", in_amount=100, out_amount=500, sol_value=0.50, pnl_sol=0.0, pnl_pct=0.0, reason="BE")
    with_be = db.get_closed_trades_summary()
    assert with_be["total_trades"] == 3
    assert with_be["wins"] == 2
    assert with_be["losses"] == 1
    assert with_be["win_rate_pct"] == 66.7
    assert with_be["net_pnl_sol"] == pytest.approx(0.15, rel=1e-4)

    # 4. Mixed wins + losses -> verify /api/telemetry summary & expanded >15 history
    for i in range(18):
        db.log_trade(
            mode="paper", side="SELL", mint=f"ML{i}", symbol=f"L{i}",
            in_amount=100, out_amount=450, sol_value=0.45, pnl_sol=-0.05, pnl_pct=-0.10, reason="STOP",
        )
    tel_mixed = tc.get("/api/telemetry").json()
    summ = tel_mixed["closed_trades_summary"]
    assert summ["total_trades"] == 21
    assert summ["wins"] == 2
    assert summ["losses"] == 19
    assert summ["win_rate_pct"] == 9.5
    assert summ["net_pnl_sol"] == pytest.approx(-0.75, rel=1e-4)
    # Payload returns up to 100 closed trades (all 21 here, not truncated at 15)
    assert len(tel_mixed["recent_trades"]) == 21
    db.close()
