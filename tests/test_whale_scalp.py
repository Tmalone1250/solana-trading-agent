"""Unit tests for the WHALE_SCALP fast-track strategy, 4-layer exit engine, and paper top-up CLI."""

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
from helius_webhook import clear_helius_roster_cache, process_helius_webhook_payload
from jupiter_token_client import clear_jupiter_token_cache
from position_monitor_node import position_monitor_node
from scanner_node import clear_geckoterminal_cache, scanner_node
from scripts.topup_paper_sol import topup_paper_balance
from state import ActivePosition, FocusState
from telemetry_api import create_telemetry_app


class DummyDecisionClient:
    """Should NEVER be called on WHALE_SCALP fast-track entries."""

    def __init__(self):
        self.called = False

    async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        self.called = True
        return {}


def _make_monitor_client(sol_quote: float) -> httpx.AsyncClient:
    lamports = int(round(sol_quote * LAMPORTS_PER_SOL))

    def handler(request: httpx.Request) -> httpx.Response:
        if "/swap/v2/order" in str(request.url):
            return httpx.Response(200, json={"outAmount": str(lamports), "transaction": None})
        return httpx.Response(
            200,
            json=[
                {
                    "chainId": "solana",
                    "liquidity": {"usd": 50_000.0},
                    "txns": {"m5": {"buys": 20, "sells": 10}},
                }
            ],
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_whale_scalp_fast_track_confluence_and_solo_buy_bypass_soak(tmp_path):
    clear_geckoterminal_cache()
    clear_jupiter_token_cache()
    clear_helius_roster_cache()

    db = AgentDatabase(db_path=str(tmp_path / "whale_ft.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(
        _env_file=None,
        WHALE_STRATEGY_ENABLED=True,
        WHALE_POSITION_SOL=0.25,
        WHALE_MIN_SOLO_BUY_SOL=2.0,
        WHALE_MIN_CONFLUENCE_WALLETS=2,
        WHALE_ENTRY_MAX_AGE_SECONDS=90,
        WHALE_MIN_LIQUIDITY_USD=15_000.0,
        ENABLE_JUPITER_DISCOVERY=True,
        JUPITER_REQUEST_DELAY_SECONDS=0.0,
    )

    now_dt = datetime.now(timezone.utc)
    mint_conf = "MintWhaleConf111111111111111111111111111111"

    # Case 1: 2-whale confluence within 5m (latest buy 20s ago)
    db.record_whale_event(
        signature="sig-c1",
        wallet="Whale1",
        mint=mint_conf,
        side="BUY",
        sol_amount=0.8,
        token_amount=100_000.0,
        timestamp_utc=(now_dt - timedelta(minutes=2)).isoformat(),
    )
    db.record_whale_event(
        signature="sig-c2",
        wallet="Whale2",
        mint=mint_conf,
        side="BUY",
        sol_amount=0.9,
        token_amount=110_000.0,
        timestamp_utc=(now_dt - timedelta(seconds=20)).isoformat(),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/toporganicscore/5m" in url or "/toptrending/5m" in url:
            return httpx.Response(200, json=[])
        if "/tokens/v2/search" in url:
            return httpx.Response(
                200,
                json=[{"id": mint_conf, "symbol": "WCONF", "organicScore": 75.0, "audit": {"isSus": False}}],
            )
        if "geckoterminal.com" in url:
            return httpx.Response(200, json={"data": []})
        if any(p in url for p in ("token-boosts", "token-profiles", "community-takeovers")):
            return httpx.Response(200, json=[])
        if "/tokens/v1/solana/" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": "solana",
                        "pairAddress": "PoolWConf",
                        "baseToken": {"address": mint_conf, "symbol": "WCONF"},
                        "marketCap": 250_000,
                        "liquidity": {"usd": 22_000},
                        "volume": {"h1": 40_000, "m5": 8_000},
                        "txns": {"m5": {"buys": 12, "sells": 5}},
                    }
                ],
            )
        if "rugcheck.xyz" in url:
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        if "/ultra/v1/shield" in url:
            return httpx.Response(200, json={"warnings": {mint_conf: []}})
        if "/swap/v2/order" in url:
            return httpx.Response(200, json={"outAmount": "2500000", "transaction": None})
        return httpx.Response(404)

    dec = DummyDecisionClient()
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

    # Entered immediately without calling alpha_node / Decision 1 or waiting 3m soak!
    assert dec.called is False
    assert result["focus_state"] == FocusState.IN_POSITION.value
    pos = result["active_position"]
    assert pos["mint"] == mint_conf
    assert pos["strategy"] == "WHALE_SCALP"
    assert pos["entry_sol"] == pytest.approx(0.25, rel=1e-4)
    assert set(pos["triggering_wallets"]) == {"Whale1", "Whale2"}

    # Verify telemetry exposes strategy == WHALE_SCALP on active position
    tc = TestClient(create_telemetry_app(config=cfg, db=db))
    tel = tc.get("/api/telemetry").json()
    assert tel["live_jupiter_pnl"]["strategy"] == "WHALE_SCALP"
    assert set(tel["live_jupiter_pnl"]["triggering_wallets"]) == {"Whale1", "Whale2"}

    # Case 2: Solo >= 2.0 SOL buy within 90s triggers fast-track
    db2 = AgentDatabase(db_path=str(tmp_path / "whale_solo.duckdb"), starting_sol=0.7)
    mint_solo = "MintWhaleSolo111111111111111111111111111111"
    db2.record_whale_event(
        signature="sig-solo-1",
        wallet="WhaleBig",
        mint=mint_solo,
        side="BUY",
        sol_amount=2.5,
        token_amount=500_000.0,
        timestamp_utc=(now_dt - timedelta(seconds=15)).isoformat(),
    )
    triggers = db2.get_whale_fast_track_triggers(
        confluence_window_minutes=5,
        max_age_seconds=90,
        min_confluence_wallets=2,
        min_solo_sol=2.0,
        now=now_dt,
    )
    assert len(triggers) == 1
    assert triggers[0]["mint"] == mint_solo
    assert triggers[0]["trigger_type"] == "SOLO"
    assert triggers[0]["triggering_wallets"] == ["WhaleBig"]

    db.close()
    db2.close()
    clear_geckoterminal_cache()
    clear_jupiter_token_cache()


