# Directive 16 — E1 Implementation Report: Pool Identity Continuity & Database Persistence

**Project:** Solana Trading Agent (`Tmalone1250/solana-trading-agent`)  
**Branch:** `dev_1`  
**Phase:** E1 — Pool Identity Continuity  
**Deliverable:** `/tmp/phase_e1_pool_identity/e1_implementation_report.md`

---

## 1. Starting and Final Commit SHAs

- **Starting Commit SHA:** `db6183d1b6df5c11dfa5114c748259b0dbcea1dd`
- **Final Commit SHA:** `db6183d1b6df5c11dfa5114c748259b0dbcea1dd` *(uncommitted working-tree changes only, per §13: "Do not commit or push unless separately authorized.")*

---

## 2. Exact Modified-File List

**Production / Persistence Modules (7 files):**
1. `state.py`
2. `action_export.py`
3. `db.py`
4. `state_ledger.py`
5. `alpha_node.py`
6. `executor_node.py`
7. `batched_price_monitor.py`

**Tests (1 new focused suite + 5 minimal pre-E1 fixture updates):**
8. `tests/test_e1_pool_identity.py` *(new — covers all 17 required §9 test cases)*
9. `tests/test_action_ledger.py` *(updated `len(ACTION_LOG_COLUMNS)` assertion from `41` to `43` for additive `pair_address`, `dex_id`)*
10. `tests/test_breakout_ignition.py` *(added `pair_address`/`dex_id` to `bo_cand` fixture)*
11. `tests/test_breakout_isolation.py` *(added `pair_address`/`dex_id` to `bo_cand` and `ok_order` fixtures)*
12. `tests/test_dual_lane_3x3_and_whale_breakout.py` *(added `pair_address`/`dex_id` to Chart `BREAKOUT_IGNITION` and `SHIT` position fixtures)*
13. `tests/test_focus_machine_transitions.py` *(added `pair_address`/`dex_id` to `_base_in_position`, `watch_pos`, and DexScreener mock pairs)*

**Confirmed Untouched (Zero Edits):** `breakout_ignition.py`, `scalper_entry_engine.py`, `chart_exit_engine.py`, `position_monitor_node.py`, `ohlcv_client.py`, `graph.py`, `config.py`, `.env`, `reports/phase1/solana_meme_agent.duckdb`, `reports/phase1/solana_meme_agent.duckdb.wal`.

---

## 3. Functions and Classes Changed

- **`state.py`:** Added `is_valid_pool_address(pair_address, *, mint=None)`. Added `pair_address`, `dex_id` to `PendingOrder`; `pair_address`, `dex_id`, `pool_identity_status` (`PINNED` | `UNKNOWN`), `pool_observation_status` to `ActivePosition` (validated in `_ensure_cost_basis_fields`); `dex_id` to `WatchlistEntry`.
- **`action_export.py`:** Appended `pair_address` and `dex_id` to `ACTION_LOG_COLUMNS` (43 total) and `ACTION_LOG_MIGRATIONS`.
- **`db.py`:** Added additive nullable `dex_id` column/migration on `active_watchlist` and `pair_address`, `dex_id` columns/migrations on `trades` and `action_log`. Updated `get_watchlist`, `upsert_watchlist_entry` (preserves original non-empty `pair_address`/`dex_id` on conflict), `log_trade`, `get_recent_trades`, and `log_action`.
- **`state_ledger.py`:** Updated `_merge_monotonic_shadow_flags` so position updates monotonically preserve original `pair_address`, `dex_id`, and `pool_identity_status` and never retroactively assign a pool identity to an unknown-pool position.
- **`alpha_node.py`:** Propagates `pair_address` and `dex_id` from candidate/watchlist to `decisions_log`, `action_log`, and `PendingOrder`; rejects new `BREAKOUT_IGNITION` approvals when `pair_address` is absent or invalid with auditable reason `BLOCKED: MISSING_OR_INVALID_POOL_IDENTITY (pair_address absent or invalid)`.
- **`executor_node.py`:** Enforces defense-in-depth `BREAKOUT_IGNITION` pool-identity check on `BUY` before slot reservation; copies `pair_address`/`dex_id` onto `ActivePosition`; retains original position `pair_address`/`dex_id` across `BUY`, `PARTIAL_SELL`, `SELL`, `WATCHING_REENTRY`, `trades`, and `action_log`.
- **`batched_price_monitor.py`:** Added `select_pinned_dexscreener_pair`; updated `fetch_batched_sol_prices(..., pinned_pools=...)` to pin DexScreener lookups when `pinned_pools` is passed and attach `all_solana_pairs` for downstream pinning; updated `apply_dex_flow_and_pnl` to enforce `pos.pair_address` pinning, reject mismatched `pairAddress`/`baseToken.address`, and prevent unrelated pool flow on `UNKNOWN` or missing/migrated pools; updated `monitor_and_enforce_hard_stops` to pass `pinned_pools`.

