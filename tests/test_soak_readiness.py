"""Stage 4 Soak-Readiness Verification Suite: State, Exit, Signal & Telemetry Invariants."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from alpha_node import alpha_node
from batched_price_monitor import clear_price_backoff
from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import AgentDatabase
from executor_node import executor_node
from helius_webhook import clear_helius_roster_cache, process_helius_webhook_payload
from narrative_cache import clear_narrative_cache
from position_monitor_node import position_monitor_node
from state import ActivePosition, FocusState, PendingOrder
from state_ledger import clear_buy_reservations, clear_sell_guard, update_lane_positions_only
from tests.test_scalper_entry_engine import make_sweep_reclaim_candles
from whale_lane import run_whale_lane_tick


def _jup_order_handler(out_tokens: int = 1_000_000, out_lamports: int = int(0.55 * LAMPORTS_PER_SOL)):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/tokens/v2/search" in url:
            return httpx.Response(200, json=[{"id": request.url.params.get("query", ""), "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "topHoldersPercentage": 15.46}}])
        if "rugcheck.xyz" in url:
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        if "/ultra/v1/shield" in url:
            return httpx.Response(200, json={"warnings": {request.url.params.get("mints", ""): []}})
        if "/swap/v2/order" in url:
            if f"inputMint={SOL_MINT}" in url:
                return httpx.Response(200, json={"outAmount": str(out_tokens), "priceImpactPct": "-0.35", "transaction": None})
            return httpx.Response(200, json={"outAmount": str(out_lamports), "priceImpactPct": "-0.40", "transaction": None})
        return httpx.Response(404)

    return handler


@pytest.mark.asyncio
async def test_concurrent_balance_pnl_and_slot_reservation_gather(tmp_path):
    """1. Interleave concurrent Chart BUY, Whale BUY, and HOLD ticks; verify zero lost updates & reservation caps."""
    clear_buy_reservations()
    clear_sell_guard()
    db = AgentDatabase(db_path=str(tmp_path / "soak_concurrent.duckdb"), starting_sol=1.20)
    cfg = AgentConfig(
        _env_file=None, PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0, EXECUTION_MODE="paper", TRADE_SIZE_SOL=0.50, WHALE_POSITION_SOL=0.25,
        PAPER_TX_FEE_SOL=0.001, MAX_CHART_POSITIONS=2, MAX_WHALE_POSITIONS=2, MAX_OPEN_POSITIONS=3,
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_jup_order_handler())) as client:
        cb1 = PendingOrder(
            side="BUY", mint="ChartMint1", symbol="CM1", amount_atomic=int(0.50 * LAMPORTS_PER_SOL),
            reason="DECISION_1_ENTRY", target_state_after_fill=FocusState.IN_POSITION,
            entry_liquidity_usd=65_000.0, entry_mc_usd=420_000.0, strategy="SWEEP_RECLAIM",
        ).model_dump()
        cb2 = PendingOrder(
            side="BUY", mint="ChartMint2", symbol="CM2", amount_atomic=int(0.50 * LAMPORTS_PER_SOL),
            reason="DECISION_1_ENTRY", target_state_after_fill=FocusState.IN_POSITION,
            entry_liquidity_usd=70_000.0, entry_mc_usd=510_000.0, strategy="SWEEP_RECLAIM",
        ).model_dump()
        wb1 = PendingOrder(
            side="BUY", mint="WhaleMint1", symbol="WM1", amount_atomic=int(0.25 * LAMPORTS_PER_SOL),
            reason="WHALE_SCALP_SOLO", target_state_after_fill=FocusState.IN_POSITION,
            entry_liquidity_usd=80_000.0, entry_mc_usd=600_000.0, strategy="WHALE_SCALP", triggering_wallets=["WhaleWalletA"],
        ).model_dump()
        wb2 = PendingOrder(
            side="BUY", mint="WhaleMint2", symbol="WM2", amount_atomic=int(0.25 * LAMPORTS_PER_SOL),
            reason="WHALE_SCALP_SOLO", target_state_after_fill=FocusState.IN_POSITION,
            entry_liquidity_usd=85_000.0, entry_mc_usd=610_000.0, strategy="WHALE_SCALP", triggering_wallets=["WhaleWalletB"],
        ).model_dump()

        stale = {"focus_state": FocusState.SCANNING.value, "sol_balance": 1.20, "daily_pnl_sol": 0.0}

        async def _hold_tick_writer() -> None:
            await asyncio.sleep(0.001)
            update_lane_positions_only(db, lane="chart", positions=db.get_state()["chart_positions"])
            update_lane_positions_only(db, lane="whale", positions=db.get_state()["whale_positions"])

        results = await asyncio.gather(
            executor_node({**stale, "pending_order": cb1, "slot": "chart_slot"}, config=cfg, db=db, http_client=client),
            executor_node({**stale, "pending_order": wb1, "slot": "whale_slot"}, config=cfg, db=db, http_client=client),
            _hold_tick_writer(),
            executor_node({**stale, "pending_order": cb2, "slot": "chart_slot"}, config=cfg, db=db, http_client=client),
            executor_node({**stale, "pending_order": wb2, "slot": "whale_slot"}, config=cfg, db=db, http_client=client),
            _hold_tick_writer(),
        )

    st = db.get_state()
    open_all = st["chart_positions"] + st["whale_positions"]
    # 4 BUY orders were fired concurrently; only 3 could fill due to 1.20 SOL / MAX_OPEN_POSITIONS=3
    assert len(open_all) == 3
    filled_buys = [r for r in results if isinstance(r, dict) and r.get("last_execution")]
    blocked_buys = [r for r in results if isinstance(r, dict) and not r.get("last_execution")]
    assert len(filled_buys) == 3 and len(blocked_buys) == 1
    spent_sol = sum(float(p["entry_sol"]) + 0.001 for p in open_all)
    assert st["sol_balance"] == pytest.approx(round(1.20 - spent_sol, 6), abs=1e-6)
    assert st["daily_pnl_sol"] == pytest.approx(round(-0.001 * len(open_all), 6), abs=1e-6)
    assert st["sol_balance"] >= 0.0
    db.close()


@pytest.mark.asyncio
async def test_whale_multi_slot_preservation_mid_tick_shadow_exit_and_webhook_1_of_3(tmp_path):
    """2. Open 3 Whale slots; exit Slot 0 while mid-tick shadow-exit flags Slot 1; verify Slots 1 & 2 + 1/3 webhook behavior."""
    clear_price_backoff()
    clear_buy_reservations()
    clear_sell_guard()
    clear_helius_roster_cache()

    db = AgentDatabase(db_path=str(tmp_path / "soak_whale_3slot.duckdb"), starting_sol=1.50)
    cfg = AgentConfig(_env_file=None, PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0, EXECUTION_MODE="paper", WHALE_POSITION_SOL=0.25, PAPER_TX_FEE_SOL=0.001)
    now_dt = datetime(2026, 11, 24, 15, 0, 0, tzinfo=timezone.utc)

    def _mk_w(idx: int) -> dict[str, Any]:
        return ActivePosition(
            mint=f"WhaleSlot{idx}", symbol=f"WS{idx}", entry_sol=0.25, token_amount=1_000_000,
            entry_liquidity_usd=90_000.0, current_sol_value=0.25, peak_sol_value=0.25,
            opened_at=(now_dt - timedelta(minutes=3 - idx)).isoformat(), strategy="WHALE_SCALP",
            triggering_wallets=[f"WalletW{idx}"],
        ).model_dump()

    w0, w1, w2 = _mk_w(0), _mk_w(1), _mk_w(2)
    db.save_state(focus_state=FocusState.SCANNING.value, sol_balance=0.75, daily_pnl_sol=-0.003, active_position=None)
    update_lane_positions_only(db, lane="whale", positions=[w0, w1, w2])
    mid_tick_fired = {"done": False}

    def mid_tick_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/tokens/v1/solana/" in url:
            # Slots 0 & 1 hold at +2% (evaluated first, appended to surviving with shadow_flag=False); Slot 2 drops -16%
            return httpx.Response(200, json=[
                {"chainId": "solana", "baseToken": {"address": "WhaleSlot0", "symbol": "WS0"}, "priceNative": "0.255", "priceUsd": "0.051", "liquidity": {"usd": 90_000.0}, "txns": {"m5": {"buys": 14, "sells": 10}}},
                {"chainId": "solana", "baseToken": {"address": "WhaleSlot1", "symbol": "WS1"}, "priceNative": "0.255", "priceUsd": "0.051", "liquidity": {"usd": 90_000.0}, "txns": {"m5": {"buys": 14, "sells": 10}}},
                {"chainId": "solana", "baseToken": {"address": "WhaleSlot2", "symbol": "WS2"}, "priceNative": "0.21", "priceUsd": "0.042", "liquidity": {"usd": 90_000.0}, "txns": {"m5": {"buys": 10, "sells": 12}}},
            ])
        if "/swap/v2/order" in url:
            # Mid-tick webhook arrives while Slot 2 is quoting/selling (AFTER Slot 1 was already evaluated as HOLD!)
            if not mid_tick_fired["done"]:
                mid_tick_fired["done"] = True
                assert db.flag_whale_shadow_exit("WhaleSlot1", "WalletW1") is True
            amt = 0.21 if "inputMint=WhaleSlot2" in url else 0.255
            return httpx.Response(200, json={"outAmount": str(int(amt * LAMPORTS_PER_SOL)), "priceImpactPct": "-0.4", "transaction": None})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(mid_tick_handler)) as client:
        cycle_out = await run_whale_lane_tick(config=cfg, db=db, http_client=client, now=now_dt)

    assert cycle_out["event"] == "NO_QUALIFYING_CONFLUENCE"
    st_after = db.get_state()
    rem = st_after["whale_positions"]
    assert [p["mint"] for p in rem] == ["WhaleSlot0", "WhaleSlot1"]
    assert next(p for p in rem if p["mint"] == "WhaleSlot1")["whale_shadow_exit_flag"] is True
    assert st_after["whale_shadow_exit_requested"] is True

    # Leave 1 of 3 Whale slots occupied and verify Helius BUY webhook does not log SKIPPED / POSITION_OPEN
    update_lane_positions_only(db, lane="whale", positions=[w2])
    buy_tx = [{
        "type": "SWAP", "signature": "sig_1of3_whale_open_check", "feePayer": "WalletTracked1",
        "timestamp": int(now_dt.timestamp()),
        "nativeTransfers": [{"fromUserAccount": "WalletTracked1", "toUserAccount": "PoolX", "amount": int(1.2 * LAMPORTS_PER_SOL)}],
        "tokenTransfers": [{"fromUserAccount": "PoolX", "toUserAccount": "WalletTracked1", "mint": "NewWhaleMint99", "tokenAmount": 500_000.0, "symbol": "NWM"}],
    }]
    res = process_helius_webhook_payload(buy_tx, config=cfg, db=db, active_roster={"WalletTracked1"}, strict_roster=True, now=now_dt)
    assert res["recorded"] == 1
    assert db.get_whale_decisions().get("NewWhaleMint99", {}).get("reason") != "POSITION_OPEN"
    db.close()


@pytest.mark.asyncio
async def test_chart_slots_2_and_3_full_exit_engine_and_sequential_priority(tmp_path):
    """3. Open 3 Chart slots; trigger Partial TP on Slot 2 (+18%) and Hard Stop on Slot 3 (-13%) on the same tick."""
    clear_price_backoff()
    clear_buy_reservations()
    clear_sell_guard()

    db = AgentDatabase(db_path=str(tmp_path / "soak_chart_3slot.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(_env_file=None, PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0, EXECUTION_MODE="paper", TRADE_SIZE_SOL=0.50, PAPER_TX_FEE_SOL=0.001)
    now_dt = datetime(2026, 11, 24, 16, 0, 0, tzinfo=timezone.utc)

    def _mk_c(idx: int) -> dict[str, Any]:
        return ActivePosition(
            mint=f"ChartSlot{idx}", symbol=f"CS{idx}", entry_sol=0.50, token_amount=1_000_000,
            entry_liquidity_usd=80_000.0, current_sol_value=0.50, peak_sol_value=0.51,
            opened_at=(now_dt - timedelta(minutes=5 - idx)).isoformat(), strategy="SWEEP_RECLAIM",
        ).model_dump()

    p1, p2, p3 = _mk_c(1), _mk_c(2), _mk_c(3)
    db.save_state(focus_state=FocusState.IN_POSITION.value, sol_balance=0.50, daily_pnl_sol=-0.003, active_position=p1)
    update_lane_positions_only(db, lane="chart", positions=[p1, p2, p3])

    def multi_chart_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/tokens/v1/solana/" in url:
            return httpx.Response(200, json=[
                {"chainId": "solana", "baseToken": {"address": "ChartSlot1", "symbol": "CS1"}, "priceNative": "0.515", "priceUsd": "0.103", "liquidity": {"usd": 80_000.0}, "txns": {"m5": {"buys": 15, "sells": 10}}},
                {"chainId": "solana", "baseToken": {"address": "ChartSlot2", "symbol": "CS2"}, "priceNative": "0.590", "priceUsd": "0.118", "liquidity": {"usd": 85_000.0}, "txns": {"m5": {"buys": 22, "sells": 8}}},
                {"chainId": "solana", "baseToken": {"address": "ChartSlot3", "symbol": "CS3"}, "priceNative": "0.435", "priceUsd": "0.087", "liquidity": {"usd": 75_000.0}, "txns": {"m5": {"buys": 6, "sells": 20}}},
            ])
        if "/swap/v2/order" in url:
            if "inputMint=ChartSlot3" in url:
                return httpx.Response(200, json={"outAmount": str(int(0.435 * LAMPORTS_PER_SOL)), "priceImpactPct": "-0.6", "transaction": None})
            if "inputMint=ChartSlot2" in url:
                amt = 0.295 if "amount=500000" in url else 0.590
                return httpx.Response(200, json={"outAmount": str(int(amt * LAMPORTS_PER_SOL)), "priceImpactPct": "-0.3", "transaction": None})
            return httpx.Response(200, json={"outAmount": str(int(0.515 * LAMPORTS_PER_SOL)), "priceImpactPct": "-0.2", "transaction": None})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(multi_chart_handler)) as client:
        st0 = db.get_state()
        mon_out = await position_monitor_node(
            {"focus_state": st0["focus_state"], "sol_balance": st0["sol_balance"], "daily_pnl_sol": st0["daily_pnl_sol"], "active_position": st0["active_position"], "chart_positions": st0["chart_positions"]},
            config=cfg, db=db, decision_client=None, http_client=client,  # type: ignore[arg-type]
        )

    assert mon_out.get("_hard_stop_already_executed") is True
    assert mon_out.get("pending_order") is None
    rem = {p["mint"]: p for p in db.get_state()["chart_positions"]}
    assert set(rem.keys()) == {"ChartSlot1", "ChartSlot2"}
    assert rem["ChartSlot1"]["token_amount"] == 1_000_000 and rem["ChartSlot1"]["partial_tp_taken"] is False
    assert rem["ChartSlot2"]["token_amount"] == 500_000 and rem["ChartSlot2"]["partial_tp_taken"] is True
    rows = db._conn.execute("SELECT action, symbol FROM action_log WHERE action IN ('SELL', 'PARTIAL_SELL') ORDER BY id ASC").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("SELL", "CS3"), ("PARTIAL_SELL", "CS2")]
    db.close()


@pytest.mark.asyncio
async def test_sell_and_partial_sell_idempotency_under_concurrency(tmp_path):
    """4. Fire concurrent PARTIAL_SELL and concurrent SELL calls for the same position; verify each settles once."""
    clear_buy_reservations()
    clear_sell_guard()

    db = AgentDatabase(db_path=str(tmp_path / "soak_idempotency.duckdb"), starting_sol=1.0)
    cfg = AgentConfig(_env_file=None, PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0, EXECUTION_MODE="paper", TRADE_SIZE_SOL=0.50, PAPER_TX_FEE_SOL=0.001)
    opened_iso = "2026-11-24T16:30:00+00:00"
    pos = ActivePosition(
        mint="MintIdem", symbol="IDEM", entry_sol=0.50, token_amount=1_000_000,
        entry_liquidity_usd=70_000.0, current_sol_value=0.60, peak_sol_value=0.60,
        opened_at=opened_iso, strategy="SWEEP_RECLAIM",
    ).model_dump()
    db.save_state(focus_state=FocusState.IN_POSITION.value, sol_balance=0.499, daily_pnl_sol=-0.001, active_position=pos)
    update_lane_positions_only(db, lane="chart", positions=[pos])

    p_order = PendingOrder(
        side="PARTIAL_SELL", mint="MintIdem", symbol="IDEM", amount_atomic=500_000, sell_fraction=0.5,
        reason="PARTIAL_TP_50 (+20%)", target_state_after_fill=FocusState.IN_POSITION,
        opened_at=opened_iso, monitor_sol_value=0.60, price_source="dexscreener_native",
    ).model_dump()

    async with httpx.AsyncClient(transport=httpx.MockTransport(_jup_order_handler(out_lamports=int(0.30 * LAMPORTS_PER_SOL)))) as client:
        b_state = {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.499, "daily_pnl_sol": -0.001, "active_position": pos, "chart_positions": [pos], "pending_order": p_order, "slot": "chart_slot"}
        p_res = await asyncio.gather(
            executor_node(dict(b_state), config=cfg, db=db, http_client=client),
            executor_node(dict(b_state), config=cfg, db=db, http_client=client),
            executor_node(dict(b_state), config=cfg, db=db, http_client=client),
        )

    p_filled = [r for r in p_res if r.get("last_execution") is not None]
    p_skipped = [r for r in p_res if r.get("last_execution") is None]
    assert len(p_filled) == 1 and len(p_skipped) == 2
    st_mid = db.get_state()
    assert len(st_mid["chart_positions"]) == 1 and st_mid["chart_positions"][0]["token_amount"] == 500_000
    assert st_mid["sol_balance"] == pytest.approx(0.499 + 0.30 - 0.001, abs=1e-6)

    runner = st_mid["chart_positions"][0]
    s_order = PendingOrder(
        side="SELL", mint="MintIdem", symbol="IDEM", amount_atomic=500_000, reason="HARD_STOP_LOSS",
        target_state_after_fill=FocusState.SCANNING, opened_at=opened_iso, monitor_sol_value=0.22, price_source="jupiter_exact_quote",
    ).model_dump()

    async with httpx.AsyncClient(transport=httpx.MockTransport(_jup_order_handler(out_lamports=int(0.22 * LAMPORTS_PER_SOL)))) as client:
        s_state = {"focus_state": FocusState.IN_POSITION.value, "sol_balance": st_mid["sol_balance"], "daily_pnl_sol": st_mid["daily_pnl_sol"], "active_position": runner, "chart_positions": [runner], "pending_order": s_order, "slot": "chart_slot"}
        s_res = await asyncio.gather(
            executor_node(dict(s_state), config=cfg, db=db, http_client=client),
            executor_node(dict(s_state), config=cfg, db=db, http_client=client),
            executor_node(dict(s_state), config=cfg, db=db, http_client=client),
        )

    s_filled = [r for r in s_res if r.get("last_execution") is not None]
    s_skipped = [r for r in s_res if r.get("last_execution") is None]
    assert len(s_filled) == 1 and len(s_skipped) == 2
    st_end = db.get_state()
    assert st_end["chart_positions"] == []
    assert st_end["sol_balance"] == pytest.approx(st_mid["sol_balance"] + 0.22 - 0.001, abs=1e-6)
    db.close()


@pytest.mark.asyncio
async def test_watching_reentry_abandon_preserves_open_chart_positions(tmp_path):
    """5. Abandon WATCHING_REENTRY while another Chart position is open (token_amount > 0) and verify preservation."""
    clear_price_backoff()
    db = AgentDatabase(db_path=str(tmp_path / "soak_watch_preserve.duckdb"), starting_sol=1.0)
    cfg = AgentConfig(_env_file=None, PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0, EXECUTION_MODE="paper", TRADE_SIZE_SOL=0.50)
    now_dt = datetime.now(timezone.utc)

    open_chart = ActivePosition(
        mint="MintStillOpen", symbol="OPEN1", entry_sol=0.50, token_amount=1_000_000,
        entry_liquidity_usd=75_000.0, current_sol_value=0.52, peak_sol_value=0.53,
        opened_at=(now_dt - timedelta(minutes=3)).isoformat(), strategy="SWEEP_RECLAIM",
    ).model_dump()
    expired_watcher = ActivePosition(
        mint="MintWatchExpired", symbol="WEXP", entry_sol=0.50, token_amount=0,
        entry_liquidity_usd=60_000.0, current_sol_value=0.50, peak_sol_value=0.60,
        run_peak_sol=0.60, retrace_low_sol=0.49, reference_token_amount=1_000_000,
        watch_started_at=(now_dt - timedelta(minutes=45)).isoformat(),
    ).model_dump()
    db.save_state(focus_state=FocusState.WATCHING_REENTRY.value, sol_balance=0.50, daily_pnl_sol=0.05, active_position=expired_watcher)
    update_lane_positions_only(db, lane="chart", positions=[open_chart], focus_state=FocusState.WATCHING_REENTRY.value, active_position=expired_watcher)

    def handler(request: httpx.Request) -> httpx.Response:
        if "/tokens/v1/solana/" in str(request.url):
            return httpx.Response(200, json=[
                {"chainId": "solana", "baseToken": {"address": "MintStillOpen", "symbol": "OPEN1"}, "priceNative": "0.52", "priceUsd": "0.104", "liquidity": {"usd": 75_000.0}, "txns": {"m5": {"buys": 15, "sells": 10}}},
                {"chainId": "solana", "baseToken": {"address": "MintWatchExpired", "symbol": "WEXP"}, "priceNative": "0.50", "priceUsd": "0.100", "liquidity": {"usd": 60_000.0}, "txns": {"m5": {"buys": 10, "sells": 10}}},
            ])
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await position_monitor_node(
            {"focus_state": FocusState.WATCHING_REENTRY.value, "sol_balance": 0.50, "daily_pnl_sol": 0.05, "active_position": expired_watcher, "chart_positions": [open_chart]},
            config=cfg, db=db, decision_client=None, http_client=client,  # type: ignore[arg-type]
        )

    assert out["focus_state"] == FocusState.IN_POSITION.value
    st_after = db.get_state()
    assert len(st_after["chart_positions"]) == 1 and st_after["chart_positions"][0]["mint"] == "MintStillOpen"
    assert st_after["focus_state"] == FocusState.IN_POSITION.value
    assert st_after["active_position"]["mint"] == "MintStillOpen"
    db.close()


@pytest.mark.asyncio
async def test_signal_and_telemetry_completeness_mc_usd_exact_quote_and_live_momentum_cache(tmp_path):
    """6. Verify Chart BUY & LEG_2_REENTRY mc_usd, jupiter_exact_quote price_source, and cached WATCH -> live BUY transition."""
    clear_price_backoff()
    clear_narrative_cache()
    clear_buy_reservations()
    clear_sell_guard()

    db = AgentDatabase(db_path=str(tmp_path / "soak_telemetry.duckdb"), starting_sol=1.50)
    cfg = AgentConfig(_env_file=None, PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0, EXECUTION_MODE="paper", TRADE_SIZE_SOL=0.50, PAPER_TX_FEE_SOL=0.001)
    now_dt = datetime(2026, 11, 24, 17, 0, 0, tzinfo=timezone.utc)
    soaked_added = (now_dt - timedelta(minutes=6)).isoformat()

    class CountingDecisionClient:
        def __init__(self) -> None:
            self.decide_calls = 0

        async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
            self.decide_calls += 1
            if "leg_2_momentum" in questions:
                return {"leg_2_momentum": {"type": "score", "score": 4.3, "confidence": 0.88}, "is_dead_cat_bounce": {"type": "noul", "noul": 0.15, "confidence": 0.90}}
            return {
                "t0_narrative_strength": {"type": "score", "score": 3.6, "confidence": 0.85},
                "t0_momentum_quality": {"type": "score", "score": 2.1, "confidence": 0.75},
                "t0_is_copycat_vamp": {"type": "noul", "noul": 0.20, "confidence": 0.88},
                "t0_action": {"type": "choice", "choice": "WATCH", "confidence": 0.68},
            }

    dec_client = CountingDecisionClient()
    cand_t1 = {
        "mint": "MintLiveTrans", "symbol": "LTRANS", "market_cap_usd": 450_000.0, "liquidity_usd": 70_000.0,
        "volume_1h_usd": 100_000.0, "volume_5m_usd": 4_000.0, "price_change_5m_pct": 0.2,
        "buys_5m": 10, "sells_5m": 10, "added_at": soaked_added, "candles_5m": [],
    }
    db.upsert_watchlist_entry(mint="MintLiveTrans", symbol="LTRANS", candidate=cand_t1, added_at=soaked_added, safety_passed=True)
    out_t1 = await alpha_node({"focus_state": FocusState.SCANNING.value, "sol_balance": 1.50, "safe_candidates": [cand_t1]}, config=cfg, db=db, decision_client=dec_client, now=now_dt)  # type: ignore[arg-type]
    assert dec_client.decide_calls == 1 and out_t1["pending_order"] is None

    cand_t2 = {
        **cand_t1,
        "market_cap_usd": 485_000.0,
        "volume_5m_usd": 18_000.0,
        "price_change_5m_pct": 8.5,
        "buys_5m": 34,
        "sells_5m": 10,
        "candles_5m": make_sweep_reclaim_candles(),
        "_ohlcv_5m_result": __import__("tests.test_e3b_cache_provenance", fromlist=["make_verified_ohlcv_result"]).make_verified_ohlcv_result(make_sweep_reclaim_candles(), last_ok_at=(now_dt + timedelta(seconds=30)).timestamp()),
    }
    out_t2 = await alpha_node({"focus_state": FocusState.SCANNING.value, "sol_balance": 1.50, "safe_candidates": [cand_t2]}, config=cfg, db=db, decision_client=dec_client, now=now_dt + timedelta(seconds=30))  # type: ignore[arg-type]
    assert dec_client.decide_calls == 1 and out_t2["pending_order"] is not None
    assert out_t2["pending_order"]["side"] == "BUY" and out_t2["pending_order"]["entry_mc_usd"] == 485_000.0

    async with httpx.AsyncClient(transport=httpx.MockTransport(_jup_order_handler(out_tokens=1_000_000))) as client:
        buy_exec = await executor_node({"focus_state": FocusState.SCANNING.value, "sol_balance": 1.50, "daily_pnl_sol": 0.0, "pending_order": out_t2["pending_order"], "slot": "chart_slot"}, config=cfg, db=db, http_client=client)
    buy_mc_row = db._conn.execute("SELECT mc_usd FROM action_log WHERE action = 'BUY' AND symbol = 'LTRANS'").fetchone()
    assert buy_mc_row is not None and float(buy_mc_row[0]) == pytest.approx(485_000.0)

    opened_pos = buy_exec["chart_positions"][0]

    def exact_quote_stop_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/tokens/v1/solana/" in url:
            return httpx.Response(200, json=[{"chainId": "solana", "baseToken": {"address": "MintLiveTrans", "symbol": "LTRANS"}, "priceNative": "0.4525", "priceUsd": "0.0905", "liquidity": {"usd": 70_000.0}, "txns": {"m5": {"buys": 8, "sells": 20}}}])
        if "/swap/v2/order" in url:
            return httpx.Response(200, json={"outAmount": str(int(0.445 * LAMPORTS_PER_SOL)), "priceImpactPct": "-0.45", "transaction": None})
        return httpx.Response(404)

    clear_price_backoff()
    async with httpx.AsyncClient(transport=httpx.MockTransport(exact_quote_stop_handler)) as client:
        st_in = db.get_state()
        await position_monitor_node({"focus_state": FocusState.IN_POSITION.value, "sol_balance": st_in["sol_balance"], "daily_pnl_sol": st_in["daily_pnl_sol"], "active_position": opened_pos, "chart_positions": [opened_pos]}, config=cfg, db=db, decision_client=dec_client, http_client=client)  # type: ignore[arg-type]

    sell_row = db._conn.execute("SELECT price_source, mc_usd FROM action_log WHERE action = 'SELL' AND symbol = 'LTRANS'").fetchone()
    assert sell_row is not None and sell_row[0] == "jupiter_exact_quote" and float(sell_row[1]) == pytest.approx(485_000.0)

    watch_pos = ActivePosition(
        mint="MintLiveTrans", symbol="LTRANS", entry_sol=0.50, token_amount=0, entry_liquidity_usd=70_000.0, entry_mc_usd=485_000.0,
        current_sol_value=0.53, peak_sol_value=0.62, run_peak_sol=0.62, retrace_low_sol=0.50, reference_token_amount=1_000_000, watch_started_at=now_dt.isoformat(),
    ).model_dump()

    def leg2_reentry_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/tokens/v1/solana/" in url:
            return httpx.Response(200, json=[{"chainId": "solana", "baseToken": {"address": "MintLiveTrans", "symbol": "LTRANS"}, "priceNative": "0.535", "priceUsd": "0.107", "marketCap": 520_000.0, "liquidity": {"usd": 75_000.0}, "volume": {"m5": 18_000.0, "h1": 100_000.0}, "priceChange": {"h1": 28.0, "m5": 7.0}, "txns": {"m5": {"buys": 32, "sells": 14}}}])
        if "/swap/v2/order" in url:
            if f"inputMint={SOL_MINT}" in url:
                return httpx.Response(200, json={"outAmount": "950000", "priceImpactPct": "-0.3", "transaction": None})
            return httpx.Response(200, json={"outAmount": str(int(0.535 * LAMPORTS_PER_SOL)), "priceImpactPct": "-0.2", "transaction": None})
        return httpx.Response(404)

    clear_price_backoff()
    async with httpx.AsyncClient(transport=httpx.MockTransport(leg2_reentry_handler)) as client:
        re_mon = await position_monitor_node({"focus_state": FocusState.WATCHING_REENTRY.value, "sol_balance": 1.0, "daily_pnl_sol": 0.0, "active_position": watch_pos, "reentry_watchers": [watch_pos]}, config=cfg, db=db, decision_client=dec_client, http_client=client, now=now_dt)  # type: ignore[arg-type]
        assert re_mon["pending_order"] is not None and re_mon["pending_order"]["entry_mc_usd"] == 485_000.0
        await executor_node({"focus_state": FocusState.WATCHING_REENTRY.value, "sol_balance": 1.0, "daily_pnl_sol": 0.0, "active_position": watch_pos, "pending_order": re_mon["pending_order"], "slot": "chart_slot"}, config=cfg, db=db, http_client=client)

    re_buy_row = db._conn.execute("SELECT mc_usd FROM action_log WHERE action = 'REENTRY_BUY' AND reason = 'LEG_2_REENTRY' AND symbol = 'LTRANS'").fetchone()
    assert re_buy_row is not None and float(re_buy_row[0]) == pytest.approx(485_000.0)
    db.close()
