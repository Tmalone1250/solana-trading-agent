"""Directive 22 Verification Suite: WATCHING_REENTRY Exodus Confirmation, Warm-Up & Pool-Pinning Integration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from batched_price_monitor import clear_price_backoff
from config import SOL_MINT, AgentConfig
from db import AgentDatabase
from position_monitor_node import position_monitor_node
from state import ActivePosition, FocusState

POOL_PINNED = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"
POOL_COMPETING = "8qbHbw2BbbTHBW1sbeqakYXVKRQM8Ne7pLK7m6CVfeR"
MINT_W = "p2Yicb86aZig616Eav2VWG9vuXR5mEqhtzshZYBxzsV"


class _NoopDecision:
    async def decide(self, **_kwargs: Any) -> dict[str, Any]:
        return {}


def _mk_watcher(
    *,
    opened_at: str | None,
    now_dt: datetime,
    entry_liq: float = 100_000.0,
) -> dict[str, Any]:
    return ActivePosition(
        mint=MINT_W,
        symbol="WRE",
        pair_address=POOL_PINNED,
        dex_id="pumpswap",
        entry_sol=0.50,
        token_amount=0,
        decimals=6,
        entry_liquidity_usd=entry_liq,
        current_sol_value=0.55,
        peak_sol_value=0.60,
        run_peak_sol=0.60,
        retrace_low_sol=0.55,
        reference_token_amount=1_000_000_000,
        opened_at=opened_at,
        watch_started_at=now_dt.isoformat(),
        strategy="BREAKOUT_IGNITION",
    ).model_dump()


def _make_client(payload_fn):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/price/v3" in url:
            return httpx.Response(200, json={SOL_MINT: {"usdPrice": 200.0, "decimals": 9}, MINT_W: {"usdPrice": 0.11, "decimals": 6}})
        if "/tokens/v1/solana/" in url:
            return httpx.Response(200, json=payload_fn())
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_watching_reentry_confirmation_persistence_duplicate_reset_and_abandon(tmp_path):
    """Tests 1–7: 1st tick confirming, DB persistence, duplicate-tick guard, clear reset, unavailable reset, low-sample, 2nd tick abandon."""
    clear_price_backoff()
    db = AgentDatabase(db_path=str(tmp_path / "watch_confirm.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(
        _env_file=None, EXECUTION_MODE="paper",
        MIN_EXODUS_TXNS_5M=10, EXODUS_CONFIRMATION_TICKS=2,
        EXODUS_FLOW_WARMUP_SECONDS=300.0, MONITOR_POLL_SECONDS=5.0,
    )
    t0 = datetime(2026, 11, 26, 16, 0, 0, tzinfo=timezone.utc)
    watcher = _mk_watcher(opened_at=(t0 - timedelta(minutes=10)).isoformat(), now_dt=t0)
    db.save_state(focus_state=FocusState.WATCHING_REENTRY.value, sol_balance=1.5, daily_pnl_sol=0.0, active_position=watcher)

    dex_payload: dict[str, Any] = {"pairs": []}

    async with _make_client(lambda: dex_payload["pairs"]) as client:
        # 7. Insufficient sample (1 sell, 0 buys) -> does not abandon or arm confirmation
        dex_payload["pairs"] = [{
            "chainId": "solana", "pairAddress": POOL_PINNED, "dexId": "pumpswap",
            "baseToken": {"address": MINT_W}, "priceNative": "0.00055",
            "liquidity": {"usd": 100_000.0}, "txns": {"m5": {"buys": 0, "sells": 1}},
        }]
        r_low = await position_monitor_node(db.get_state(), config=cfg, db=db, decision_client=_NoopDecision(), http_client=client, now=t0)  # type: ignore[arg-type]
        assert r_low["monitor_event"] == "WATCHING_REENTRY"
        assert r_low["active_position"]["exodus_confirm_count"] == 0
        assert r_low["active_position"]["last_exodus_status"] == "EXODUS_FLOW_INSUFFICIENT_SAMPLE"

        # 1 & 2. First qualifying sell-flow observation -> continues watching (1/2) and persists in DB
        dex_payload["pairs"] = [{
            "chainId": "solana", "pairAddress": POOL_PINNED, "dexId": "pumpswap",
            "baseToken": {"address": MINT_W}, "priceNative": "0.00055",
            "liquidity": {"usd": 100_000.0}, "txns": {"m5": {"buys": 3, "sells": 17}},
        }]
        r1 = await position_monitor_node(db.get_state(), config=cfg, db=db, decision_client=_NoopDecision(), http_client=client, now=t0 + timedelta(seconds=6))  # type: ignore[arg-type]
        assert r1["monitor_event"] == "WATCHING_REENTRY"
        st1 = db.get_state()
        assert st1["active_position"]["exodus_confirm_count"] == 1
        assert st1["active_position"]["last_exodus_status"] == "EXODUS_FLOW_CONFIRMING"

        # 4. Duplicate invocation inside MONITOR_POLL_SECONDS (+1s) -> must NOT count twice or abandon
        r_dup = await position_monitor_node(st1, config=cfg, db=db, decision_client=_NoopDecision(), http_client=client, now=t0 + timedelta(seconds=7))  # type: ignore[arg-type]
        assert r_dup["monitor_event"] == "WATCHING_REENTRY"
        assert db.get_state()["active_position"]["exodus_confirm_count"] == 1

        # 5. Cleared signal (+12s) -> resets confirmation to 0
        dex_payload["pairs"] = [{
            "chainId": "solana", "pairAddress": POOL_PINNED, "dexId": "pumpswap",
            "baseToken": {"address": MINT_W}, "priceNative": "0.00055",
            "liquidity": {"usd": 100_000.0}, "txns": {"m5": {"buys": 14, "sells": 6}},
        }]
        r_clr = await position_monitor_node(db.get_state(), config=cfg, db=db, decision_client=_NoopDecision(), http_client=client, now=t0 + timedelta(seconds=12))  # type: ignore[arg-type]
        assert r_clr["monitor_event"] == "WATCHING_REENTRY"
        assert db.get_state()["active_position"]["exodus_confirm_count"] == 0
        assert db.get_state()["active_position"]["last_exodus_status"] == "EXODUS_CLEAR"

        # 6. Re-arm 1/2 (+18s), then unavailable flow (+24s) -> resets confirmation without abandonment
        dex_payload["pairs"] = [{
            "chainId": "solana", "pairAddress": POOL_PINNED, "dexId": "pumpswap",
            "baseToken": {"address": MINT_W}, "priceNative": "0.00055",
            "liquidity": {"usd": 100_000.0}, "txns": {"m5": {"buys": 3, "sells": 17}},
        }]
        await position_monitor_node(db.get_state(), config=cfg, db=db, decision_client=_NoopDecision(), http_client=client, now=t0 + timedelta(seconds=18))  # type: ignore[arg-type]
        assert db.get_state()["active_position"]["exodus_confirm_count"] == 1

        dex_payload["pairs"] = []
        r_unavail = await position_monitor_node(db.get_state(), config=cfg, db=db, decision_client=_NoopDecision(), http_client=client, now=t0 + timedelta(seconds=24))  # type: ignore[arg-type]
        assert r_unavail["monitor_event"] == "WATCHING_REENTRY"
        assert db.get_state()["active_position"]["exodus_confirm_count"] == 0
        assert db.get_state()["active_position"]["last_exodus_status"] == "EXODUS_FLOW_UNAVAILABLE"

        # 3. Two spaced qualifying ticks (+30s, +36s) -> 2nd tick abandons watcher for confirmed Exodus
        dex_payload["pairs"] = [{
            "chainId": "solana", "pairAddress": POOL_PINNED, "dexId": "pumpswap",
            "baseToken": {"address": MINT_W}, "priceNative": "0.00055",
            "liquidity": {"usd": 100_000.0}, "txns": {"m5": {"buys": 3, "sells": 17}},
        }]
        r_a1 = await position_monitor_node(db.get_state(), config=cfg, db=db, decision_client=_NoopDecision(), http_client=client, now=t0 + timedelta(seconds=30))  # type: ignore[arg-type]
        assert r_a1["monitor_event"] == "WATCHING_REENTRY"
        assert db.get_state()["active_position"]["exodus_confirm_count"] == 1

        r_a2 = await position_monitor_node(db.get_state(), config=cfg, db=db, decision_client=_NoopDecision(), http_client=client, now=t0 + timedelta(seconds=36))  # type: ignore[arg-type]
        assert r_a2["focus_state"] == FocusState.SCANNING.value
        assert r_a2["active_position"] is None
        assert "EXODUS_SELL_FLOW" in r_a2["monitor_event"]
    db.close()


@pytest.mark.asyncio
async def test_watching_reentry_warmup_missing_opened_at_liq_drop_and_competing_pool(tmp_path):
    """Tests 8–11: 5m warm-up suppression, missing opened_at, immediate pinned liq drop during warm-up, competing pool ignored."""
    clear_price_backoff()
    db = AgentDatabase(db_path=str(tmp_path / "watch_warmup.duckdb"), starting_sol=2.0)
    cfg = AgentConfig(
        _env_file=None, EXECUTION_MODE="paper",
        MIN_EXODUS_TXNS_5M=10, EXODUS_CONFIRMATION_TICKS=1,
        EXODUS_FLOW_WARMUP_SECONDS=300.0, MONITOR_POLL_SECONDS=5.0,
    )
    t0 = datetime(2026, 11, 26, 16, 0, 0, tzinfo=timezone.utc)

    # 8. Within 5m warm-up (60s since opened_at) -> sell-flow Exodus suppressed
    w_warm = _mk_watcher(opened_at=(t0 - timedelta(seconds=60)).isoformat(), now_dt=t0)
    db.save_state(focus_state=FocusState.WATCHING_REENTRY.value, sol_balance=1.5, daily_pnl_sol=0.0, active_position=w_warm)
    dex_payload: dict[str, Any] = {
        "pairs": [{
            "chainId": "solana", "pairAddress": POOL_PINNED, "dexId": "pumpswap",
            "baseToken": {"address": MINT_W}, "priceNative": "0.00055",
            "liquidity": {"usd": 100_000.0}, "txns": {"m5": {"buys": 2, "sells": 25}},
        }]
    }
    async with _make_client(lambda: dex_payload["pairs"]) as client:
        r_warm = await position_monitor_node(db.get_state(), config=cfg, db=db, decision_client=_NoopDecision(), http_client=client, now=t0)  # type: ignore[arg-type]
        assert r_warm["monitor_event"] == "WATCHING_REENTRY"
        assert r_warm["active_position"]["last_exodus_status"] == "EXODUS_FLOW_WARMUP"

        # 9. Missing opened_at -> conservatively suppressed as EXODUS_FLOW_WARMUP
        w_no_open = _mk_watcher(opened_at=None, now_dt=t0)
        db.save_state(focus_state=FocusState.WATCHING_REENTRY.value, sol_balance=1.5, daily_pnl_sol=0.0, active_position=w_no_open)
        r_no_open = await position_monitor_node(db.get_state(), config=cfg, db=db, decision_client=_NoopDecision(), http_client=client, now=t0)  # type: ignore[arg-type]
        assert r_no_open["monitor_event"] == "WATCHING_REENTRY"
        assert r_no_open["active_position"]["last_exodus_status"] == "EXODUS_FLOW_WARMUP"

        # 11. Competing pool has heavy sell flow & 0 liquidity while pinned pool is healthy -> ignored
        w_aged = _mk_watcher(opened_at=(t0 - timedelta(minutes=10)).isoformat(), now_dt=t0)
        db.save_state(focus_state=FocusState.WATCHING_REENTRY.value, sol_balance=1.5, daily_pnl_sol=0.0, active_position=w_aged)
        dex_payload["pairs"] = [
            {
                "chainId": "solana", "pairAddress": POOL_COMPETING, "dexId": "raydium",
                "baseToken": {"address": MINT_W}, "priceNative": "0.00055",
                "liquidity": {"usd": 0.0}, "txns": {"m5": {"buys": 0, "sells": 100}},
            },
            {
                "chainId": "solana", "pairAddress": POOL_PINNED, "dexId": "pumpswap",
                "baseToken": {"address": MINT_W}, "priceNative": "0.00055",
                "liquidity": {"usd": 100_000.0}, "txns": {"m5": {"buys": 15, "sells": 5}},
            },
        ]
        r_comp = await position_monitor_node(db.get_state(), config=cfg, db=db, decision_client=_NoopDecision(), http_client=client, now=t0)  # type: ignore[arg-type]
        assert r_comp["monitor_event"] == "WATCHING_REENTRY"
        assert r_comp["active_position"]["last_exodus_status"] == "EXODUS_CLEAR"

        # 10. Valid pinned-pool liquidity drop during warm-up -> abandons immediately regardless of warm-up
        db.save_state(focus_state=FocusState.WATCHING_REENTRY.value, sol_balance=1.5, daily_pnl_sol=0.0, active_position=w_warm)
        dex_payload["pairs"] = [{
            "chainId": "solana", "pairAddress": POOL_PINNED, "dexId": "pumpswap",
            "baseToken": {"address": MINT_W}, "priceNative": "0.00055",
            "liquidity": {"usd": 85_000.0}, "txns": {"m5": {"buys": 15, "sells": 5}},
        }]
        r_liq = await position_monitor_node(db.get_state(), config=cfg, db=db, decision_client=_NoopDecision(), http_client=client, now=t0)  # type: ignore[arg-type]
        assert r_liq["focus_state"] == FocusState.SCANNING.value
        assert r_liq["active_position"] is None
        assert "EXODUS_LIQ_DROP" in r_liq["monitor_event"]
    db.close()
