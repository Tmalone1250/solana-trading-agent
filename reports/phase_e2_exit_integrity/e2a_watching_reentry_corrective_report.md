# Directive 22 — E2A `WATCHING_REENTRY` Confirmation & Warm-Up Integration Report

**Repository:** `Tmalone1250/solana-trading-agent`  
**Branch:** `dev_1`  
**Starting Commit SHA:** `d10ac192c4665b684618133ac7040fdb72821ad5`  
**Deliverables:**
- `reports/phase_e2_exit_integrity/e2a_watching_reentry_corrective_report.md`
- `reports/phase_e2_exit_integrity/e2a_watching_reentry_corrective.patch`

---

## 1. Summary of Changes

- **`position_monitor_node.py:position_monitor_node`:** Replaced the unconfirmed `compute_exodus()` call in the `WATCHING_REENTRY` branch with the shared `evaluate_exodus_signal(...)` evaluator (`enforce_warmup=True`, `enforce_confirmation=True`, `opened_at=pos.opened_at`, `now_dt=now_dt`, `confirm_count=pos.exodus_confirm_count`, `last_confirmed_at=pos.exodus_last_confirmed_at`), persisting `exodus_confirm_count`, `exodus_last_confirmed_at`, and `last_exodus_status` on the watcher across ticks via the existing `db.update_lane_positions_only(...)` persistence path.
- **`executor_node.py:executor_node`:** Preserved `opened_at=pos_opened_at` when constructing `watch_pos` (`FocusState.WATCHING_REENTRY`) on `SELL` transitions so watchers retain the original position's `opened_at` timestamp for warm-up enforcement.
- **`tests/test_e2a_watching_reentry.py`:** Added 2 comprehensive integration tests covering all 11 required `WATCHING_REENTRY` scenarios:
  1. Single qualifying sell-flow observation does not abandon watcher (`1/2`, `EXODUS_FLOW_CONFIRMING`).
  2. Confirmation count persists in DuckDB across monitor invocations.
  3. Second spaced qualifying observation abandons watcher (`EXODUS_SELL_FLOW`).
  4. Duplicate invocation within `MONITOR_POLL_SECONDS` does not double-count.
  5. Cleared signal resets confirmation (`EXODUS_CLEAR`).
  6. Unavailable flow resets confirmation without abandonment (`EXODUS_FLOW_UNAVAILABLE`).
  7. Insufficient transaction sample (`1` sell, `0` buys) does not abandon (`EXODUS_FLOW_INSUFFICIENT_SAMPLE`).
  8. 5-minute warm-up suppresses sell-flow Exodus (`EXODUS_FLOW_WARMUP`).
  9. Missing `opened_at` conservatively suppresses sell-flow Exodus (`EXODUS_FLOW_WARMUP`).
  10. Valid pinned-pool liquidity drop abandons immediately during warm-up (`EXODUS_LIQ_DROP`).
  11. Competing pool sell flow / `$0` liquidity is ignored in favor of the E1-pinned pool.

---

## 2. Scope & Forbidden-File Verification

- **Modified Production Files:** `position_monitor_node.py`, `executor_node.py` (1-line `opened_at` persistence on `watch_pos`).
- **Added Test File:** `tests/test_e2a_watching_reentry.py`.
- **Untouched Forbidden Files:** `chart_exit_engine.py`, `config.py`, `breakout_ignition.py`, `scalper_entry_engine.py`, `ohlcv_client.py`, `graph.py`, `.env`, `db.py`, and historical DuckDB/WAL records.

---

## 3. Verification Results

**Exact Regression Command:**
```bash
venv/bin/pytest -q tests/test_e2a_watching_reentry.py tests/test_e2a_exit_integrity.py tests/test_e1_pool_identity.py tests/test_focus_machine_transitions.py tests/test_alpha_and_executor.py tests/test_sprint1_instrumentation.py tests/test_sprint2a_accounting.py tests/test_breakout_ignition.py tests/test_breakout_isolation.py tests/test_action_ledger.py tests/test_scanner_and_safety.py tests/test_scalper_entry_engine.py tests/test_dual_lane_3x3_and_whale_breakout.py tests/test_rotating_watchlist.py tests/test_replay_simulator.py tests/test_export_soak_report.py tests/test_two_slot_and_whale_hardening.py tests/test_whale_control_separation.py
```
- **Results:** **`86 passed, 0 failed, 0 skipped, 0 errored in 34.72s`**

**DB/WAL SHA-256 Checksums:**
- `reports/phase1/solana_meme_agent.duckdb`: `fd5fe1ef0517eda7a23fe8d0240d8bbec058b5584f3ce4d4a505f1a3e8dc3abd` *(unchanged)*
- `reports/phase1/solana_meme_agent.duckdb.wal`: `771c2ce29a60edc121b42427317aeab48b665cedcab99ad98fd108eaf7e5252b` *(unchanged)*

**Safety Confirmation:** Paper execution mode unchanged; no Raspberry Pi access, no service restarts, no `.env` changes, and no E2B/E3 work started.
