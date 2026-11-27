# Phase E4E Test Results

- **Branch:** `impl/e4e-safety-freshness`
- **Baseline:** `7ca340f1f9c309dd9dafd9291b0d324c7ac76226` (corrective pass 3 parent `dc16be9d8b27f8d3db4ef66db44857b95aae311e`)

## 1. Focused E4E Suite

Command:
```bash
venv/bin/pytest -q tests/test_e4e_safety_freshness.py
```

Result: **`12 passed in 2.27s`**

1. `test_e4e_additive_migration_and_null_timestamp_treated_stale` — PASSED
2. `test_e4e_timestamp_validation_edges` — PASSED
3. `test_e4e_watchlist_ttl_reuse_expiry_and_no_refresh_on_failure` — PASSED
4. `test_e4e_pre_execution_safety_unset_fresh_stale_and_lock_freedom` — PASSED
5. `test_e4e_pre_execution_rejects_shield_issus_api_error_and_pool_mismatch` — PASSED
6. `test_e4e_corrective_no_unconfigured_memory_cache_bypass` — PASSED
7. `test_e4e_corrective_incomplete_evidence_fails_closed_without_eviction` — PASSED
8. `test_e4e_corrective_paper_and_non_breakout_live_require_ttl_and_sell_unaffected` — PASSED
9. `test_e4e_corrective_pinned_pool_required_before_any_safety_or_quote` — PASSED
10. `test_e4e_fresh_watchlist_still_requires_explicit_token_v2_is_sus` — PASSED
11. `test_e4e_nested_empty_jupiter_shield_rejected_without_eviction` — PASSED
12. `test_e4e_safety_gate_unavailable_does_not_evict_while_confirmed_and_mixed_do` — PASSED

## 2. E4A–E4D & E1–E3 Regression Suites

Command:
```bash
venv/bin/pytest -q tests/test_e4a_approval_quote_integrity.py tests/test_e4b_execution_pool_integrity.py tests/test_e4c_reservation_risk.py tests/test_e4d_transaction_reconciliation.py tests/test_e2b_fee_accounting.py tests/test_e3a_candle_eligibility.py tests/test_scanner_and_safety.py tests/test_rotating_watchlist.py tests/test_leg2_predictor.py tests/test_jupiter_token_client.py tests/test_memory_bounds_and_backoff.py tests/test_focus_machine_transitions.py
```

Result: **`78 passed in 11.85s`**

## 3. Full Test Suite

Command:
```bash
venv/bin/pytest -q tests/
```

Result: **`209 passed, 3 failed in 35.40s`**

### Pre-Existing E1 Baseline Failures (Unchanged, 3 total)
1. `tests/test_insider_scanner.py::test_unconditional_hard_stop_loss_on_leg2_reentry_chart_and_whale_positions_at_minus_11_75_pct`
2. `tests/test_outcome_tracker.py::test_paper_tx_fee_sol_deducted_on_buy_partial_sell_and_sell`
3. `tests/test_soak_readiness.py::test_signal_and_telemetry_completeness_mc_usd_exact_quote_and_live_momentum_cache`

Zero new failures introduced across all other tests. `solana_meme_agent.duckdb` (`sha256: ef8a11baf9537e12d91873933f202a68af37ba1566253727caae5d8dd6ecbc03`) and `.env` were not modified.
