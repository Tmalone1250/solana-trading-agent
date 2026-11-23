"""Unit tests for the WHALE_SCALP micro-cap fast-track strategy, 5-layer exit engine, and paper top-up CLI."""

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
                    "liquidity": {"usd": 20_000.0},
                    "txns": {"m5": {"buys": 20, "sells": 10}},
                }
            ],
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_whale_scalp_fast_track_confluence_and_solo_buy_within_mcap_band(tmp_path):
    clear_geckoterminal_cache()
    clear_jupiter_token_cache()
    clear_helius_roster_cache()

    db = AgentDatabase(db_path=str(tmp_path / "whale_ft.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(
        _env_file=None,
        WHALE_STRATEGY_ENABLED=True,
        WHALE_POSITION_SOL=0.25,
        WHALE_MIN_SOLO_BUY_SOL=1.5,
        WHALE_MIN_UNIQUE_WALLETS=2,
        WHALE_MIN_CONFLUENCE_WALLETS=2,
        WHALE_ENTRY_MAX_AGE_SECONDS=60,
        WHALE_MIN_MCAP_USD=15_000.0,
        WHALE_MAX_MCAP_USD=50_000_000.0,
        WHALE_MIN_LIQUIDITY_USD=40_000.0,
        ENABLE_JUPITER_DISCOVERY=True,
        JUPITER_REQUEST_DELAY_SECONDS=0.0,
    )

    now_dt = datetime.now(timezone.utc)
    mint_conf = "MintWhaleMicro11111111111111111111111111111"

    # 2-whale confluence within 5m (latest buy 20s ago <= 60s)
    db.record_whale_event(
        signature="sig-c1", wallet="Whale1", mint=mint_conf, side="BUY",
        sol_amount=0.8, token_amount=100_000.0, timestamp_utc=(now_dt - timedelta(minutes=2)).isoformat(),
    )
    db.record_whale_event(
        signature="sig-c2", wallet="Whale2", mint=mint_conf, side="BUY",
        sol_amount=0.9, token_amount=110_000.0, timestamp_utc=(now_dt - timedelta(seconds=20)).isoformat(),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/toporganicscore/5m" in url or "/toptrending/5m" in url:
            return httpx.Response(200, json=[])
        if "/tokens/v2/search" in url:
            return httpx.Response(
                200,
                json=[{"id": mint_conf, "symbol": "MICRO", "organicScore": 75.0, "audit": {"isSus": False}}],
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
                        "pairAddress": "PoolMicro",
                        "baseToken": {"address": mint_conf, "symbol": "MICRO"},
                        "marketCap": 55_000,
                        "liquidity": {"usd": 45_000},  # >= $40K
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

    assert dec.called is False
    # Slot 2 (whale_slot) opens independently without mutating Slot 1 focus_state
    pos = result["whale_position"]
    assert pos["mint"] == mint_conf
    assert pos["strategy"] == "WHALE_SCALP"
    assert pos["entry_sol"] == pytest.approx(0.25, rel=1e-4)
    assert pos["opened_at"] is not None
    assert set(pos["triggering_wallets"]) == {"Whale1", "Whale2"}

    # Verify telemetry exposes strategy == WHALE_SCALP on both live_jupiter_pnl & whale_position
    tc = TestClient(create_telemetry_app(config=cfg, db=db))
    tel = tc.get("/api/telemetry").json()
    assert tel["live_jupiter_pnl"]["strategy"] == "WHALE_SCALP"
    assert tel["whale_position"]["strategy"] == "WHALE_SCALP"
    assert len(tel["active_positions"]) == 1

    # Solo buy >= min_solo_sol MUST trigger SOLO even when WHALE_MIN_UNIQUE_WALLETS=2; below threshold must not
    db2 = AgentDatabase(db_path=str(tmp_path / "whale_solo.duckdb"), starting_sol=0.7)
    db2.record_whale_event(
        signature="sig-solo-pass", wallet="WhaleBig", mint="MintSoloPass", side="BUY",
        sol_amount=1.5, token_amount=500_000.0, timestamp_utc=(now_dt - timedelta(seconds=15)).isoformat(),
    )
    db2.record_whale_event(
        signature="sig-solo-small", wallet="WhaleSmall", mint="MintSoloSmall", side="BUY",
        sol_amount=0.5, token_amount=100_000.0, timestamp_utc=(now_dt - timedelta(seconds=10)).isoformat(),
    )
    trigs = db2.get_whale_fast_track_triggers(max_age_seconds=60, min_confluence_wallets=2, min_solo_sol=0.9, now=now_dt)
    assert len(trigs) == 1 and trigs[0]["mint"] == "MintSoloPass" and trigs[0]["trigger_type"] == "SOLO"
    assert trigs[0]["triggering_wallets"] == ["WhaleBig"]

    db.close()
    db2.close()
    clear_geckoterminal_cache()
    clear_jupiter_token_cache()


@pytest.mark.asyncio
async def test_whale_scalp_mcap_band_gating_stale_60s_and_is_sus(tmp_path):
    clear_geckoterminal_cache()
    clear_jupiter_token_cache()
    db = AgentDatabase(db_path=str(tmp_path / "whale_gate.duckdb"), starting_sol=0.7)
    cfg = AgentConfig(
        _env_file=None,
        WHALE_STRATEGY_ENABLED=True,
        WHALE_ENTRY_MAX_AGE_SECONDS=60,
        WHALE_MIN_MCAP_USD=15_000.0,
        WHALE_MAX_MCAP_USD=50_000_000.0,
        WHALE_MIN_LIQUIDITY_USD=10_000.0,
        ENABLE_JUPITER_DISCOVERY=True,
        JUPITER_REQUEST_DELAY_SECONDS=0.0,
    )
    now_dt = datetime.now(timezone.utc)
    now_ms = int(now_dt.timestamp() * 1000)

    def _seed_confluence(m: str, age_sec: int, tag: str) -> None:
        db.record_whale_event(
            signature=f"sig-{tag}-1",
            wallet=f"W{tag}A",
            mint=m,
            side="BUY",
            sol_amount=2.0,
            token_amount=100_000.0,
            timestamp_utc=(now_dt - timedelta(seconds=age_sec + 5)).isoformat(),
        )
        db.record_whale_event(
            signature=f"sig-{tag}-2",
            wallet=f"W{tag}B",
            mint=m,
            side="BUY",
            sol_amount=2.0,
            token_amount=100_000.0,
            timestamp_utc=(now_dt - timedelta(seconds=age_sec)).isoformat(),
        )

    # 1. Stale (> 60s) -> ignored
    _seed_confluence("MintStale", 75, "stale")
    # 2. Too small MCAP ($10K < $15K) -> rejected
    _seed_confluence("MintTooSmall", 10, "small")
    # 3. Too large MCAP ($60M > $50M) -> rejected
    _seed_confluence("MintTooLarge", 10, "large")
    # 4. Inside MCAP band ($14M) but Jupiter audit.isSus == True -> rejected
    _seed_confluence("MintSus", 10, "sus")
    # 5. Valid $14M mid/high-cap whale runner (inside [$15K, $50M]), liq $500K, clean -> ACCEPTED
    _seed_confluence("MintJustRight", 10, "ok")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/toporganicscore/5m" in url or "/toptrending/5m" in url:
            return httpx.Response(200, json=[])
        if "/tokens/v2/search" in url:
            return httpx.Response(
                200,
                json=[
                    {"id": "MintSus", "symbol": "SUS", "organicScore": 50.0, "audit": {"isSus": True}},
                    {"id": "MintJustRight", "symbol": "RIGHT", "organicScore": 85.0, "audit": {"isSus": False}},
                ],
            )
        if "geckoterminal.com" in url:
            return httpx.Response(200, json={"data": []})
        if any(p in url for p in ("token-boosts", "token-profiles", "community-takeovers")):
            return httpx.Response(200, json=[])
        if "/tokens/v1/solana/" in url:
            return httpx.Response(
                200,
                json=[
                    {"chainId": "solana", "baseToken": {"address": "MintTooSmall", "symbol": "SM"}, "marketCap": 10_000, "liquidity": {"usd": 12_000}},
                    {"chainId": "solana", "baseToken": {"address": "MintTooLarge", "symbol": "LG"}, "marketCap": 60_000_000, "liquidity": {"usd": 1_000_000}},
                    {"chainId": "solana", "baseToken": {"address": "MintSus", "symbol": "SUS"}, "marketCap": 14_000_000, "liquidity": {"usd": 300_000}},
                    {"chainId": "solana", "baseToken": {"address": "MintJustRight", "symbol": "RIGHT"}, "marketCap": 14_000_000, "liquidity": {"usd": 500_000}},
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

    assert [c["mint"] for c in out["whale_fast_track_candidates"]] == ["MintJustRight"]
    decisions = db.get_whale_decisions()
    assert decisions["MintTooLarge"]["reason"] == "MCAP > $50M"
    db.close()
    clear_geckoterminal_cache()
    clear_jupiter_token_cache()


@pytest.mark.asyncio
async def test_whale_scalp_five_layer_exit_engine(tmp_path):
    cfg = AgentConfig(_env_file=None)
    dec = DummyDecisionClient()
    mint = "MintWhaleExit111111111111111111111111111111"
    now_dt = datetime.now(timezone.utc)

    def _open_whale_pos(entry_sol: float = 0.25, opened_at: datetime | None = None) -> dict[str, Any]:
        return ActivePosition(
            mint=mint,
            symbol="WEXIT",
            entry_sol=entry_sol,
            token_amount=1_000_000,
            strategy="WHALE_SCALP",
            triggering_wallets=["WhaleTriggerA", "WhaleTriggerB"],
            opened_at=(opened_at or now_dt).isoformat(),
            entry_liquidity_usd=20_000.0,
            current_sol_value=entry_sol,
            peak_sol_value=entry_sol,
            reference_token_amount=1_000_000,
        ).model_dump()

    # (a) WHALE_SHADOW_EXIT when triggering whale sells
    db_a = AgentDatabase(db_path=str(tmp_path / "exit_a.duckdb"), starting_sol=0.7)
    pos_a = _open_whale_pos(0.25)
    db_a.save_state(focus_state=FocusState.IN_POSITION.value, sol_balance=0.45, daily_pnl_sol=0.0, active_position=pos_a)
    process_helius_webhook_payload(
        [
            {
                "signature": "sig-whale-dump", "type": "SWAP", "timestamp": int(now_dt.timestamp()), "feePayer": "WhaleTriggerA",
                "nativeTransfers": [{"fromUserAccount": "Pool", "toUserAccount": "WhaleTriggerA", "amount": 500_000_000}],
                "tokenTransfers": [{"mint": mint, "fromUserAccount": "WhaleTriggerA", "toUserAccount": "Pool", "tokenAmount": 500_000.0}],
            }
        ],
        config=cfg, db=db_a,
    )
    async with _make_monitor_client(sol_quote=0.26) as client:
        mon_a = await position_monitor_node(
            {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.45, "daily_pnl_sol": 0.0, "active_position": pos_a},
            config=cfg, db=db_a, decision_client=dec, http_client=client, now=now_dt,  # type: ignore[arg-type]
        )
        assert mon_a["pending_order"]["reason"] == "WHALE_SHADOW_EXIT"
        await executor_node(
            {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.45, "daily_pnl_sol": 0.0, "active_position": pos_a, "pending_order": mon_a["pending_order"]},
            config=cfg, db=db_a, http_client=client,
        )
    assert db_a.get_recent_closed_trades(limit=5)[0]["strategy"] == "WHALE_SCALP"
    db_a.close()

    # (b) WHALE_PARTIAL_TP_50PCT (+15%) at +15% (0.25 -> 0.2875 SOL) scales out 50%
    db_b = AgentDatabase(db_path=str(tmp_path / "exit_b.duckdb"), starting_sol=0.7)
    async with _make_monitor_client(sol_quote=0.2875) as client:
        mon_b = await position_monitor_node(
            {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.45, "daily_pnl_sol": 0.0, "active_position": _open_whale_pos(0.25)},
            config=cfg, db=db_b, decision_client=dec, http_client=client, now=now_dt,  # type: ignore[arg-type]
        )
    assert mon_b["pending_order"]["side"] == "PARTIAL_SELL"
    assert mon_b["pending_order"]["reason"] == "WHALE_PARTIAL_TP_50PCT (+15%)"
    assert mon_b["pending_order"]["amount_atomic"] == 500_000
    db_b.close()

    # (c1) WHALE_PEAK_TRAIL Tier 1: arms at +8% (0.2725 SOL = +9%), exits on 3.5% drop (0.2629 SOL)
    db_c1 = AgentDatabase(db_path=str(tmp_path / "exit_c1.duckdb"), starting_sol=0.7)
    st_c1 = {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.45, "daily_pnl_sol": 0.0, "active_position": _open_whale_pos(0.25)}
    async with _make_monitor_client(sol_quote=0.2725) as client:
        m1 = await position_monitor_node(st_c1, config=cfg, db=db_c1, decision_client=dec, http_client=client, now=now_dt)  # type: ignore[arg-type]
        assert m1["pending_order"] is None
        assert m1["active_position"]["trail_armed"] is True
    st_c1.update(m1)
    async with _make_monitor_client(sol_quote=0.2657) as client:
        m_hold = await position_monitor_node(st_c1, config=cfg, db=db_c1, decision_client=dec, http_client=client, now=now_dt)  # type: ignore[arg-type]
        assert m_hold["pending_order"] is None
    async with _make_monitor_client(sol_quote=0.2626) as client:
        m2 = await position_monitor_node(st_c1, config=cfg, db=db_c1, decision_client=dec, http_client=client, now=now_dt)  # type: ignore[arg-type]
        assert m2["pending_order"]["reason"] == "WHALE_PEAK_TRAIL"
    db_c1.close()

    # (c2) WHALE_PEAK_TRAIL Tier 2 (Tight): hits +13% (0.2825 SOL >= +12%), exits on 2.5% drop (0.2754 SOL)
    db_c2 = AgentDatabase(db_path=str(tmp_path / "exit_c2.duckdb"), starting_sol=0.7)
    st_c2 = {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.45, "daily_pnl_sol": 0.0, "active_position": _open_whale_pos(0.25)}
    async with _make_monitor_client(sol_quote=0.2825) as client:
        mt1 = await position_monitor_node(st_c2, config=cfg, db=db_c2, decision_client=dec, http_client=client, now=now_dt)  # type: ignore[arg-type]
        assert mt1["pending_order"] is None
    st_c2.update(mt1)
    async with _make_monitor_client(sol_quote=0.2752) as client:
        mt2 = await position_monitor_node(st_c2, config=cfg, db=db_c2, decision_client=dec, http_client=client, now=now_dt)  # type: ignore[arg-type]
        assert mt2["pending_order"]["reason"] == "WHALE_PEAK_TRAIL"
    db_c2.close()

    # (d) WHALE_HARD_STOP at -10% (0.25 -> 0.225 SOL)
    db_d = AgentDatabase(db_path=str(tmp_path / "exit_d.duckdb"), starting_sol=0.7)
    async with _make_monitor_client(sol_quote=0.225) as client:
        mon_d = await position_monitor_node(
            {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.45, "daily_pnl_sol": 0.0, "active_position": _open_whale_pos(0.25)},
            config=cfg, db=db_d, decision_client=dec, http_client=client, now=now_dt,  # type: ignore[arg-type]
        )
    assert mon_d["pending_order"]["reason"] == "WHALE_HARD_STOP"
    db_d.close()

    # (e) WHALE_TIME_STOP: default 300s from config (holds at 95s, exits at 305s if PnL < +4%)
    db_e = AgentDatabase(db_path=str(tmp_path / "exit_e.duckdb"), starting_sol=0.7)
    opened_95s_ago = now_dt - timedelta(seconds=95)
    opened_305s_ago = now_dt - timedelta(seconds=305)
    async with _make_monitor_client(sol_quote=0.255) as client:
        mon_e_early = await position_monitor_node(
            {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.45, "daily_pnl_sol": 0.0, "active_position": _open_whale_pos(0.25, opened_at=opened_95s_ago)},
            config=cfg, db=db_e, decision_client=dec, http_client=client, now=now_dt,  # type: ignore[arg-type]
        )
    assert mon_e_early["pending_order"] is None
    async with _make_monitor_client(sol_quote=0.2625) as client:
        mon_e_hold = await position_monitor_node(
            {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.45, "daily_pnl_sol": 0.0, "active_position": _open_whale_pos(0.25, opened_at=opened_305s_ago)},
            config=cfg, db=db_e, decision_client=dec, http_client=client, now=now_dt,  # type: ignore[arg-type]
        )
    assert mon_e_hold["pending_order"] is None
    async with _make_monitor_client(sol_quote=0.255) as client:
        mon_e_stop = await position_monitor_node(
            {"focus_state": FocusState.IN_POSITION.value, "sol_balance": 0.45, "daily_pnl_sol": 0.0, "active_position": _open_whale_pos(0.25, opened_at=opened_305s_ago)},
            config=cfg, db=db_e, decision_client=dec, http_client=client, now=now_dt,  # type: ignore[arg-type]
        )
    assert mon_e_stop["pending_order"]["reason"] == "WHALE_TIME_STOP"
    db_e.close()


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


def test_telemetry_whale_trades_stats_and_confluence_agent_reasons(tmp_path):
    clear_helius_roster_cache()
    db = AgentDatabase(db_path=str(tmp_path / "whale_vis.duckdb"), starting_sol=1.0)
    cfg = AgentConfig(_env_file=None, ENABLE_HELIUS_WHALE_WEBHOOK=True)
    now_dt = datetime.now(timezone.utc)
    jijo = "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk"

    # Seed two closed WHALE_SCALP trades (1 win +25%, 1 loss -12%)
    db.log_trade(
        mode="paper", side="SELL", mint="MintWin1", symbol="WIN1",
        in_amount=1_000_000, out_amount=312_500_000, sol_value=0.3125,
        pnl_sol=0.0625, pnl_pct=0.25, reason="WHALE_PEAK_TRAIL",
        strategy="WHALE_SCALP", triggering_wallets=[jijo],
        opened_at=(now_dt - timedelta(seconds=40)).isoformat(),
    )
    db.log_trade(
        mode="paper", side="SELL", mint="MintLoss1", symbol="LOS1",
        in_amount=1_000_000, out_amount=220_000_000, sol_value=0.22,
        pnl_sol=-0.03, pnl_pct=-0.12, reason="WHALE_HARD_STOP",
        strategy="WHALE_SCALP", triggering_wallets=[jijo],
        opened_at=(now_dt - timedelta(seconds=60)).isoformat(),
    )

    # Seed a currently open WHALE_SCALP position
    open_pos = ActivePosition(
        mint="MintOpen1", symbol="OPENW", entry_sol=0.25, token_amount=1_000_000,
        strategy="WHALE_SCALP", triggering_wallets=[jijo],
        opened_at=(now_dt - timedelta(seconds=15)).isoformat(),
        current_sol_value=0.28, peak_sol_value=0.28,
    ).model_dump()
    db.save_state(
        focus_state=FocusState.IN_POSITION.value, sol_balance=0.75,
        daily_pnl_sol=0.0325, active_position=open_pos,
    )

    # Seed two active whale signals: MintOpen1 (ENTERED) and MintSkip1 (SKIPPED: MCAP > $120K)
    db.record_whale_event(
        signature="sig-open", wallet=jijo, mint="MintOpen1", side="BUY",
        sol_amount=2.5, token_amount=500_000.0, timestamp_utc=now_dt.isoformat(),
    )
    db.recompute_whale_signal("MintOpen1", ttl_minutes=30, now=now_dt)

    db.record_whale_event(
        signature="sig-skip", wallet=jijo, mint="MintSkip1", side="BUY",
        sol_amount=2.2, token_amount=400_000.0, timestamp_utc=now_dt.isoformat(),
    )
    db.recompute_whale_signal("MintSkip1", ttl_minutes=30, now=now_dt)
    db.upsert_whale_decision("MintSkip1", "SKIPPED", "MCAP > $120K", updated_at=now_dt.isoformat())

    tc = TestClient(create_telemetry_app(config=cfg, db=db))
    resp = tc.get("/api/telemetry")
    assert resp.status_code == 200
    wd = resp.json()["whale_discovery"]

    # whale_trades: prepended OPEN trade + 2 closed trades
    assert len(wd["whale_trades"]) == 3
    assert wd["whale_trades"][0]["status"] == "OPEN"
    assert wd["whale_trades"][0]["symbol"] == "OPENW"
    assert wd["whale_trades"][0]["triggering_wallets"][0]["label"] == "Jijo"
    assert wd["whale_trades"][1]["status"] == "CLOSED"
    assert wd["whale_trades"][1]["hold_seconds"] is not None

    # whale_stats
    ws = wd["whale_stats"]
    assert ws["total_trades"] == 2
    assert ws["wins"] == 1
    assert ws["losses"] == 1
    assert ws["win_rate_pct"] == 50.0
    assert ws["total_pnl_sol"] == pytest.approx(0.0325, rel=1e-4)
    assert ws["avg_hold_seconds"] > 0

    # active_signals carry agent_status & agent_reason
    sigs_by_mint = {s["mint"]: s for s in wd["active_signals"]}
    assert sigs_by_mint["MintOpen1"]["agent_status"] == "ENTERED"
    assert sigs_by_mint["MintOpen1"]["agent_reason"] == "OPEN"
    assert sigs_by_mint["MintSkip1"]["agent_status"] == "SKIPPED"
    assert sigs_by_mint["MintSkip1"]["agent_reason"] == "MCAP > $120K"
    db.close()
