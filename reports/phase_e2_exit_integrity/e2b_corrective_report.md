# Directive 26 — E2B Lifecycle PnL Integrity Corrective Pass Report

**Repository:** `Tmalone1250/solana-trading-agent`  
**Branch:** `dev_2`  
**Starting Commit:** `eb767b3151e4530c85b912ae87cdcfb08d567944` (`Directive 25`)  
**Ending Commit:** `eb767b3151e4530c85b912ae87cdcfb08d567944` (uncommitted working-tree changes; not committed, pushed, merged, or deployed)

---

## 1. Corrective Defects Addressed

### C1-R1 — Unknown-Fee / Incomplete Lifecycles Excluded from Net PnL & Win Rate
- **`db.py` (`_build_closed_lifecycles`, `get_closed_trades_summary`)**:
  - Unknown-fee or incomplete lifecycles now explicitly set:
    - `net_pnl_known = False`
    - `lifecycle_complete = False`
    - `pnl_sol = None`
    - `pnl_pct = None`
    - `total_fees_sol = None`
  - Any recoverable gross calculation is preserved separately under `gross_pnl_sol` with `gross_pnl_incomplete = True`, never exposed as net `pnl_sol`.
  - `get_closed_trades_summary()` counts all closed lifecycles in `total_trades`, exposes `known_trades`, `unknown_trades`, and `win_rate_basis: "known_lifecycles_only"`, computes `wins`, `losses`, `win_rate_pct`, and `net_pnl_sol` strictly over lifecycles with known net PnL, and returns `net_pnl_sol = None` and `win_rate_pct = None` when only unknown lifecycles exist (never substituting `0.0`).
- **Telemetry / Dashboard Consumers (`dashboard/src/lib/types.ts`, `dashboard/src/components/RecentTradesTable.tsx`)**:
  - Updated `ClosedTrade` and `ClosedTradesSummary` types to accept nullable `entry_sol`, `pnl_sol`, `pnl_pct`, `win_rate_pct`, `net_pnl_sol` plus `gross_pnl_sol`, `gross_pnl_incomplete`, `net_pnl_known`, `known_trades`, `unknown_trades`, and `win_rate_basis`.
  - Updated `RecentTradesTable.tsx` to display `UNKNOWN` instead of gross PnL or fabricated entry cost when `net_pnl_known === false` / `pnl_sol === null` / `entry_sol === null`, and to display `UNKNOWN WR` and `(N unknown)` counts when unknown lifecycles are present.

### C1-R2 — Remove All Inferred Historical Entry Costs
- **`db.py` (`_build_closed_lifecycles`)**:
  - Removed the `final_sell_sol - final_sell_pnl` entry-cost inference branch.
  - Whenever no matching `BUY` row exists, `entry_sol` remains `None`, `pnl_sol` remains `None`, `net_pnl_known = False`, and `lifecycle_complete = False` — regardless of whether `PARTIAL_SELL` rows exist or whether the final `SELL` row carries an event-level `pnl_sol`.
  - Historical database rows remain completely untouched with no backfills or fabricated data.

---

## 2. Lifecycle Arithmetic Examples

Assuming `PAPER_TX_FEE_SOL = 0.001` and `entry_sol = 0.50`:
1. **Complete `BUY -> SELL` (paper, known fees)**:
   - `BUY`: `0.50` SOL (`entry_fee = 0.001`)
   - `SELL`: `0.55` SOL (`final_sell_pnl = 0.55 - 0.50 - 0.001 = +0.049`)
   - `pnl_sol = 0.0 + 0.049 - 0.001 = +0.048` SOL, `total_fees_sol = 0.002`, `gross_pnl_sol = +0.050`, `net_pnl_known = True`
2. **Complete `BUY -> PARTIAL_SELL -> SELL` (profitable partial + losing runner)**:
   - `BUY`: `0.50` SOL (`entry_fee = 0.001`)
   - `PARTIAL_SELL` (basis `0.25`): `0.30` SOL (`realized_partial_pnl = +0.049`)
   - `SELL` (basis `0.25`): `0.15` SOL (`final_sell_pnl = -0.101`)
   - `pnl_sol = 0.049 + (-0.101) - 0.001 = -0.053` SOL, `total_fees_sol = 0.003`, `net_pnl_known = True`
3. **Complete `BUY -> PARTIAL_SELL -> SELL` (losing partial + profitable runner)**:
   - `PARTIAL_SELL` (basis `0.25`): `0.20` SOL (`realized_partial_pnl = -0.051`)
   - `SELL` (basis `0.25`): `0.40` SOL (`final_sell_pnl = +0.149`)
   - `pnl_sol = -0.051 + 0.149 - 0.001 = +0.097` SOL, `total_fees_sol = 0.003`, `net_pnl_known = True`
