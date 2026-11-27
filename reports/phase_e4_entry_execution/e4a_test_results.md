# Phase E4A Test Results

- **Branch:** `impl/e4a-approval-quote-integrity`
- **Baseline:** `f7c04c27279524fe12f442236df3be205a65718b` (post-broadcast corrective pass parent `531272a637905f105d85a2f11ea1e973874b22af`)

## 1. Focused Suites (`E4A`, `E4C`, `E4D`, `E2B`, `E3A`)

Command:
```bash
venv/bin/pytest -q tests/test_e4a_approval_quote_integrity.py tests/test_e4c_reservation_risk.py tests/test_e4d_transaction_reconciliation.py tests/test_e2b_fee_accounting.py tests/test_e3a_candle_eligibility.py
```

Result: **`45 passed in 3.95s`**

### `tests/test_e4a_approval_quote_integrity.py` (7 tests)
1. `test_e4a_alpha_node_selects_exact_pinned_pool_price_and_decimals` — PASSED
2. `test_e4a_approval_provenance_rejects_wrong_pool_invalid_price_decimals_and_stale` — PASSED
3. `test_e4a_live_fails_closed_when_thresholds_unset_and_paper_rejects_zero_out_amount` — PASSED
4. `test_e4a_quote_receipt_rejects_slippage_impact_drift_worst_case_and_expiry_headroom` — PASSED
5. `test_e4a_quote_age_enforced_at_all_three_checkpoints` — PASSED
6. `test_e4a_corrective_strict_parsing_missing_oat_and_router_allowlist` — PASSED
7. `test_e4a_post_broadcast_output_amount_result_strict_and_unresolved_lifecycle` — PASSED

## 2. Full Test Suite

Command:
```bash
venv/bin/pytest -q tests/
```

Result: **`194 passed, 3 failed in 32.25s`**

### Pre-Existing E1 Baseline Failures (Unchanged, 3 total)
1. `tests/test_insider_scanner.py::test_unconditional_hard_stop_loss_on_leg2_reentry_chart_and_whale_positions_at_minus_11_75_pct`
2. `tests/test_outcome_tracker.py::test_paper_tx_fee_sol_deducted_on_buy_partial_sell_and_sell`
3. `tests/test_soak_readiness.py::test_signal_and_telemetry_completeness_mc_usd_exact_quote_and_live_momentum_cache`

Zero regressions introduced across all other tests.
