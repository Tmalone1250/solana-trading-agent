"""Directive 16 & 19 (E1 + Corrective Pass) Verification Suite: Pool Identity Continuity & DB Persistence."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any

import duckdb
import httpx
import pytest
from fastapi.testclient import TestClient

from alpha_node import alpha_node
from batched_price_monitor import (
    apply_dex_flow_and_pnl,
    clear_price_backoff,
    fetch_batched_sol_prices,
    select_pinned_dexscreener_pair,
)
from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from db import ACTION_LOG_COLUMNS, AgentDatabase
from executor_node import clear_sell_idempotency_guard, executor_node
from narrative_cache import clear_narrative_cache
from position_monitor_node import position_monitor_node
from state import ActivePosition, FocusState, PendingOrder, is_valid_pool_address
from state_ledger import clear_buy_reservations
from telemetry_api import create_telemetry_app

# Valid 32-byte Base58 Solana public keys for deterministic test fixtures
POOL_PINNED = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"
POOL_COMPETING = "8qbHbw2BbbTHBW1sbeqakYXVKRQM8Ne7pLK7m6CVfeR"
POOL_TAMPERED = "CktRuQ2mttgRGkXJtyksdKHjUdc2C4TgDzyB98oEzy8"
POOL_WRONG = "GgBaCs3NCBuZN12kCJgAW63ydqohFkHEdfdEXBPzLHq"
POOL_LEGACY_WL = "LbUiWL3xVV8hTFYBVdbTNrpDo41NKS6o3LHHuDzjfcY"
MINT_LIFE = "p2Yicb86aZig616Eav2VWG9vuXR5mEqhtzshZYBxzsV"
MINT_NOPOOL = "swqrv48gsrwpBFbftEwnP2vB4jckpvfGJfXkwaniLCC"
MINT_MULTI = "ws91DX9HBAAxGW77BZs5FogRDwpRtcUpiLBpKdPTfWu"
MINT_IMPOSTOR = "21nS9Wz9sUTQ6MkcYUtnN8aSfPA26xJJP7zqshfzCzqc"


class _StubDecision:
    def __init__(self, answers: dict[str, dict[str, Any]]):
        self._answers = answers

    async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return self._answers


def _make_breakout_candles(count: int = 18) -> list[dict[str, float]]:
    """Construct realistic 5m candles with alternating pullbacks (RSI ~60) + tight coil + breakout."""
    base_ts = 1_795_680_000.0
    candles: list[dict[str, float]] = []
    prices = [
        0.96, 0.99, 0.97, 1.01, 0.98, 1.02, 0.99, 1.03,
        1.00, 1.02, 1.00, 1.01, 1.02, 1.015, 1.025, 1.03, 1.035,
    ]
    for i in range(count - 1):
        p = prices[i % len(prices)]
        prev_p = prices[(i - 1) % len(prices)] if i > 0 else p
        candles.append(
            {
                "timestamp": base_ts + i * 300.0,
                "open": prev_p,
                "high": max(p, prev_p) * 1.008,
                "low": min(p, prev_p) * 0.994,
                "close": p,
                "volume": 100.0,
            }
        )
    last_high = max(c["high"] for c in candles[-6:])
    candles.append(
        {
            "timestamp": base_ts + (count - 1) * 300.0,
            "open": last_high * 0.995,
            "high": last_high * 1.045,
            "low": last_high * 0.992,
            "close": last_high * 1.040,
            "volume": 260.0,
        }
    )
    return candles


def _strong_flow_pair(price: float = 1.12) -> dict[str, Any]:
    return {
        "price_usd": price,
        "market_cap_usd": 500_000.0,
        "liquidity_usd": 80_000.0,
        "volume_1h_usd": 200_000.0,
        "volume_5m_usd": 35_000.0,
        "buys_5m": 40,
        "sells_5m": 10,
        "buys_1h": 150,
        "sells_1h": 80,
        "price_change_5m_pct": 6.0,
        "price_change_1h_pct": 18.0,
        "age_seconds": 3600.0,
    }


def _buy_decision_answers() -> dict[str, dict[str, Any]]:
    return {
        "t0_narrative_strength": {"type": "score", "score": 4.0, "confidence": 0.85},
        "t0_momentum_quality": {"type": "score", "score": 4.2, "confidence": 0.88},
        "t0_is_copycat_vamp": {"type": "noul", "noul": 0.15, "confidence": 0.90},
        "t0_action": {"type": "choice", "choice": "BUY", "confidence": 0.86},
    }


def test_e1_solana_pubkey_validation_rejects_malformed_addresses():
    """M-2: is_valid_pool_address requires a valid 32-byte Base58 Solana Pubkey distinct from mint."""
    assert is_valid_pool_address(POOL_PINNED, mint=MINT_LIFE) is True
    assert is_valid_pool_address(POOL_PINNED) is True

    invalid_cases = [
        None,
        "",
        "   ",
        f" {POOL_PINNED} ",
        f"{POOL_PINNED[:10]} {POOL_PINNED[10:]}",
        "unknown",
        "none",
        "null",
        "n/a",
        "!!!!",
        "PoolAddr0",
        "https://dexscreener.com/solana/pool",
        "0OIl" * 11,  # invalid Base58 chars
        "1" * 32,  # System Program ID (all-zero bytes)
        "2" * 10,  # valid Base58 but wrong decoded length (< 32 bytes)
        "2" * 50,  # valid Base58 but wrong decoded length (> 32 bytes)
        MINT_LIFE,  # token mint itself used as pool address
    ]
    for bad in invalid_cases:
        assert is_valid_pool_address(bad, mint=MINT_LIFE) is False, f"Expected invalid: {bad!r}"


@pytest.mark.asyncio
async def test_e1_pool_identity_full_lifecycle_watchlist_partial_and_final_settlement(tmp_path):
    """Tests 1–7 + M-3 + L-1A: Watchlist -> Alpha (atomic tuple) -> BUY -> PARTIAL_SELL -> SELL."""
    clear_narrative_cache()
    clear_buy_reservations()
    clear_sell_idempotency_guard()

    db = AgentDatabase(db_path=str(tmp_path / "e1_lifecycle.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(
        _env_file=None,
        EXECUTION_MODE="paper",
        CHART_ENABLED_STRATEGIES="BREAKOUT_IGNITION",
        WHALE_STRATEGY_ENABLED=False,
        PAPER_TX_FEE_SOL=0.001,
        PRE_EXECUTION_SAFETY_TTL_SECONDS=60.0,
    )
    now_dt = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
    dex_id = "pumpswap"

    # 1. Watchlist persistence preserves (pair_address, dex_id) and refuses silent overwrite
    c_life = _make_breakout_candles(18)
    cand = {
        "mint": MINT_LIFE,
        "symbol": "PLIFE",
        "pair_address": POOL_PINNED,
        "dex_id": dex_id,
        "added_at": (now_dt - timedelta(minutes=5)).isoformat(),
        "candles_5m": c_life,
        **_strong_flow_pair(),
    }
    db.upsert_watchlist_entry(
        mint=MINT_LIFE, symbol="PLIFE", pair_address=POOL_PINNED, dex_id=dex_id,
        candidate=cand, added_at=cand["added_at"], safety_passed=True,
    )
    db.mark_watchlist_safety_passed(MINT_LIFE, True, checked_at=now_dt.isoformat())
    # Upsert again with a competing pool & different DEX -> original watchlist identity must remain intact
    db.upsert_watchlist_entry(
        mint=MINT_LIFE, symbol="PLIFE", pair_address=POOL_COMPETING, dex_id="raydium",
        candidate={**cand, "pair_address": POOL_COMPETING, "dex_id": "raydium"},
        added_at=cand["added_at"],
    )
    wl = db.get_watchlist()
    assert len(wl) == 1
    assert wl[0]["pair_address"] == POOL_PINNED
    assert wl[0]["dex_id"] == dex_id

    # 2 & 3 (M-3). Even if refreshed candidate disagrees with pinned watchlist (POOL_COMPETING, raydium),
    # alpha_node preserves atomic watchlist tuple (POOL_PINNED, pumpswap) without mixing DEX IDs.
    drifted_cand = {
        **cand,
        "pair_address": POOL_COMPETING,
        "dex_id": "raydium",
        "_ohlcv_5m_result": __import__("tests.test_e3b_cache_provenance", fromlist=["make_verified_ohlcv_result"]).make_verified_ohlcv_result(c_life, last_ok_at=now_dt.timestamp()),
    }
    out_alpha = await alpha_node(
        {"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "safe_candidates": [drifted_cand]},
        config=cfg, db=db, decision_client=_StubDecision(_buy_decision_answers()), now=now_dt,  # type: ignore[arg-type]
    )
    pending = out_alpha["pending_order"]
    assert pending is not None
    assert pending["strategy"] == "BREAKOUT_IGNITION"
    assert pending["pair_address"] == POOL_PINNED
    assert pending["dex_id"] == dex_id

    # 4, 5, 6, 7 & L-1A. Executor BUY -> ActivePosition -> PARTIAL_SELL -> Final SELL
    def jup_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/tokens/v2/search" in url:
            return httpx.Response(200, json=[{"id": MINT_LIFE, "audit": {"isSus": False}}])
        if "rugcheck.xyz" in url:
            return httpx.Response(200, json={"rugged": False, "markets": [{"lp": {"lpLockedPct": 100.0}}], "risks": []})
        if "/ultra/v1/shield" in url:
            return httpx.Response(200, json={"warnings": {MINT_LIFE: []}})
        if request.url.params.get("inputMint") == SOL_MINT:
            return httpx.Response(200, json={"outAmount": "1000000", "priceImpactPct": "-0.01", "transaction": None})
        if request.url.params.get("amount") == "500000":
            return httpx.Response(200, json={"outAmount": "290000000", "priceImpactPct": "-0.01", "transaction": None})
        return httpx.Response(200, json={"outAmount": "280000000", "priceImpactPct": "-0.01", "transaction": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(jup_handler)) as client:
        out_buy = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "daily_pnl_sol": 0.0, "pending_order": pending},
            config=cfg, db=db, http_client=client,
        )
        pos_dict = out_buy["chart_positions"][0]
        assert pos_dict["pair_address"] == POOL_PINNED
        assert pos_dict["dex_id"] == dex_id
        assert pos_dict["pool_identity_status"] == "PINNED"

        # Round-trip DB reload + tamper protection
        reloaded = db.get_state()["chart_positions"][0]
        pos_obj = ActivePosition.model_validate(reloaded)
        assert pos_obj.pair_address == POOL_PINNED
        assert pos_obj.dex_id == dex_id

        tampered = dict(reloaded)
        tampered["pair_address"] = POOL_TAMPERED
        tampered["dex_id"] = "meteora"
        merged = db.update_lane_positions_only(lane="chart", positions=[tampered])
        assert merged[0]["pair_address"] == POOL_PINNED
        assert merged[0]["dex_id"] == dex_id

        # Actual PARTIAL_SELL execution (50% scale-out)
        partial_order = PendingOrder(
            side="PARTIAL_SELL", mint=MINT_LIFE, symbol="PLIFE", opened_at=pos_obj.opened_at,
            amount_atomic=500_000, reason="PARTIAL_TP_50PCT (+15%)",
            target_state_after_fill=FocusState.IN_POSITION, strategy="BREAKOUT_IGNITION",
        ).model_dump()
        out_partial = await executor_node(
            {**db.get_state(), "pending_order": partial_order},
            config=cfg, db=db, http_client=client,
        )
        # Position remains open with original (pair_address, dex_id) intact
        assert out_partial["focus_state"] == FocusState.IN_POSITION.value
        assert len(out_partial["chart_positions"]) == 1
        runner = out_partial["chart_positions"][0]
        assert runner["partial_tp_taken"] is True
        assert runner["token_amount"] == 500_000
        assert runner["pair_address"] == POOL_PINNED
        assert runner["dex_id"] == dex_id

        # Subsequent final SELL retains the same original identity
        sell_order = PendingOrder(
            side="SELL", mint=MINT_LIFE, symbol="PLIFE", opened_at=runner["opened_at"],
            amount_atomic=runner["token_amount"], reason="TRAILING_TP",
            target_state_after_fill=FocusState.SCANNING, strategy="BREAKOUT_IGNITION",
        ).model_dump()
        await executor_node(
            {**db.get_state(), "pending_order": sell_order},
            config=cfg, db=db, http_client=client,
        )

    # Verify BUY, PARTIAL_SELL, and SELL in trades & action_log all preserve (POOL_PINNED, pumpswap)
    recent_trades = db.get_recent_trades(limit=10)
    assert [t["side"] for t in reversed(recent_trades)] == ["BUY", "PARTIAL_SELL", "SELL"]
    assert all(t["pair_address"] == POOL_PINNED and t["dex_id"] == dex_id for t in recent_trades)

    actions = db.get_action_log(limit=20)
    for act_name in ("DECISION_1", "BUY", "PARTIAL_SELL", "SELL"):
        matched_rows = [a for a in actions if a["action"] == act_name]
        assert matched_rows, f"Missing action_log row for {act_name}"
        assert matched_rows[0]["pair_address"] == POOL_PINNED
        assert matched_rows[0]["dex_id"] == dex_id
    db.close()


@pytest.mark.asyncio
async def test_e1_missing_or_invalid_pair_address_rejects_breakout_ignition(tmp_path):
    """Test 8: Missing or malformed pair_address rejects BREAKOUT_IGNITION at both alpha and executor."""
    clear_narrative_cache()
    clear_buy_reservations()

    db = AgentDatabase(db_path=str(tmp_path / "e1_reject.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(
        _env_file=None,
        EXECUTION_MODE="paper",
        CHART_ENABLED_STRATEGIES="BREAKOUT_IGNITION",
        WHALE_STRATEGY_ENABLED=False,
    )
    now_dt = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)

    for bad_pair in ("", "   ", "unknown", "!!!!", "https://bad.url", "PoolNotBase58", MINT_NOPOOL):
        clear_narrative_cache()
        assert not is_valid_pool_address(bad_pair, mint=MINT_NOPOOL)
        c_bad = _make_breakout_candles(18)
        cand = {
            "mint": MINT_NOPOOL,
            "symbol": "NOPOOL",
            "pair_address": bad_pair,
            "added_at": (now_dt - timedelta(minutes=5)).isoformat(),
            "candles_5m": c_bad,
            "_ohlcv_5m_result": __import__("tests.test_e3b_cache_provenance", fromlist=["make_verified_ohlcv_result"]).make_verified_ohlcv_result(c_bad, last_ok_at=now_dt.timestamp()),
            **_strong_flow_pair(),
        }
        out = await alpha_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "safe_candidates": [cand]},
            config=cfg, db=db, decision_client=_StubDecision(_buy_decision_answers()), now=now_dt,  # type: ignore[arg-type]
        )
        assert out["pending_order"] is None
        assert out["alpha_decisions"][0]["approved"] is False
        assert "MISSING_OR_INVALID_POOL_IDENTITY" in out["alpha_decisions"][0]["rationale"]

    actions = db.get_action_log(limit=20)
    assert any("MISSING_OR_INVALID_POOL_IDENTITY" in str(a.get("reason") or "") for a in actions)

    # Executor defense-in-depth rejects both None and malformed strings
    for bad_exec_pair in (None, "!!!!", "PoolShort"):
        bad_order = PendingOrder(
            side="BUY", mint=MINT_NOPOOL, symbol="NOPOOL", pair_address=bad_exec_pair,
            amount_atomic=int(0.5 * LAMPORTS_PER_SOL), reason="DECISION_1_ENTRY",
            target_state_after_fill=FocusState.IN_POSITION, strategy="BREAKOUT_IGNITION",
        ).model_dump()
        out_ex = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "pending_order": bad_order},
            config=cfg, db=db,
        )
        assert out_ex["pending_order"] is None
    assert db.get_state()["chart_positions"] == []
    db.close()


def test_e1_historical_null_identities_migration_idempotency_and_exports(tmp_path):
    """Tests 9, 10, 16, 17: Historical NULL identities readable, never fabricated, migrations idempotent, CSV works."""
    db_file = tmp_path / "legacy_pre_e1.duckdb"
    conn = duckdb.connect(str(db_file))
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY, timestamp VARCHAR NOT NULL, mode VARCHAR NOT NULL,
            side VARCHAR NOT NULL, mint VARCHAR NOT NULL, symbol VARCHAR NOT NULL,
            in_amount BIGINT NOT NULL, out_amount BIGINT NOT NULL, sol_value DOUBLE NOT NULL,
            pnl_sol DOUBLE, pnl_pct DOUBLE, reason VARCHAR NOT NULL, tx_signature VARCHAR,
            rationale VARCHAR, strategy VARCHAR DEFAULT 'SCALPER',
            triggering_wallets_json VARCHAR DEFAULT '[]', opened_at VARCHAR, jupiter_price_impact_raw VARCHAR
        )
    """)
    conn.execute("""
        INSERT INTO trades VALUES (
            1, '2026-11-26T05:00:00+00:00', 'paper', 'BUY', 'MintLegacy111111111111111111111111111111',
            'OLD', 500000000, 1000000, 0.5, NULL, NULL, 'DECISION_1_ENTRY', 'paper-buy-legacy',
            'old trade', 'BREAKOUT_IGNITION', '[]', '2026-11-26T05:00:00+00:00', '-0.01'
        )
    """)
    conn.execute("""
        CREATE TABLE active_watchlist (
            mint VARCHAR PRIMARY KEY, symbol VARCHAR NOT NULL, pair_address VARCHAR DEFAULT '',
            added_at VARCHAR NOT NULL, candidate_json VARCHAR NOT NULL
        )
    """)
    conn.execute(f"""
        INSERT INTO active_watchlist VALUES (
            'MintLegacyWL', 'OLDWL', '{POOL_LEGACY_WL}',
            '2026-11-26T05:00:00+00:00', '{{"mint":"MintLegacyWL","symbol":"OLDWL"}}'
        )
    """)
    conn.execute("""
        CREATE TABLE action_log (
            id INTEGER PRIMARY KEY, timestamp_utc VARCHAR NOT NULL, bot VARCHAR NOT NULL DEFAULT 'solana_meme',
            mode VARCHAR NOT NULL, action VARCHAR NOT NULL, symbol VARCHAR NOT NULL, asset_id VARCHAR NOT NULL,
            qty DOUBLE, notional DOUBLE, unit VARCHAR NOT NULL DEFAULT 'SOL', price_or_value DOUBLE,
            pnl_amount DOUBLE, pnl_pct DOUBLE, strategy VARCHAR, reason VARCHAR
        )
    """)
    conn.execute("""
        INSERT INTO action_log VALUES (
            1, '2026-11-26T05:00:00+00:00', 'solana_meme', 'paper', 'BUY', 'OLD',
            'MintLegacy111111111111111111111111111111', 1000000, 0.5, 'SOL', 0.5, NULL, NULL,
            'BREAKOUT_IGNITION', 'DECISION_1_ENTRY'
        )
    """)
    conn.close()

    db1 = AgentDatabase(db_path=str(db_file), starting_sol=1.0)
    db1.close()
    db2 = AgentDatabase(db_path=str(db_file), starting_sol=1.0)

    trades = db2.get_recent_trades(limit=5)
    assert len(trades) == 1
    assert trades[0]["pair_address"] is None
    assert trades[0]["dex_id"] is None

    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper")
    tc = TestClient(create_telemetry_app(config=cfg, db=db2))
    csv_resp = tc.get("/api/export/csv")
    assert csv_resp.status_code == 200
    rows = list(csv.DictReader(io.StringIO(csv_resp.text)))
    assert list(rows[0].keys()) == ACTION_LOG_COLUMNS
    assert rows[0]["pair_address"] == ""
    assert rows[0]["dex_id"] == ""

    legacy_pos = ActivePosition(
        mint="MintLegacy111111111111111111111111111111", symbol="OLD",
        entry_sol=0.5, token_amount=1_000_000, entry_liquidity_usd=50_000.0,
    )
    assert legacy_pos.pair_address is None
    assert legacy_pos.pool_identity_status == "UNKNOWN"
    b5, s5, _, liq = apply_dex_flow_and_pnl(
        legacy_pos,
        current_sol_value=0.51,
        flow={
            "all_solana_pairs": [{
                "chainId": "solana", "pairAddress": POOL_COMPETING,
                "baseToken": {"address": legacy_pos.mint},
                "liquidity": {"usd": 999_000.0}, "txns": {"m5": {"buys": 1, "sells": 99}},
            }]
        },
    )
    assert legacy_pos.pair_address is None
    assert legacy_pos.pool_identity_status == "UNKNOWN"
    assert legacy_pos.pool_observation_status == "UNKNOWN_POOL_IDENTITY"
    assert (b5, s5, liq) == (0, 0, 0.0)
    db2.close()


