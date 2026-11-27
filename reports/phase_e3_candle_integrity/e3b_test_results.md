# Phase E3B — Test Results

- **Implementation Branch:** `impl/e3b-cache-provenance`
- **Source Baseline Commit:** `6492acd4f39522aef71bb814bb567288f7b5993a` (`impl/e3a-candle-eligibility`)
- **Approved Design Commit:** `e40c4f2928019b0346d9985fed4ebe06ba005c83` (`design/e3b-cache-provenance`)
- **Date:** 2026-11-27

---

## 1. Focused E3B & E3A Regression Suites

Command:
```bash
./venv/bin/pytest tests/test_e3b_cache_provenance.py tests/test_e3a_candle_eligibility.py -q
```
Result:
- **`18 passed in 0.83s`** (0 failed, 0 skipped)

Prior focused breakout/entry/E1/dual-lane run:
```bash
./venv/bin/pytest tests/test_e3b_cache_provenance.py tests/test_e3a_candle_eligibility.py tests/test_breakout_ignition.py tests/test_breakout_isolation.py tests/test_scalper_entry_engine.py tests/test_dual_lane_3x3_and_whale_breakout.py tests/test_e1_pool_identity.py -q
```
Result:
- **`49 passed in 3.27s`** (0 failed, 0 skipped)

---

## 2. Full `tests/` Directory Suite

Command:
```bash
./venv/bin/pytest tests/ -q --tb=line
```
Result:
- **`160 passed, 3 failed in 34.05s`** (0 skipped)

### Pre-Existing Baseline Failures (Verified at `a5d829a` and `19104e3`)
1. `tests/test_insider_scanner.py::test_unconditional_hard_stop_loss_on_leg2_reentry_chart_and_whale_positions_at_minus_11_75_pct` (`test_insider_scanner.py:428`: `assert 0 == 1`)
2. `tests/test_outcome_tracker.py::test_paper_tx_fee_sol_deducted_on_buy_partial_sell_and_sell` (`test_outcome_tracker.py:183`: `KeyError: 'sol_balance'` after `MISSING_OR_INVALID_POOL_IDENTITY`)
3. `tests/test_soak_readiness.py::test_signal_and_telemetry_completeness_mc_usd_exact_quote_and_live_momentum_cache` (`test_soak_readiness.py:423`: `assert (None is not None)`)

All three failures were independently confirmed on the pre-E3A baseline and are unrelated to Phase E3B.
