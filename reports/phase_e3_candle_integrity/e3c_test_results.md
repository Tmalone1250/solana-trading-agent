# Phase E3C — Test Results

- **Implementation Branch:** `impl/e3c-parser-cache-integrity`
- **E3B Baseline Commit:** `35ef3b77163e302f564e0133c9e4a216490aae1d` (`impl/e3b-cache-provenance`)
- **Date:** 2026-11-27

---

## 1. Focused E3C, E3B, E3A, & Entry Suites

Command:
```bash
./venv/bin/pytest tests/test_e3c_parser_cache_admission.py tests/test_e3b_cache_provenance.py tests/test_e3a_candle_eligibility.py tests/test_breakout_ignition.py tests/test_breakout_isolation.py tests/test_scalper_entry_engine.py tests/test_dual_lane_3x3_and_whale_breakout.py tests/test_e1_pool_identity.py tests/test_alpha_and_executor.py tests/test_whale_scalp.py tests/test_whale_control_separation.py -q
```
Result:
- **`72 passed in 4.70s`** (0 failed, 0 skipped)

---

## 2. Full `tests/` Directory Suite

Command:
```bash
./venv/bin/pytest tests/ -q --tb=line
```
Result:
- **`170 passed, 3 failed in 27.62s`** (0 skipped)

### Pre-Existing Baseline Failures (Verified at `a5d829a` and `19104e3`)
1. `tests/test_insider_scanner.py::test_unconditional_hard_stop_loss_on_leg2_reentry_chart_and_whale_positions_at_minus_11_75_pct` (`test_insider_scanner.py:428`: `assert 0 == 1`)
2. `tests/test_outcome_tracker.py::test_paper_tx_fee_sol_deducted_on_buy_partial_sell_and_sell` (`test_outcome_tracker.py:183`: `KeyError: 'sol_balance'` after `MISSING_OR_INVALID_POOL_IDENTITY`)
3. `tests/test_soak_readiness.py::test_signal_and_telemetry_completeness_mc_usd_exact_quote_and_live_momentum_cache` (`test_soak_readiness.py:423`: `assert (None is not None)`)

All three failures were independently confirmed on the pre-E3A baseline and are unrelated to Phase E3C.
