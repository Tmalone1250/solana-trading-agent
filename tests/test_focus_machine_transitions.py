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
            req_mint = str(request.url).rsplit("/", 1)[-1].split(",")[0]
            pair_addr = (
                "PoolWatchEx1111111111111111111111111111111"
                if req_mint == "MintWatchEx11111111111111111111111111111111"
                else "PoolFocus111111111111111111111111111111111"
            )
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": "solana",
                        "pairAddress": pair_addr,
                        "dexId": "pumpswap",
                        "baseToken": {"address": req_mint},
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
        pair_address="PoolFocus111111111111111111111111111111111",
        dex_id="pumpswap",
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
    cfg = AgentConfig(_env_file=None, PAPER_TX_FEE_SOL=0.0)
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

    # Tick 1: Runs to +14% (0.57 SOL) -> arms trailing TP (+12%) below the +15% partial TP threshold
    async with _make_monitor_client(sol_quote=0.57) as client:
        mon1 = await position_monitor_node(state, config=cfg, db=db, decision_client=leg2_approve, http_client=client)  # type: ignore[arg-type]
        assert mon1["pending_order"] is None
        assert mon1["active_position"]["trail_armed"] is True
        assert pytest.approx(mon1["active_position"]["peak_sol_value"]) == 0.57

    # Tick 2: Pulls back -6.5% from 0.57 peak -> 0.533 SOL -> sells 100% -> WATCHING_REENTRY
    state.update(mon1)
    async with _make_monitor_client(sol_quote=0.533) as client:
        mon2 = await position_monitor_node(state, config=cfg, db=db, decision_client=leg2_approve, http_client=client)  # type: ignore[arg-type]
        assert mon2["pending_order"] is not None
        assert "TRAILING_TP" in mon2["pending_order"]["reason"]
        assert mon2["pending_order"]["target_state_after_fill"] == FocusState.WATCHING_REENTRY.value
        state.update(mon2)
        ex_sell = await executor_node(state, config=cfg, db=db, http_client=client)

    assert ex_sell["focus_state"] == FocusState.WATCHING_REENTRY.value
    assert not db.is_on_cooldown("MintFocus1111111111111111111111111111111111")
    assert pytest.approx(ex_sell["sol_balance"], rel=1e-4) == 0.733

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
        pair_address="PoolWatchEx1111111111111111111111111111111",
        dex_id="pumpswap",
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
    cfg = AgentConfig(_env_file=None, HUNTING_LANE="graduations", OHLCV_PACING_DELAY_SECONDS=0.0, PAPER_TX_FEE_SOL=0.0)
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

    # Verify FastAPI GET /api/telemetry reflects the live DuckDB state, CORS, & closed recent_trades
    db.log_trade(
        mode="paper",
        side="SELL",
        mint="MintClosed1",
        symbol="WIN",
        in_amount=1_000_000,
        out_amount=575_000_000,
        sol_value=0.575,
        pnl_sol=0.075,
        pnl_pct=0.15,
        reason="TRAILING_TP",
        tx_signature="paper-sell-1",
    )
    app = create_telemetry_app(config=cfg, db=db)
    test_client = TestClient(app)
    resp = test_client.get("/api/telemetry", headers={"Origin": "http://localhost:3000"})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "*"
    payload = resp.json()
    assert payload["focus_state"] == "IN_POSITION"
    assert payload["active_mint"] == "MintGrad11111111111111111111111111111111111"
    assert payload["active_symbol"] == "GRAD"
    assert pytest.approx(payload["sol_balance"], rel=1e-4) == 0.2
    assert payload["live_jupiter_pnl"]["entry_sol"] == 0.5
    assert len(payload["decision_1_rationales"]) == 1
    assert "[DECISION-1 ENTRY] GRAD" in payload["latest_decision_1_rationale"]
    assert len(payload["recent_trades"]) == 1
    closed = payload["recent_trades"][0]
    assert closed["symbol"] == "WIN"
    assert closed["mint"] == "MintClosed1"
    assert closed["entry_sol"] == 0.5
    assert closed["exit_sol"] == 0.575
    assert closed["pnl_sol"] == 0.075
    assert closed["pnl_pct"] == 15.0
    assert closed["exit_reason"] == "TRAILING_TP"
    assert closed["closed_at"]
    db.close()


