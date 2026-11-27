# Directive 19 — E1 Corrective Pass Report

**Project:** Solana Trading Agent (`Tmalone1250/solana-trading-agent`)  
**Branch:** `dev_1`  
**Starting Commit SHA:** `ea0c187e66f5315a2bf103254e69a1d9c3303e64`  
**Deliverable:** `reports/phase_e1_pool_identity/e1_corrective_report.md`

---

## 1. Starting Commit

- **Starting Commit:** `ea0c187e66f5315a2bf103254e69a1d9c3303e64` (`dev_1`, uncommitted corrective changes only per §11).

---

## 2. Modified-File List

**Production Modules (3 files):**
1. `position_monitor_node.py` *(M-1)*
2. `state.py` *(M-2)*
3. `alpha_node.py` *(M-3)*

**Tests (8 files — 1 focused E1 suite + 7 synthetic fixture updates to valid 32-byte Base58 Solana public keys per §4 & §6):**
4. `tests/test_e1_pool_identity.py` *(M-2, M-3, L-1A, L-1B)*
5. `tests/test_sprint1_instrumentation.py` *(M-4 + valid Pubkey fixtures for multi-slot monitor test)*
6. `tests/test_breakout_ignition.py` *(valid Pubkey fixture)*
7. `tests/test_breakout_isolation.py` *(valid Pubkey fixtures)*
8. `tests/test_dual_lane_3x3_and_whale_breakout.py` *(valid Pubkey fixtures)*
9. `tests/test_focus_machine_transitions.py` *(valid Pubkey fixtures)*
10. `tests/test_sprint2a_accounting.py` *(valid Pubkey fixture for `dexscreener_native` fallback test)*
11. `tests/test_whale_control_separation.py` *(valid Pubkey fixture for `open_hold` position)*

**Confirmed Untouched (Zero Edits):** `breakout_ignition.py`, `scalper_entry_engine.py`, `chart_exit_engine.py`, `ohlcv_client.py`, `graph.py`, `config.py`, `.env`, `db.py`, `executor_node.py`, `batched_price_monitor.py`, `reports/phase1/solana_meme_agent.duckdb`, `reports/phase1/solana_meme_agent.duckdb.wal`.

---

## 3. Exact Functions Changed

- **`position_monitor_node.py:position_monitor_node`:** Constructs `pinned_pools: dict[str, Optional[str]]` from all open `open_chart_positions` plus `primary_pos` when in `WATCHING_REENTRY` (`leg2_enabled`), and passes `pinned_pools=pinned_pools` into `fetch_batched_sol_prices(...)`.
- **`state.py:is_valid_pool_address`:** Validates real 32-byte Base58 Solana public keys via `solders.pubkey.Pubkey.from_string(cleaned)` (existing project dependency), checking exact 32-byte length, canonical Base58 round-trip, no whitespace, non-placeholder, not System Program ID (`11111111111111111111111111111111`), and distinct from `mint`.
- **`alpha_node.py:_resolve_pool_identity` & `alpha_node`:** Treats `(pair_address, dex_id)` as one atomic pool-identity tuple. When a valid pinned `active_watchlist` identity exists, preserves `(wl_pair, wl_dex)`, logs an auditable warning + `pool_identity_disagreement=True` in `decisions_log` if candidate pool disagrees, and never mixes `wl_pair` with an unrelated candidate's `dex_id`. Carries the exact resolved tuple directly into `approved_pairs` and `PendingOrder`.

---

## 4. Resolution of Findings M-1 through M-4

