"""LangGraph StateGraph wiring the 3-State Focus Machine."""

from __future__ import annotations

from functools import partial
from typing import Optional

import httpx
from langgraph.graph import END, START, StateGraph

from alpha_node import alpha_node
from config import AgentConfig
from db import AgentDatabase
from decision_client import GeminiDecisionClient
from executor_node import executor_node
from position_monitor_node import position_monitor_node
from safety_gate_node import safety_gate_node
from scanner_node import scanner_node
from state import AgentGraphState, FocusState


def _route_entry(state: AgentGraphState) -> str:
    if state.get("focus_state") == FocusState.SCANNING.value and not state.get("chart_positions"):
        return "scanner"
    return "position_monitor"


def _route_after_scanner(state: AgentGraphState) -> str:
    if state.get("raw_candidates") or state.get("whale_fast_track_candidates"):
        return "safety_gate"
    return END


def _route_after_safety(state: AgentGraphState) -> str:
    if state.get("pending_order"):
        return "executor"
    if state.get("safe_candidates"):
        return "alpha"
    return END


def _route_if_order(state: AgentGraphState) -> str:
    if state.get("_hard_stop_already_executed"):
        return END
    if state.get("pending_order"):
        return "executor"
    return END


def build_agent_graph(
    *,
    config: AgentConfig,
    db: AgentDatabase,
    decision_client: GeminiDecisionClient,
    http_client: Optional[httpx.AsyncClient] = None,
):
    """Build and compile the LangGraph StateGraph for one autonomous tick."""
    graph = StateGraph(AgentGraphState)

    async def _scanner(state: AgentGraphState):
        return await scanner_node(state, config=config, db=db, http_client=http_client)

    async def _safety_gate(state: AgentGraphState):
        return await safety_gate_node(
            state, config=config, db=db, http_client=http_client
        )

    async def _alpha(state: AgentGraphState):
        return await alpha_node(
            state,
            config=config,
            db=db,
            decision_client=decision_client,
            http_client=http_client,
        )

    async def _position_monitor(state: AgentGraphState):
        return await position_monitor_node(
            state,
            config=config,
            db=db,
            decision_client=decision_client,
            http_client=http_client,
        )

    async def _executor(state: AgentGraphState):
        return await executor_node(state, config=config, db=db, http_client=http_client)

    graph.add_node("scanner", _scanner)
    graph.add_node("safety_gate", _safety_gate)
    graph.add_node("alpha", _alpha)
    graph.add_node("position_monitor", _position_monitor)
    graph.add_node("executor", _executor)

    graph.add_conditional_edges(
        START,
        _route_entry,
        {"scanner": "scanner", "position_monitor": "position_monitor"},
    )
    graph.add_conditional_edges(
        "scanner",
        _route_after_scanner,
        {"safety_gate": "safety_gate", END: END},
    )
    graph.add_conditional_edges(
        "safety_gate",
        _route_after_safety,
        {"executor": "executor", "alpha": "alpha", END: END},
    )
    graph.add_conditional_edges(
        "alpha",
        _route_if_order,
        {"executor": "executor", END: END},
    )
    graph.add_conditional_edges(
        "position_monitor",
        _route_if_order,
        {"executor": "executor", END: END},
    )
    graph.add_edge("executor", END)

    return graph.compile()