4. **Live `BUY -> SELL` (`UNKNOWN` fees)**:
   - `BUY`: `0.50` SOL (`tx_fee_sol = NULL`, `fee_status = 'UNKNOWN'`)
   - `SELL`: `0.65` SOL (`tx_fee_sol = NULL`, `fee_status = 'UNKNOWN'`)
   - `entry_sol = 0.50`, `exit_sol = 0.65`, `pnl_sol = None`, `pnl_pct = None`, `gross_pnl_sol = +0.15`, `gross_pnl_incomplete = True`, `total_fees_sol = None`, `net_pnl_known = False`
5. **Historical `SELL` without `BUY`**:
   - `SELL`: `0.60` SOL (`pnl_sol = 0.10` event-level, no `BUY`)
   - `entry_sol = None`, `pnl_sol = None`, `pnl_pct = None`, `gross_pnl_sol = +0.10`, `gross_pnl_incomplete = True`, `net_pnl_known = False`, `lifecycle_complete = False`

---

## 3. Known vs. Unknown Summary Behavior

| Scenario | `total_trades` | `known_trades` | `unknown_trades` | `wins` | `losses` | `win_rate_pct` | `net_pnl_sol` |
|---|---:|---:|---:|---:|---:|---:|---:|
| Empty DB | 0 | 0 | 0 | 0 | 0 | `0.0` | `0.0` |
| Only unknown lifecycles (2) | 2 | 0 | 2 | 0 | 0 | `None` | `None` |
| Mixed (1 paper win `+0.048`, 1 paper loss `-0.052`, 1 live unknown `+0.15` gross, 1 legacy `SELL` without `BUY`) | 4 | 2 | 2 | 1 | 1 | `50.0` | `-0.004` |

`win_rate_basis` is always `"known_lifecycles_only"`.

---

## 4. Verification Results

### Exact Test Commands & Results
1. **Focused E2B suite**:
   ```text
   venv/bin/pytest -q tests/test_e2b_fee_accounting.py
   ```
   Result: **9 passed in 1.19s** (0 failed, 0 skipped).

2. **Full 19-suite regression batch (including E1 & E2A)**:
   ```text
   venv/bin/pytest -q tests/test_e2b_fee_accounting.py tests/test_e2a_watching_reentry.py tests/test_e2a_exit_integrity.py tests/test_e1_pool_identity.py tests/test_focus_machine_transitions.py tests/test_alpha_and_executor.py tests/test_sprint1_instrumentation.py tests/test_sprint2a_accounting.py tests/test_breakout_ignition.py tests/test_breakout_isolation.py tests/test_action_ledger.py tests/test_scanner_and_safety.py tests/test_scalper_entry_engine.py tests/test_dual_lane_3x3_and_whale_breakout.py tests/test_rotating_watchlist.py tests/test_replay_simulator.py tests/test_export_soak_report.py tests/test_two_slot_and_whale_hardening.py tests/test_whale_control_separation.py
   ```
   Result: **95 passed in 22.82s** (0 failed, 0 skipped).

3. **Dashboard TypeScript check**:
   ```text
   cd dashboard && npx tsc --noEmit
   ```
   Result: **exit 0** (no type errors).

---

## 5. Changed Files & Integrity Confirmations

### Changed Files (`dev_2`, uncommitted)
- `db.py`
- `dashboard/src/lib/types.ts`
- `dashboard/src/components/RecentTradesTable.tsx`
- `tests/test_e2b_fee_accounting.py`
- `tests/test_action_ledger.py`
- `tests/test_focus_machine_transitions.py`
- `reports/phase_e2_exit_integrity/e2b_corrective_report.md`
- `reports/phase_e2_exit_integrity/e2b_corrective_diff.patch`

### Database & Forbidden-File Integrity
- **Production / soak databases unmodified**: No production or live DuckDB files were modified; all tests used isolated `tmp_path` DuckDB instances.
- **DuckDB / WAL SHA-256 checksums (unchanged)**:
  - `reports/phase1/solana_meme_agent.duckdb`: `fd5fe1ef0517eda7a23fe8d0240d8bbec058b5584f3ce4d4a505f1a3e8dc3abd`
  - `reports/phase1/solana_meme_agent.duckdb.wal`: `771c2ce29a60edc121b42427317aeab48b665cedcab99ad98fd108eaf7e5252b`
- **Untouched files confirmed** (`git diff` empty): `.env`, `breakout_ignition.py`, `scalper_entry_engine.py`, `ohlcv_client.py`, `graph.py`, `executor_node.py`, `batched_price_monitor.py`, `chart_exit_engine.py`, `position_monitor_node.py`, `state.py`. No RPC fee lookup, providers, or new fee assumptions were added.

### Git Status & Commits
- **Branch**: `dev_2`
- **Starting / Current `HEAD`**: `eb767b3151e4530c85b912ae87cdcfb08d567944`
- **Working tree**: 8 modified files listed above, ready for independent review (not committed, pushed, merged, or deployed).
