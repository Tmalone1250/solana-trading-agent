"""Unit tests for Sprint 1 — Baseline Recovery & Instrumentation."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from alpha_node import alpha_node
from audit_outcomes import (
    _normalize_action_row,
    _normalize_outcome_row,
    classify_observation_states,
    render_audit_report,
)
from audit_reports import (
    build_lane_comparison,
    build_loss_attribution,
    build_rejection_counterfactuals,
    build_wallet_attribution,
    compute_bucket_expectancy,
    compute_delayed_proxy_pct,
)
from config import AgentConfig
from db import AgentDatabase
from executor_node import executor_node, extract_raw_price_impact
from narrative_cache import (
    clear_narrative_cache,
    get_cached_narrative,
    get_narrative_cache_size,
    put_cached_narrative,
)
from state import FocusState, PendingOrder
from telemetry_api import create_telemetry_app


class CountingStubDecisionClient:
    def __init__(self, answers: dict[str, dict[str, Any]]):
        self.answers = answers
        self.calls = 0

    async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        self.calls += 1
        return self.answers


def _mk_outcome(
    *,
    oid: str,
    mint: str,
    discovered_at: datetime,
    source: str = "CHART_ALPHA",
    decision: str = "ENTERED",
    decision_reason: str = "BUY -> APPROVE",
    strategy: str = "BREAKOUT_IGNITION",
    p1: Any = 2.0,
    p5: Any = 6.0,
    p15: Any = 12.0,
    p30: Any = 15.0,
    mfe: Any = 18.0,
    mae: Any = -3.0,
    finalized: bool = True,
    entry_liq_usd: float = 80_000.0,
    triggering_wallet: str = "",
) -> dict[str, Any]:
    return _normalize_outcome_row(
        {
            "id": oid,
            "mint": mint,
            "symbol": mint[:6],
            "source": source,
            "discovered_at": discovered_at.isoformat(),
            "decision": decision,
            "decision_reason": decision_reason,
            "strategy": strategy,
            "entry_price_usd": 0.001,
            "entry_mc_usd": 120_000.0,
            "entry_liq_usd": entry_liq_usd,
            "price_1m_pct": p1,
            "price_5m_pct": p5,
            "price_15m_pct": p15,
            "price_30m_pct": p30,
            "mfe_pct": mfe,
            "mae_pct": mae,
            "finalized": finalized,
            "triggering_wallet": triggering_wallet,
        }
    )


def test_audit_5_states_stress_test_and_delayed_entry_proxy():
    now_dt = datetime(2026, 11, 22, 18, 0, 0, tzinfo=timezone.utc)
    rows = [
        # 1. COMPLETE_30M
        _mk_outcome(oid="c1", mint="MintComp1", discovered_at=now_dt - timedelta(minutes=60), p1=10.0, p5=21.0, p15=32.0, p30=20.0, finalized=True),
        # 2. IN_PROGRESS (< 35m before latest_ts)
        _mk_outcome(oid="p1", mint="MintProg1", discovered_at=now_dt - timedelta(minutes=10), p1=1.0, p5=2.0, p15=None, p30=None, finalized=False),
        # 3. INTERRUPTED_BY_RESTART: 4 unfinalized rows (>35m old) all stopping at ~now-90m (within 2m window)
        _mk_outcome(oid="r1", mint="MintR1", discovered_at=now_dt - timedelta(minutes=95), p1=1.0, p5=2.0, p15=None, p30=None, finalized=False),
        _mk_outcome(oid="r2", mint="MintR2", discovered_at=now_dt - timedelta(minutes=95, seconds=20), p1=1.0, p5=2.0, p15=None, p30=None, finalized=False),
        _mk_outcome(oid="r3", mint="MintR3", discovered_at=now_dt - timedelta(minutes=94, seconds=40), p1=1.0, p5=2.0, p15=None, p30=None, finalized=False),
        _mk_outcome(oid="r4", mint="MintR4", discovered_at=now_dt - timedelta(minutes=95, seconds=10), p1=1.0, p5=2.0, p15=None, p30=None, finalized=False),
        # 4. VANISHED_OR_DEAD: >35m old, isolated stop time, missing 15m/30m, severe mae <= -70%
        _mk_outcome(oid="v1", mint="MintVan1", discovered_at=now_dt - timedelta(minutes=180), p1=-20.0, p5=-50.0, p15=None, p30=None, mae=-78.0, finalized=False),
        # 5. PARTIAL_OR_UNRESOLVED: >35m old, isolated stop time, missing 30m, mild mae (-5%) — NOT labeled dead!
        _mk_outcome(oid="u1", mint="MintUnr1", discovered_at=now_dt - timedelta(minutes=240), p1=1.0, p5=2.0, p15=3.0, p30=None, mae=-5.0, finalized=False),
    ]

    counts = classify_observation_states(rows, reference_ts=now_dt.timestamp())
    assert sum(counts.values()) == len(rows)
    assert counts["COMPLETE_30M"] == 1
    assert counts["IN_PROGRESS"] == 1
    assert counts["INTERRUPTED_BY_RESTART"] == 4
    assert counts["VANISHED_OR_DEAD"] == 1
    assert counts["PARTIAL_OR_UNRESOLVED"] == 1

    # Dual-bound + multi-level stress test (-50%, -80%, -95%) with 3.0% friction
    stats = compute_bucket_expectancy(rows, friction_pct=3.0)
    assert stats["low_sample"] is True
    assert stats["n_complete"] == 1
    assert stats["n_vanished"] == 1
    # Observed only: (20.0 - 3.0) / 1 = +17.0%
    assert stats["observed_expectancy_pct"] == pytest.approx(17.0)
    # Stress -50%: ((20-3) + (-50-3)) / 2 = (17 - 53) / 2 = -18.0%
    assert stats["stress_expectancy_pct"][-50.0] == pytest.approx(-18.0)
    # Stress -80%: (17 - 83) / 2 = -33.0%
    assert stats["stress_expectancy_pct"][-80.0] == pytest.approx(-33.0)
    # Stress -95%: (17 - 98) / 2 = -40.5%
    assert stats["stress_expectancy_pct"][-95.0] == pytest.approx(-40.5)

    # Milestone-based delayed-entry proxy math: ((1 + 0.21)/(1 + 0.10) - 1) * 100 = +10.0%
    assert compute_delayed_proxy_pct(10.0, 21.0) == pytest.approx(10.0)
    assert compute_delayed_proxy_pct(10.0, 32.0) == pytest.approx(20.0)


def test_audit_loss_attribution_wallet_rank_farmer_and_lanes():
    t0 = datetime(2026, 11, 22, 16, 0, 0, tzinfo=timezone.utc)
    outcomes = [
        # Closed losing trade with strong MFE (MANAGEMENT + stop_triggered_during_recovery + weak_liquidity)
        _mk_outcome(oid="o_mgmt", mint="MintMgmt", discovered_at=t0, p1=-2.0, p5=-11.0, p15=14.0, p30=18.0, mfe=20.0, mae=-12.0, entry_liq_usd=25_000.0),
        # Closed losing trade that never moved (SELECTION)
        _mk_outcome(oid="o_sel", mint="MintSel", discovered_at=t0, p1=-4.0, p5=-8.0, p15=-15.0, p30=-22.0, mfe=1.0, mae=-25.0),
        # Rejected candidate counterfactual
        _mk_outcome(oid="o_rej", mint="MintRej", discovered_at=t0, decision="BLOCKED", decision_reason="VOL_SPIKE_LOW", p1=5.0, p5=15.5, p15=26.0, p30=30.0),
        # Whale 1st mover (fast-exit farmer < 45s) and follower (rank 2) on same mint
        _mk_outcome(oid="w1", mint="MintWhale1", discovered_at=t0, source="WHALE_WEBHOOK", decision="WHALE_BUY", strategy="WHALE_SCALP", triggering_wallet="WalletFirst", p1=4.0, p5=14.4, p15=24.8, p30=20.0, mfe=25.0),
        _mk_outcome(oid="w2", mint="MintWhale1", discovered_at=t0 + timedelta(seconds=30), source="WHALE_WEBHOOK", decision="WHALE_BUY", strategy="WHALE_SCALP", triggering_wallet="WalletFollow", p1=2.0, p5=12.2, p15=18.0, p30=15.0, mfe=19.0),
    ]
    actions = [
        _normalize_action_row({"timestamp_utc": t0.isoformat(), "action": "BUY", "symbol": "MGMT", "asset_id": "MintMgmt", "strategy": "BREAKOUT_IGNITION", "jupiter_price_impact_raw": "2.4"}),
        _normalize_action_row({"timestamp_utc": (t0 + timedelta(minutes=6)).isoformat(), "action": "SELL", "symbol": "MGMT", "asset_id": "MintMgmt", "strategy": "BREAKOUT_IGNITION", "reason": "HARD_STOP_LOSS", "pnl_pct": -10.5, "peak_pnl_pct": 12.0, "liq_usd": 25_000.0}),
        _normalize_action_row({"timestamp_utc": (t0 + timedelta(minutes=8)).isoformat(), "action": "SELL", "symbol": "SEL", "asset_id": "MintSel", "strategy": "SCALPER", "reason": "HARD_STOP_LOSS", "pnl_pct": -11.0, "peak_pnl_pct": 0.5, "liq_usd": 90_000.0}),
        # WalletFirst sells after 30 seconds -> fast_exit_farmer = True & exit_before_expansion = 1
        _normalize_action_row({"timestamp_utc": (t0 + timedelta(seconds=30)).isoformat(), "action": "WHALE_WEBHOOK", "symbol": "W1", "asset_id": "MintWhale1", "reason": "WHALE_SELL 1.5 SOL", "triggering_whales": "WalletFirst"}),
    ]
    classify_observation_states(outcomes, actions, reference_ts=(t0 + timedelta(hours=2)).timestamp())

    attr = build_loss_attribution(outcomes, actions, friction_pct=3.0)
    by_mint = {r["mint"]: r for r in attr}
    assert by_mint["MintMgmt"]["primary_cause"] == "MANAGEMENT"
    assert "weak_liquidity" in by_mint["MintMgmt"]["secondary_factors"]
    assert "stop_triggered_during_recovery" in by_mint["MintMgmt"]["secondary_factors"]
    assert "high_entry_friction" in by_mint["MintMgmt"]["secondary_factors"]
    assert by_mint["MintSel"]["primary_cause"] == "SELECTION"

    rej = build_rejection_counterfactuals(outcomes, friction_pct=3.0)
    assert any("VOL_SPIKE_LOW" in r["bucket"] for r in rej)

    wallets = {w["wallet"]: w for w in build_wallet_attribution(outcomes, actions, [], friction_pct=3.0)}
    assert wallets["WalletFirst"]["avg_entry_rank"] == 1.0
    assert wallets["WalletFirst"]["median_hold_seconds"] == pytest.approx(30.0)
    assert wallets["WalletFirst"]["fast_exit_farmer"] is True
    assert wallets["WalletFirst"]["wallet_exit_before_expansion_count"] == 1
    assert wallets["WalletFollow"]["avg_entry_rank"] == 2.0
    assert wallets["WalletFollow"]["fast_exit_farmer"] is False

    lanes = build_lane_comparison(outcomes, friction_pct=3.0)
    assert lanes["CHART_ALPHA"]["n_complete"] == 3
    assert lanes["WHALE_WEBHOOK"]["n_complete"] == 2

    md = render_audit_report(outcomes, actions, friction_pct=3.0)
    assert "5-State Completeness Ledger" in md
    assert "milestone-based delayed-entry proxies" in md
    assert "stress-test assumptions, not observed fills" in md


@pytest.mark.asyncio
async def test_jupiter_raw_price_impact_persisted_without_altering_fills(tmp_path):
    assert extract_raw_price_impact({"priceImpactPct": "-1.425"}) == "-1.425"
    assert extract_raw_price_impact({"priceImpact": 0.031}) == "0.031"
    assert extract_raw_price_impact({"outAmount": "100"}) is None

    db = AgentDatabase(db_path=str(tmp_path / "impact.duckdb"), starting_sol=1.0)
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper")

    # Even with an extreme price impact (e.g. "25.5"), instrumentation MUST NOT veto or block the fill
    def jup_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"outAmount": "2000000", "priceImpactPct": "25.5", "transaction": None})

    order = PendingOrder(
        side="BUY",
        mint="MintImpact11111111111111111111111111111111",
        symbol="IMP",
        amount_atomic=500_000_000,
        reason="DECISION_1_ENTRY",
        target_state_after_fill=FocusState.IN_POSITION,
        strategy="BREAKOUT_IGNITION",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(jup_handler)) as client:
        out = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 1.0, "daily_pnl_sol": 0.0, "pending_order": order.model_dump()},
            config=cfg,
            db=db,
            http_client=client,
        )

    assert out["focus_state"] == FocusState.IN_POSITION.value
    assert out["last_execution"]["jupiter_price_impact_raw"] == "25.5"

    # Verify persisted in DuckDB trades table and exported in GET /api/export/csv
    row = db._conn.execute("SELECT jupiter_price_impact_raw FROM trades WHERE mint = ?", [order.mint]).fetchone()
    assert row is not None and row[0] == "25.5"

    app = create_telemetry_app(config=cfg, db=db)
    resp = TestClient(app).get("/api/export/csv?type=trades")
    assert resp.status_code == 200
    parsed = list(csv.DictReader(io.StringIO(resp.text)))
    assert parsed[0]["jupiter_price_impact_raw"] == "25.5"
    db.close()


@pytest.mark.asyncio
async def test_gemini_narrative_cache_ttl_eviction_and_timestamp_preservation(tmp_path):
    clear_narrative_cache()
    t0 = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Bounded max_size=200 eviction check
    for i in range(205):
        put_cached_narrative(
            f"MintCap{i:03d}",
            narrative_strength={"type": "score", "score": 3.0},
            momentum_quality={"type": "score", "score": 3.0},
            is_copycat_vamp={"type": "noul", "noul": 0.2},
            action={"type": "choice", "choice": "WATCH", "confidence": 0.7},
            now_dt=t0 + timedelta(seconds=i),
        )
    assert get_narrative_cache_size() == 200
    assert get_cached_narrative("MintCap000", now_ts=(t0 + timedelta(seconds=210)).timestamp()) is None
    assert get_cached_narrative("MintCap204", now_ts=(t0 + timedelta(seconds=210)).timestamp()) is not None

    # 2. alpha_node 30m TTL cache hit vs expiry without changing decision eligibility
    clear_narrative_cache()
    db = AgentDatabase(db_path=str(tmp_path / "narr_cache.duckdb"), starting_sol=1.0)
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper", NARRATIVE_CACHE_TTL_SECONDS=1800)
    stub = CountingStubDecisionClient(
        {
            "t0_narrative_strength": {"type": "score", "score": 3.5, "confidence": 0.85},
            "t0_momentum_quality": {"type": "score", "score": 3.2, "confidence": 0.82},
            "t0_is_copycat_vamp": {"type": "noul", "noul": 0.15, "confidence": 0.90},
            "t0_action": {"type": "choice", "choice": "WATCH", "confidence": 0.75},
        }
    )
    cand = {"mint": "MintCacheTest111111111111111111111111111111", "symbol": "CACH", "added_at": t0.isoformat(), "liquidity_usd": 60_000.0}

    # Tick 1 (t=0m): Cache miss -> calls Gemini once
    out1 = await alpha_node({"focus_state": FocusState.SCANNING.value, "sol_balance": 1.0, "safe_candidates": [cand]}, config=cfg, db=db, decision_client=stub, now=t0)  # type: ignore[arg-type]
    assert stub.calls == 1
    assert out1["alpha_decisions"][0]["narrative_strength"] == 3.5

    # Tick 2 (t=15s): Cache hit -> zero extra Gemini call, original cached_at preserved in decisions_log
    out2 = await alpha_node({"focus_state": FocusState.SCANNING.value, "sol_balance": 1.0, "safe_candidates": [cand]}, config=cfg, db=db, decision_client=stub, now=t0 + timedelta(seconds=15))  # type: ignore[arg-type]
    assert stub.calls == 1
    assert out2["alpha_decisions"][0]["narrative_strength"] == 3.5
    recent_decs = db.get_recent_decisions(limit=2)
    assert recent_decs[0]["scores"]["narrative_cache_hit"] is True
    assert recent_decs[0]["scores"]["narrative_cached_at"] == t0.isoformat()

    # Tick 3 (t=31m): Cache expired -> calls Gemini again
    await alpha_node({"focus_state": FocusState.SCANNING.value, "sol_balance": 1.0, "safe_candidates": [cand]}, config=cfg, db=db, decision_client=stub, now=t0 + timedelta(minutes=31))  # type: ignore[arg-type]
    assert stub.calls == 2
    db.close()
    clear_narrative_cache()


@pytest.mark.asyncio
async def test_hard_stop_loss_no_duplicate_sell_in_graph_or_concurrent_executor(tmp_path):
    """Reproduce LangGraph double-trigger + concurrent/back-to-back HARD_STOP_LOSS SELL calls."""
    import asyncio
    from executor_node import clear_sell_idempotency_guard
    from graph import build_agent_graph
    from state import ActivePosition

    clear_sell_idempotency_guard()
    t0 = datetime(2026, 11, 22, 19, 0, 0, tzinfo=timezone.utc)
    mint = "MintZolHardStop1111111111111111111111111111"

    # 1. End-to-end LangGraph tick at -12.30% (must execute HARD_STOP_LOSS SELL exactly ONCE)
    db1 = AgentDatabase(db_path=str(tmp_path / "graph_hs.duckdb"), starting_sol=1.0)
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper", HARD_STOP_LOSS_PCT=0.12, PAPER_TX_FEE_SOL=0.001)
    pos = ActivePosition(
        mint=mint, symbol="ZOL", entry_sol=0.50, token_amount=1_000_000,
        strategy="BREAKOUT_IGNITION", opened_at=t0.isoformat(),
        current_sol_value=0.50, peak_sol_value=0.50, reference_token_amount=1_000_000,
    ).model_dump()
    db1.save_lane_positions(lane="chart", positions=[pos], sol_balance=0.50, daily_pnl_sol=0.0, focus_state=FocusState.IN_POSITION.value, active_position=pos)

    def handler_graph(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/swap/v2/order" in url:
            return httpx.Response(200, json={"outAmount": "438500000", "transaction": None})
        if "/tokens/v1/solana/" in url:
            return httpx.Response(200, json=[{"chainId": "solana", "baseToken": {"address": mint, "symbol": "ZOL"}, "priceNative": "0.4385", "liquidity": {"usd": 80_000.0}, "txns": {"m5": {"buys": 20, "sells": 10}}}])
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_graph)) as client:
        compiled = build_agent_graph(config=cfg, db=db1, decision_client=CountingStubDecisionClient({}), http_client=client)  # type: ignore[arg-type]
        st = db1.get_state()
        await compiled.ainvoke({
            "focus_state": st["focus_state"], "sol_balance": st["sol_balance"],
            "daily_start_sol": st["daily_start_sol"], "daily_pnl_sol": st["daily_pnl_sol"],
            "active_position": st["active_position"], "chart_positions": st["chart_positions"],
        })

    st_after = db1.get_state()
    assert st_after["focus_state"] == FocusState.SCANNING.value
    assert st_after["active_position"] is None
    assert st_after["sol_balance"] == pytest.approx(0.50 + 0.4385 - 0.001, rel=1e-6)
    assert len(db1.get_recent_closed_trades(limit=10)) == 1
    assert len(db1.get_action_log(limit=10, actions=["SELL"])) == 1
    db1.close()

    # 2. Concurrent + back-to-back direct executor_node HARD_STOP_LOSS SELL calls
    db2 = AgentDatabase(db_path=str(tmp_path / "concurrent_hs.duckdb"), starting_sol=1.0)
    db2.save_lane_positions(lane="chart", positions=[pos], sol_balance=0.50, daily_pnl_sol=0.0, focus_state=FocusState.IN_POSITION.value, active_position=pos)
    sell_order = PendingOrder(
        side="SELL", mint=mint, symbol="ZOL", amount_atomic=1_000_000,
        reason="HARD_STOP_LOSS (-12.30% <= -12%)", target_state_after_fill=FocusState.SCANNING,
        apply_cooldown=True, strategy="BREAKOUT_IGNITION",
    ).model_dump()
    exec_state = {
        "focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.50, "daily_pnl_sol": 0.0,
        "active_position": pos, "chart_positions": [pos], "pending_order": sell_order,
    }
    swap_calls = {"n": 0}

    def handler_concurrent(request: httpx.Request) -> httpx.Response:
        swap_calls["n"] += 1
        return httpx.Response(200, json={"outAmount": "438500000", "transaction": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_concurrent)) as client:
        await asyncio.gather(
            executor_node(dict(exec_state), config=cfg, db=db2, http_client=client),
            executor_node(dict(exec_state), config=cfg, db=db2, http_client=client),
        )
        # Back-to-back 3rd call immediately after
        await executor_node(dict(exec_state), config=cfg, db=db2, http_client=client)

    assert swap_calls["n"] == 1
    assert db2.get_state()["sol_balance"] == pytest.approx(0.50 + 0.4385 - 0.001, rel=1e-6)
    assert len(db2.get_recent_closed_trades(limit=10)) == 1
    assert len(db2.get_action_log(limit=10, actions=["SELL"])) == 1
    db2.close()
    clear_sell_idempotency_guard()
