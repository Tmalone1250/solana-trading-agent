"""Directive 21 (E2A) Verification Suite: Exit Priority & Exodus Signal Integrity."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from batched_price_monitor import clear_price_backoff
from chart_exit_engine import (
    PRIO_BREAKEVEN,
    PRIO_EXODUS,
    PRIO_HARD_STOP,
    PRIO_HOLD,
    PRIO_PARTIAL_TP,
    PRIO_TRAILING_TP,
    compute_exodus,
    evaluate_chart_position_exit,
    evaluate_exodus_signal,
)
from config import SOL_MINT, AgentConfig
from db import AgentDatabase
from executor_node import clear_sell_idempotency_guard
from position_monitor_node import position_monitor_node
from state import ActivePosition, FocusState

POOL_PINNED = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"
POOL_COMPETING = "8qbHbw2BbbTHBW1sbeqakYXVKRQM8Ne7pLK7m6CVfeR"
MINT_A = "p2Yicb86aZig616Eav2VWG9vuXR5mEqhtzshZYBxzsV"
MINT_B = "swqrv48gsrwpBFbftEwnP2vB4jckpvfGJfXkwaniLCC"


class _NoopDecision:
    async def decide(self, **_kwargs: Any) -> dict[str, Any]:
        return {}


def _mk_pos(
    *,
    mint: str = MINT_A,
    symbol: str = "EXA",
    pair_address: str = POOL_PINNED,
    opened_at: str | None = None,
    entry_sol: float = 0.50,
    entry_liq: float = 100_000.0,
    token_amount: int = 1_000_000_000,
    exodus_confirm_count: int = 0,
    exodus_last_confirmed_at: str | None = None,
) -> ActivePosition:
    return ActivePosition(
        mint=mint,
        symbol=symbol,
        pair_address=pair_address,
        dex_id="pumpswap",
        entry_sol=entry_sol,
        token_amount=token_amount,
        decimals=6,
        entry_liquidity_usd=entry_liq,
        current_sol_value=entry_sol,
        peak_sol_value=entry_sol,
        reference_token_amount=token_amount,
        strategy="BREAKOUT_IGNITION",
        opened_at=opened_at,
        exodus_confirm_count=exodus_confirm_count,
        exodus_last_confirmed_at=exodus_last_confirmed_at,
    )


def test_e2a_1_exit_priority_hard_stop_over_exodus_and_exodus_over_partial_tp():
    """Tests 1, 2, 3: Hard Stop > Exodus > Partial TP; Partial TP still fires when Exodus is unconfirmed."""
    cfg = AgentConfig(
        _env_file=None,
        HARD_STOP_LOSS_PCT=0.10,
        PARTIAL_TP_TRIGGER_PCT=15.0,
        MIN_EXODUS_TXNS_5M=10,
        EXODUS_CONFIRMATION_TICKS=2,
        EXODUS_FLOW_WARMUP_SECONDS=300.0,
    )
    now_dt = datetime(2026, 11, 26, 14, 0, 0, tzinfo=timezone.utc)
    warmed_open = (now_dt - timedelta(minutes=10)).isoformat()

    # 1. Simultaneous Hard Stop (-12%) AND confirmed Exodus -> HARD_STOP_LOSS wins (PRIO=0)
    pos_hs = _mk_pos(
        opened_at=warmed_open,
        exodus_confirm_count=1,
        exodus_last_confirmed_at=(now_dt - timedelta(seconds=10)).isoformat(),
    )
    order_hs, ev_hs, prio_hs = evaluate_chart_position_exit(
        pos_hs, current_sol_value=0.44, buys_5m=2, sells_5m=18, current_liquidity_usd=80_000.0,
        config=cfg, flow_available=True, liq_available=True, now_dt=now_dt,
    )
    assert order_hs is not None
    assert prio_hs == PRIO_HARD_STOP
    assert "HARD_STOP_LOSS" in ev_hs

    # 2. Simultaneous +18% Partial TP AND confirmed Exodus (both sell flow and liq drop) -> EXODUS wins (PRIO=1)
    pos_ex = _mk_pos(
        opened_at=warmed_open,
        exodus_confirm_count=1,
        exodus_last_confirmed_at=(now_dt - timedelta(seconds=10)).isoformat(),
    )
    order_ex, ev_ex, prio_ex = evaluate_chart_position_exit(
        pos_ex, current_sol_value=0.59, buys_5m=3, sells_5m=17, current_liquidity_usd=100_000.0,
        config=cfg, flow_available=True, liq_available=True, now_dt=now_dt,
    )
    assert order_ex is not None
    assert prio_ex == PRIO_EXODUS
    assert order_ex.side == "SELL"
    assert "EXODUS_SELL_FLOW" in ev_ex
    assert pos_ex.last_exodus_status == "EXODUS_FLOW_CONFIRMED"

    # 3. Partial TP (+18%) still executes when Exodus is only confirming (1/2) and not yet confirmed
    pos_ptp = _mk_pos(opened_at=warmed_open, exodus_confirm_count=0)
    order_ptp, ev_ptp, prio_ptp = evaluate_chart_position_exit(
        pos_ptp, current_sol_value=0.59, buys_5m=3, sells_5m=17, current_liquidity_usd=100_000.0,
        config=cfg, flow_available=True, liq_available=True, now_dt=now_dt,
    )
    assert order_ptp is not None
    assert prio_ptp == PRIO_PARTIAL_TP
    assert order_ptp.side == "PARTIAL_SELL"
    assert "PARTIAL_TP_50PCT" in ev_ptp
    assert pos_ptp.last_exodus_status == "EXODUS_FLOW_CONFIRMING"
    assert pos_ptp.exodus_confirm_count == 1


def test_e2a_2_minimum_transaction_sample_blocks_noise_but_allows_liq_drop():
    """Tests 4, 5, 6: 1 sell/0 buys, 0 txns, and below-min samples do not trigger sell-flow Exodus."""
    cfg = AgentConfig(_env_file=None, MIN_EXODUS_TXNS_5M=10, EXODUS_SELL_RATIO=0.65)
    now_dt = datetime(2026, 11, 26, 14, 0, 0, tzinfo=timezone.utc)
    warmed_open = (now_dt - timedelta(minutes=10)).isoformat()

    # 4. 1 sell and 0 buys -> insufficient sample
    ev1 = evaluate_exodus_signal(
        buys_5m=0, sells_5m=1, entry_liquidity_usd=100_000.0, current_liquidity_usd=100_000.0,
        config=cfg, opened_at=warmed_open, now_dt=now_dt, enforce_warmup=True,
    )
    assert ev1.triggered is False
    assert ev1.status == "EXODUS_FLOW_INSUFFICIENT_SAMPLE"

    # 5. 0 buys and 0 sells -> insufficient sample
    ev0 = evaluate_exodus_signal(
        buys_5m=0, sells_5m=0, entry_liquidity_usd=100_000.0, current_liquidity_usd=100_000.0,
        config=cfg, opened_at=warmed_open, now_dt=now_dt, enforce_warmup=True,
    )
    assert ev0.triggered is False
    assert ev0.status == "EXODUS_FLOW_INSUFFICIENT_SAMPLE"

    # 6. 2 buys / 7 sells (9 txns < 10) -> insufficient sample
    ev9 = evaluate_exodus_signal(
        buys_5m=2, sells_5m=7, entry_liquidity_usd=100_000.0, current_liquidity_usd=100_000.0,
        config=cfg, opened_at=warmed_open, now_dt=now_dt, enforce_warmup=True,
    )
    assert ev9.triggered is False
    assert ev9.status == "EXODUS_FLOW_INSUFFICIENT_SAMPLE"

    # Liquidity drop remains independent of tx sample size (even with 0 txns)
    trig_liq, _, _, detail_liq = compute_exodus(
        buys_5m=0, sells_5m=0, entry_liquidity_usd=100_000.0, current_liquidity_usd=85_000.0,
        config=cfg, opened_at=warmed_open, now_dt=now_dt, enforce_warmup=True,
    )
    assert trig_liq is True
    assert "EXODUS_LIQ_DROP" in detail_liq


def test_e2a_3_confirmation_debounce_reset_duplicate_guard_and_isolation():
    """Tests 7, 8, 9: Confirmation count, reset on clear/unavailable, duplicate-tick guard, per-position isolation."""
    cfg = AgentConfig(
        _env_file=None, MIN_EXODUS_TXNS_5M=10, EXODUS_CONFIRMATION_TICKS=2,
        EXODUS_FLOW_WARMUP_SECONDS=300.0, MONITOR_POLL_SECONDS=5.0,
    )
    t0 = datetime(2026, 11, 26, 14, 10, 0, tzinfo=timezone.utc)
    opened = (t0 - timedelta(minutes=10)).isoformat()
    pos_a = _mk_pos(mint=MINT_A, opened_at=opened)
    pos_b = _mk_pos(mint=MINT_B, opened_at=opened)

    # Tick 1: qualifying sell flow -> CONFIRMING (1/2), HOLD
    o1, _, p1 = evaluate_chart_position_exit(
        pos_a, current_sol_value=0.51, buys_5m=3, sells_5m=17, current_liquidity_usd=100_000.0,
        config=cfg, flow_available=True, liq_available=True, now_dt=t0,
    )
    assert o1 is None and p1 == PRIO_HOLD
    assert pos_a.exodus_confirm_count == 1
    assert pos_a.last_exodus_status == "EXODUS_FLOW_CONFIRMING"
    assert pos_b.exodus_confirm_count == 0  # isolated per position

    # Duplicate rapid tick (+1s < 5s MONITOR_POLL_SECONDS): must NOT double-count!
    o_dup, _, _ = evaluate_chart_position_exit(
        pos_a, current_sol_value=0.51, buys_5m=3, sells_5m=17, current_liquidity_usd=100_000.0,
        config=cfg, flow_available=True, liq_available=True, now_dt=t0 + timedelta(seconds=1),
    )
    assert o_dup is None
    assert pos_a.exodus_confirm_count == 1

    # Signal clears on next tick (+6s) -> confirmation resets to 0
    evaluate_chart_position_exit(
        pos_a, current_sol_value=0.51, buys_5m=12, sells_5m=8, current_liquidity_usd=100_000.0,
        config=cfg, flow_available=True, liq_available=True, now_dt=t0 + timedelta(seconds=6),
    )
    assert pos_a.exodus_confirm_count == 0
    assert pos_a.last_exodus_status == "EXODUS_CLEAR"

    # Re-arm 1/2 (+12s), then flow becomes unavailable (+18s) -> confirmation resets to 0
    evaluate_chart_position_exit(
        pos_a, current_sol_value=0.51, buys_5m=3, sells_5m=17, current_liquidity_usd=100_000.0,
        config=cfg, flow_available=True, liq_available=True, now_dt=t0 + timedelta(seconds=12),
    )
    assert pos_a.exodus_confirm_count == 1
    evaluate_chart_position_exit(
        pos_a, current_sol_value=0.51, buys_5m=None, sells_5m=None, current_liquidity_usd=None,
        config=cfg, flow_available=False, liq_available=False, now_dt=t0 + timedelta(seconds=18),
    )
    assert pos_a.exodus_confirm_count == 0
    assert pos_a.last_exodus_status == "EXODUS_FLOW_UNAVAILABLE"

    # Two spaced qualifying ticks (+24s, +30s) -> triggers EXODUS_FLOW_CONFIRMED (2/2)
    o_c1, _, _ = evaluate_chart_position_exit(
        pos_a, current_sol_value=0.51, buys_5m=3, sells_5m=17, current_liquidity_usd=100_000.0,
        config=cfg, flow_available=True, liq_available=True, now_dt=t0 + timedelta(seconds=24),
    )
    assert o_c1 is None and pos_a.exodus_confirm_count == 1
    o_c2, ev_c2, p_c2 = evaluate_chart_position_exit(
        pos_a, current_sol_value=0.51, buys_5m=3, sells_5m=17, current_liquidity_usd=100_000.0,
        config=cfg, flow_available=True, liq_available=True, now_dt=t0 + timedelta(seconds=30),
    )
    assert o_c2 is not None and p_c2 == PRIO_EXODUS
    assert "EXODUS_SELL_FLOW" in ev_c2
    assert pos_a.last_exodus_status == "EXODUS_FLOW_CONFIRMED"


def test_e2a_4_and_5_warmup_missing_opened_at_zero_liq_vs_missing_liq():
    """Tests 11, 12, 13, 14, 15: Warm-up, missing opened_at, liq drop during warm-up, valid $0 liq vs missing liq."""
    cfg = AgentConfig(_env_file=None, MIN_EXODUS_TXNS_5M=10, EXODUS_CONFIRMATION_TICKS=1, EXODUS_FLOW_WARMUP_SECONDS=300.0)
    now_dt = datetime(2026, 11, 26, 14, 0, 0, tzinfo=timezone.utc)

    # 11. Within 5m warm-up (60s elapsed) -> pre-entry sell flow suppressed with EXODUS_FLOW_WARMUP
    pos_warm = _mk_pos(opened_at=(now_dt - timedelta(seconds=60)).isoformat())
    o_w, _, _ = evaluate_chart_position_exit(
        pos_warm, current_sol_value=0.51, buys_5m=2, sells_5m=25, current_liquidity_usd=100_000.0,
        config=cfg, flow_available=True, liq_available=True, now_dt=now_dt,
    )
    assert o_w is None
    assert pos_warm.last_exodus_status == "EXODUS_FLOW_WARMUP"

    # 12. After warm-up elapsed (305s) -> flow becomes eligible and triggers
    o_after, ev_after, p_after = evaluate_chart_position_exit(
        pos_warm, current_sol_value=0.51, buys_5m=2, sells_5m=25, current_liquidity_usd=100_000.0,
        config=cfg, flow_available=True, liq_available=True, now_dt=now_dt + timedelta(seconds=245),
    )
    assert o_after is not None and p_after == PRIO_EXODUS and "EXODUS_SELL_FLOW" in ev_after

    # 13. Missing/invalid opened_at conservatively treated as NOT warmed up
    pos_no_open = _mk_pos(opened_at=None)
    o_no, _, _ = evaluate_chart_position_exit(
        pos_no_open, current_sol_value=0.51, buys_5m=2, sells_5m=25, current_liquidity_usd=100_000.0,
        config=cfg, flow_available=True, liq_available=True, now_dt=now_dt,
    )
    assert o_no is None
    assert pos_no_open.last_exodus_status == "EXODUS_FLOW_WARMUP"

    # 14. Valid liquidity drop remains independently protective DURING warm-up
    o_ld, ev_ld, p_ld = evaluate_chart_position_exit(
        pos_warm, current_sol_value=0.51, buys_5m=20, sells_5m=2, current_liquidity_usd=85_000.0,
        config=cfg, flow_available=True, liq_available=True, now_dt=now_dt,
    )
    assert o_ld is not None and p_ld == PRIO_EXODUS and "EXODUS_LIQ_DROP" in ev_ld
    assert pos_warm.last_exodus_status == "EXODUS_LIQ_DROP"

    # 15. Valid confirmed $0.0 liquidity triggers EXODUS_LIQ_DROP, whereas missing liquidity (liq_available=False) does NOT
    ev_zero_liq = evaluate_exodus_signal(
        buys_5m=10, sells_5m=2, entry_liquidity_usd=100_000.0, current_liquidity_usd=0.0,
        config=cfg, flow_available=True, liq_available=True,
    )
    assert ev_zero_liq.triggered is True
    assert ev_zero_liq.status == "EXODUS_LIQ_DROP"
    assert ev_zero_liq.liq_drop_pct == pytest.approx(1.0)

    ev_missing_liq = evaluate_exodus_signal(
        buys_5m=10, sells_5m=2, entry_liquidity_usd=100_000.0, current_liquidity_usd=None,
        config=cfg, flow_available=True, liq_available=False,
    )
    assert ev_missing_liq.triggered is False
    assert ev_missing_liq.status == "EXODUS_LIQ_UNAVAILABLE"
    assert ev_missing_liq.liq_status == "EXODUS_LIQ_UNAVAILABLE"


@pytest.mark.asyncio
async def test_e2a_position_monitor_confirmation_persistence_missing_pool_and_competing_pool(tmp_path):
    """Tests 10, 16, 17, 18: End-to-end monitor confirmation persistence, missing/migrated pool safety, competing pool blocked."""
    clear_price_backoff()
    clear_sell_idempotency_guard()
    db = AgentDatabase(db_path=str(tmp_path / "e2a_monitor.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(
        _env_file=None, EXECUTION_MODE="paper",
        MIN_EXODUS_TXNS_5M=10, EXODUS_CONFIRMATION_TICKS=2,
        EXODUS_FLOW_WARMUP_SECONDS=300.0, MONITOR_POLL_SECONDS=5.0,
    )
    t0 = datetime(2026, 11, 26, 15, 0, 0, tzinfo=timezone.utc)
    warmed_open = (t0 - timedelta(minutes=10)).isoformat()

    pos = _mk_pos(opened_at=warmed_open).model_dump()
    db.save_lane_positions(
        lane="chart", positions=[pos], sol_balance=1.5, daily_pnl_sol=0.0,
        focus_state=FocusState.IN_POSITION.value, active_position=pos,
    )

    mode = {"dex": "competing_only"}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/price/v3" in url:
            return httpx.Response(200, json={SOL_MINT: {"usdPrice": 200.0, "decimals": 9}, MINT_A: {"usdPrice": 0.104, "decimals": 6}})
        if "/tokens/v1/solana/" in url:
            if mode["dex"] == "competing_only":
                # Pinned pool missing; competing higher-liq pool has heavy sell flow & low liq -> MUST NOT trigger Exodus
                return httpx.Response(200, json=[{
                    "chainId": "solana", "pairAddress": POOL_COMPETING, "dexId": "raydium",
                    "baseToken": {"address": MINT_A}, "priceNative": "0.00052",
                    "liquidity": {"usd": 500_000.0}, "txns": {"m5": {"buys": 1, "sells": 99}},
                }])
            # Both pools present; pinned pool has qualifying sell flow (20 txns, 85% sells)
            return httpx.Response(200, json=[
                {
                    "chainId": "solana", "pairAddress": POOL_COMPETING, "dexId": "raydium",
                    "baseToken": {"address": MINT_A}, "priceNative": "0.00052",
                    "liquidity": {"usd": 500_000.0}, "txns": {"m5": {"buys": 90, "sells": 5}},
                },
                {
                    "chainId": "solana", "pairAddress": POOL_PINNED, "dexId": "pumpswap",
                    "baseToken": {"address": MINT_A}, "priceNative": "0.00052",
                    "liquidity": {"usd": 100_000.0}, "txns": {"m5": {"buys": 3, "sells": 17}},
                },
            ])
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        # 16 & 17. Pinned pool missing, competing pool has 99% sells -> HOLD, EXODUS_FLOW_UNAVAILABLE
        out0 = await position_monitor_node(
            db.get_state(), config=cfg, db=db, decision_client=_NoopDecision(), http_client=client, now=t0,  # type: ignore[arg-type]
        )
        assert out0["pending_order"] is None
        assert out0["chart_positions"][0]["last_exodus_status"] == "EXODUS_FLOW_UNAVAILABLE"
        assert out0["chart_positions"][0]["exodus_confirm_count"] == 0

        # 10. Tick 1 with pinned pool qualifying sell flow -> persists confirm_count=1 in DuckDB, HOLD
        mode["dex"] = "both_pinned_sell"
        out1 = await position_monitor_node(
            db.get_state(), config=cfg, db=db, decision_client=_NoopDecision(), http_client=client, now=t0 + timedelta(seconds=10),  # type: ignore[arg-type]
        )
        assert out1["pending_order"] is None
        st_mid = db.get_state()
        assert st_mid["chart_positions"][0]["exodus_confirm_count"] == 1
        assert st_mid["chart_positions"][0]["last_exodus_status"] == "EXODUS_FLOW_CONFIRMING"

        # 10. Tick 2 (+6s later) reads persisted confirm_count=1 from DB -> confirms 2/2 and triggers EXODUS_SELL_FLOW
        out2 = await position_monitor_node(
            st_mid, config=cfg, db=db, decision_client=_NoopDecision(), http_client=client, now=t0 + timedelta(seconds=16),  # type: ignore[arg-type]
        )
        assert out2["pending_order"] is not None
        assert "EXODUS_SELL_FLOW" in out2["pending_order"]["reason"]
        assert out2["active_position"]["last_exodus_status"] == "EXODUS_FLOW_CONFIRMED"

    # 18. Trailing TP & Breakeven still work normally
    pos_tr = _mk_pos(opened_at=warmed_open)
    pos_tr.peak_sol_value = 0.60  # +20% peak -> trail armed
    pos_tr.trail_armed = True
    o_tr, _, p_tr = evaluate_chart_position_exit(
        pos_tr, current_sol_value=0.55, buys_5m=15, sells_5m=5, current_liquidity_usd=100_000.0,
        config=cfg, flow_available=True, liq_available=True, now_dt=t0,
    )
    assert o_tr is not None and p_tr == PRIO_TRAILING_TP

    pos_be = _mk_pos(opened_at=warmed_open)
    pos_be.breakeven_armed = True
    o_be, _, p_be = evaluate_chart_position_exit(
        pos_be, current_sol_value=0.505, buys_5m=15, sells_5m=5, current_liquidity_usd=100_000.0,
        config=cfg, flow_available=True, liq_available=True, now_dt=t0,
    )
    assert o_be is not None and p_be == PRIO_BREAKEVEN
    db.close()