@pytest.mark.asyncio
async def test_reentry_anti_chase_aborts_if_price_rips_during_decision_1(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "anti_chase.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None, MAX_REENTRY_CHASE_PCT=0.08)
    leg2_approve = StubDecisionClient(
        {
            "leg_2_momentum": {"type": "score", "score": 4.2, "confidence": 0.90},
            "is_dead_cat_bounce": {"type": "noul", "noul": 0.12, "confidence": 0.88},
        }
    )
    watch_pos = ActivePosition(
        mint="MintJensen111111111111111111111111111111111",
        symbol="JENSEN",
        pair_address="PoolJensen11111111111111111111111111111111",
        dex_id="pumpswap",
        entry_sol=0.5,
        token_amount=0,
        entry_liquidity_usd=100_000.0,
        current_sol_value=0.48,
        peak_sol_value=0.65,
        run_peak_sol=0.65,
        retrace_low_sol=0.48,
        reference_token_amount=1_000_000,
        watch_started_at=datetime.now(timezone.utc).isoformat(),
    ).model_dump()

    # 1st Jupiter quote = 0.5099 SOL (+6.2% curl trigger); 2nd re-check quote = 0.6018 SOL (+18% rip during Decision-1)
    quote_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/swap/v2/order" in str(request.url):
            quote_calls["n"] += 1
            sol_val = 0.5099 if quote_calls["n"] == 1 else 0.6018
            return httpx.Response(200, json={"outAmount": str(int(round(sol_val * 1_000_000_000))), "transaction": None})
        return httpx.Response(
            200,
            json=[
                {
                    "chainId": "solana",
                    "pairAddress": "PoolJensen11111111111111111111111111111111",
                    "dexId": "pumpswap",
                    "baseToken": {"address": "MintJensen111111111111111111111111111111111"},
                    "liquidity": {"usd": 100_000.0},
                    "volume": {"h1": 240_000.0, "m5": 36_000.0},
                    "priceChange": {"h1": 28.0, "m5": 5.0},
                    "txns": {"m5": {"buys": 65, "sells": 35}},
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await position_monitor_node(
            {"focus_state": FocusState.WATCHING_REENTRY.value, "sol_balance": 0.80, "daily_pnl_sol": 0.10, "active_position": watch_pos},
            config=cfg,
            db=db,
            decision_client=leg2_approve,  # type: ignore[arg-type]
            http_client=client,
        )

    assert quote_calls["n"] == 2
    assert res["pending_order"] is None
    assert res["focus_state"] == FocusState.SCANNING.value
    assert res["active_position"] is None
    assert res["monitor_event"] == "LEG_2_REENTRY_ABORT_CHASE"
    assert "REENTRY_ANTI_CHASE" in res["last_decision_rationale"]
    db.close()


@pytest.mark.asyncio
async def test_max_reentries_per_mint_puts_mint_on_cooldown_after_second_reentry(tmp_path):
    db = AgentDatabase(db_path=str(tmp_path / "max_reentries.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(_env_file=None, MAX_REENTRIES_PER_MINT=2)
    dec = StubDecisionClient({})
    mint = "MintExhaust11111111111111111111111111111111"

    # Position on its 2nd re-entry leg (3rd total trade on this mint) with trailing TP armed
    pos_leg3 = ActivePosition(
        mint=mint,
        symbol="EXH",
        entry_sol=0.5,
        token_amount=1_000_000,
        strategy="LEG2_ENTRY",
        entry_liquidity_usd=100_000.0,
        current_sol_value=0.60,
        peak_sol_value=0.60,
        trail_armed=True,
        reentry_count=2,
    ).model_dump()

    # Pulls back -6.5% from 0.60 peak -> 0.561 SOL -> hits Trailing TP, must exit to SCANNING + cooldown (NOT WATCHING_REENTRY)
    async with _make_monitor_client(sol_quote=0.561) as client:
        mon = await position_monitor_node(
            {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.2, "daily_pnl_sol": 0.1, "active_position": pos_leg3},
            config=cfg,
            db=db,
            decision_client=dec,  # type: ignore[arg-type]
            http_client=client,
        )
        assert mon["pending_order"] is not None
        assert mon["pending_order"]["target_state_after_fill"] == FocusState.SCANNING.value
        assert mon["pending_order"]["apply_cooldown"] is True
        assert "MAX_REENTRIES=2/2" in mon["pending_order"]["reason"]

        ex = await executor_node(
            {
                "focus_state": FocusState.IN_POSITION.value,
                "sol_balance": 0.2,
                "daily_pnl_sol": 0.1,
                "active_position": mon["active_position"],
                "pending_order": mon["pending_order"],
            },
            config=cfg,
            db=db,
            http_client=client,
        )

    assert ex["focus_state"] == FocusState.SCANNING.value
    assert ex["active_position"] is None
    assert db.is_on_cooldown(mint)
    db.close()


@pytest.mark.asyncio
async def test_two_step_partial_tp_scale_out_and_runner_trail_and_breakeven(tmp_path):
    """Verify Step 1 (50% scale-out at +15%) and Step 2 (runner -10% trailing stop & +2% breakeven floor)."""
    db = AgentDatabase(db_path=str(tmp_path / "partial_tp.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(
        _env_file=None,
        PARTIAL_TP_TRIGGER_PCT=15.0,
        PARTIAL_TP_SELL_FRACTION=0.50,
        RUNNER_TRAIL_PULLBACK_PCT=10.0,
        PAPER_TX_FEE_SOL=0.0,
    )
    dec = StubDecisionClient({})
    mint = "MintPartialTP1111111111111111111111111111111"
    start_pos = ActivePosition(
        mint=mint,
        symbol="PART",
        entry_sol=0.50,
        token_amount=1_000_000,
        strategy="BREAKOUT_IGNITION",
        entry_liquidity_usd=100_000.0,
        current_sol_value=0.50,
        peak_sol_value=0.50,
        reference_token_amount=1_000_000,
    ).model_dump()

    # Proportional Jupiter mock: quotes SOL value proportional to input `amount`
    unit_sol_price = {"val": 0.575 / 1_000_000}  # +15% on 0.50 SOL (1,000,000 tokens -> 0.575 SOL)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/swap/v2/order" in url:
            amt = int(request.url.params.get("amount") or 1_000_000)
            out_sol = amt * unit_sol_price["val"]
            return httpx.Response(200, json={"outAmount": str(int(round(out_sol * 1_000_000_000))), "transaction": None})
        return httpx.Response(
            200,
            json=[
                {
                    "chainId": "solana",
                    "baseToken": {"address": mint, "symbol": "PART"},
                    "priceNative": f"{unit_sol_price['val'] * (10 ** 6):.10f}",
                    "liquidity": {"usd": 100_000.0},
                    "txns": {"m5": {"buys": 55, "sells": 25}},
                }
            ],
        )

    state = {
        "focus_state": FocusState.IN_POSITION.value,
        "sol_balance": 0.20,
        "daily_pnl_sol": 0.0,
        "active_position": start_pos,
        "chart_positions": [start_pos],
    }

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # Tick 1: Price hits +15% (0.575 SOL total) -> Step 1 Partial TP sells 50% (500,000 tokens for 0.2875 SOL)
        mon1 = await position_monitor_node(state, config=cfg, db=db, decision_client=dec, http_client=client)  # type: ignore[arg-type]
        assert mon1["focus_state"] == FocusState.IN_POSITION.value
        assert mon1["pending_order"] is None
        assert mon1["monitor_event"] == "PARTIAL_TP_50PCT (+15%)"
        pos1 = mon1["active_position"]
        assert pos1["partial_tp_taken"] is True
        assert pos1["token_amount"] == 500_000
        assert pytest.approx(pos1["initial_entry_sol"]) == 0.50
        assert pytest.approx(pos1["remaining_cost_basis_sol"]) == 0.25
        assert pytest.approx(pos1["realized_partial_pnl_sol"]) == 0.0375
        assert pos1["breakeven_armed"] is True
        assert pos1["trail_armed"] is True
        assert pytest.approx(mon1["sol_balance"]) == 0.4875  # 0.20 + 0.2875 proceeds
        assert pytest.approx(mon1["daily_pnl_sol"]) == 0.0375  # 0.2875 - 0.25 cost

        # Verify PARTIAL_SELL row logged in DuckDB trades & action_log + telemetry API exposes partial fields
        partial_rows = db.get_action_log(actions=["PARTIAL_SELL"], limit=5)
        assert len(partial_rows) == 1
        assert partial_rows[0]["reason"] == "PARTIAL_TP_50PCT (+15%)"
        assert pytest.approx(partial_rows[0]["pnl_amount"]) == 0.0375

        app = create_telemetry_app(config=cfg, db=db)
        t_resp = TestClient(app).get("/api/telemetry").json()
        assert t_resp["chart_positions"][0]["partial_tp_taken"] is True
        assert pytest.approx(t_resp["chart_positions"][0]["realized_partial_pnl_sol"]) == 0.0375

        # Tick 2: Runner climbs to +20% (500,000 tokens worth 0.30 SOL on 0.25 SOL remaining basis) -> does NOT re-fire partial TP
        state.update(mon1)
        unit_sol_price["val"] = 0.30 / 500_000
        mon2 = await position_monitor_node(state, config=cfg, db=db, decision_client=dec, http_client=client)  # type: ignore[arg-type]
        assert mon2["pending_order"] is None
        assert mon2["monitor_event"] == "HOLD_IN_POSITION"
        assert pytest.approx(mon2["active_position"]["peak_sol_value"]) == 0.30
        assert len(db.get_action_log(actions=["PARTIAL_SELL"], limit=10)) == 1

        # Tick 3: Runner pulls back -8% from 0.30 peak (to 0.276 SOL) -> survives without exiting (-8% < -10% runner trail)
        state.update(mon2)
        unit_sol_price["val"] = 0.276 / 500_000
        mon3 = await position_monitor_node(state, config=cfg, db=db, decision_client=dec, http_client=client)  # type: ignore[arg-type]
        assert mon3["pending_order"] is None
        assert mon3["monitor_event"] == "HOLD_IN_POSITION"

        # Tick 4: Runner pulls back -10% from 0.30 peak (to 0.270 SOL) -> triggers TRAILING_TP exit
        state.update(mon3)
        unit_sol_price["val"] = 0.270 / 500_000
        mon4 = await position_monitor_node(state, config=cfg, db=db, decision_client=dec, http_client=client)  # type: ignore[arg-type]
        assert mon4["pending_order"] is not None
        assert "TRAILING_TP" in mon4["pending_order"]["reason"]
        assert "-10%" in mon4["pending_order"]["reason"]
        state.update(mon4)
        ex_runner = await executor_node(state, config=cfg, db=db, http_client=client)
        assert ex_runner["chart_positions"] == []
        assert pytest.approx(ex_runner["sol_balance"]) == 0.7575  # 0.4875 + 0.270
        assert pytest.approx(ex_runner["daily_pnl_sol"]) == 0.0575  # 0.0375 partial + 0.020 runner

        # Also verify +2% Breakeven Floor protection on a runner that drops to +1.6% (0.254 SOL on 0.25 basis) before peak climbs
        be_runner = dict(
            mon1["active_position"],
            peak_sol_value=0.28,  # -9.3% pullback to 0.254 (< -10% trail), so Breakeven Shield (+2% = 0.255 SOL) catches it
            current_sol_value=0.28,
        )
        unit_sol_price["val"] = 0.254 / 500_000
        mon_be = await position_monitor_node(
            {
                "focus_state": FocusState.IN_POSITION.value,
                "sol_balance": 0.4875,
                "daily_pnl_sol": 0.0375,
                "active_position": be_runner,
                "chart_positions": [be_runner],
            },
            config=cfg,
            db=db,
            decision_client=dec,  # type: ignore[arg-type]
            http_client=client,
        )
        assert mon_be["pending_order"] is not None
        assert "BREAKEVEN_SHIELD" in mon_be["pending_order"]["reason"]

    db.close()
