# Directive 24 — E2B Fee Accounting & Net Breakeven Implementation Report

**Repository:** `Tmalone1250/solana-trading-agent`  
**Branch:** `dev_1`  
**Starting Commit:** `94a0c3e8835d19fc3d93f9db1d1a71ebcafd622b`  
**Mode:** Paper only (no Raspberry Pi access, no service restarts, no `.env` or live DB/WAL modifications)

---

## 1. Summary of Changes

Implemented explicit transaction-fee accounting across position lifecycles and updated the Chart breakeven shield so it evaluates **net lifecycle PnL** after known fees, while never fabricating live fees as `0.0` or claiming net breakeven when required fees are unknown.

### Changed Files
- `state.py` — Added backward-compatible fee accounting fields on `ActivePosition`.
- `db.py` — Added nullable `tx_fee_sol DOUBLE` and `fee_status VARCHAR` columns (plus idempotent `ALTER TABLE` migrations) on `trades`, and updated `log_trade(...)` and `get_recent_trades(...)`.
- `executor_node.py` — Separated paper-assumed fees (`PAPER_TX_FEE_SOL`, `PAPER_ASSUMED`) from live unknown fees (`None` / `UNKNOWN`), populated `ActivePosition` fee fields on `BUY`, accumulated fees on `PARTIAL_SELL`, and persisted `tx_fee_sol` / `fee_status` to `trades` and `last_execution`.
- `chart_exit_engine.py` — Added `compute_net_lifecycle_pnl(...)` and updated `evaluate_chart_position_exit(...)` to arm and trigger `BREAKEVEN_SHIELD` on net lifecycle PnL when fees are known, or suppress breakeven (`BREAKEVEN_SUPPRESSED_UNKNOWN_FEES`) when fees are unknown or live, while preserving `HARD_STOP_LOSS`, `EXODUS`, `PARTIAL_TP`, and `TRAILING_TP`.
- `batched_price_monitor.py` — Added paper round-trip `fee_buf_100` to `needs_exact_jupiter_quote` for the Chart breakeven check.
- `tests/test_e2b_fee_accounting.py` — Added dedicated E2B regression tests covering `BUY -> SELL`, `BUY -> PARTIAL_SELL -> SELL / WATCHING_REENTRY -> Re-entry BUY -> SELL`, legacy DB migration + live `UNKNOWN` fee handling, and net breakeven / `UNKNOWN`-suppression behavior.
- `tests/test_e2a_exit_integrity.py` & `tests/test_focus_machine_transitions.py` — Updated synthetic paper `ActivePosition` test fixtures to include explicit `PAPER_ASSUMED` fee fields and aligned the partial-TP runner breakeven assertion with net-lifecycle breakeven semantics.

---

## 2. Position & Trade Accounting Semantics

### `ActivePosition` (`state.py`)
- `entry_fee_sol: Optional[float] = None` — Entry transaction fee in SOL (`None` when unknown).
- `accumulated_fees_sol: float = 0.0` — Cumulative known lifecycle fees in SOL (initialized to `entry_fee_sol` on `BUY`, incremented on `PARTIAL_SELL` when fee statuses match).
- `fee_accounting_status: str = "UNKNOWN"` — One of `PAPER_ASSUMED`, `OBSERVED`, or `UNKNOWN`. Historical positions without fee fields default conservatively to `UNKNOWN` and `entry_fee_sol = None`.
- `last_breakeven_status: Optional[str] = None` — Diagnostic status for breakeven evaluation (e.g., `BREAKEVEN_SUPPRESSED_UNKNOWN_FEES`).

### `trades` Table (`db.py`)
- `tx_fee_sol DOUBLE` — Nullable; stores `PAPER_TX_FEE_SOL` in paper mode and `NULL` (`None`) in live mode when fees are not reliably observed.
- `fee_status VARCHAR` — Nullable for legacy rows; new rows record `PAPER_ASSUMED` in paper mode or `UNKNOWN` in live mode.
- Legacy rows are preserved untouched without fabricating historical fees.

