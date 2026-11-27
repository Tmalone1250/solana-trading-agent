"""Directive 16 (E1) Verification Suite: Pool Identity Continuity & Database Persistence."""

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
from state import ActivePosition, FocusState, PendingOrder, is_valid_pool_address
from state_ledger import clear_buy_reservations
from telemetry_api import create_telemetry_app


class _StubDecision:
    def __init__(self, answers: dict[str, dict[str, Any]]):
        self._answers = answers

    async def decide(self, *, input_text: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return self._answers


def _make_breakout_candles(count: int = 18) -> list[dict[str, float]]:
    """Construct realistic 5m candles with alternating pullbacks (RSI ~60) + tight coil + breakout."""
    base_ts = 1_795_700_000.0
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
    # Breakout candle: closes cleanly above 6-bar swing high, strong body, 2.5x volume
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


@pytest.mark.asyncio
async def test_e1_pool_identity_full_lifecycle_watchlist_to_settlement(tmp_path):
    """Tests 1–7: Watchlist -> Alpha -> PendingOrder -> ActivePosition -> Ser/Deser -> Trades -> ActionLog."""
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
    )
    now_dt = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
    mint = "MintPoolLife111111111111111111111111111111"
    pair_addr = "PoolPinned11111111111111111111111111111111"
    dex_id = "pumpswap"

    # 1. Watchlist persistence & retrieval preserves pair_address + dex_id and refuses silent overwrite
    cand = {
        "mint": mint,
        "symbol": "PLIFE",
        "pair_address": pair_addr,
        "dex_id": dex_id,
        "added_at": (now_dt - timedelta(minutes=5)).isoformat(),
        "candles_5m": _make_breakout_candles(18),
        **_strong_flow_pair(),
    }
    db.upsert_watchlist_entry(
        mint=mint, symbol="PLIFE", pair_address=pair_addr, dex_id=dex_id,
        candidate=cand, added_at=cand["added_at"],
    )
    # Upsert again with a different pool address -> original pool identity must remain preserved
    db.upsert_watchlist_entry(
        mint=mint, symbol="PLIFE", pair_address="PoolOther999999999999999999999999999999999",
        dex_id="raydium", candidate={**cand, "pair_address": "PoolOther999999999999999999999999999999999"},
        added_at=cand["added_at"],
    )
    wl = db.get_watchlist()
    assert len(wl) == 1
    assert wl[0]["pair_address"] == pair_addr
    assert wl[0]["dex_id"] == dex_id

    # 2 & 3. Entry approval reaches alpha_node and PendingOrder stores pair_address + dex_id
    out_alpha = await alpha_node(
        {"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "safe_candidates": [cand]},
        config=cfg, db=db, decision_client=_StubDecision(_buy_decision_answers()), now=now_dt,  # type: ignore[arg-type]
    )
    pending = out_alpha["pending_order"]
    assert pending is not None
    assert pending["strategy"] == "BREAKOUT_IGNITION"
    assert pending["pair_address"] == pair_addr
    assert pending["dex_id"] == dex_id

    # 4, 5, 6, 7. Executor BUY -> ActivePosition inherits identity -> ser/deser -> PARTIAL_SELL -> SELL
    def jup_handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("inputMint") == SOL_MINT:
            return httpx.Response(200, json={"outAmount": "1000000", "priceImpactPct": "-0.01", "transaction": None})
        return httpx.Response(200, json={"outAmount": "550000000", "priceImpactPct": "-0.01", "transaction": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(jup_handler)) as client:
        out_buy = await executor_node(
            {"focus_state": FocusState.SCANNING.value, "sol_balance": 2.0, "daily_pnl_sol": 0.0, "pending_order": pending},
            config=cfg, db=db, http_client=client,
        )
        pos_dict = out_buy["chart_positions"][0]
        assert pos_dict["pair_address"] == pair_addr
        assert pos_dict["dex_id"] == dex_id
        assert pos_dict["pool_identity_status"] == "PINNED"

        # Verify round-trip DB serialization/deserialization and protection against silent overwrite
        reloaded = db.get_state()["chart_positions"][0]
        pos_obj = ActivePosition.model_validate(reloaded)
        assert pos_obj.pair_address == pair_addr
        assert pos_obj.dex_id == dex_id
        assert pos_obj.pool_identity_status == "PINNED"

        # Attempt to overwrite pair_address via update_lane_positions_only -> original preserved
        tampered = dict(reloaded)
        tampered["pair_address"] = "PoolTampered11111111111111111111111111111"
        tampered["dex_id"] = "meteora"
        merged = db.update_lane_positions_only(lane="chart", positions=[tampered])
        assert merged[0]["pair_address"] == pair_addr
        assert merged[0]["dex_id"] == dex_id

        # Exit SELL (even if exit PendingOrder omitted pair_address, executor inherits from ActivePosition)
        sell_order = PendingOrder(
            side="SELL", mint=mint, symbol="PLIFE", opened_at=pos_obj.opened_at,
            amount_atomic=pos_obj.token_amount, reason="TRAILING_TP",
            target_state_after_fill=FocusState.SCANNING, strategy="BREAKOUT_IGNITION",
        ).model_dump()
        await executor_node(
            {**db.get_state(), "pending_order": sell_order},
            config=cfg, db=db, http_client=client,
        )

    # Verify trades & action_log retain pair_address and dex_id across BUY and SELL
    recent_trades = db.get_recent_trades(limit=10)
    assert len(recent_trades) == 2
    assert all(t["pair_address"] == pair_addr and t["dex_id"] == dex_id for t in recent_trades)

    actions = db.get_action_log(limit=20)
    d1_rows = [a for a in actions if a["action"] == "DECISION_1"]
    buy_rows = [a for a in actions if a["action"] == "BUY"]
    sell_rows = [a for a in actions if a["action"] == "SELL"]
    assert d1_rows and d1_rows[0]["pair_address"] == pair_addr and d1_rows[0]["dex_id"] == dex_id
    assert buy_rows and buy_rows[0]["pair_address"] == pair_addr and buy_rows[0]["dex_id"] == dex_id
    assert sell_rows and sell_rows[0]["pair_address"] == pair_addr and sell_rows[0]["dex_id"] == dex_id
    db.close()


@pytest.mark.asyncio
async def test_e1_missing_or_invalid_pair_address_rejects_breakout_ignition(tmp_path):
    """Test 8: Missing or invalid pair_address rejects a new BREAKOUT_IGNITION entry at both alpha and executor."""
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
    mint = "MintNoPool11111111111111111111111111111111"

    for bad_pair in ("", "   ", "unknown", "none", mint):
        clear_narrative_cache()
        assert not is_valid_pool_address(bad_pair, mint=mint)
        cand = {
            "mint": mint,
            "symbol": "NOPOOL",
            "pair_address": bad_pair,
            "added_at": (now_dt - timedelta(minutes=5)).isoformat(),
            "candles_5m": _make_breakout_candles(18),
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

    # Executor-level defense-in-depth guard also rejects BREAKOUT_IGNITION BUY lacking pair_address
    bad_order = PendingOrder(
        side="BUY", mint=mint, symbol="NOPOOL", pair_address=None,
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
    # Build a legacy pre-E1 DuckDB without pair_address/dex_id on trades & action_log and without dex_id on watchlist
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
    conn.execute("""
        INSERT INTO active_watchlist VALUES (
            'MintLegacyWL', 'OLDWL', 'PoolOldWL111111111111111111111111111111',
            '2026-11-26T05:00:00+00:00', '{"mint":"MintLegacyWL","symbol":"OLDWL"}'
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

    # Initialize AgentDatabase TWICE to verify additive migration + idempotency
    db1 = AgentDatabase(db_path=str(db_file), starting_sol=1.0)
    db1.close()
    db2 = AgentDatabase(db_path=str(db_file), starting_sol=1.0)

    # Historical trade row remains intact with NULL pair_address/dex_id (never fabricated)
    trades = db2.get_recent_trades(limit=5)
    assert len(trades) == 1
    assert trades[0]["mint"] == "MintLegacy111111111111111111111111111111"
    assert trades[0]["pair_address"] is None
    assert trades[0]["dex_id"] is None

    # Historical action_log row remains intact with NULL pair_address/dex_id and exports cleanly via CSV
    cfg = AgentConfig(_env_file=None, EXECUTION_MODE="paper")
    tc = TestClient(create_telemetry_app(config=cfg, db=db2))
    csv_resp = tc.get("/api/export/csv")
    assert csv_resp.status_code == 200
    rows = list(csv.DictReader(io.StringIO(csv_resp.text)))
    assert list(rows[0].keys()) == ACTION_LOG_COLUMNS
    assert rows[0]["pair_address"] == ""
    assert rows[0]["dex_id"] == ""

    # Historical ActivePosition without pair_address loads as UNKNOWN and never adopts foreign pool flow
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
                "chainId": "solana", "pairAddress": "PoolUnrelated111111111111111111111111",
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
    mint = "MintMultiPool11111111111111111111111111111"
    pinned_pool = "PoolOriginal11111111111111111111111111111"
    higher_liq_pool = "PoolHigherLiq2222222222222222222222222222"

    # 11. Multiple pools exist: monitor must use pinned_pool, NOT higher_liq_pool
    def multi_pool_handler(request: httpx.Request) -> httpx.Response:
        if "/price/v3" in str(request.url):
            return httpx.Response(200, json={SOL_MINT: {"usdPrice": 200.0, "decimals": 9}, mint: {"usdPrice": 0.02, "decimals": 6}})
        return httpx.Response(
            200,
            json=[
                {
                    "chainId": "solana", "pairAddress": higher_liq_pool, "dexId": "raydium",
                    "baseToken": {"address": mint}, "priceNative": "0.00010",
                    "liquidity": {"usd": 500_000.0}, "txns": {"m5": {"buys": 1, "sells": 99}},
                },
                {
                    "chainId": "solana", "pairAddress": pinned_pool, "dexId": "pumpswap",
                    "baseToken": {"address": mint}, "priceNative": "0.00010",
                    "liquidity": {"usd": 60_000.0}, "txns": {"m5": {"buys": 42, "sells": 8}},
                },
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(multi_pool_handler)) as client:
        _, dex_flow = await fetch_batched_sol_prices(
            [mint], cfg, client, now_ts=1000.0, pinned_pools={mint: pinned_pool}
        )
    obs = dex_flow[mint]
    assert obs["pool_status"] == "MATCHED"
    assert obs["flow_available"] is True
    assert obs["pair_address"] == pinned_pool
    assert obs["dex_id"] == "pumpswap"
    assert obs["buys_5m"] == 42 and obs["sells_5m"] == 8
    assert obs["liquidity_usd"] == 60_000.0

    # 12. Provider response with mismatched pair_address is rejected (no silent substitution)
    res_bad_pair = select_pinned_dexscreener_pair(
        [{
            "chainId": "solana", "pairAddress": "PoolWrong99999999999999999999999999999999",
            "baseToken": {"address": "OtherMint11111111111111111111111111111111"},
            "liquidity": {"usd": 80_000.0}, "txns": {"m5": {"buys": 10, "sells": 10}},
        }],
        expected_mint=mint, expected_pair_address=pinned_pool,
    )
    assert res_bad_pair["pool_status"] == "MISMATCHED_PAIR"
    assert res_bad_pair["flow_available"] is False

    # 13. Provider response with mismatched token mint on same pair_address is rejected
    res_bad_mint = select_pinned_dexscreener_pair(
        [{
            "chainId": "solana", "pairAddress": pinned_pool,
            "baseToken": {"address": "MintImpostor11111111111111111111111111111"},
            "liquidity": {"usd": 80_000.0}, "txns": {"m5": {"buys": 10, "sells": 10}},
        }],
        expected_mint=mint, expected_pair_address=pinned_pool,
    )
    assert res_bad_mint["pool_status"] == "MISMATCHED_MINT"
    assert res_bad_mint["flow_available"] is False

    # 14. Original pool temporarily unavailable or migrated -> no silent substitution, original identity preserved
    pos = ActivePosition(
        mint=mint, symbol="MULTI", pair_address=pinned_pool, dex_id="pumpswap",
        entry_sol=0.5, token_amount=1_000_000, entry_liquidity_usd=60_000.0, last_liquidity_usd=60_000.0,
    )
    b5_m, s5_m, _, liq_m = apply_dex_flow_and_pnl(
        pos, current_sol_value=0.50,
        flow={
            "all_solana_pairs": [{
                "chainId": "solana", "pairAddress": higher_liq_pool, "dexId": "raydium",
                "baseToken": {"address": mint}, "liquidity": {"usd": 500_000.0},
                "txns": {"m5": {"buys": 0, "sells": 50}},
            }]
        },
    )
    assert pos.pair_address == pinned_pool
    assert pos.dex_id == "pumpswap"
    assert pos.pool_observation_status == "POOL_MIGRATED_OR_MISSING"
    assert pos.last_liquidity_usd == 60_000.0  # preserved, not overwritten
    assert (b5_m, s5_m, liq_m) == (0, 0, 0.0)

    # 15. Distinguish ZERO_LIQUIDITY, POOL_UNAVAILABLE, INCOMPLETE_DATA, and valid zero-transaction flow
    res_zero_flow = select_pinned_dexscreener_pair(
        [{
            "chainId": "solana", "pairAddress": pinned_pool, "dexId": "pumpswap",
            "baseToken": {"address": mint}, "liquidity": {"usd": 60_000.0},
            "txns": {"m5": {"buys": 0, "sells": 0}},
        }],
        expected_mint=mint, expected_pair_address=pinned_pool,
    )
    assert res_zero_flow["pool_status"] == "MATCHED"
    assert res_zero_flow["flow_available"] is True
    assert res_zero_flow["buys_5m"] == 0 and res_zero_flow["sells_5m"] == 0

    res_zero_liq = select_pinned_dexscreener_pair(
        [{
            "chainId": "solana", "pairAddress": pinned_pool, "dexId": "pumpswap",
            "baseToken": {"address": mint}, "liquidity": {"usd": 0.0},
            "txns": {"m5": {"buys": 0, "sells": 5}},
        }],
        expected_mint=mint, expected_pair_address=pinned_pool,
    )
    assert res_zero_liq["pool_status"] == "ZERO_LIQUIDITY"
    assert res_zero_liq["flow_available"] is True
    assert res_zero_liq["liquidity_usd"] == 0.0

    res_missing = select_pinned_dexscreener_pair([], expected_mint=mint, expected_pair_address=pinned_pool)
    assert res_missing["pool_status"] == "POOL_UNAVAILABLE"
    assert res_missing["flow_available"] is False
    assert res_missing["buys_5m"] is None and res_missing["liquidity_usd"] is None

    res_incomplete = select_pinned_dexscreener_pair(
        [{
            "chainId": "solana", "pairAddress": pinned_pool, "dexId": "pumpswap",
            "baseToken": {"address": mint}, "liquidity": {"usd": 60_000.0},
        }],
        expected_mint=mint, expected_pair_address=pinned_pool,
    )
    assert res_incomplete["pool_status"] == "INCOMPLETE_DATA"
    assert res_incomplete["flow_available"] is False
