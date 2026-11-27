# Directive 21 — E2A Implementation Report: Exit Priority & Exodus Signal Integrity

**Repository:** `Tmalone1250/solana-trading-agent`  
**Branch:** `dev_1`  
**Starting Commit SHA:** `ea055d751f02194bbff47b6935f3379274aebcdd`  
**Deliverables:**
- `reports/phase_e2_exit_integrity/e2a_implementation_report.md`
- `reports/phase_e2_exit_integrity/e2a_full_diff.patch`

---

## 1. Starting Commit & Changed Files

- **Starting Commit:** `ea055d751f02194bbff47b6935f3379274aebcdd`
- **Changed Production Files (4):**
  1. `config.py`
  2. `state.py`
  3. `chart_exit_engine.py`
  4. `position_monitor_node.py`
- **Changed / Added Test Files (2):**
  5. `tests/test_e2a_exit_integrity.py` *(new — covers all 18 required §3 scenarios + all 7 §4 telemetry statuses)*
  6. `tests/test_focus_machine_transitions.py` *(updated `test_exodus_bleed_and_timeout_abandonment` to set `opened_at` past warm-up and run 2 spaced confirmation ticks)*
- **Confirmed Untouched (Forbidden Files):** `breakout_ignition.py`, `scalper_entry_engine.py`, `ohlcv_client.py`, `graph.py`, `.env`, `reports/phase1/solana_meme_agent.duckdb`, `reports/phase1/solana_meme_agent.duckdb.wal`.

---

## 2. Exact Functions & Models Changed

- **`config.py:AgentConfig`:** Added three conservative noise-guard settings (explicitly not historically optimized trading thresholds):
  - `MIN_EXODUS_TXNS_5M: int = 10` (independent of `SCALPER_MIN_TXNS_5M`)
  - `EXODUS_CONFIRMATION_TICKS: int = 2`
  - `EXODUS_FLOW_WARMUP_SECONDS: float = 300.0`
- **`state.py:ActivePosition`:** Added per-position confirmation and telemetry fields:
  - `exodus_confirm_count: int = 0`
  - `exodus_last_confirmed_at: Optional[str] = None`
  - `last_exodus_status: Optional[str] = None`
- **`chart_exit_engine.py`:**
  - Added `ExodusEvaluation` model and `evaluate_exodus_signal(...)`.
  - Updated `compute_exodus(...)` to delegate to `evaluate_exodus_signal(...)`.
  - Updated `evaluate_chart_position_exit(...)` to enforce strict priority ordering (`HARD_STOP_LOSS` -> `EXODUS` -> `PARTIAL_TP` -> `TRAILING_TP` -> `BREAKEVEN` -> `HOLD`), propagate `pair_address`/`dex_id` onto exit `PendingOrder`, and persist per-position confirmation/status state.
- **`position_monitor_node.py`:**
  - Added `_resolve_exodus_observation_inputs(...)` distinguishing pinned pool `flow_available`, `liq_available`, and `current_liquidity_usd` (including confirmed `$0.0` liquidity vs. missing `None`).
  - Passed `flow_available`, `liq_available`, `current_liquidity_usd`, and `now_dt` to `evaluate_chart_position_exit(...)` and `WATCHING_REENTRY`'s `compute_exodus(...)`.

---

## 3. Exit-Priority Behavior Before & After (E2A-1)

- **Before E2A:** `evaluate_chart_position_exit` evaluated `HARD_STOP_LOSS` -> `PARTIAL_TP_50PCT` (returning early!) -> `EXODUS` -> `TRAILING_TP` -> `BREAKEVEN` -> `HOLD`. Simultaneous `+15%` Partial TP and Exodus returned `PARTIAL_SELL` (`PRIO=2`), suppressing protective `EXODUS` (`PRIO=1`).
- **After E2A:** Evaluates strictly in protective order:
  1. `HARD_STOP_LOSS` (`PRIO_HARD_STOP = 0`)
  2. `EXODUS` (`PRIO_EXODUS = 1`)
  3. `PARTIAL_TP` (`PRIO_PARTIAL_TP = 2`)
  4. `TRAILING_TP` (`PRIO_TRAILING_TP = 3`)
  5. `BREAKEVEN` (`PRIO_BREAKEVEN = 4`)
  6. `HOLD` (`PRIO_HOLD = 99`)