@pytest.mark.asyncio
async def test_e1_monitor_pins_stored_pool_and_rejects_mismatches_and_migrations():
    """Tests 11–15: Stored pool pinned (no highest-liq substitution), mismatches rejected, missing vs zero-liq vs zero-flow."""
    clear_price_backoff()
    cfg = AgentConfig(_env_file=None)

    def multi_pool_handler(request: httpx.Request) -> httpx.Response:
        if "/price/v3" in str(request.url):
            return httpx.Response(200, json={SOL_MINT: {"usdPrice": 200.0, "decimals": 9}, MINT_MULTI: {"usdPrice": 0.02, "decimals": 6}})
        return httpx.Response(
            200,
            json=[
                {
                    "chainId": "solana", "pairAddress": POOL_COMPETING, "dexId": "raydium",
                    "baseToken": {"address": MINT_MULTI}, "priceNative": "0.00010",
                    "liquidity": {"usd": 500_000.0}, "txns": {"m5": {"buys": 1, "sells": 99}},
                },
                {
                    "chainId": "solana", "pairAddress": POOL_PINNED, "dexId": "pumpswap",
                    "baseToken": {"address": MINT_MULTI}, "priceNative": "0.00010",
                    "liquidity": {"usd": 60_000.0}, "txns": {"m5": {"buys": 42, "sells": 8}},
                },
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(multi_pool_handler)) as client:
        _, dex_flow = await fetch_batched_sol_prices(
            [MINT_MULTI], cfg, client, now_ts=1000.0, pinned_pools={MINT_MULTI: POOL_PINNED}
        )
    obs = dex_flow[MINT_MULTI]
    assert obs["pool_status"] == "MATCHED"
    assert obs["flow_available"] is True
    assert obs["pair_address"] == POOL_PINNED
    assert obs["dex_id"] == "pumpswap"
    assert obs["buys_5m"] == 42 and obs["sells_5m"] == 8
    assert obs["liquidity_usd"] == 60_000.0

    # Pair mismatch rejected
    res_bad_pair = select_pinned_dexscreener_pair(
        [{
            "chainId": "solana", "pairAddress": POOL_WRONG,
            "baseToken": {"address": MINT_IMPOSTOR},
            "liquidity": {"usd": 80_000.0}, "txns": {"m5": {"buys": 10, "sells": 10}},
        }],
        expected_mint=MINT_MULTI, expected_pair_address=POOL_PINNED,
    )
    assert res_bad_pair["pool_status"] == "MISMATCHED_PAIR"
    assert res_bad_pair["flow_available"] is False

    # Mint mismatch on same pair_address rejected
    res_bad_mint = select_pinned_dexscreener_pair(
        [{
            "chainId": "solana", "pairAddress": POOL_PINNED,
            "baseToken": {"address": MINT_IMPOSTOR},
            "liquidity": {"usd": 80_000.0}, "txns": {"m5": {"buys": 10, "sells": 10}},
        }],
        expected_mint=MINT_MULTI, expected_pair_address=POOL_PINNED,
    )
    assert res_bad_mint["pool_status"] == "MISMATCHED_MINT"
    assert res_bad_mint["flow_available"] is False

    # Original pool migrated/missing -> no silent substitution
    pos = ActivePosition(
        mint=MINT_MULTI, symbol="MULTI", pair_address=POOL_PINNED, dex_id="pumpswap",
        entry_sol=0.5, token_amount=1_000_000, entry_liquidity_usd=60_000.0, last_liquidity_usd=60_000.0,
    )
    b5_m, s5_m, _, liq_m = apply_dex_flow_and_pnl(
        pos, current_sol_value=0.50,
        flow={
            "all_solana_pairs": [{
                "chainId": "solana", "pairAddress": POOL_COMPETING, "dexId": "raydium",
                "baseToken": {"address": MINT_MULTI}, "liquidity": {"usd": 500_000.0},
                "txns": {"m5": {"buys": 0, "sells": 50}},
            }]
        },
    )
    assert pos.pair_address == POOL_PINNED
    assert pos.dex_id == "pumpswap"
    assert pos.pool_observation_status == "POOL_MIGRATED_OR_MISSING"
    assert pos.last_liquidity_usd == 60_000.0
    assert (b5_m, s5_m, liq_m) == (0, 0, 0.0)

    # Zero-flow, zero-liquidity, missing, and incomplete data distinguished
    res_zero_flow = select_pinned_dexscreener_pair(
        [{
            "chainId": "solana", "pairAddress": POOL_PINNED, "dexId": "pumpswap",
            "baseToken": {"address": MINT_MULTI}, "liquidity": {"usd": 60_000.0},
            "txns": {"m5": {"buys": 0, "sells": 0}},
        }],
        expected_mint=MINT_MULTI, expected_pair_address=POOL_PINNED,
    )
    assert res_zero_flow["pool_status"] == "MATCHED" and res_zero_flow["flow_available"] is True

    res_zero_liq = select_pinned_dexscreener_pair(
        [{
            "chainId": "solana", "pairAddress": POOL_PINNED, "dexId": "pumpswap",
            "baseToken": {"address": MINT_MULTI}, "liquidity": {"usd": 0.0},
            "txns": {"m5": {"buys": 0, "sells": 5}},
        }],
        expected_mint=MINT_MULTI, expected_pair_address=POOL_PINNED,
    )
    assert res_zero_liq["pool_status"] == "ZERO_LIQUIDITY" and res_zero_liq["flow_available"] is True

    res_missing = select_pinned_dexscreener_pair([], expected_mint=MINT_MULTI, expected_pair_address=POOL_PINNED)
    assert res_missing["pool_status"] == "POOL_UNAVAILABLE" and res_missing["flow_available"] is False

    res_incomplete = select_pinned_dexscreener_pair(
        [{
            "chainId": "solana", "pairAddress": POOL_PINNED, "dexId": "pumpswap",
            "baseToken": {"address": MINT_MULTI}, "liquidity": {"usd": 60_000.0},
        }],
        expected_mint=MINT_MULTI, expected_pair_address=POOL_PINNED,
    )
    assert res_incomplete["pool_status"] == "INCOMPLETE_DATA" and res_incomplete["flow_available"] is False


@pytest.mark.asyncio
async def test_e1_position_monitor_multi_pool_jupiter_miss_and_reentry_pinning(tmp_path):
    """M-1 + L-1B: End-to-end position_monitor_node with Jupiter miss + competing higher-liq pool."""
    clear_price_backoff()
    db = AgentDatabase(db_path=str(tmp_path / "e1_monitor_multipool.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper")
    now_dt = datetime(2026, 11, 26, 13, 0, 0, tzinfo=timezone.utc)

    # 1. Open position pinned to POOL_PINNED; Jupiter /price/v3 omits MINT_MULTI.
    # Competing POOL_COMPETING has higher liquidity ($500k) and an extreme dump priceNative (0.00010 -> -80%!) + 99% sells.
    # Pinned POOL_PINNED has priceNative 0.00052 (+4% -> 0.52 SOL), $65k liq, and 80% buys.
    open_pos = ActivePosition(
        mint=MINT_MULTI, symbol="MULTI", pair_address=POOL_PINNED, dex_id="pumpswap",
        entry_sol=0.50, token_amount=1_000_000_000, decimals=6,
        entry_liquidity_usd=65_000.0, current_sol_value=0.50, peak_sol_value=0.50,
        reference_token_amount=1_000_000_000, strategy="BREAKOUT_IGNITION",
        opened_at=now_dt.isoformat(),
    ).model_dump()

    def multi_miss_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/price/v3" in url:
            # Jupiter misses MINT_MULTI (only returns SOL)
            return httpx.Response(200, json={SOL_MINT: {"usdPrice": 200.0, "decimals": 9}})
        if "/tokens/v1/solana/" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": "solana", "pairAddress": POOL_COMPETING, "dexId": "raydium",
                        "baseToken": {"address": MINT_MULTI}, "priceNative": "0.00010",
                        "liquidity": {"usd": 500_000.0}, "txns": {"m5": {"buys": 1, "sells": 99}},
                    },
                    {
                        "chainId": "solana", "pairAddress": POOL_PINNED, "dexId": "pumpswap",
                        "baseToken": {"address": MINT_MULTI}, "priceNative": "0.00052",
                        "liquidity": {"usd": 65_000.0}, "txns": {"m5": {"buys": 40, "sells": 10}},
                    },
                ],
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(multi_miss_handler)) as client:
        mon_out = await position_monitor_node(
            {
                "focus_state": FocusState.IN_POSITION.value,
                "sol_balance": 1.5,
                "daily_pnl_sol": 0.0,
                "active_position": open_pos,
                "chart_positions": [open_pos],
            },
            config=cfg, db=db, decision_client=_StubDecision({}), http_client=client, now=now_dt,  # type: ignore[arg-type]
        )

    # Must HOLD using POOL_PINNED's priceNative (0.52 SOL), liquidity ($65k), and 80% buys (ignoring POOL_COMPETING!)
    assert mon_out["monitor_event"] == "HOLD_IN_POSITION"
    cp = mon_out["chart_positions"][0]
    assert cp["pair_address"] == POOL_PINNED
    assert cp["dex_id"] == "pumpswap"
    assert cp["current_sol_value"] == pytest.approx(0.52, rel=1e-4)
    assert cp["last_liquidity_usd"] == pytest.approx(65_000.0, rel=1e-4)
    assert cp["last_5m_buy_ratio"] == pytest.approx(0.80, rel=1e-3)
    assert cp["last_price_source"] == "dexscreener_native"

    # 2. WATCHING_REENTRY pinned to POOL_PINNED with POOL_PINNED absent (only POOL_COMPETING present):
    # Competing pool has bullish curl/flow, but missing pinned pool must NOT substitute POOL_COMPETING or trigger reentry!
    watch_pos = ActivePosition(
        mint=MINT_MULTI, symbol="MULTI", pair_address=POOL_PINNED, dex_id="pumpswap",
        entry_sol=0.50, token_amount=0, decimals=6,
        entry_liquidity_usd=65_000.0, current_sol_value=0.48, peak_sol_value=0.65,
        run_peak_sol=0.65, retrace_low_sol=0.48, reference_token_amount=1_000_000_000,
        watch_started_at=now_dt.isoformat(), strategy="BREAKOUT_IGNITION",
    ).model_dump()

    def missing_pinned_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/price/v3" in url:
            return httpx.Response(200, json={SOL_MINT: {"usdPrice": 200.0, "decimals": 9}})
        if "/swap/v2/order" in url:
            return httpx.Response(200, json={"outAmount": "520000000", "transaction": None})
        if "/tokens/v1/solana/" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "chainId": "solana", "pairAddress": POOL_COMPETING, "dexId": "raydium",
                        "baseToken": {"address": MINT_MULTI}, "priceNative": "0.00055",
                        "liquidity": {"usd": 500_000.0},
                        "volume": {"h1": 300_000.0, "m5": 60_000.0},
                        "priceChange": {"h1": 25.0, "m5": 8.0},
                        "txns": {"m5": {"buys": 80, "sells": 10}},
                    }
                ],
            )
        return httpx.Response(404)

    reentry_dec = _StubDecision({
        "leg_2_momentum": {"type": "score", "score": 4.5, "confidence": 0.90},
        "is_dead_cat_bounce": {"type": "noul", "noul": 0.10, "confidence": 0.90},
    })
    async with httpx.AsyncClient(transport=httpx.MockTransport(missing_pinned_handler)) as client:
        watch_out = await position_monitor_node(
            {
                "focus_state": FocusState.WATCHING_REENTRY.value,
                "sol_balance": 1.5,
                "daily_pnl_sol": 0.0,
                "active_position": watch_pos,
            },
            config=cfg, db=db, decision_client=reentry_dec, http_client=client, now=now_dt,  # type: ignore[arg-type]
        )

    # Must NOT substitute POOL_COMPETING for reentry scoring or fallback price
    assert watch_out["pending_order"] is None
    assert watch_out["active_position"]["pair_address"] == POOL_PINNED
    assert watch_out["active_position"]["pool_observation_status"] == "POOL_MIGRATED_OR_MISSING"
    db.close()