@pytest.mark.asyncio
async def test_whale_scalp_rejects_stale_entries_and_is_sus(tmp_path):
    clear_geckoterminal_cache()
    clear_jupiter_token_cache()
    db = AgentDatabase(db_path=str(tmp_path / "whale_reject.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(
        _env_file=None,
        WHALE_STRATEGY_ENABLED=True,
        WHALE_ENTRY_MAX_AGE_SECONDS=90,
        ENABLE_JUPITER_DISCOVERY=True,
        JUPITER_REQUEST_DELAY_SECONDS=0.0,
    )
    now_dt = datetime.now(timezone.utc)
    now_ms = int(now_dt.timestamp() * 1000)

    # 1. Stale solo buy (120s > 90s) -> must NOT qualify for fast-track
    db.record_whale_event(
        signature="sig-stale",
        wallet="WhaleStale",
        mint="MintStaleWhale",
        side="BUY",
        sol_amount=3.0,
        token_amount=400_000.0,
        timestamp_utc=(now_dt - timedelta(seconds=120)).isoformat(),
    )
    assert (
        db.get_whale_fast_track_triggers(max_age_seconds=90, min_solo_sol=2.0, now=now_dt)
        == []
    )

    # 2. Fresh solo buy (10s) on a Jupiter audit.isSus == True token -> rejected by scanner
    db.record_whale_event(
        signature="sig-sus",
        wallet="WhaleSus",
        mint="MintSusWhale",
        side="BUY",
        sol_amount=3.0,
        token_amount=400_000.0,
        timestamp_utc=(now_dt - timedelta(seconds=10)).isoformat(),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/toporganicscore/5m" in url or "/toptrending/5m" in url:
            return httpx.Response(200, json=[])
        if "/tokens/v2/search" in url:
            return httpx.Response(
                200,
                json=[{"id": "MintSusWhale", "symbol": "SUSW", "organicScore": 50.0, "audit": {"isSus": True}}],
            )
        if "geckoterminal.com" in url:
            return httpx.Response(200, json={"data": []})
        if any(p in url for p in ("token-boosts", "token-profiles", "community-takeovers")):
            return httpx.Response(200, json=[])
        if "/tokens/v1/solana/" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": "solana",
                        "baseToken": {"address": "MintSusWhale", "symbol": "SUSW"},
                        "marketCap": 200_000,
                        "liquidity": {"usd": 30_000},
                        "volume": {"h1": 50_000},
                    }
                ],
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await scanner_node(
            {"focus_state": FocusState.SCANNING.value, "daily_start_sol": 0.7, "daily_pnl_sol": 0.0},
            config=cfg,
            db=db,
            http_client=client,
            now_ms=now_ms,
            now=now_dt,
        )
    assert out["whale_fast_track_candidates"] == []
    db.close()
    clear_geckoterminal_cache()
    clear_jupiter_token_cache()


