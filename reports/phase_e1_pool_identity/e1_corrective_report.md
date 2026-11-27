# Directive 19 & 20 — E1 Corrective Pass Report

**Project:** Solana Trading Agent (`Tmalone1250/solana-trading-agent`)  
**Branch:** `dev_1`  
**Directive 19 Starting Commit SHA:** `ea0c187e66f5315a2bf103254e69a1d9c3303e64`  
**Directive 19 Implementation Commit SHA:** `99df27e1e0fdb8bed03ff1747aaf0199108deab9` *(parent: `ea0c187e66f5315a2bf103254e69a1d9c3303e64`)*  
**Deliverable:** `reports/phase_e1_pool_identity/e1_corrective_report.md`

---

## 1. Starting Commit & History

- **Directive 19 Starting Commit:** `ea0c187e66f5315a2bf103254e69a1d9c3303e64` (`Directive 17`)
- **Directive 19 Implementation Commit:** `99df27e1e0fdb8bed03ff1747aaf0199108deab9` (`Directive 19`)
- **Directive 20 Preservation Commit:** Finalizes artifact preservation (`e1_corrective_diff.patch`, updated `e1_corrective_report.md`, updated `artifact_preservation_report.md`) on top of `99df27e1e0fdb8bed03ff1747aaf0199108deab9`.

---

## 2. Modified Production Files (3 files)

1. `position_monitor_node.py` *(M-1)*
2. `state.py` *(M-2)*
3. `alpha_node.py` *(M-3)*

---

## 3. Modified / Added Test Files (8 files)

1. `tests/test_e1_pool_identity.py` *(M-2, M-3, L-1A, L-1B)*
2. `tests/test_sprint1_instrumentation.py` *(M-4 + valid Pubkey fixtures for multi-slot monitor test)*
3. `tests/test_breakout_ignition.py` *(valid Pubkey fixture)*
4. `tests/test_breakout_isolation.py` *(valid Pubkey fixtures)*
5. `tests/test_dual_lane_3x3_and_whale_breakout.py` *(valid Pubkey fixtures)*
6. `tests/test_focus_machine_transitions.py` *(valid Pubkey fixtures)*
7. `tests/test_sprint2a_accounting.py` *(valid Pubkey fixture for `dexscreener_native` fallback test)*
8. `tests/test_whale_control_separation.py` *(valid Pubkey fixture for `open_hold` position)*

---

## 4. Exact Corrections (M-1 through M-4 & L-1)

- **M-1 (`position_monitor_node.py:position_monitor_node`):** Constructs `pinned_pools: dict[str, Optional[str]]` from every open `open_chart_positions` entry plus `primary_pos` when in `WATCHING_REENTRY` (`leg2_enabled=True`), and passes `pinned_pools=pinned_pools` to `fetch_batched_sol_prices(...)`. DexScreener flow, liquidity, `dexscreener_native` price fallback on Jupiter `/price/v3` misses, and `compute_leg2_score(flow.get("pair") or {}, ...)` all strictly use the pinned pool without substituting competing pools.
- **M-2 (`state.py:is_valid_pool_address`):** Validates actual 32-byte Base58 Solana public keys (see §5). Updated synthetic test fixtures to valid 32-byte Base58 Solana public keys.
- **M-3 (`alpha_node.py:_resolve_pool_identity` & `alpha_node`):** Treats `(pair_address, dex_id)` as a single atomic tuple. When a valid pinned `active_watchlist` identity exists, preserves `(wl_pair, wl_dex)`, logs an auditable warning and `pool_identity_disagreement=True` in `decisions_log` if the refreshed candidate pool disagrees, and never mixes `wl_pair` with an unrelated candidate's `dex_id`. Passes the exact resolved tuple to `approved_pairs`, `decisions_log`, `action_log`, and `PendingOrder`.
- **M-4 (`tests/test_sprint1_instrumentation.py:test_jupiter_raw_price_impact_persisted_without_altering_fills`):** Added valid 32-byte Base58 Solana `pair_address="4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"` and `dex_id="pumpswap"` to the `BREAKOUT_IGNITION` `PendingOrder` fixture while preserving all original price-impact, fill, trade, and CSV assertions and retaining `executor_node`'s identity guard.
- **L-1 (`tests/test_e1_pool_identity.py`):** Added `PARTIAL_SELL` lifecycle persistence coverage (`L-1A`) and end-to-end multi-pool `position_monitor_node` integration coverage (`L-1B`), detailed in §6 and §7.

---

## 5. Public-Key Validation Implementation & Dependency Requirements

- **Implementation (`state.py:is_valid_pool_address`):**
  - Uses existing project dependency `solders.pubkey.Pubkey` (`from solders.pubkey import Pubkey`; zero new dependencies added).
  - Rejects non-strings, any whitespace, empty strings, placeholder strings (`none`, `null`, `unknown`, `n/a`, `na`, `undefined`), the System Program ID (`11111111111111111111111111111111`), `pair_address == mint`, invalid Base58 characters, and decoded byte lengths `!= 32` via `Pubkey.from_string(cleaned)` + `len(bytes(parsed)) == 32 and str(parsed) == cleaned`.
- **Unit Test Coverage:** `test_e1_solana_pubkey_validation_rejects_malformed_addresses` verifies rejection of `None`, `""`, whitespace, `"!!!!"`, `"PoolAddr0"`, URLs, invalid Base58 (`"0OIl" * 11`), `"1" * 32`, short/long Base58 strings (`"2" * 10`, `"2" * 50`), and `MINT_LIFE`.

---

