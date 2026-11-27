# Phase E4B Test Results

- **Branch:** `impl/e4b-execution-pool-integrity`
- **Baseline / Parent:** `b3a0eb824352da65a8f04b5cc7fb132239e3750c`

## 1. Focused Suites (`E4B`, `E4A`, `E4C`, `E4D`, `E2B`, `E3A`, `action_ledger`)

Command:
```bash
venv/bin/pytest -q tests/test_e4b_execution_pool_integrity.py tests/test_e4a_approval_quote_integrity.py tests/test_e4c_reservation_risk.py tests/test_e4d_transaction_reconciliation.py tests/test_e2b_fee_accounting.py tests/test_e3a_candle_eligibility.py tests/test_action_ledger.py
```

Result: **`50 passed in 5.12s`**

### `tests/test_e4b_execution_pool_integrity.py` (3 test functions)
1. `test_e4b_live_metis_verified_pinned_pool_and_multi_hop_continuity` — PASSED
2. `test_e4b_live_rejects_mismatched_missing_and_unverifiable_routes_without_signing` — PASSED
3. `test_e4b_paper_telemetry_all_statuses_and_opt_in_enforcement` — PASSED

## 2. Full Test Suite

Command:
```bash
venv/bin/pytest -q tests/
```

Result: **`197 passed, 3 failed in 34.41s`**

### Pre-Existing E1 Baseline Failures (Unchanged, 3 total)
1. `tests/test_insider_scanner.py::test_unconditional_hard_stop_loss_on_leg2_reentry_chart_and_whale_positions_at_minus_11_75_pct`
2. `tests/test_outcome_tracker.py::test_paper_tx_fee_sol_deducted_on_buy_partial_sell_and_sell`
3. `tests/test_soak_readiness.py::test_signal_and_telemetry_completeness_mc_usd_exact_quote_and_live_momentum_cache`

Zero new failures introduced across all other tests.
