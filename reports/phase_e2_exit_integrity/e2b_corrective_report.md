# Directive 25 — E2B Corrective Pass Report

**Repository:** `Tmalone1250/solana-trading-agent`  
**Branch:** `dev_1`  
**Starting Commit:** `d88e0f374cb7af4c157557ff1e11247de54d5687`  
**Status:** Uncommitted (ready for independent review; not committed or pushed)

---

## 1. Corrective Findings Addressed

### C-1 — Complete Lifecycle PnL in Trade Reporting (`executor_node.py`, `db.py`)
- **`executor_node.py` (`BUY`)**: Added `opened_at=pos.opened_at` to `db.log_trade(...)` so every `BUY`, `PARTIAL_SELL`, and `SELL` in a position lifecycle shares the `(mint, opened_at)` identity.
- **`db.py` (`_build_closed_lifecycles`, `get_recent_closed_trades`, `get_closed_trades_summary`)**:
  - Groups `BUY`, `PARTIAL_SELL`, and `SELL` rows by `(mint, opened_at)` so separate entries and re-entries on the same mint never combine.
  - Reconciles complete lifecycles using:
    `lifecycle_net_pnl = realized_partial_pnl + final_sell_pnl - entry_fee`
    where `realized_partial_pnl` already includes partial-sale fees, `final_sell_pnl` includes the final-sale fee, and `entry_fee` is subtracted once.
  - Preserves event-level fields (`realized_partial_pnl_sol`, `final_sell_pnl_sol`, `final_sell_sol`) alongside lifecycle-level totals (`entry_sol`, `exit_sol`, `pnl_sol`, `pnl_pct`, `total_fees_sol`, `fee_accounting_status`, `net_pnl_known`, `lifecycle_complete`).
  - `get_closed_trades_summary()` aggregates `total_trades`, `wins`, `losses`, `win_rate_pct`, and `net_pnl_sol` over closed lifecycles consistently with `get_recent_closed_trades()`.

### C-2 — Remove Zero Substitution for Unknown Live Fees (`executor_node.py`)
- Removed `tx_fee_sol = 0.0` from the non-paper branch in `executor_node.py`.
- Paper mode continues using `tx_fee_opt = float(config.PAPER_TX_FEE_SOL)` with `fee_status = "PAPER_ASSUMED"`.
- Live mode retains `tx_fee_opt = None` and `fee_status = "UNKNOWN"` across `BUY`, `PARTIAL_SELL`, and `SELL` (`ActivePosition.entry_fee_sol = None`, `fee_accounting_status = "UNKNOWN"`, `trades.tx_fee_sol = NULL`, `last_execution.fee_accounting_incomplete = True`).
- When `tx_fee_opt is None`, gross SOL balance deltas are applied without subtracting or fabricating an assumed fee, and `db.reserve_buy_slot` receives `0.0` solely for reservation headroom without storing a fabricated fee.

### C-3 — Align Exact Jupiter Quote Selection with Net Lifecycle Breakeven (`batched_price_monitor.py`, `chart_exit_engine.py`)
- Moved `compute_net_lifecycle_pnl(pos, current_sol_value, config)` into `batched_price_monitor.py` (re-exported by `chart_exit_engine.py` with no circular imports).
- Updated `needs_exact_jupiter_quote()` to use `compute_net_lifecycle_pnl()` for Chart breakeven proximity checks:
  - **Before partial TP**: accounts for `entry_fee_sol` and expected close fee (`PAPER_TX_FEE_SOL`) against `initial_entry_sol`.
  - **After partial TP**: credits `realized_partial_pnl_sol` once, accounts for `entry_fee_sol` and expected close fee, and divides by `initial_entry_sol` (not remaining cost basis).
  - **UNKNOWN fees**: suppresses the net breakeven quote threshold while preserving `HARD_STOP_LOSS`, `PARTIAL_TP`, and `TRAILING_TP` quote triggers.

---

## 2. Lifecycle Accounting Examples (Arithmetic)

Assuming `PAPER_TX_FEE_SOL = 0.001` and `initial_entry_sol = 0.50`:
1. **`BUY -> SELL`**:
   - `BUY`: `0.50` SOL, `entry_fee = 0.001`
   - `SELL`: `0.55` SOL, `final_sell_pnl = 0.55 - 0.50 - 0.001 = +0.049`
   - `lifecycle_net_pnl = 0.0 + 0.049 - 0.001 = +0.048` SOL (`total_fees_sol = 0.002`)
