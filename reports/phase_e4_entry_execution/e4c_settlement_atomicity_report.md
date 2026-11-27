# Phase E4C Corrective Pass Report — Post-Persistence Reservation Release & Transaction Boundary

**Repository:** `Tmalone1250/solana-trading-agent`  
**Branch:** `impl/e4c-reservation-risk`  
**Starting Commit:** `b436a064bfe2306e47c13c5b6bdae378926462cb`  
**Date:** 2026-11-27

---

## 1. Blocking Finding Resolved

In `state_ledger.py`, `apply_trade_settlement()` previously called `release_buy_reservation()` immediately after acquiring `db._lock` and before executing DuckDB persistence. If a subsequent DuckDB operation raised, `executor_node.py` called `mark_buy_reservation_broadcast_unresolved()`, but the reservation had already been deleted.

## 2. Changes Implemented

1. **Explicit DuckDB Transaction Boundary (`state_ledger.py`)**:
   - Wrapped the DuckDB read, UTC daily rollover, and `UPDATE agent_state` inside `apply_trade_settlement()` in `BEGIN TRANSACTION` ... `COMMIT` with `ROLLBACK` on any `BaseException`.
   - Moved `release_buy_reservation(db, reservation_id)` to execute **only after `COMMIT` succeeds**, while still holding `db._lock`, so no concurrent `BUY` can observe a released reservation between the balance/position update and reservation cleanup.
   - If any DuckDB failure occurs, `ROLLBACK` prevents partial position/balance persistence, the reservation remains intact, and `executor_node.py` transitions it to `BROADCAST_UNRESOLVED`.
   - Preserved `SELL` / `PARTIAL_SELL` accounting and guard semantics.
2. **Regression Tests (`tests/test_e4c_reservation_risk.py`)**:
   - Added `test_e4c_post_broadcast_settlement_failure_retains_reservation_and_rolls_back_db` verifying:
     - Direct `apply_trade_settlement()` failure rolls back DuckDB state and retains the reservation.
     - End-to-end live `executor_node` post-broadcast settlement failure transitions the reservation to `BROADCAST_UNRESOLVED`, leaves `sol_balance` and `chart_positions` rolled back, and blocks subsequent duplicate `BUY`s (`DUPLICATE_MINT`).
     - Successful live `BUY` settlement still releases its reservation cleanly.

---

## 3. Test Results

- **Focused E4C suite** (`venv/bin/pytest -q tests/test_e4c_reservation_risk.py`): **10 passed in 0.92s** (0 failed, 0 skipped).
- **Full repository suite** (`venv/bin/pytest -q tests/`): **180 passed, 3 failed in 31.20s** (0 regressions; the 3 failures are the same known E1 pool-pinning baseline fixture gaps in `test_insider_scanner.py`, `test_outcome_tracker.py`, and `test_soak_readiness.py`).
