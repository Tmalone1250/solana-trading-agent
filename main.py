"""Standalone autonomous entrypoint running the 3-State Focus Machine + Telemetry API."""

from __future__ import annotations

import asyncio
import logging

import httpx
import uvicorn

from config import load_config
from db import AgentDatabase
from decision_client import GeminiDecisionClient
from graph import build_agent_graph
from memory_guard import maybe_log_rss_memory
from state import FocusState
from telemetry_api import create_telemetry_app
from whale_lane import run_whale_lane_tick
from x_discovery_worker import run_x_discovery_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("solana-meme-agent")


async def run_autonomous_loop(
    config,
    db: AgentDatabase,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Continuously tick the compiled LangGraph 3-State Focus Machine."""
    decision_client = GeminiDecisionClient(
        api_key=config.GEMINI_API_KEY,
        model=config.DECISION_MODEL,
    )
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(
        follow_redirects=True,
        timeout=15.0,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    try:
        compiled = build_agent_graph(
            config=config,
            db=db,
            decision_client=decision_client,
            http_client=client,
        )
        logger.info(
            "Starting solana-meme-agent | mode=%s | lane=%s | trade_size=%.2f SOL",
            config.EXECUTION_MODE,
            config.HUNTING_LANE,
            config.TRADE_SIZE_SOL,
        )
        maybe_log_rss_memory(db=db, force=True)
        while True:
            maybe_log_rss_memory(db=db)
            st = db.get_state()
            initial_state = {
                "focus_state": st["focus_state"],
                "sol_balance": st["sol_balance"],
                "daily_start_sol": st["daily_start_sol"],
                "daily_pnl_sol": st["daily_pnl_sol"],
                "active_position": st["active_position"],
                "whale_position": st["whale_position"],
                "chart_positions": st["chart_positions"],
                "whale_positions": st["whale_positions"],
                "last_decision_rationale": st["last_decision_rationale"],
            }
            try:
                result = await compiled.ainvoke(initial_state)
                focus_state = result.get("focus_state", st["focus_state"])
                active_pos = result.get("active_position", st["active_position"])
            except Exception as exc:
                logger.exception("Error during LangGraph tick: %s", exc)
                focus_state = st["focus_state"]
                active_pos = st["active_position"]

            is_whale_scalp = (
                focus_state == FocusState.IN_POSITION.value
                and isinstance(active_pos, dict)
                and str(active_pos.get("strategy") or "").upper() == "WHALE_SCALP"
            )
            if focus_state == FocusState.SCANNING.value:
                sleep_sec = config.SCAN_POLL_SECONDS
            elif is_whale_scalp:
                sleep_sec = config.WHALE_MONITOR_POLL_SECONDS
            else:
                sleep_sec = config.MONITOR_POLL_SECONDS
            await asyncio.sleep(sleep_sec)
    finally:
        if owns_client:
            await client.aclose()


async def run_whale_lane_loop(
    config,
    db: AgentDatabase,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Whale lane independent loop (up to MAX_WHALE_POSITIONS=3): batched monitor + breakout-gated entry."""
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(
        follow_redirects=True,
        timeout=15.0,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    try:
        while True:
            try:
                res = await run_whale_lane_tick(config=config, db=db, http_client=client)
                has_open = bool(res.get("whale_positions") or res.get("whale_position"))
            except Exception as exc:
                logger.exception("Error during Whale Lane tick: %s", exc)
                has_open = False
            await asyncio.sleep(config.WHALE_MONITOR_POLL_SECONDS if has_open else 5.0)
    finally:
        if owns_client:
            await client.aclose()


async def main() -> None:
    config = load_config()
    db = AgentDatabase(
        db_path=config.DUCKDB_PATH,
        starting_sol=config.PAPER_STARTING_SOL,
    )
    shared_http_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=15.0,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    app = create_telemetry_app(config=config, db=db, http_client=shared_http_client)

    uv_config = uvicorn.Config(
        app,
        host=config.TELEMETRY_HOST,
        port=config.TELEMETRY_PORT,
        log_level="info",
    )
    server = uvicorn.Server(uv_config)

    agent_task = asyncio.create_task(run_autonomous_loop(config, db, http_client=shared_http_client))
    whale_task = (
        asyncio.create_task(run_whale_lane_loop(config, db, http_client=shared_http_client))
        if config.WHALE_STRATEGY_ENABLED
        else None
    )
    x_task = (
        asyncio.create_task(run_x_discovery_loop(config, db, http_client=shared_http_client))
        if config.ENABLE_X_DISCOVERY
        else None
    )
    try:
        await server.serve()
    finally:
        agent_task.cancel()
        if whale_task is not None:
            whale_task.cancel()
        if x_task is not None:
            x_task.cancel()
        await shared_http_client.aclose()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