---

## 4. Sample Size, Confirmation/Debounce & Warm-Up Semantics (E2A-2, E2A-3, E2A-4)

- **Minimum Transaction Sample (`MIN_EXODUS_TXNS_5M = 10`):** Zero transactions, 1 sell / 0 buys, and any `total_5m < MIN_EXODUS_TXNS_5M` reset confirmation and emit `EXODUS_FLOW_INSUFFICIENT_SAMPLE`. Liquidity-drop Exodus remains independent of transaction count.
- **Confirmation / Debounce (`EXODUS_CONFIRMATION_TICKS = 2`):** Maintained per `ActivePosition` (`exodus_confirm_count`, `exodus_last_confirmed_at`) and persisted across ticks by `db.update_lane_positions_only(...)`. Duplicate invocations within `< MONITOR_POLL_SECONDS` do not double-count. Resets to `0` when the signal clears (`EXODUS_CLEAR`), drops below sample size (`EXODUS_FLOW_INSUFFICIENT_SAMPLE`), or becomes unavailable (`EXODUS_FLOW_UNAVAILABLE`).
- **Pre-Entry Rolling 5m Warm-Up (`EXODUS_FLOW_WARMUP_SECONDS = 300.0`):** Suppresses sell-ratio Exodus during the first 300 seconds after `opened_at` (`EXODUS_FLOW_WARMUP`). Missing or invalid `opened_at` is handled conservatively (treated as NOT elapsed). Liquidity-drop Exodus remains active during warm-up.

---

## 5. Flow & Liquidity Availability Distinctions (E2A-5) & Telemetry (§4)

All 7 required telemetry statuses are explicitly surfaced on `ExodusEvaluation.status` / `ActivePosition.last_exodus_status`:
- `EXODUS_FLOW_CONFIRMED`
- `EXODUS_FLOW_CONFIRMING`
- `EXODUS_FLOW_INSUFFICIENT_SAMPLE`
- `EXODUS_FLOW_WARMUP`
- `EXODUS_FLOW_UNAVAILABLE`
- `EXODUS_LIQ_DROP` *(including confirmed `$0.0` pinned-pool liquidity, `liq_drop_pct = 1.0`)*
- `EXODUS_LIQ_UNAVAILABLE` *(missing/migrated/unavailable pinned-pool liquidity — never triggers `EXODUS_LIQ_DROP`)*

Pinned-pool identity from E1 is preserved end-to-end; competing higher-liquidity pools cannot supply Exodus flow or liquidity. Existing `action_log` schema and compatibility are untouched.

---

## 6. Verification & Test Results

**Exact Command Executed:**
```bash
venv/bin/pytest -q tests/test_e2a_exit_integrity.py tests/test_e1_pool_identity.py tests/test_alpha_and_executor.py tests/test_sprint1_instrumentation.py tests/test_sprint2a_accounting.py tests/test_breakout_ignition.py tests/test_breakout_isolation.py tests/test_action_ledger.py tests/test_scanner_and_safety.py tests/test_scalper_entry_engine.py tests/test_dual_lane_3x3_and_whale_breakout.py tests/test_focus_machine_transitions.py tests/test_rotating_watchlist.py tests/test_replay_simulator.py tests/test_export_soak_report.py tests/test_two_slot_and_whale_hardening.py tests/test_whale_control_separation.py
```
- **Results:** **`84 passed, 0 failed, 0 skipped, 0 errored in 21.64s`**

**DB/WAL Checksum Verification (`sha256sum -c /tmp/phase_e1_pool_identity/pre_hashes.txt`):**
- `reports/phase1/solana_meme_agent.duckdb`: **`OK`** (`fd5fe1ef0517eda7a23fe8d0240d8bbec058b5584f3ce4d4a505f1a3e8dc3abd`)
- `reports/phase1/solana_meme_agent.duckdb.wal`: **`OK`** (`771c2ce29a60edc121b42427317aeab48b665cedcab99ad98fd108eaf7e5252b`)

**Safety Confirmation:** No Raspberry Pi access, no service restarts, no `.env` changes, no live execution, and no database or WAL modifications were performed.
