"""Unit tests for Sprint 2A — Accounting Integrity & Telemetry Repair."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from audit_outcomes import (
    _normalize_action_row,
    _normalize_outcome_row,
    classify_observation_states,
    load_trades_from_duckdb,
    reconcile_trades_accounting,
    render_audit_report,
)
from audit_reports import build_loss_attribution, build_wallet_attribution
from batched_price_monitor import clear_price_backoff
from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase
from executor_node import clear_sell_idempotency_guard, executor_node
from position_monitor_node import position_monitor_node
from state import ActivePosition, FocusState, PendingOrder
from telemetry_api import create_telemetry_app


class _EmptyDecClient:
    async def decide(self, *, input_text: str, questions: dict):
        return {}


@pytest.mark.asyncio
async def test_high_profit_runner_exit_credits_full_proceeds_and_never_halves(tmp_path):
    """0.50 SOL entry -> 50% partial -> remaining 50% sells for 0.471350 SOL (> +60%): full 0.471350 credited (+88.14% PnL)."""
    clear_sell_idempotency_guard()
    db = AgentDatabase(db_path=str(tmp_path / "runner_88pct.duckdb"), starting_sol=1.0)
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper", PAPER_TX_FEE_SOL=0.001)
    mint = "MintRunner88Pct1111111111111111111111111111"

    quote_resp = {"outAmount": str(int(round(0.2875 * LAMPORTS_PER_SOL))), "priceImpactPct": "-0.45", "transaction": None}

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=quote_resp)

    pos = ActivePosition(
        mint=mint, symbol="RUN88", entry_sol=0.50, token_amount=1_000_000,
        strategy="BREAKOUT_IGNITION", opened_at=datetime.now(timezone.utc).isoformat(),
        current_sol_value=0.575, peak_sol_value=0.575, reference_token_amount=1_000_000,
        position_mfe_pct=15.0, position_mae_pct=-2.0, last_price_source="jupiter_v3",
    ).model_dump()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # 1. Partial sell 50% (500k tokens -> 0.2875 SOL)
        p_order = PendingOrder(
            side="PARTIAL_SELL", mint=mint, symbol="RUN88", amount_atomic=500_000,
            reason="PARTIAL_TP_50PCT (+15%)", target_state_after_fill=FocusState.IN_POSITION,
            strategy="BREAKOUT_IGNITION", monitor_sol_value=0.575, price_source="jupiter_v3",
        )
        out_p = await executor_node(
            {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.50, "daily_pnl_sol": 0.0, "active_position": pos, "chart_positions": [pos], "pending_order": p_order.model_dump()},
            config=cfg, db=db, http_client=client,
        )
        assert out_p["active_position"]["remaining_cost_basis_sol"] == pytest.approx(0.25, rel=1e-6)
        assert out_p["active_position"]["initial_entry_sol"] == pytest.approx(0.50, rel=1e-6)
        assert out_p["sol_balance"] == pytest.approx(0.50 + 0.2875 - 0.001, rel=1e-6)

        # 2. Remaining 50% runner explodes to 0.471350 SOL (> 0.50 * 0.80 = 0.40 SOL -> old hack halved this!)
        quote_resp["outAmount"] = str(int(round(0.471350 * LAMPORTS_PER_SOL)))
        quote_resp["priceImpactPct"] = "-0.82"
        runner_pos = dict(out_p["active_position"], position_mfe_pct=92.5, position_mae_pct=-2.0, current_sol_value=0.475)
        s_order = PendingOrder(
            side="SELL", mint=mint, symbol="RUN88", amount_atomic=500_000,
            reason="TRAILING_TP", target_state_after_fill=FocusState.SCANNING,
            strategy="BREAKOUT_IGNITION", monitor_sol_value=0.475, price_source="jupiter_v3",
        )
        out_s = await executor_node(
            {"focus_state": FocusState.IN_POSITION.value, "sol_balance": out_p["sol_balance"], "daily_pnl_sol": out_p["daily_pnl_sol"], "active_position": runner_pos, "chart_positions": [runner_pos], "pending_order": s_order.model_dump()},
            config=cfg, db=db, http_client=client,
        )

    expected_runner_pnl = 0.471350 - 0.25 - 0.001  # +0.220350 SOL (+88.14%)
    assert out_s["sol_balance"] == pytest.approx(out_p["sol_balance"] + 0.471350 - 0.001, rel=1e-6)
    sell_act = db.get_action_log(actions=["SELL"], limit=1)[0]
    assert sell_act["notional"] == pytest.approx(0.471350, rel=1e-6)
    assert sell_act["pnl_amount"] == pytest.approx(expected_runner_pnl, rel=1e-6)
    assert sell_act["pnl_pct"] == pytest.approx(88.14, abs=0.05)
    assert sell_act["jupiter_price_impact_raw"] == "-0.82"
    assert sell_act["monitor_sol_value"] == pytest.approx(0.475, rel=1e-6)
    assert sell_act["price_source"] == "jupiter_v3"
    assert sell_act["position_mfe_pct"] == pytest.approx(92.5, rel=1e-4)
    assert sell_act["position_mae_pct"] == pytest.approx(-2.0, rel=1e-4)

    part_act = db.get_action_log(actions=["PARTIAL_SELL"], limit=1)[0]
    assert part_act["jupiter_price_impact_raw"] == "-0.45"

    # DuckDB read-only accounting reconciliation must report 0 discrepancies
    db.close()
    rec = reconcile_trades_accounting(load_trades_from_duckdb(str(tmp_path / "runner_88pct.duckdb")))
    assert rec["checked"] == 2
    assert rec["discrepancy_count"] == 0
    clear_sell_idempotency_guard()


@pytest.mark.asyncio
async def test_paper_partial_sell_jupiter_fallback_scales_by_frac_and_monitor_tracks_position_mfe_mae(tmp_path):
    """Verify paper-mode Jupiter-failure fallback on PARTIAL_SELL scales by frac, and monitor tracks position_mfe_pct/position_mae_pct."""
    clear_sell_idempotency_guard()
    clear_price_backoff()
    db = AgentDatabase(db_path=str(tmp_path / "fallback_mfe.duckdb"), starting_sol=1.0)
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper", PAPER_TX_FEE_SOL=0.0)
    mint = "MintFallbackMfe1111111111111111111111111111"

    # 1. Monitor ticks track position_mfe_pct and position_mae_pct + dexscreener_native price_source
    price_native = {"val": 0.47}  # -6% on 0.50 entry

    def mon_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/price/v3" in url:
            return httpx.Response(200, json={SOL_MINT: {"usdPrice": 200.0, "decimals": 9}})  # omits mint -> dexscreener_native
        if "/tokens/v1/solana/" in url:
            return httpx.Response(200, json=[{"chainId": "solana", "baseToken": {"address": mint, "symbol": "MFE"}, "priceNative": f"{price_native['val']:.6f}", "liquidity": {"usd": 90_000.0}, "txns": {"m5": {"buys": 30, "sells": 10}}}])
        return httpx.Response(404)

    pos = ActivePosition(
        mint=mint, symbol="MFE", entry_sol=0.50, token_amount=1_000_000,
        strategy="SCALPER", current_sol_value=0.50, peak_sol_value=0.50, reference_token_amount=1_000_000,
    ).model_dump()
    st = {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.50, "daily_pnl_sol": 0.0, "active_position": pos, "chart_positions": [pos]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(mon_handler)) as client:
        m1 = await position_monitor_node(st, config=cfg, db=db, decision_client=_EmptyDecClient(), http_client=client)  # type: ignore[arg-type]
        assert m1["active_position"]["position_mae_pct"] == pytest.approx(-6.0, abs=0.1)
        assert m1["active_position"]["last_price_source"] == "dexscreener_native"

        st.update(m1)
        price_native["val"] = 0.55  # +10% on 0.50 entry
        m2 = await position_monitor_node(st, config=cfg, db=db, decision_client=_EmptyDecClient(), http_client=client)  # type: ignore[arg-type]
        assert m2["active_position"]["position_mfe_pct"] == pytest.approx(10.0, abs=0.1)
        assert m2["active_position"]["position_mae_pct"] == pytest.approx(-6.0, abs=0.1)

    # 2. Paper-mode Jupiter quote failure on PARTIAL_SELL must scale current_sol_value (0.60 SOL) by 0.50 -> 0.30 SOL
    def fail_jup(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    pos_fb = dict(m2["active_position"], current_sol_value=0.60, peak_sol_value=0.60)
    p_order = PendingOrder(
        side="PARTIAL_SELL", mint=mint, symbol="MFE", amount_atomic=500_000,
        reason="PARTIAL_TP_50PCT (+15%)", target_state_after_fill=FocusState.IN_POSITION,
        strategy="SCALPER", monitor_sol_value=0.60, price_source="dexscreener_native",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_jup)) as client:
        out_fb = await executor_node(
            {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.50, "daily_pnl_sol": 0.0, "active_position": pos_fb, "chart_positions": [pos_fb], "pending_order": p_order.model_dump()},
            config=cfg, db=db, http_client=client,
        )
    assert out_fb["sol_balance"] == pytest.approx(0.50 + 0.30, rel=1e-6)
    assert out_fb["active_position"]["remaining_cost_basis_sol"] == pytest.approx(0.25, rel=1e-6)
    assert out_fb["active_position"]["realized_partial_pnl_sol"] == pytest.approx(0.05, rel=1e-6)

    # CSV export includes all Sprint 2A columns
    tc = TestClient(create_telemetry_app(config=cfg, db=db))
    rows = list(csv.DictReader(io.StringIO(tc.get("/api/export/csv?type=trades").text)))
    assert rows[0]["price_source"] == "dexscreener_native"
    assert float(rows[0]["position_mfe_pct"]) == pytest.approx(10.0, abs=0.1)
    assert float(rows[0]["position_mae_pct"]) == pytest.approx(-6.0, abs=0.1)
    db.close()
    clear_sell_idempotency_guard()


def test_repaired_high_entry_friction_and_fast_exit_farmer_classifiers():
    """Verify high_entry_friction never fires from default --friction-pct 3.0, and a 345s-median wallet with 1 fast exit is NOT flagged farmer."""
    t0 = datetime(2026, 11, 24, 16, 0, 0, tzinfo=timezone.utc)
    # 1. Clean SELL with 0.5% price impact & 0% slippage -> must NOT get high_entry_friction even when friction_pct=3.0
    outcomes = [
        _normalize_outcome_row({"id": "o1", "mint": "MintClean", "symbol": "CLN", "source": "CHART_ALPHA", "discovered_at": t0.isoformat(), "decision": "ENTERED", "price_5m_pct": -5.0, "price_15m_pct": -12.0, "price_30m_pct": -15.0, "mfe_pct": 1.0, "mae_pct": -16.0, "finalized": True}),
    ]
    actions = [
        _normalize_action_row({"timestamp_utc": t0.isoformat(), "action": "BUY", "symbol": "CLN", "asset_id": "MintClean", "jupiter_price_impact_raw": "0.5"}),
        _normalize_action_row({"timestamp_utc": (t0 + timedelta(minutes=5)).isoformat(), "action": "SELL", "symbol": "CLN", "asset_id": "MintClean", "reason": "HARD_STOP_LOSS", "pnl_pct": -10.5, "notional": 0.448, "monitor_sol_value": 0.450, "position_mfe_pct": 1.5, "position_mae_pct": -10.8, "jupiter_price_impact_raw": "0.4"}),
    ]
    classify_observation_states(outcomes, actions, reference_ts=(t0 + timedelta(hours=2)).timestamp())
    attr = build_loss_attribution(outcomes, actions, friction_pct=3.0)
    assert "high_entry_friction" not in attr[0]["secondary_factors"]
    assert attr[0]["metrics_source"] == "position_anchored"
    assert attr[0]["mfe_pct"] == pytest.approx(1.5)

    # 2. Wallet with 5 trades: holds = [30s, 300s, 345s, 400s, 500s] (median=345s, 1 fast exit = 20% < 30%) -> NOT farmer!
    w_outcomes = []
    w_actions = []
    for idx, hold_s in enumerate((30, 300, 345, 400, 500)):
        bt = t0 + timedelta(minutes=idx * 20)
        m = f"MintW{idx}"
        w_outcomes.append(_normalize_outcome_row({"id": f"w{idx}", "mint": m, "symbol": f"W{idx}", "source": "WHALE_WEBHOOK", "discovered_at": bt.isoformat(), "decision": "WHALE_BUY", "triggering_wallet": "WalletSteady", "price_1m_pct": 1.0, "price_5m_pct": 5.0, "price_15m_pct": 8.0, "price_30m_pct": 10.0, "mfe_pct": 12.0, "mae_pct": -2.0, "finalized": True}))
        w_actions.append(_normalize_action_row({"timestamp_utc": (bt + timedelta(seconds=hold_s)).isoformat(), "action": "WHALE_WEBHOOK", "symbol": f"W{idx}", "asset_id": m, "reason": "WHALE_SELL 1.0 SOL", "triggering_whales": "WalletSteady"}))
    classify_observation_states(w_outcomes, w_actions, reference_ts=(t0 + timedelta(hours=4)).timestamp())
    w_rep = build_wallet_attribution(w_outcomes, w_actions, [], friction_pct=3.0)[0]
    assert w_rep["median_hold_seconds"] == pytest.approx(345.0)
    assert w_rep["hold_count"] == 5
    assert w_rep["fast_exit_count"] == 1
    assert w_rep["fast_exit_farmer"] is False

    # 3. Verify report renders reconciliation section cleanly
    md = render_audit_report(outcomes, actions, friction_pct=3.0, trades=[{"id": 1, "side": "SELL", "symbol": "CLN", "out_amount": 448_000_000, "sol_value": 0.448}])
    assert "Read-Only Accounting Reconciliation" in md
    assert "Discrepancies" in md
