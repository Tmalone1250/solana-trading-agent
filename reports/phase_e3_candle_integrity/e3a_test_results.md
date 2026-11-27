# Phase E3A — Test Results

- **Branch:** `impl/e3a-candle-eligibility`
- **Baseline Commit:** `a5d829a` (`audit/e3-candle-integrity-20261127`)
- **Date:** 2026-11-27

---

## 1. Focused E3A & Breakout/Entry Test Suites

Command:
```bash
./venv/bin/pytest tests/test_e3a_candle_eligibility.py tests/test_breakout_ignition.py tests/test_breakout_isolation.py tests/test_scalper_entry_engine.py tests/test_dual_lane_3x3_and_whale_breakout.py tests/test_e1_pool_identity.py -q
```

Result:
- **`43 passed in 2.98s`** (0 failed, 0 skipped)

---

## 2. Full `tests/` Directory Suite

Command:
```bash
./venv/bin/pytest tests/ -q --tb=line
```

Result:
- **`154 passed, 3 failed in 33.72s`** (0 skipped)

### Pre-Existing Unrelated Failures (3)
1. `tests/test_insider_scanner.py::test_unconditional_hard_stop_loss_on_leg2_reentry_chart_and_whale_positions_at_minus_11_75_pct` (`test_insider_scanner.py:428`: `assert 0 == 1` in `position_monitor_node` / `batched_price_monitor` 503 test).
2. `tests/test_outcome_tracker.py::test_paper_tx_fee_sol_deducted_on_buy_partial_sell_and_sell` (`test_outcome_tracker.py:183`: `KeyError: 'sol_balance'` after `executor_node` blocked `FEE` with `MISSING_OR_INVALID_POOL_IDENTITY`).
3. `tests/test_soak_readiness.py::test_signal_and_telemetry_completeness_mc_usd_exact_quote_and_live_momentum_cache` (`test_soak_readiness.py:414`: `assert (None is not None)` in `position_monitor_node` `WATCHING_REENTRY` test).

None of the 3 failures are related to Phase E3A candle eligibility, timestamp propagation, 1m fallback counting, or 15m aggregation.
