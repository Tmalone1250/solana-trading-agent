# 2-Step Partial Take-Profit (Scale-Out at +15%) & Runner Trailing Stop Plan

## 1. Position State & Configuration (`config.py`, `.env.example`, `state.py`, `action_export.py`)
- Add `.env`-configurable exit parameters:
  - `PARTIAL_TP_TRIGGER_PCT=15.0` (scale out at `+15%`)
  - `PARTIAL_TP_SELL_FRACTION=0.50` (sell `50%` of `token_amount`)
  - `RUNNER_TRAIL_PULLBACK_PCT=10.0` (loosen trailing TP pullback from `-6%` to `-10%` on the remaining 50% runner)
- Extend `ActivePosition` in `state.py` with:
  - `partial_tp_taken: bool = False`
  - `initial_entry_sol: float` (defaults to `entry_sol`)
  - `remaining_cost_basis_sol: float` (starts at `entry_sol`, reduced proportionally after partial sell)
  - `realized_partial_pnl_sol: float = 0.0`
- Include `"PARTIAL_SELL"` in `EXPORT_TYPE_ACTIONS["trades"]` in `action_export.py`.

## 2. Step 1 Scale-Out & Step 2 Runner Trailing Stop (`batched_price_monitor.py`, `executor_node.py`, `position_monitor_node.py`)
- **Step 1 — Scale-Out at `+15%`:**
  - When `not pos.partial_tp_taken` and `pnl_pct >= PARTIAL_TP_TRIGGER_PCT`:
    - Compute `sell_qty = int(pos.token_amount * PARTIAL_TP_SELL_FRACTION)`.
    - Fetch an exact Jupiter Swap Quote (`/swap/v2/order`) for `sell_qty -> SOL` and execute a partial paper/live sell.
    - Subtract `sell_qty` from `pos.token_amount`, credit SOL proceeds to `sol_balance` (`sol_equity`), and add `(proceeds - cost_of_sold_portion)` to `daily_pnl_sol` (`daily_realized_sol`) and `pos.realized_partial_pnl_sol`.
    - Set `pos.remaining_cost_basis_sol = pos.entry_sol * (1 - PARTIAL_TP_SELL_FRACTION)`, `pos.partial_tp_taken = True`, and arm `pos.breakeven_armed = True` (`+2%` floor) and `pos.trail_armed = True`.
    - Log a `PARTIAL_SELL` row to DuckDB and `solana_actions.csv` with `reason="PARTIAL_TP_50PCT (+15%)"`.
- **Step 2 — Runner Trailing Stop:**
  - When `pos.partial_tp_taken == True`, evaluate PnL and peak pullback against `remaining_cost_basis_sol` and use `RUNNER_TRAIL_PULLBACK_PCT` (`-10%` pullback from peak instead of `-6%`) while protected by the `+2%` breakeven floor.
  - On final exit, compute realized PnL on the runner against `remaining_cost_basis_sol`, log the closing `SELL` row, and remove the position from `chart_positions` / `whale_positions`.

## 3. Dashboard Telemetry & UI (`telemetry_api.py`, `dashboard/src/lib/types.ts`, `dashboard/src/components/ActivePositionHero.tsx`)
- Expose `partial_tp_taken`, `initial_entry_sol`, `remaining_cost_basis_sol`, and `realized_partial_pnl_sol` on active position cards in `/api/telemetry`.
- Render a compact emerald badge (`50% BANKED (+0.0375 SOL)`) on `PositionCard` in `ActivePositionHero.tsx` when `partial_tp_taken` is true.

## 4. Verification
- Add unit tests in `tests/test_focus_machine_transitions.py` covering:
  1. Partial sell firing once at `+15%` (`sell_qty == 50%`, `partial_tp_taken=True`, `sol_equity` + `daily_realized_sol` credited, `PARTIAL_SELL` logged, does not re-fire on next tick).
  2. Runner surviving a `-8%` pullback from peak without exiting, then exiting cleanly on a `-10%` pullback or `+2%` breakeven floor.
- Run `./venv/bin/pytest -v` and `cd dashboard && npm run build`.
