"""Directive 03 Regression Tests: Independent Whale/Webhook controls & early disabled re-entry abandonment."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from alpha_node import alpha_node
from batched_price_monitor import clear_price_backoff
from config import LAMPORTS_PER_SOL, AgentConfig
from db import AgentDatabase
from helius_webhook import clear_helius_roster_cache
from narrative_cache import clear_narrative_cache
from position_monitor_node import position_monitor_node
from scanner_node import clear_geckoterminal_cache, scanner_node
from state import ActivePosition, FocusState
from state_ledger import clear_buy_reservations, clear_sell_guard, update_lane_positions_only
from telemetry_api import create_telemetry_app
from whale_lane import run_whale_lane_tick


class _CountingDecision:
    def __init__(self) -> None:
        self.calls = 0
        self.last_input = ""

    async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        self.calls += 1
        self.last_input = input_text
        return {
            "t0_narrative_strength": {"type": "score", "score": 3.0, "confidence": 0.8},
            "t0_momentum_quality": {"type": "score", "score": 3.0, "confidence": 0.8},
            "t0_is_copycat_vamp": {"type": "noul", "noul": 0.2, "confidence": 0.8},
            "t0_action": {"type": "choice", "choice": "WATCH", "confidence": 0.7},
        }


def _whale_swap_payload(sig: str, wallet: str, mint: str, now_ts: int, sol_amt: float = 1.6) -> list[dict[str, Any]]:
    return [{
        "type": "SWAP",
        "signature": sig,
        "feePayer": wallet,
        "timestamp": now_ts,
        "nativeTransfers": [{"fromUserAccount": wallet, "toUserAccount": "PoolX", "amount": int(sol_amt * LAMPORTS_PER_SOL)}],
        "tokenTransfers": [{"fromUserAccount": "PoolX", "toUserAccount": wallet, "mint": mint, "tokenAmount": 500_000.0, "symbol": "WHL"}],
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "whale_enabled,webhook_enabled,expect_ingest,expect_chart_signal",
    [
        (False, False, False, False),
        (False, True, True, False),
        (True, False, False, True),
        (True, True, True, True),
    ],
)
async def test_whale_and_webhook_four_way_flag_matrix(
    tmp_path,
    whale_enabled: bool,
    webhook_enabled: bool,
    expect_ingest: bool,
    expect_chart_signal: bool,
):
    """Verify all 4 combinations of (WHALE_STRATEGY_ENABLED, ENABLE_HELIUS_WHALE_WEBHOOK)."""
    clear_helius_roster_cache()
    clear_geckoterminal_cache()
    clear_narrative_cache()

    db = AgentDatabase(db_path=str(tmp_path / f"matrix_{whale_enabled}_{webhook_enabled}.duckdb"), starting_sol=1.5)
    now_dt = datetime.now(timezone.utc)
    hist_mint = "HistMatrixMint111111111111111111111111111111"
    new_mint = "NewMatrixMint2222222222222222222222222222222"
    wallet = "TrackedWhaleMatrix11111111111111111111111111"

    cfg = AgentConfig(
        _env_file=None,
        EXECUTION_MODE="paper",
        CHART_ENABLED_STRATEGIES="BREAKOUT_IGNITION",
        WHALE_STRATEGY_ENABLED=whale_enabled,
        ENABLE_HELIUS_WHALE_WEBHOOK=webhook_enabled,
        HELIUS_AUTH_HEADER="Bearer matrix-secret",
        HELIUS_TRACKED_WALLETS=wallet,
        ENABLE_JUPITER_DISCOVERY=False,
    )

    # Seed 1 historical whale event & signal (must remain intact in all 4 modes)
    db.record_whale_event(
        signature="hist_matrix_sig", wallet=wallet, mint=hist_mint,
        side="BUY", sol_amount=2.0, token_amount=250_000.0, timestamp_utc=now_dt.isoformat(),
    )
    db.recompute_whale_signal(hist_mint, ttl_minutes=30, now=now_dt)

    app = create_telemetry_app(config=cfg, db=db)
    tc = TestClient(app)

    # 1. Webhook ingestion & sync routes strictly follow ENABLE_HELIUS_WHALE_WEBHOOK
    if webhook_enabled:
        r_bad_auth = tc.post("/api/webhooks/helius", headers={"Authorization": "Bearer wrong"}, json=[])
        assert r_bad_auth.status_code == 401

    r_hook = tc.post(
        "/api/webhooks/helius",
        headers={"Authorization": "Bearer matrix-secret"},
        json=_whale_swap_payload("new_matrix_sig", wallet, new_mint, int(now_dt.timestamp())),
    )
    assert r_hook.status_code == 200
    body = r_hook.json()
    if expect_ingest:
        assert body["status"] == "ok" and body["recorded"] == 1
        assert len(db.get_recent_whale_events(limit=10)) == 2
    else:
        assert body["status"] == "disabled" and body["recorded"] == 0
        assert len(db.get_recent_whale_events(limit=10)) == 1  # Historical intact, zero new writes

    r_sync = tc.post("/api/webhooks/helius/sync")
    assert r_sync.status_code == 200
    if expect_ingest:
        assert r_sync.json().get("status") != "disabled"
    else:
        assert r_sync.json() == {"status": "disabled", "synced_count": 0, "wallets": []}
        assert db._conn.execute("SELECT COUNT(*) FROM action_log WHERE action = 'WHALE_DISCOVERY'").fetchone()[0] == 0

    # 2. Chart Scanner & Alpha signal consumption strictly follow WHALE_STRATEGY_ENABLED
    seen_urls: list[str] = []

    def _scan_handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"data": [], "included": []} if "geckoterminal" in str(request.url) else [])

    async with httpx.AsyncClient(transport=httpx.MockTransport(_scan_handler)) as client:
        await scanner_node(
            {"focus_state": FocusState.SCANNING.value, "daily_start_sol": 1.5, "daily_pnl_sol": 0.0, "chart_positions": []},
            config=cfg, db=db, http_client=client, now=now_dt,
        )
    has_hist_in_scan = any(hist_mint in u for u in seen_urls)
    assert has_hist_in_scan is expect_chart_signal

    dec = _CountingDecision()
    await alpha_node(
        {
            "focus_state": FocusState.SCANNING.value, "sol_balance": 1.5,
            "safe_candidates": [{"mint": hist_mint, "symbol": "HMAT", "added_at": (now_dt - timedelta(minutes=5)).isoformat(), "candles_5m": []}],
        },
        config=cfg, db=db, decision_client=dec, now=now_dt,  # type: ignore[arg-type]
    )
    assert ("whale_buy_count=" in dec.last_input) is expect_chart_signal

    # 3. Independent Whale Lane execution strictly follows WHALE_STRATEGY_ENABLED (even when webhook is False!)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=[]))) as client:
        w_out = await run_whale_lane_tick(config=cfg, db=db, http_client=client, now=now_dt)
    if whale_enabled:
        assert w_out["event"] != "DISABLED"
    else:
        assert w_out["event"] == "DISABLED"

    # Dashboard telemetry remains available in all 4 modes
    assert tc.get("/api/telemetry").status_code == 200
    db.close()


@pytest.mark.asyncio
async def test_disabled_watching_reentry_abandons_early_without_http_and_preserves_open_position_exits(tmp_path):
    """Disabled LEG2_ENTRY abandons WATCHING_REENTRY with 0 HTTP/Gemini calls and preserves coexisting open-slot exits."""
    clear_price_backoff()
    clear_buy_reservations()
    clear_sell_guard()

    db = AgentDatabase(db_path=str(tmp_path / "early_reentry.duckdb"), starting_sol=1.5)
    cfg = AgentConfig(
        _env_file=None, EXECUTION_MODE="paper", CHART_ENABLED_STRATEGIES="BREAKOUT_IGNITION",
        MAX_REENTRIES_PER_MINT=2, PAPER_TX_FEE_SOL=0.001,
    )
    now_dt = datetime(2026, 11, 25, 23, 0, 0, tzinfo=timezone.utc)
    watcher = ActivePosition(
        mint="MintWatchEarly", symbol="WEARL", entry_sol=0.50, token_amount=0,
        entry_liquidity_usd=70_000.0, current_sol_value=0.54, peak_sol_value=0.60,
        run_peak_sol=0.60, retrace_low_sol=0.50, reference_token_amount=1_000_000,
        watch_started_at=now_dt.isoformat(),
    ).model_dump()
    db.save_state(focus_state=FocusState.WATCHING_REENTRY.value, sol_balance=1.0, daily_pnl_sol=0.05, active_position=watcher)

    http_calls: list[str] = []

    def _fail_if_called(request: httpx.Request) -> httpx.Response:
        http_calls.append(str(request.url))
        raise AssertionError(f"Unexpected HTTP call for disabled watcher: {request.url}")

    dec = _CountingDecision()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_fail_if_called)) as client:
        out_solo = await position_monitor_node(
            {"focus_state": FocusState.WATCHING_REENTRY.value, "sol_balance": 1.0, "daily_pnl_sol": 0.05, "active_position": watcher, "chart_positions": []},
            config=cfg, db=db, decision_client=dec, http_client=client, now=now_dt,  # type: ignore[arg-type]
        )
    assert http_calls == []
    assert dec.calls == 0
    assert out_solo["focus_state"] == FocusState.SCANNING.value
    assert out_solo["monitor_event"] == "LEG2_ENTRY_DISABLED"
    assert db.is_on_cooldown("MintWatchEarly")

    # 2. WATCHING_REENTRY coexisting with 1 open Chart position that hits HARD_STOP_LOSS:
    # Watcher mint must NOT be queried, receives LEG2_ENTRY_DISABLED cooldown, and final state is SCANNING.
    watcher2 = {**watcher, "mint": "MintWatchCoexist1", "symbol": "WCX1"}
    open_pos = ActivePosition(
        mint="MintOpenExit", symbol="OPEX", entry_sol=0.50, token_amount=1_000_000,
        entry_liquidity_usd=80_000.0, current_sol_value=0.50, peak_sol_value=0.50,
        opened_at=(now_dt - timedelta(minutes=2)).isoformat(), strategy="BREAKOUT_IGNITION",
    ).model_dump()
    db.save_state(focus_state=FocusState.WATCHING_REENTRY.value, sol_balance=0.50, daily_pnl_sol=0.0, active_position=watcher2)
    update_lane_positions_only(db, lane="chart", positions=[open_pos], focus_state=FocusState.WATCHING_REENTRY.value, active_position=watcher2)

    coexist_urls: list[str] = []

    def _coexist_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        coexist_urls.append(url)
        if "/tokens/v1/solana/" in url:
            return httpx.Response(200, json=[
                {
                    "chainId": "solana", "baseToken": {"address": "MintOpenExit", "symbol": "OPEX"},
                    "priceNative": "0.43", "priceUsd": "0.086", "liquidity": {"usd": 80_000.0},
                    "txns": {"m5": {"buys": 5, "sells": 15}},
                },
                {
                    "chainId": "solana", "baseToken": {"address": "MintOpenHold", "symbol": "OPHD"},
                    "priceNative": "0.52", "priceUsd": "0.104", "liquidity": {"usd": 85_000.0},
                    "txns": {"m5": {"buys": 16, "sells": 10}},
                },
            ])
        if "/swap/v2/order" in url:
            return httpx.Response(200, json={"outAmount": str(int(0.43 * LAMPORTS_PER_SOL)), "priceImpactPct": "-0.4", "transaction": None})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_coexist_handler)) as client:
        out_co = await position_monitor_node(
            {"focus_state": FocusState.WATCHING_REENTRY.value, "sol_balance": 0.50, "daily_pnl_sol": 0.0, "active_position": watcher2, "chart_positions": [open_pos]},
            config=cfg, db=db, decision_client=dec, http_client=client, now=now_dt,  # type: ignore[arg-type]
        )
    assert not any("MintWatchCoexist1" in u for u in coexist_urls)
    assert dec.calls == 0
    assert out_co.get("_hard_stop_already_executed") is True
    assert out_co["focus_state"] == FocusState.SCANNING.value
    assert out_co["active_position"] is None
    assert out_co["chart_positions"] == []
    st_after_last = db.get_state()
    assert st_after_last["focus_state"] == FocusState.SCANNING.value
    assert st_after_last["active_position"] is None
    assert st_after_last["chart_positions"] == []
    assert db.is_on_cooldown("MintWatchCoexist1")

    # 3. WATCHING_REENTRY coexisting with 2 open Chart positions: 1 exits via HARD_STOP_LOSS, 1 remains open -> IN_POSITION
    clear_sell_guard()
    watcher3 = {**watcher, "mint": "MintWatchCoexist2", "symbol": "WCX2"}
    open_exit_2 = {**open_pos, "opened_at": (now_dt - timedelta(minutes=1)).isoformat()}
    open_hold = ActivePosition(
        mint="MintOpenHold", symbol="OPHD", entry_sol=0.50, token_amount=1_000_000,
        entry_liquidity_usd=85_000.0, current_sol_value=0.52, peak_sol_value=0.52,
        opened_at=(now_dt - timedelta(minutes=3)).isoformat(), strategy="BREAKOUT_IGNITION",
    ).model_dump()
    db.save_state(focus_state=FocusState.WATCHING_REENTRY.value, sol_balance=0.50, daily_pnl_sol=0.0, active_position=watcher3)
    update_lane_positions_only(db, lane="chart", positions=[open_exit_2, open_hold], focus_state=FocusState.WATCHING_REENTRY.value, active_position=watcher3)
    coexist_urls.clear()

    async with httpx.AsyncClient(transport=httpx.MockTransport(_coexist_handler)) as client:
        out_co2 = await position_monitor_node(
            {"focus_state": FocusState.WATCHING_REENTRY.value, "sol_balance": 0.50, "daily_pnl_sol": 0.0, "active_position": watcher3, "chart_positions": [open_exit_2, open_hold]},
            config=cfg, db=db, decision_client=dec, http_client=client, now=now_dt,  # type: ignore[arg-type]
        )
    assert not any("MintWatchCoexist2" in u for u in coexist_urls)
    assert dec.calls == 0
    assert out_co2.get("_hard_stop_already_executed") is True
    assert out_co2["focus_state"] == FocusState.IN_POSITION.value
    assert out_co2["active_position"]["mint"] == "MintOpenHold"
    assert [p["mint"] for p in out_co2["chart_positions"]] == ["MintOpenHold"]
    st_after_rem = db.get_state()
    assert st_after_rem["focus_state"] == FocusState.IN_POSITION.value
    assert st_after_rem["active_position"]["mint"] == "MintOpenHold"
    assert [p["mint"] for p in st_after_rem["chart_positions"]] == ["MintOpenHold"]
    assert db.is_on_cooldown("MintWatchCoexist2")
    db.close()
