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
from state import FocusState
from telemetry_api import create_telemetry_app
from x_discovery_worker import run_x_discovery_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("solana-meme-agent")


async def run_autonomous_loop(config, db: AgentDatabase) -> None:
    """Continuously tick the compiled LangGraph 3-State Focus Machine."""
    decision_client = GeminiDecisionClient(
        api_key=config.GEMINI_API_KEY,
        model=config.DECISION_MODEL,
    )
    async with httpx.AsyncClient() as http_client:
        compiled = build_agent_graph(
            config=config,
            db=db,
            decision_client=decision_client,
            http_client=http_client,
        )
        logger.info(
            "Starting solana-meme-agent | mode=%s | lane=%s | trade_size=%.2f SOL",
            config.EXECUTION_MODE,
            config.HUNTING_LANE,
            config.TRADE_SIZE_SOL,
        )
        while True:
            st = db.get_state()
            initial_state = {
                "focus_state": st["focus_state"],
                "sol_balance": st["sol_balance"],
                "daily_start_sol": st["daily_start_sol"],
                "daily_pnl_sol": st["daily_pnl_sol"],
                "active_position": st["active_position"],
                "last_decision_rationale": st["last_decision_rationale"],
            }
            try:
                result = await compiled.ainvoke(initial_state)
                focus_state = result.get("focus_state", st["focus_state"])
            except Exception as exc:
                logger.exception("Error during LangGraph tick: %s", exc)
                focus_state = st["focus_state"]

            sleep_sec = (
                config.SCAN_POLL_SECONDS
                if focus_state == FocusState.SCANNING.value
                else config.MONITOR_POLL_SECONDS
            )
            await asyncio.sleep(sleep_sec)


async def main() -> None:
    config = load_config()
    db = AgentDatabase(
        db_path=config.DUCKDB_PATH,
        starting_sol=config.PAPER_STARTING_SOL,
    )
    app = create_telemetry_app(config=config, db=db)

    uv_config = uvicorn.Config(
        app,
        host=config.TELEMETRY_HOST,
        port=config.TELEMETRY_PORT,
        log_level="info",
    )
    server = uvicorn.Server(uv_config)

    agent_task = asyncio.create_task(run_autonomous_loop(config, db))
    x_task = (
        asyncio.create_task(run_x_discovery_loop(config, db))
        if config.ENABLE_X_DISCOVERY
        else None
    )
    try:
        await server.serve()
    finally:
        agent_task.cancel()
        if x_task is not None:
            x_task.cancel()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