- **M-1 (Resolved):** `position_monitor_node.py` now passes `pinned_pools` to `fetch_batched_sol_prices(...)`. DexScreener flow, liquidity, `dexscreener_native` price fallback (when Jupiter `/price/v3` misses), and `WATCHING_REENTRY`'s `compute_leg2_score(flow.get("pair") or {}, ...)` all strictly use the pinned pool. When the pinned pool is missing/migrated/unavailable, no competing pool is substituted and no foreign fallback price is used.
- **M-2 (Resolved):** `is_valid_pool_address()` now uses `solders.pubkey.Pubkey.from_string` to validate actual 32-byte Base58 Solana public keys, rejecting punctuation, URLs, whitespace, placeholders, invalid Base58 characters, wrong decoded byte lengths, System Program ID, and `mint == pair_address`. Synthetic test fixtures were updated to valid 32-byte Base58 Solana public keys.
- **M-3 (Resolved):** `_resolve_pool_identity()` in `alpha_node.py` preserves `(pair_address, dex_id)` atomically from the pinned watchlist entry, logs explicit disagreement warnings without silently swapping or mixing DEX IDs, and propagates the exact resolved tuple to `decisions_log`, `action_log`, and `PendingOrder`.
- **M-4 (Resolved):** Added a valid 32-byte Base58 Solana `pair_address` and `dex_id="pumpswap"` to the `PendingOrder` fixture in `tests/test_sprint1_instrumentation.py::test_jupiter_raw_price_impact_persisted_without_altering_fills`, preserving all original assertions without weakening `executor_node.py`'s pool-identity guard.

---

## 5. L-1 Test Coverage Added (`tests/test_e1_pool_identity.py`)

- **L-1A (Partial-Sale Persistence):** `test_e1_pool_identity_full_lifecycle_watchlist_partial_and_final_settlement` now executes an actual `PARTIAL_SELL` (50% scale-out) between `BUY` and final `SELL`, verifying the position stays open with original `(pair_address, dex_id)`, and that `DECISION_1`, `BUY`, `PARTIAL_SELL`, and `SELL` rows in both `trades` and `action_log` retain the exact original identity.
- **L-1B (Multi-Pool Monitor Integration):** Added `test_e1_position_monitor_multi_pool_jupiter_miss_and_reentry_pinning` simulating a Jupiter `/price/v3` miss with a pinned pool (`$65k` liq, `priceNative=0.00052`, `80%` buys) alongside a competing higher-liquidity pool (`$500k` liq, `priceNative=0.00010`, `99%` sells), verifying pinned fallback price, pinned flow/liquidity, and `WATCHING_REENTRY` non-substitution when the pinned pool is missing.
- **M-2 Validation Suite:** Added `test_e1_solana_pubkey_validation_rejects_malformed_addresses` covering punctuation, URLs, whitespace, placeholders, invalid Base58 chars, System Program ID, wrong decoded lengths, and `mint == pair_address`.

---

## 6. Test Commands and Results

**Exact Command Executed:**
```bash
venv/bin/pytest -q tests/test_e1_pool_identity.py tests/test_sprint1_instrumentation.py tests/test_breakout_isolation.py tests/test_breakout_ignition.py tests/test_alpha_and_executor.py tests/test_action_ledger.py tests/test_scanner_and_safety.py tests/test_scalper_entry_engine.py tests/test_sprint2a_accounting.py tests/test_dual_lane_3x3_and_whale_breakout.py tests/test_focus_machine_transitions.py tests/test_rotating_watchlist.py tests/test_replay_simulator.py tests/test_export_soak_report.py tests/test_two_slot_and_whale_hardening.py tests/test_whale_control_separation.py
```
- **Results:** **`79 passed, 0 failed, 0 skipped, 0 errored in 22.22s`**

---

## 7. Remaining Limitations

- By E1 design, Jupiter `/price/v3` and `/swap/v2/order` remain mint-level / multi-AMM aggregated; E1 establishes pool identity continuity and pool-pinned DexScreener observations without claiming Jupiter routes exclusively through a single pool.

---

## 8. Final Git Status & Safety Confirmation

- **DB/WAL Integrity (`sha256sum -c /tmp/phase_e1_pool_identity/pre_hashes.txt`):** `reports/phase1/solana_meme_agent.duckdb: OK`, `reports/phase1/solana_meme_agent.duckdb.wal: OK`.
- **Non-Modification Confirmation:** No Raspberry Pi access, no service restarts, no `.env` or threshold changes, no commits or pushes, and no live DuckDB or WAL modifications were performed.
