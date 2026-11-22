# Unconditional HARD_STOP_LOSS Enforcement Plan

## 1. Root Cause & Batched Price Monitor Audit (`batched_price_monitor.py`, `position_monitor_node.py`, `whale_lane.py`, `executor_node.py`, `graph.py`)
- **Audit Findings**:
  1. In `position_monitor_node.py`, multi-slot chart positions in `all_chart_dicts[1:]` (or any open chart position when `focus_state` was `WATCHING_REENTRY` / `SCANNING`) had their PnL logged by `apply_dex_flow_and_pnl` (`[Monitor] [chart_slot] $MAYA ... PnL=-11.75%, -16.80%, -21.54%, -42.72%`) without executing exit rules.
  2. In `position_monitor_node.py`, Step 1 Partial TP ran before `HARD_STOP_LOSS`, and `HARD_STOP_LOSS` only emitted a `pending_order` without executing immediately or falling back if Jupiter `/swap/v2/order` failed during a rapid dump.
- **Fixes**:
  1. **`batched_price_monitor.py`**:
     - Evaluate `active_cost_basis_sol(pos)` unconditionally against `remaining_cost_basis_sol` (when `> 0`, else `entry_sol`).
     - Add `hard_stop_limit_pct_100(pos, config)` and `is_hard_stop_hit(pos, current_sol_value, config)` returning `True` unconditionally whenever `pos.token_amount > 0` and `pnl_pct <= -HARD_STOP_LOSS_PCT` (`-10.0%`), regardless of `partial_tp_taken`, `breakeven_armed`, `trail_armed`, `LEG_2_REENTRY` (`LEG2_ENTRY`), or any other flag.
     - Put the `HARD_STOP_LOSS` check at the very top of `needs_exact_jupiter_quote` so `resolve_position_sol_value` always fetches an exact Jupiter `swap/v2/order` exit quote (and falls back to the live price quote if Jupiter fails).
     - Add `enforce_hard_stop_if_hit` and `monitor_and_enforce_hard_stops` in `batched_price_monitor.py` to immediately fetch the exact Jupiter `swap/v2/order` exit quote (with live price fallback) and execute a 100% `SELL` (`reason="HARD_STOP_LOSS"`) via `executor_node`, removing the position from `chart_positions` / `whale_positions`.
  2. **`executor_node.py`**:
     - Add a fallback in `executor_node` when `fetch_jupiter_order` (`swap/v2/order`) fails on a `SELL` so `HARD_STOP_LOSS` always executes 100% using the live `current_sol_value` quote and removes the position from `chart_positions` / `whale_positions`.
     - Promote any remaining open `chart_positions[0]` when one chart slot closes so multi-slot chart positions continue to be monitored.
  3. **`position_monitor_node.py` & `whale_lane.py` & `graph.py`**:
     - Enforce `HARD_STOP_LOSS` across all open `chart_positions` (including `LEG_2_REENTRY` and secondary chart slots) and `whale_positions` as an unconditional top-priority exit.

## 2. Verification
- Add regression unit test `test_unconditional_hard_stop_loss_on_leg2_reentry_chart_and_whale_positions` in `tests/test_dual_lane_3x3_and_whale_breakout.py` where a `LEG_2_REENTRY` chart position and a `WHALE_SCALP` position drop to `-11.75%` and assert that `HARD_STOP_LOSS` immediately fetches `swap/v2/order` (and falls back cleanly if Jupiter errors), executes a 100% `SELL`, and removes both positions from `chart_positions` and `whale_positions`.
- Run `./venv/bin/pytest -v`.