---

## 4. Pool Identity Lifecycle Before and After

- **Before E1:** `Discovery -> Watchlist (pair_address could be overwritten on conflict) -> Alpha (pair_address dropped on PendingOrder) -> Executor (ActivePosition/trades/action_log had no pair_address/dex_id) -> Monitor (re-selected DexScreener highest-liquidity pool every 5s)`.
- **After E1:** `Discovery -> Watchlist (pair_address + dex_id preserved immutably on conflict) -> Alpha (validates pair_address for BREAKOUT_IGNITION; rejects if missing/invalid) -> PendingOrder (pair_address + dex_id) -> Executor BUY (validates + copies to ActivePosition, trades, action_log) -> Monitor (pins DexScreener observations strictly to pos.pair_address + pos.mint) -> Exit / Settlement (preserves original pair_address + dex_id in trades, action_log, and WATCHING_REENTRY)`.

---

## 5. Database Schema Changes & Migration Approach

- **Additive, Nullable Columns Added:**
  - `active_watchlist`: `dex_id VARCHAR` (alongside existing `pair_address VARCHAR DEFAULT ''`).
  - `trades`: `pair_address VARCHAR`, `dex_id VARCHAR`.
  - `action_log`: `pair_address VARCHAR`, `dex_id VARCHAR` (via `ACTION_LOG_MIGRATIONS`).
- **Idempotency:** `_migrate_watchlist_columns()` and `_migrate_action_log_columns()` check `PRAGMA table_info(...)` before executing `ALTER TABLE ... ADD COLUMN ...`, making repeated `AgentDatabase(...)` initializations safe and idempotent. Verified on a simulated pre-E1 DuckDB initialized twice consecutively.

---

## 6. Handling of Historical NULL Identities

- Historical `trades`, `action_log`, and `active_watchlist` rows without pool identity remain `NULL` (empty string in CSV export); no pool identity is ever fabricated.
- Legacy `ActivePosition` dicts lacking `pair_address` deserialize with `pair_address=None` and `pool_identity_status="UNKNOWN"`.
- During monitoring (`apply_dex_flow_and_pnl`), unknown-pool positions set `pool_observation_status="UNKNOWN_POOL_IDENTITY"`, never retroactively adopt the highest-liquidity pool, never apply unrelated pool flow/liquidity, and are not force-closed.

---

## 7. Handling of Inactive, Missing, or Migrated Pools

`select_pinned_dexscreener_pair` classifies every DexScreener observation explicitly without ever substituting another pool:
- `MATCHED` (`flow_available=True`, `liquidity_usd > 0`): valid pinned pool observation (including valid `0`-buy/`0`-sell flow).
- `ZERO_LIQUIDITY` (`flow_available=True`, `liquidity_usd <= 0.0`): pinned pool explicitly reported `$0` liquidity.
- `INCOMPLETE_DATA` (`flow_available=False`): pinned pool present but `txns.m5` or `liquidity.usd` missing.
- `MISMATCHED_MINT` / `MISMATCHED_PAIR` (`flow_available=False`): provider response failed mint or pair-address verification and was rejected.
- `POOL_MIGRATED_OR_MISSING` (`flow_available=False`): other Solana pools exist for the mint, but original `pair_address` is absent (no silent migration).
- `POOL_UNAVAILABLE` (`flow_available=False`): no Solana pools returned / API error / 429 backoff.