2. **`BUY -> PARTIAL_SELL -> SELL` (profitable partial + losing runner)**:
   - `BUY`: `0.50` SOL, `entry_fee = 0.001`
   - `PARTIAL_SELL` (50%, basis `0.25`): `0.30` SOL, `realized_partial_pnl = 0.30 - 0.25 - 0.001 = +0.049`
   - `SELL` (50%, basis `0.25`): `0.15` SOL, `final_sell_pnl = 0.15 - 0.25 - 0.001 = -0.101`
   - `lifecycle_net_pnl = 0.049 + (-0.101) - 0.001 = -0.053` SOL (`total_fees_sol = 0.003`, counted as a loss)
3. **`BUY -> PARTIAL_SELL -> SELL` (losing partial + profitable runner)**:
   - `PARTIAL_SELL` (basis `0.25`): `0.20` SOL, `realized_partial_pnl = 0.20 - 0.25 - 0.001 = -0.051`
   - `SELL` (basis `0.25`): `0.40` SOL, `final_sell_pnl = 0.40 - 0.25 - 0.001 = +0.149`
   - `lifecycle_net_pnl = -0.051 + 0.149 - 0.001 = +0.097` SOL (`total_fees_sol = 0.003`, counted as a win)

---

## 3. Historical Unknown-Data & Paper vs. Live Behavior

- **Historical incomplete lifecycles**: If a lifecycle has `PARTIAL_SELL` rows and no matching `BUY` row, `entry_sol` is set to `None` (never inferred via `exit_sol - final_sell_pnl`), `total_fees_sol = None`, `fee_accounting_status = "UNKNOWN"`, `lifecycle_complete = False`, and `net_pnl_known = False`.
- **Paper vs. Live**: Paper mode records `PAPER_ASSUMED` with `PAPER_TX_FEE_SOL`. Live mode records `tx_fee_sol = NULL` / `None` and `fee_status = "UNKNOWN"`, marks `fee_accounting_incomplete = True` on `last_execution`, and suppresses net breakeven while keeping `HARD_STOP_LOSS`, `EXODUS`, `PARTIAL_TP`, and `TRAILING_TP` active.

---

## 4. Verification Results

### Test Failure Encountered & Resolved
- During the first full run after removing `tx_fee_sol = 0.0` in `executor_node.py`, 18 tests failed with `NameError: name 'tx_fee_sol' is not defined` at `executor_node.py:172` (`db.reserve_buy_slot`). Resolved by passing `tx_fee_sol=float(tx_fee_opt) if tx_fee_opt is not None else 0.0` to `db.reserve_buy_slot`.

### Final Passing Test Runs
1. **Focused E2B suite**:
   ```text
   venv/bin/pytest -q tests/test_e2b_fee_accounting.py
   ```
   Result: **8 passed in 1.09s**
2. **Full 19-suite regression batch (including E1 & E2A)**:
   ```text
   venv/bin/pytest -q tests/test_e2b_fee_accounting.py tests/test_e2a_watching_reentry.py tests/test_e2a_exit_integrity.py tests/test_e1_pool_identity.py tests/test_focus_machine_transitions.py tests/test_alpha_and_executor.py tests/test_sprint1_instrumentation.py tests/test_sprint2a_accounting.py tests/test_breakout_ignition.py tests/test_breakout_isolation.py tests/test_action_ledger.py tests/test_scanner_and_safety.py tests/test_scalper_entry_engine.py tests/test_dual_lane_3x3_and_whale_breakout.py tests/test_rotating_watchlist.py tests/test_replay_simulator.py tests/test_export_soak_report.py tests/test_two_slot_and_whale_hardening.py tests/test_whale_control_separation.py
   ```
   Result: **94 passed in 22.93s** (0 failed, 0 skipped)

### Integrity & Repository State
- **Changed files**: `batched_price_monitor.py`, `chart_exit_engine.py`, `db.py`, `executor_node.py`, `tests/test_e2b_fee_accounting.py`
- **Artifacts added**: `reports/phase_e2_exit_integrity/e2b_corrective_report.md`, `reports/phase_e2_exit_integrity/e2b_corrective_diff.patch`
- **Forbidden files** (`breakout_ignition.py`, `scalper_entry_engine.py`, `ohlcv_client.py`, `graph.py`, `.env`): untouched (`git diff` empty)
- **DuckDB / WAL SHA-256 checksums** (unchanged):
  - `reports/phase1/solana_meme_agent.duckdb`: `fd5fe1ef0517eda7a23fe8d0240d8bbec058b5584f3ce4d4a505f1a3e8dc3abd`
  - `reports/phase1/solana_meme_agent.duckdb.wal`: `771c2ce29a60edc121b42427317aeab48b665cedcab99ad98fd108eaf7e5252b`