@pytest.mark.asyncio
async def test_whale_scalp_four_layer_exit_engine(tmp_path):
    cfg = AgentConfig(_env_file=None)
    dec = DummyDecisionClient()
    mint = "MintWhaleExit111111111111111111111111111111"

    def _open_whale_pos(entry_sol: float = 0.25) -> dict[str, Any]:
        return ActivePosition(
            mint=mint,
            symbol="WEXIT",
            entry_sol=entry_sol,
            token_amount=1_000_000,
            strategy="WHALE_SCALP",
            triggering_wallets=["WhaleTriggerA", "WhaleTriggerB"],
            entry_liquidity_usd=30_000.0,
            current_sol_value=entry_sol,
            peak_sol_value=entry_sol,
            reference_token_amount=1_000_000,
        ).model_dump()

    # Exit A: WHALE_SHADOW_EXIT triggered when helius_webhook ingests a SELL from a triggering wallet
    db_a = AgentDatabase(db_path=str(tmp_path / "exit_a.duckdb"), starting_sol=0.7)
    pos_a = _open_whale_pos(0.25)
    db_a.save_state(
        focus_state=FocusState.IN_POSITION.value,
        sol_balance=0.45,
        daily_pnl_sol=0.0,
        active_position=pos_a,
    )
    sell_webhook_payload = [
        {
            "signature": "sig-whale-dump",
            "type": "SWAP",
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
            "feePayer": "WhaleTriggerA",
            "nativeTransfers": [{"fromUserAccount": "Pool", "toUserAccount": "WhaleTriggerA", "amount": 500_000_000}],
            "tokenTransfers": [{"mint": mint, "fromUserAccount": "WhaleTriggerA", "toUserAccount": "Pool", "tokenAmount": 500_000.0}],
        }
    ]
    process_helius_webhook_payload(sell_webhook_payload, config=cfg, db=db_a)
    assert db_a.get_state()["whale_shadow_exit_requested"] is True

    async with _make_monitor_client(sol_quote=0.27) as client:
        mon_a = await position_monitor_node(
            {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.45, "daily_pnl_sol": 0.0, "active_position": pos_a},
            config=cfg,
            db=db_a,
            decision_client=dec,  # type: ignore[arg-type]
            http_client=client,
        )
        assert mon_a["pending_order"]["reason"] == "WHALE_SHADOW_EXIT"
        ex_a = await executor_node(
            {
                "focus_state": FocusState.IN_POSITION.value,
                "sol_balance": 0.45,
                "daily_pnl_sol": 0.0,
                "active_position": pos_a,
                "pending_order": mon_a["pending_order"],
            },
            config=cfg,
            db=db_a,
            http_client=client,
        )
    assert ex_a["focus_state"] == FocusState.SCANNING.value
    closed_a = db_a.get_recent_closed_trades(limit=5)
    assert closed_a[0]["strategy"] == "WHALE_SCALP"
    assert closed_a[0]["exit_reason"] == "WHALE_SHADOW_EXIT"
    db_a.close()

    # Exit B: WHALE_FRONT_RUN_TP at +50% (0.25 -> 0.375 SOL)
    db_b = AgentDatabase(db_path=str(tmp_path / "exit_b.duckdb"), starting_sol=0.7)
    async with _make_monitor_client(sol_quote=0.375) as client:
        mon_b = await position_monitor_node(
            {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.45, "daily_pnl_sol": 0.0, "active_position": _open_whale_pos(0.25)},
            config=cfg,
            db=db_b,
            decision_client=dec,  # type: ignore[arg-type]
            http_client=client,
        )
    assert mon_b["pending_order"]["reason"] == "WHALE_FRONT_RUN_TP"
    db_b.close()

    # Exit C: WHALE_PEAK_TRAIL (+15% armed -> 12% drop from peak)
    db_c = AgentDatabase(db_path=str(tmp_path / "exit_c.duckdb"), starting_sol=0.7)
    state_c = {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.45, "daily_pnl_sol": 0.0, "active_position": _open_whale_pos(0.25)}
    # Tick 1: hits +30% (0.325 SOL) -> arms trail
    async with _make_monitor_client(sol_quote=0.325) as client:
        mon_c1 = await position_monitor_node(state_c, config=cfg, db=db_c, decision_client=dec, http_client=client)  # type: ignore[arg-type]
        assert mon_c1["pending_order"] is None
        assert mon_c1["active_position"]["trail_armed"] is True
    # Tick 2: drops 12.5% from 0.325 peak -> 0.284375 SOL -> WHALE_PEAK_TRAIL
    state_c.update(mon_c1)
    async with _make_monitor_client(sol_quote=0.284) as client:
        mon_c2 = await position_monitor_node(state_c, config=cfg, db=db_c, decision_client=dec, http_client=client)  # type: ignore[arg-type]
        assert mon_c2["pending_order"]["reason"] == "WHALE_PEAK_TRAIL"
    db_c.close()

    # Exit D: WHALE_HARD_STOP at -15% (0.25 -> 0.2125 SOL)
    db_d = AgentDatabase(db_path=str(tmp_path / "exit_d.duckdb"), starting_sol=0.7)
    async with _make_monitor_client(sol_quote=0.2125) as client:
        mon_d = await position_monitor_node(
            {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.45, "daily_pnl_sol": 0.0, "active_position": _open_whale_pos(0.25)},
            config=cfg,
            db=db_d,
            decision_client=dec,  # type: ignore[arg-type]
            http_client=client,
        )
    assert mon_d["pending_order"]["reason"] == "WHALE_HARD_STOP"
    db_d.close()


def test_topup_paper_sol_helper(tmp_path):
    db_path = str(tmp_path / "topup.duckdb")
    db = AgentDatabase(db_path=db_path, starting_sol=0.7)
    db.close()

    before, after = topup_paper_balance(1.0, db_path=db_path)
    assert before == pytest.approx(0.7, rel=1e-4)
    assert after == pytest.approx(1.7, rel=1e-4)

    db_check = AgentDatabase(db_path=db_path, starting_sol=0.7)
    assert db_check.get_state()["sol_balance"] == pytest.approx(1.7, rel=1e-4)
    db_check.close()