## 6. Multi-Pool Integration Test Setup & Assertions (`L-1B`)

**Test:** `tests/test_e1_pool_identity.py::test_e1_position_monitor_multi_pool_jupiter_miss_and_reentry_pinning`
- **Part 1 (Open `IN_POSITION` with Jupiter Price Miss):**
  - **Setup:** Position pinned to `POOL_PINNED` (`pumpswap`, `$65,000` liq, `priceNative="0.00052"`, `buys=40, sells=10`). Competing `POOL_COMPETING` (`raydium`, `$500,000` liq) returns `priceNative="0.00010"` (-80% dump) and `buys=1, sells=99`. Jupiter `/price/v3` omits `MINT_MULTI`.
  - **Assertions:** `monitor_event == "HOLD_IN_POSITION"`, `pair_address == POOL_PINNED`, `dex_id == "pumpswap"`, `current_sol_value ≈ 0.52`, `last_liquidity_usd ≈ 65,000.0`, `last_5m_buy_ratio ≈ 0.80`, and `last_price_source == "dexscreener_native"` (proving `POOL_COMPETING` was not used for fallback price, flow, or liquidity).
- **Part 2 (`WATCHING_REENTRY` with Missing Pinned Pool):**
  - **Setup:** `WATCHING_REENTRY` position pinned to `POOL_PINNED`; DexScreener returns only `POOL_COMPETING` with bullish `$500k` liq and `80/10` buy flow.
  - **Assertions:** `watch_out["pending_order"] is None`, `active_position["pair_address"] == POOL_PINNED`, and `pool_observation_status == "POOL_MIGRATED_OR_MISSING"` (no silent substitution for reentry scoring or fallback price).

---

## 7. `PARTIAL_SELL` Persistence Test Lifecycle & Assertions (`L-1A`)

**Test:** `tests/test_e1_pool_identity.py::test_e1_pool_identity_full_lifecycle_watchlist_partial_and_final_settlement`
- **Lifecycle:** Watchlist upsert (`POOL_PINNED`, `pumpswap`) + conflicting upsert (`POOL_COMPETING`, `raydium`) → `alpha_node` with drifted candidate (`POOL_COMPETING`, `raydium`) → `executor_node` `BUY` (`1,000,000` tokens) → DB reload & tamper attempt (`update_lane_positions_only`) → `executor_node` `PARTIAL_SELL` (`500,000` tokens, `PARTIAL_TP_50PCT (+15%)`) → `executor_node` final `SELL` (`500,000` tokens, `TRAILING_TP`).
- **Assertions:**
  - After `PARTIAL_SELL`: `focus_state == "IN_POSITION"`, `len(chart_positions) == 1`, `partial_tp_taken is True`, `token_amount == 500_000`, `pair_address == POOL_PINNED`, `dex_id == "pumpswap"`.
  - After final `SELL`: `trades` contains `["BUY", "PARTIAL_SELL", "SELL"]` all with `(POOL_PINNED, pumpswap)`; `action_log` contains `DECISION_1`, `BUY`, `PARTIAL_SELL`, and `SELL` all with `(POOL_PINNED, pumpswap)`.

---

## 8. Regression Test Commands & Results

**Exact Command Executed Against Final Working Tree:**
```bash
venv/bin/pytest -q tests/test_e1_pool_identity.py tests/test_sprint1_instrumentation.py tests/test_breakout_isolation.py tests/test_breakout_ignition.py tests/test_alpha_and_executor.py tests/test_action_ledger.py tests/test_scanner_and_safety.py tests/test_scalper_entry_engine.py tests/test_sprint2a_accounting.py tests/test_dual_lane_3x3_and_whale_breakout.py tests/test_focus_machine_transitions.py tests/test_rotating_watchlist.py tests/test_replay_simulator.py tests/test_export_soak_report.py tests/test_two_slot_and_whale_hardening.py tests/test_whale_control_separation.py
```
- **Results:** **`79 passed, 0 failed, 0 skipped, 0 errored in 22.22s`**

---

## 9. DB/WAL SHA-256 Verification Results

Verified unchanged against baseline `pre_hashes.txt` via `sha256sum -c /tmp/phase_e1_pool_identity/pre_hashes.txt`:
- `reports/phase1/solana_meme_agent.duckdb`: **`OK`** (`fd5fe1ef0517eda7a23fe8d0240d8bbec058b5584f3ce4d4a505f1a3e8dc3abd`)
- `reports/phase1/solana_meme_agent.duckdb.wal`: **`OK`** (`771c2ce29a60edc121b42427317aeab48b665cedcab99ad98fd108eaf7e5252b`)

---

## 10. Forbidden Files Untouched

Confirmed zero modifications to: `breakout_ignition.py`, `scalper_entry_engine.py`, `chart_exit_engine.py`, `ohlcv_client.py`, `graph.py`, `config.py`, and `.env`.

---

## 11. Final Git Status & Commit Lineage

- `ea0c187e66f5315a2bf103254e69a1d9c3303e64`: Directive 17 (E1 base implementation)
- `99df27e1e0fdb8bed03ff1747aaf0199108deab9`: Directive 19 (E1 corrective implementation + initial reports)
- Directive 20 commit on `dev_1` preserves `e1_corrective_diff.patch`, updated `e1_corrective_report.md`, and updated `artifact_preservation_report.md`, pushed to `origin/dev_1`.

---

## 12. Safety & Non-Modification Confirmation

Explicitly confirmed: No Raspberry Pi access, no running service restarts or alterations, no deployed configuration or `.env` changes, and no live trading execution were performed.
