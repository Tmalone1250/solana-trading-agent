# 2-Step Partial Take-Profit (Scale-Out at +15%) & Runner Trailing Stop Plan (Chart + Whale Lanes)

## 1. Position State & Configuration (`config.py`, `.env.example`, `state.py`, `action_export.py`)
- Global `.env`-configurable exit parameters:
  - `PARTIAL_TP_TRIGGER_PCT=15.0` (scale out at `+15%`)
  - `PARTIAL_TP_SELL_FRACTION=0.50` (sell `50%` of `token_amount`)
  - `RUNNER_TRAIL_PULLBACK_PCT=10.0` (loosen trailing TP pullback from `-6%` to `-10%` on the remaining 50% runner)
- Optional Whale-specific overrides (defaulting to global values when unset):
  - `WHALE_PARTIAL_TP_TRIGGER_PCT=15.0`
  - `WHALE_PARTIAL_TP_SELL_FRACTION=0.50`
  - `WHALE_RUNNER_TRAIL_PULLBACK_PCT=10.0`
- Track on `ActivePosition` (`state.py`):
  - `partial_tp_taken: bool = False`
  - `initial_entry_sol: float` (defaults to `entry_sol`)
  - `remaining_cost_basis_sol: float` (starts at `entry_sol`, reduced by `50%` after partial sell)
  - `realized_partial_pnl_sol: float = 0.0`

## 2. Shared Chart & Whale Scale-Out + Runner Trail (`batched_price_monitor.py`, `whale_lane.py`, `position_monitor_node.py`, `executor_node.py`)
- **Layer 1 Priority (`WHALE_SHADOW_EXIT`)**:
  - If a triggering whale dumps (`whale_shadow_exit_flag == True`) before or *after* `partial_tp_taken == True`, immediately exit `100%` of the remaining `token_amount` (`WHALE_SHADOW_EXIT`).
- **Step 1 — Scale-Out at `+15%` (`chart_positions` & `whale_positions`)**:
  - When `not pos.partial_tp_taken` and `pnl_pct >= WHALE_PARTIAL_TP_TRIGGER_PCT` (or `PARTIAL_TP_TRIGGER_PCT` for chart positions):
    - Sell `50%` of `token_amount` via Jupiter `swap/v2/order` (`side="PARTIAL_SELL"`).
    - Credit proceeds to `sol_equity` (`sol_balance`), update `daily_realized_sol` (`daily_pnl_sol`) and `realized_partial_pnl_sol`, reduce `remaining_cost_basis_sol` by `50%`, set `partial_tp_taken = True`, and arm `breakeven_armed = True` (`+2%` floor) and `trail_armed = True`.
    - Log a `PARTIAL_SELL` row in DuckDB and `solana_actions.csv` with `strategy="WHALE_SCALP"` and `reason="WHALE_PARTIAL_TP_50PCT (+15%)"` (or `reason="PARTIAL_TP_50PCT (+15%)"` for chart positions).
- **Step 2 — Runner Trailing Stop (`-10%`) & `+2%` Breakeven Floor**:
  - Once `partial_tp_taken == True`, evaluate PnL and peak pullback against `remaining_cost_basis_sol` using `WHALE_RUNNER_TRAIL_PULLBACK_PCT` (`-10%` pullback from peak) and a `+2%` breakeven floor.

## 3. Dashboard Telemetry & UI (`telemetry_api.py`, `ActivePositionHero.tsx`, `WhaleTradesTab.tsx`, `types.ts`)
- Render the compact emerald `50% BANKED (+X.XXXX SOL)` badge and updated runner risk gauge (`BE +5%/+2%`, `Trail +15%/-10%`) on active **Whale Position** cards in `ActivePositionHero.tsx` and open rows in `WhaleTradesTab.tsx`.

## 4. Verification
- Add unit tests covering:
  1. `WHALE_SCALP` scaling out `50%` at `+15%`, arming breakeven (`+2%` floor), logging `PARTIAL_SELL` with `strategy="WHALE_SCALP"` and `reason="WHALE_PARTIAL_TP_50PCT (+15%)"`, and trailing the remaining `50%` runner at `-10%`.
  2. `WHALE_SHADOW_EXIT` firing on the remaining `50%` runner and closing the position cleanly with accurate remaining PnL.
- Run `./venv/bin/pytest -v` and `cd dashboard && npm run build`.