Whenever `flow_available=False`, `apply_dex_flow_and_pnl` preserves `pos.pair_address`, `pos.dex_id`, and `pos.last_liquidity_usd`, logs the status, and returns `(0, 0, last_buy_ratio, 0.0)` so downstream exit logic does not fabricate a liquidity collapse or unrelated sell-flow exit.

---

## 8. Monitor Pool-Selection Behavior

- `fetch_batched_sol_prices(..., pinned_pools=...)` strictly selects only the pair matching `(expected_pair_address, expected_mint)` via `select_pinned_dexscreener_pair` and never falls back to `best_by_mint`.
- When called from `position_monitor_node.py` (which remained untouched per §2), `fetch_batched_sol_prices` attaches `all_solana_pairs` to `dex_flow[m]`, and `apply_dex_flow_and_pnl(pos, ...)` resolves the pinned pool strictly against `pos.pair_address` and `pos.mint`.

---

## 9. Test Results

**Exact Command Executed:**
```bash
venv/bin/pytest -q tests/test_e1_pool_identity.py tests/test_action_ledger.py tests/test_scanner_and_safety.py tests/test_alpha_and_executor.py tests/test_breakout_ignition.py tests/test_breakout_isolation.py tests/test_scalper_entry_engine.py tests/test_sprint2a_accounting.py tests/test_dual_lane_3x3_and_whale_breakout.py tests/test_focus_machine_transitions.py tests/test_rotating_watchlist.py tests/test_replay_simulator.py tests/test_export_soak_report.py
```
- **Results:** **`63 passed, 0 failed, 0 skipped, 0 errored in 15.10s`**
- **Pre-existing failures:** None.
- **Test artifacts created:** Temporary pytest directories under `/tmp/pytest-of-tmalone1250/` only; no repository DB/WAL artifacts created.

---

## 10. Known Remaining Limitations

1. **Jupiter Valuation vs. DexScreener Pool Flow:** By design in E1 (§8), Jupiter `/price/v3` remains mint-level aggregated and Jupiter `/swap/v2/order` routes across Solana liquidity venues. E1 guarantees pool identity continuity and pool-pinned DexScreener flow/liquidity observations; it does not claim Jupiter executes exclusively on that single pool.
2. **`position_monitor_node.py` Unmodified:** Because `position_monitor_node.py` was excluded from E1 edit scope (§2), `fetch_batched_sol_prices` receives `pinned_pools` directly from `monitor_and_enforce_hard_stops` and tests, while `position_monitor_node.py` achieves strict pool pinning via `apply_dex_flow_and_pnl` using `all_solana_pairs`. Passing `pinned_pools` directly from `position_monitor_node.py` can be added trivially in a later authorized directive (E2/E5) to also pin the `dexscreener_native` fallback price when Jupiter `/price/v3` misses a mint.

---

## 11. Deployment Requirements for Future Authorized Rollout

1. Stop the agent service cleanly before updating code on the deployment host.
2. Back up the live DuckDB file and WAL before starting the updated code.
3. On first startup, `AgentDatabase.__init__()` automatically applies the additive, idempotent `ALTER TABLE` migrations (`dex_id` on `active_watchlist`; `pair_address`, `dex_id` on `trades` and `action_log`).
4. Existing historical rows remain `NULL`; existing open positions without `pair_address` remain safely marked `UNKNOWN`.
5. No `.env` changes or threshold changes are required for E1.

---

## 12. Non-Modification & Safety Confirmation

Confirmed: No Raspberry Pi access, no live deployment changes, no service restarts, no `.env` changes, no wallet/execution-mode changes, and no modifications to `reports/phase1/solana_meme_agent.duckdb` or `reports/phase1/solana_meme_agent.duckdb.wal` (SHA-256 checksums verified unchanged against `pre_hashes.txt`).
