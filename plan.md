# Fix Duplicate `HARD_STOP_LOSS` SELL Execution Plan

## Root Cause
- `position_monitor_node.py` executed `executor_node(...)` immediately on `HARD_STOP_LOSS` and returned `"pending_order": hs_order.model_dump()` alongside `"_hard_stop_already_executed": True`.
- Because `_hard_stop_already_executed` was not declared on `AgentGraphState` in `state.py`, LangGraph silently stripped the flag when merging state, causing `_route_if_order` in `graph.py` to route `pending_order` to `executor_node` a second time ~200ms later with `active_position=None` (logging a duplicate `0.00%` SELL and deducting an extra `PAPER_TX_FEE_SOL`).

## Fix
1. **Eliminate double-trigger path (`state.py`, `graph.py`):**
   - Declare `_hard_stop_already_executed: bool` in `AgentGraphState`.
   - Short-circuit `_route_if_order` in `graph.py` to `END` whenever `_hard_stop_already_executed` is truthy.
2. **Enforce SELL idempotency (`executor_node.py`):**
   - Add a bounded in-flight & recently-closed SELL guard (`_inflight_sells` + `_recently_closed_sells`, max 64 entries / 60s TTL) keyed by `(db.db_path, lane, mint, opened_at)`.
   - Reject duplicate concurrent/back-to-back full `SELL` calls and already-closed positions before calling Jupiter or writing to DuckDB.
3. **Verification (`tests/test_sprint1_instrumentation.py`):**
   - Add regression tests covering both LangGraph end-to-end `HARD_STOP_LOSS` and concurrent/back-to-back `executor_node` `SELL` calls for the same position, asserting 1 fill, 1 `sol_balance` credit, and 1 DuckDB closed trade row.
   - Run `./venv/bin/pytest -v`.