### `executor_node.py`
- **Paper mode:** `tx_fee_opt = float(config.PAPER_TX_FEE_SOL)`, `tx_fee_status = "PAPER_ASSUMED"`.
  - `BUY`: `sol_delta = -(spent_sol + tx_fee_sol)`, `pnl_delta = -tx_fee_sol`, `entry_fee_sol = tx_fee_opt`, `accumulated_fees_sol = round(tx_fee_opt, 6)`, `fee_accounting_status = "PAPER_ASSUMED"`.
  - `PARTIAL_SELL`: `partial_pnl = round(received_sol - cost_of_sold - tx_fee_sol, 6)`, `accumulated_fees_sol` incremented by `tx_fee_opt` when status matches.
  - `SELL`: `pnl_sol = round((received_sol - cost_basis - tx_fee_sol) if cost_basis > 0 else -tx_fee_sol, 6)`.
  - Re-entry `BUY` creates a fresh `ActivePosition` fee scope (`entry_fee_sol`, `accumulated_fees_sol`, `realized_partial_pnl_sol = 0.0`) without inheriting prior leg fees.
- **Live mode:** `tx_fee_opt = None`, `tx_fee_status = "UNKNOWN"`, `tx_fee_sol = 0.0` for internal delta math without fabricating stored `tx_fee_sol` (`NULL` in `trades`, `UNKNOWN` on `ActivePosition`).

---

## 3. Net Lifecycle Breakeven Formula (`chart_exit_engine.py`)

`compute_net_lifecycle_pnl(pos, current_sol_value, config)` returns `(fees_known, net_now_sol, net_now_pct, net_peak_pct, status)`:
- `fees_known` is `True` only when `config.EXECUTION_MODE == "paper"`, `pos.fee_accounting_status == "PAPER_ASSUMED"`, and `pos.entry_fee_sol is not None`.
- When known:
  - `rem_basis = active_cost_basis_sol(pos)`
  - `est_close_fee = float(config.PAPER_TX_FEE_SOL)`
  - `net_now_sol = pos.realized_partial_pnl_sol + (current_sol_value - rem_basis - est_close_fee) - pos.entry_fee_sol`
  - `net_now_pct = net_now_sol / initial_entry_sol` (where `initial_entry_sol = pos.initial_entry_sol or pos.entry_sol`)
  - `net_peak_sol = pos.realized_partial_pnl_sol + (pos.peak_sol_value - rem_basis - est_close_fee) - pos.entry_fee_sol`
  - `net_peak_pct = net_peak_sol / initial_entry_sol`
- When unknown or live:
  - `BREAKEVEN_SHIELD` arming and triggering are suppressed (`last_breakeven_status = "BREAKEVEN_SUPPRESSED_UNKNOWN_FEES"`).
  - `HARD_STOP_LOSS`, `EXODUS`, `PARTIAL_TP`, and `TRAILING_TP` remain active.

---

## 4. Known Limitations

- Live RPC transaction fee retrieval (`OBSERVED` status) is outside E2B scope; live trades continue to record `tx_fee_sol = NULL` and `fee_status = "UNKNOWN"` until a future live-fee observation pass is implemented.

---

## 5. Verification Results

### Regression Suite (19 test files)
Executed after final edit:
```text
venv/bin/pytest -q tests/test_e2b_fee_accounting.py tests/test_e2a_watching_reentry.py tests/test_e2a_exit_integrity.py tests/test_e1_pool_identity.py tests/test_focus_machine_transitions.py tests/test_alpha_and_executor.py tests/test_sprint1_instrumentation.py tests/test_sprint2a_accounting.py tests/test_breakout_ignition.py tests/test_breakout_isolation.py tests/test_action_ledger.py tests/test_scanner_and_safety.py tests/test_scalper_entry_engine.py tests/test_dual_lane_3x3_and_whale_breakout.py tests/test_rotating_watchlist.py tests/test_replay_simulator.py tests/test_export_soak_report.py tests/test_two_slot_and_whale_hardening.py tests/test_whale_control_separation.py
```
Result: **90 passed in 22.54s** (0 failed, 0 skipped).

### Integrity Checks
- `reports/phase1/solana_meme_agent.duckdb`: `fd5fe1ef0517eda7a23fe8d0240d8bbec058b5584f3ce4d4a505f1a3e8dc3abd` (unchanged)
- `reports/phase1/solana_meme_agent.duckdb.wal`: `771c2ce29a60edc121b42427317aeab48b665cedcab99ad98fd108eaf7e5252b` (unchanged)
- Forbidden files (`breakout_ignition.py`, `scalper_entry_engine.py`, `ohlcv_client.py`, `graph.py`, `.env`): untouched (`git diff` empty).
