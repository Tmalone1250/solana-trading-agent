# Fix Multi-Slot Chart Routing (`graph.py`, `safety_gate_node.py`, `scanner_node.py`)

## Root Cause
- `graph.py` routed `position_monitor -> END` whenever no `pending_order` was emitted, freezing the 5-slot rotating watchlist and blocking Chart slots 2–3 whenever `len(chart_positions) >= 1`.
- `safety_gate_node.py` also short-circuited on `focus_state != SCANNING`, blocking multi-slot Chart entries when `focus_state == IN_POSITION`.
- `scanner_node.py` did not filter out already-open mints from `chart_positions` / `whale_positions` during watchlist backfill.

## Fix
1. **`graph.py`:** Add `_route_after_position_monitor` so when `position_monitor` has no executable `pending_order` (or after an already-executed hard stop) AND `len(chart_positions) < config.MAX_CHART_POSITIONS` (and total open `< config.MAX_OPEN_POSITIONS`), it routes to `scanner` instead of `END`.
2. **`safety_gate_node.py`:** Mirror the `if "chart_positions" in state:` multi-slot capacity check used by `scanner_node.py` and `alpha_node.py`.
3. **`scanner_node.py`:** Exclude all open mints (`chart_positions`, `whale_positions`, `active_position`, `whale_position`) from `clean_eligible` so open positions never re-enter the watchlist.
4. **Verification (`tests/test_sprint1_instrumentation.py`):** Add unit test verifying `len(chart_positions) == 1 < 3` runs `position_monitor -> scanner -> safety_gate -> alpha -> executor` (evicting stale watchlist entries and opening slot 2), while `len(chart_positions) == 3` stops at `END`. Run `./venv/bin/pytest -v`.
