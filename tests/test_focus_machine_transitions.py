"""Unit tests for the full 3-State Focus Machine transitions & Telemetry API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from config import LAMPORTS_PER_SOL, AgentConfig
from db import AgentDatabase
from executor_node import executor_node
from graph import build_agent_graph
from position_monitor_node import position_monitor_node
from state import ActivePosition, FocusState
from telemetry_api import create_telemetry_app


class StubDecisionClient:
    def __init__(self, answers: dict[str, dict[str, Any]]):
        self.answers = answers
        self.call_count = 0

    async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        self.call_count += 1
        return self.answers


def _make_monitor_client(
    *,
    sol_quote: float,
    buys_5m: int = 30,
    sells_5m: int = 10,
    liquidity_usd: float = 100_000.0,
    market_cap_usd: float = 800_000.0,
    vol_5m: float = 20_000.0,
    vol_1h: float = 150_000.0,
    pc_5m: float = 6.0,
    pc_1h: float = 25.0,
) -> httpx.AsyncClient:
    lamports = int(round(sol_quote * LAMPORTS_PER_SOL))

    def handler(request: httpx.Request) -> httpx.Response:
        if "/swap/v2/order" in str(request.url):
            return httpx.Response(200, json={"outAmount": str(lamports), "transaction": None})
        if "/tokens/v1/solana/" in str(request.url):
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": "solana",
                        "marketCap": market_cap_usd,
                        "liquidity": {"usd": liquidity_usd},
                        "volume": {"m5": vol_5m, "h1": vol_1h},
                        "priceChange": {"m5": pc_5m, "h1": pc_1h},
                        "txns": {"m5": {"buys": buys_5m, "sells": sells_5m}},
                    }
                ],
            )
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _base_in_position(entry_sol: float = 0.5) -> dict[str, Any]:
    return ActivePosition(
        mint="MintFocus1111111111111111111111111111111111",
        symbol="FOCUS",
        entry_sol=entry_sol,
        token_amount=1_000_000,
        entry_liquidity_usd=100_000.0,
        current_sol_value=entry_sol,
        peak_sol_value=entry_sol,
        reference_token_amount=1_000_000,
    ).model_dump()


@pytest.mark.asyncio
async def test_hard_stop_minus_10_pct_exits_to_scanning_with_cooldown(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "hard_stop.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None)
    dec = StubDecisionClient({})
    state = {
        "focus_state": FocusState.IN_POSITION.value,
        "sol_balance": 0.2,
        "daily_pnl_sol": 0.0,
        "active_position": _base_in_position(0.5),
    }

    # Drop -10% (0.5 -> 0.45 SOL)
    async with _make_monitor_client(sol_quote=0.45) as client:
        mon = await position_monitor_node(state, config=cfg, db=db, decision_client=dec, http_client=client)  # type: ignore[arg-type]
        assert mon["pending_order"] is not None
        assert "HARD_STOP_LOSS" in mon["pending_order"]["reason"]
        assert mon["pending_order"]["target_state_after_fill"] == FocusState.SCANNING.value

        state.update(mon)
        ex = await executor_node(state, config=cfg, db=db, http_client=client)

    assert ex["focus_state"] == FocusState.SCANNING.value
    assert ex["active_position"] is None
    assert db.is_on_cooldown("MintFocus1111111111111111111111111111111111")
    db.close()


@pytest.mark.asyncio
async def test_breakeven_shield_arms_at_plus_8_and_exits_at_plus_2(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "breakeven.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None)
    dec = StubDecisionClient({})
    state = {
        "focus_state": FocusState.IN_POSITION.value,
        "sol_balance": 0.2,
        "daily_pnl_sol": 0.0,
        "active_position": _base_in_position(0.5),
    }

    # Tick 1: Price reaches +9% (0.545 SOL) -> arms breakeven (+8%), does NOT arm trail (+12%)
    async with _make_monitor_client(sol_quote=0.545) as client:
        mon1 = await position_monitor_node(state, config=cfg, db=db, decision_client=dec, http_client=client)  # type: ignore[arg-type]
        assert mon1["pending_order"] is None
        assert mon1["active_position"]["breakeven_armed"] is True
        assert mon1["active_position"]["trail_armed"] is False

    # Tick 2: Price drops back to +2% floor (0.51 SOL) -> triggers breakeven shield -> SCANNING + cooldown
    state.update(mon1)
    async with _make_monitor_client(sol_quote=0.51) as client:
        mon2 = await position_monitor_node(state, config=cfg, db=db, decision_client=dec, http_client=client)  # type: ignore[arg-type]
        assert mon2["pending_order"] is not None
        assert "BREAKEVEN_SHIELD" in mon2["pending_order"]["reason"]
        state.update(mon2)
        ex = await executor_node(state, config=cfg, db=db, http_client=client)

    assert ex["focus_state"] == FocusState.SCANNING.value
    assert db.is_on_cooldown("MintFocus1111111111111111111111111111111111")
    db.close()


@pytest.mark.asyncio
async def test_trailing_tp_plus_12_minus_6_transitions_to_watching_and_leg2_reentry(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "trail_leg2.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None)
    leg2_approve = StubDecisionClient(
        {
            "leg_2_momentum": {"type": "score", "score": 4.1, "confidence": 0.90},
            "is_dead_cat_bounce": {"type": "noul", "noul": 0.15, "confidence": 0.88},
        }
    )
    state = {
        "focus_state": FocusState.IN_POSITION.value,
        "sol_balance": 0.2,
        "daily_pnl_sol": 0.0,
        "active_position": _base_in_position(0.5),
    }

    # Tick 1: Runs to +20% (0.60 SOL) -> arms trailing TP (+12%)
    async with _make_monitor_client(sol_quote=0.60) as client:
        mon1 = await position_monitor_node(state, config=cfg, db=db, decision_client=leg2_approve, http_client=client)  # type: ignore[arg-type]
        assert mon1["pending_order"] is None
        assert mon1["active_position"]["trail_armed"] is True
        assert pytest.approx(mon1["active_position"]["peak_sol_value"]) == 0.60

    # Tick 2: Pulls back -6.5% from 0.60 peak -> 0.561 SOL -> sells 100% -> WATCHING_REENTRY
    state.update(mon1)
    async with _make_monitor_client(sol_quote=0.561) as client:
        mon2 = await position_monitor_node(state, config=cfg, db=db, decision_client=leg2_approve, http_client=client)  # type: ignore[arg-type]
        assert mon2["pending_order"] is not None
        assert "TRAILING_TP" in mon2["pending_order"]["reason"]
        assert mon2["pending_order"]["target_state_after_fill"] == FocusState.WATCHING_REENTRY.value
        state.update(mon2)
        ex_sell = await executor_node(state, config=cfg, db=db, http_client=client)

    assert ex_sell["focus_state"] == FocusState.WATCHING_REENTRY.value
    assert not db.is_on_cooldown("MintFocus1111111111111111111111111111111111")
    assert pytest.approx(ex_sell["sol_balance"], rel=1e-4) == 0.761

    # Tick 3 (WATCHING_REENTRY): Retraces lower to 0.52 SOL (updates retrace_low)
    state.update(ex_sell)
    async with _make_monitor_client(sol_quote=0.52, buys_5m=20, sells_5m=20) as client:
        mon3 = await position_monitor_node(state, config=cfg, db=db, decision_client=leg2_approve, http_client=client)  # type: ignore[arg-type]
        assert mon3["pending_order"] is None
        assert pytest.approx(mon3["active_position"]["retrace_low_sol"]) == 0.52

    # Tick 4 (WATCHING_REENTRY): Curls +6% off 0.52 -> 0.5512 SOL, 5m buys = 65% (>= 58%), Decision 1 confirms
    state.update(mon3)
    async with _make_monitor_client(sol_quote=0.5512, buys_5m=65, sells_5m=35) as client:
        mon4 = await position_monitor_node(state, config=cfg, db=db, decision_client=leg2_approve, http_client=client)  # type: ignore[arg-type]
        assert mon4["pending_order"] is not None
        assert mon4["pending_order"]["side"] == "BUY"
        assert mon4["pending_order"]["reason"] == "LEG_2_REENTRY"
        assert "[DECISION-1 LEG-2] FOCUS" in mon4["last_decision_rationale"]
        state.update(mon4)
        ex_rebuy = await executor_node(state, config=cfg, db=db, http_client=client)

    assert ex_rebuy["focus_state"] == FocusState.IN_POSITION.value
    assert leg2_approve.call_count == 1
    db.close()


@pytest.mark.asyncio
async def test_exodus_bleed_and_timeout_abandonment(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "abandon.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None)
    dec = StubDecisionClient({})

    # 1. Exodus in IN_POSITION (5m sells >= 65%)
    state_pos = {
        "focus_state": FocusState.IN_POSITION.value,
        "sol_balance": 0.2,
        "daily_pnl_sol": 0.0,
        "active_position": _base_in_position(0.5),
    }
    async with _make_monitor_client(sol_quote=0.52, buys_5m=30, sells_5m=70) as client:
        mon = await position_monitor_node(state_pos, config=cfg, db=db, decision_client=dec, http_client=client)  # type: ignore[arg-type]
        assert mon["pending_order"] is not None
        assert "EXODUS_SELL_FLOW" in mon["pending_order"]["reason"]

    # 2. Exodus in WATCHING_REENTRY via >= 10% liquidity drop
    watch_pos = ActivePosition(
        mint="MintWatchEx11111111111111111111111111111111",
        symbol="WEX",
        entry_sol=0.5,
        token_amount=0,
        entry_liquidity_usd=100_000.0,
        current_sol_value=0.55,
        peak_sol_value=0.60,
        run_peak_sol=0.60,
        retrace_low_sol=0.55,
        reference_token_amount=1_000_000,
        watch_started_at=datetime.now(timezone.utc).isoformat(),
    ).model_dump()
    async with _make_monitor_client(sol_quote=0.54, buys_5m=50, sells_5m=50, liquidity_usd=88_000.0) as client:
        mon_ex = await position_monitor_node(
            {"focus_state": FocusState.WATCHING_REENTRY.value, "sol_balance": 0.75, "daily_pnl_sol": 0.05, "active_position": watch_pos},
            config=cfg,
            db=db,
            decision_client=dec,  # type: ignore[arg-type]
            http_client=client,
        )
    assert mon_ex["focus_state"] == FocusState.SCANNING.value
    assert "EXODUS_LIQ_DROP" in mon_ex["monitor_event"]
    assert db.is_on_cooldown("MintWatchEx11111111111111111111111111111111")

    # 3. -25% bleed from run peak in WATCHING_REENTRY
    watch_bleed = dict(watch_pos, mint="MintBleed1111111111111111111111111111111111")
    async with _make_monitor_client(sol_quote=0.44, buys_5m=50, sells_5m=50, liquidity_usd=100_000.0) as client:
        mon_bleed = await position_monitor_node(
            {"focus_state": FocusState.WATCHING_REENTRY.value, "sol_balance": 0.75, "daily_pnl_sol": 0.05, "active_position": watch_bleed},
            config=cfg,
            db=db,
            decision_client=dec,  # type: ignore[arg-type]
            http_client=client,
        )
    assert mon_bleed["focus_state"] == FocusState.SCANNING.value
    assert "WATCH_BLEED" in mon_bleed["monitor_event"]

    # 4. 20m timeout in WATCHING_REENTRY
    started = datetime.now(timezone.utc) - timedelta(minutes=21)
    watch_to = dict(
        watch_pos,
        mint="MintTimeout11111111111111111111111111111111",
        watch_started_at=started.isoformat(),
    )
    async with _make_monitor_client(sol_quote=0.55, buys_5m=50, sells_5m=50, liquidity_usd=100_000.0) as client:
        mon_to = await position_monitor_node(
            {"focus_state": FocusState.WATCHING_REENTRY.value, "sol_balance": 0.75, "daily_pnl_sol": 0.05, "active_position": watch_to},
            config=cfg,
            db=db,
            decision_client=dec,  # type: ignore[arg-type]
            http_client=client,
        )
    assert mon_to["focus_state"] == FocusState.SCANNING.value
    assert "WATCH_TIMEOUT" in mon_to["monitor_event"]
    db.close()


@pytest.mark.asyncio
async def test_langgraph_end_to_end_and_telemetry_api(tmp_path):
    from ohlcv_client import clear_ohlcv_cache
    from scanner_node import clear_geckoterminal_cache
    from tests.test_scalper_entry_engine import make_sweep_reclaim_candles

    clear_geckoterminal_cache()
    clear_ohlcv_cache()
    db = AgentDatabase(db_path=str(tmp_path / "e2e.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None, HUNTING_LANE="graduations", OHLCV_PACING_DELAY_SECONDS=0.0)
    # Pre-seed MintGrad in active_watchlist soaked for 5m so the Scalper Entry Engine allows entry
    db.upsert_watchlist_entry(
        mint="MintGrad11111111111111111111111111111111111",
        symbol="GRAD",
        pair_address="PoolGrad11111111111111111111111111111111111",
        candidate={"mint": "MintGrad11111111111111111111111111111111111", "symbol": "GRAD"},
        added_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    )
    sweep_ohlcv = [
        [c["timestamp"], c["open"], c["high"], c["low"], c["close"], c["volume"]]
        for c in make_sweep_reclaim_candles()
    ]
    dec = StubDecisionClient(
        {
            "t0_narrative_strength": {"type": "score", "score": 4.5, "confidence": 0.92},
            "t0_momentum_quality": {"type": "score", "score": 4.3, "confidence": 0.91},
            "t0_is_copycat_vamp": {"type": "noul", "noul": 0.10, "confidence": 0.95},
            "t0_action": {"type": "choice", "choice": "BUY", "confidence": 0.90},
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/ohlcv/minute" in url:
            return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": sweep_ohlcv}}})
        if "geckoterminal.com" in url:
            return httpx.Response(200, json={"data": []})
        if "token-boosts" in url:
            return httpx.Response(
                200,
                json=[{"chainId": "solana", "tokenAddress": "MintGrad11111111111111111111111111111111111", "totalAmount": 200}],
            )
        if "token-profiles" in url or "community-takeovers" in url:
            return httpx.Response(200, json=[])
        if "/tokens/v1/solana/" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": "solana",
                        "pairAddress": "PoolGrad11111111111111111111111111111111111",
                        "baseToken": {"address": "MintGrad11111111111111111111111111111111111", "symbol": "GRAD"},
                        "marketCap": 100_000,
                        "liquidity": {"usd": 40_000},
                        "volume": {"h1": 80_000, "m5": 12_000},
                        "txns": {"m5": {"buys": 35, "sells": 10}},
                    }
                ],
            )
        if "rugcheck.xyz" in url:
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        if "/ultra/v1/shield" in url:
            return httpx.Response(200, json={"warnings": {"MintGrad11111111111111111111111111111111111": []}})
        if "/swap/v2/order" in url:
            return httpx.Response(200, json={"outAmount": "5555555", "transaction": None})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        compiled = build_agent_graph(config=cfg, db=db, decision_client=dec, http_client=client)  # type: ignore[arg-type]
        st = db.get_state()
        result = await compiled.ainvoke(
            {
                "focus_state": st["focus_state"],
                "sol_balance": st["sol_balance"],
                "daily_start_sol": st["daily_start_sol"],
                "daily_pnl_sol": st["daily_pnl_sol"],
                "active_position": st["active_position"],
                "last_decision_rationale": st["last_decision_rationale"],
            }
        )

    assert result["focus_state"] == FocusState.IN_POSITION.value
    assert result["active_position"]["symbol"] == "GRAD"

    # Verify FastAPI GET /api/telemetry reflects the live DuckDB state
    app = create_telemetry_app(config=cfg, db=db)
    test_client = TestClient(app)
    resp = test_client.get("/api/telemetry")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["focus_state"] == "IN_POSITION"
    assert payload["active_mint"] == "MintGrad11111111111111111111111111111111111"
    assert payload["active_symbol"] == "GRAD"
    assert pytest.approx(payload["sol_balance"], rel=1e-4) == 0.2
    assert payload["live_jupiter_pnl"]["entry_sol"] == 0.5
    assert len(payload["decision_1_rationales"]) == 1
    assert "[DECISION-1 ENTRY] GRAD" in payload["latest_decision_1_rationale"]
    db.close()
