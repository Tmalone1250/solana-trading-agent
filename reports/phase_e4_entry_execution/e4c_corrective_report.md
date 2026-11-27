# Phase E4C Corrective Pass Report — Lifecycle-Bound Reservation Ownership

**Repository:** `Tmalone1250/solana-trading-agent`  
**Branch:** `impl/e4c-reservation-risk`  
**Starting Commit:** `dbc84a61a3286ce3160bd533661bcc3c608150cf`  
**Date:** 2026-11-27

---

## 1. Finding Resolved

Previously, `reserve_buy_slot()` set a permanently true `active_coroutine=True` flag that prevented `_prune_reservations_locked()` from ever reclaiming an orphaned pre-broadcast reservation if an executor task terminated without releasing it, and `executor_node.py`'s `except BaseException:` unconditionally released a live reservation even after `execute_jupiter_signed_order()` had been called.

## 2. Changes Implemented

1. **Task-Lifecycle-Bound Reservation Ownership (`state_ledger.py`)**:
   - Introduced explicit reservation states: `RESERVATION_STATE_ACTIVE` (`"ACTIVE"`) and `RESERVATION_STATE_BROADCAST_UNRESOLVED` (`"BROADCAST_UNRESOLVED"`).
   - Bound active ownership to a `weakref.ref(asyncio.current_task())` (`owner_task_ref`) alongside `owner_task_id`, evaluated via `_is_reservation_owner_active()` (`task is not None and not task.done()`).
   - Updated `_prune_reservations_locked()`:
     - `BROADCAST_UNRESOLVED` reservations are **never pruned** (even after task completion or `RESERVATION_TTL_SEC`), conserving the lock until E4D durable reconciliation exists.
     - Task-owned `ACTIVE` reservations survive `RESERVATION_TTL_SEC` while the owning task is alive and not done, and are immediately pruned if the owning task has completed (`done()`) without releasing (preventing permanent capacity/SOL leaks).
     - Non-task synchronous callers fall back to `RESERVATION_TTL_SEC`.
   - Added `mark_buy_reservation_broadcast_unresolved()` (with explicit note that E4C locks are in-process only; durable restart recovery belongs to E4D).
2. **Executor Broadcast & Exception Boundaries (`executor_node.py`)**:
   - Tracked `broadcast_attempted` (flipped to `True` immediately before `execute_jupiter_signed_order()` in live mode) and `settled_buy`.
   - Pre-broadcast exceptions, post-quote/pre-broadcast exceptions (e.g., missing `transaction`), and `asyncio.CancelledError` release `reservation_id`.
   - Any post-broadcast live exception or settlement error transitions `reservation_id` to `BROADCAST_UNRESOLVED` and **never releases** the lock.
3. **Regression Tests (`tests/test_e4c_reservation_risk.py`)**:
   - Verified active `asyncio.Task` survives `RESERVATION_TTL_SEC` and completed task's abandoned pre-broadcast reservation is pruned on next reserve.
   - Verified pre-broadcast `asyncio.CancelledError` and post-quote/pre-broadcast exceptions release reservations cleanly.
   - Verified live timeout during `/swap/v2/execute` transitions reservation to `BROADCAST_UNRESOLVED`, remains locked past task completion and TTL, and blocks duplicate `BUY`s.
   - Preserved concurrent BUY serialization and cross-thread RPC-outside-`db._lock` verification.

---

## 3. Test Results

- **Focused E4C suite** (`venv/bin/pytest -q tests/test_e4c_reservation_risk.py`): **9 passed in 0.78s** (0 failed, 0 skipped).
- **Full repository suite** (`venv/bin/pytest -q tests/`): **179 passed, 3 failed in 32.35s** (0 regressions; the 3 failures are the same known E1 pool-pinning baseline fixture gaps in `test_insider_scanner.py`, `test_outcome_tracker.py`, and `test_soak_readiness.py`).
