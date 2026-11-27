# Phase E4D Corrective Pass Report — Atomic Reconciled Settlement, Missing-Tx Guard & Decimal Persistence

**Repository:** `Tmalone1250/solana-trading-agent`  
**Branch:** `impl/e4d-transaction-reconciliation`  
**Starting Commit:** `a6179174f25b70e83e48e5cf7747c29dcf239651`  
**Date:** 2026-11-27

---

## 1. Findings Resolved

1. **Finding 1 — Atomic & Idempotent Reconciled BUY Persistence (`state_ledger.py`, `tx_reconciler.py`, `db.py`)**:
   - Extended `apply_trade_settlement()` with `reconciled_trade_row` (`_insert_reconciled_trade_locked()`) so position persistence, SOL/PnL persistence, `active_watchlist` cleanup, reconciled `BUY` `trades` insertion (using the existing `trades` schema), and final `RECONCILED` `order_intents` status all execute inside **one atomic DuckDB transaction** (`BEGIN TRANSACTION` ... `COMMIT`, `ROLLBACK` on any exception).
   - Reservation release executes only after `COMMIT` succeeds.
   - Guarded `transition_order_intent()` against backward transitions from terminal states (`RECONCILED`, `FAILED`).
2. **Finding 2 — Missing / Malformed `getTransaction` Never Establishes Terminal Failure (`tx_reconciler.py`)**:
   - In `reconcile_single_intent()`, when `getSignatureStatuses` reports `err != None`, required `isinstance(tx_obj, dict) and isinstance(tx_obj.get("meta"), dict)` and `tx_obj["meta"].get("err") is not None` before transitioning to `FAILED`.
   - Null `getTransaction`, missing/malformed `meta`, or status/`getTransaction` disagreements all remain `UNRESOLVED` with reservation locked.
3. **Finding 3 — Persist Reconciled Token Decimals (`tx_reconciler.py`)**:
   - Persisted `snap["reconciled_decimals"] = attr.get("decimals")` alongside `reconciled_out_tokens` and `reconciled_entry_sol`.
   - On `SETTLEMENT_PENDING_PERSISTENCE` retry, restored `snap.get("reconciled_decimals")` (falling back to `intent.get("token_decimals")` only if `None`), preserving non-6 decimals across settlement failures and retries.

---

## 2. Test Results

- **Focused E4D + E4C suites** (`venv/bin/pytest -q tests/test_e4d_transaction_reconciliation.py tests/test_e4c_reservation_risk.py`): **17 passed in 1.54s** (7 E4D + 10 E4C; 0 failed, 0 skipped).
- **Full repository suite** (`venv/bin/pytest -q tests/`): **187 passed, 3 failed in 33.45s** (0 regressions; the 3 failures are the known E1 pool-pinning baseline fixture gaps in `test_insider_scanner.py`, `test_outcome_tracker.py`, and `test_soak_readiness.py`).
