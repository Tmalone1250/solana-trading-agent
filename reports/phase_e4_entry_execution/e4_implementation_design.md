# Phase E4 Implementation Design: `BREAKOUT_IGNITION` Entry Execution & TOCTOU Safeguards

**Document Type:** Implementation Design Only (No Code Changes)  
**Repository:** `Tmalone1250/solana-trading-agent`  
**Design Branch:** `design/e4-entry-execution`  
**Exact Source Baseline:** `ca01c640ac60477e63a33a86217298573680c5f0` (`impl/e3c-parser-cache-integrity`)  
**Audit Reference:** `reports/phase_e4_entry_execution/e4_breakout_ignition_entry_audit.md` (report-only commit `1780d0836af205103887c2e5818a3ed817922c86` on `impl/e3c-parser-cache-integrity`)  
**Date:** 2026-11-27

---

## 1. Executive Summary & Design Objectives

The Phase E4 audit (`1780d08`) confirmed that while `BREAKOUT_IGNITION` entry decisions are protected by Phase E3A–E3C closed-candle/provenance binding and Phase E1 pool pinning at the decision layer, the execution boundary (`executor_node.py`, `state_ledger.py`, `safety_gate_node.py`) is **not safe for unattended live execution**. Specifically, an approved `PendingOrder` crosses into Jupiter quote/sign/submit with no approval-age or price-drift checks, no `priceImpactPct` or slippage enforcement, no pinned-pool route verification, no durable pre-broadcast intent persistence, no live RPC wallet balance check, and reservation/risk-guard gaps under concurrency and timeouts.

### Design Objectives
1. **Fail closed** whenever approval freshness, price/slippage/impact bounds, pinned-pool route identity, safety freshness, or spendable SOL sufficiency cannot be verified.
2. **Eliminate TOCTOU drift** between `alpha_node` approval and `executor_node` submission without invalidating or altering E3A–E3C OHLCV provenance guarantees.
3. **Separate `out_amount <= 0` from empty `tx_signature`** (correcting the audit's E4-04 conflation) so zero/negative token output is rejected before settlement, while missing signature is handled as a validation/reconciliation defect without discarding valid positive-token fills.
4. **Never treat an HTTP timeout as a failed transaction** and never release a reservation or permit a duplicate `BUY` while an on-chain outcome remains `UNRESOLVED`.
5. **Preserve existing invariants**: E1 pool-identity pinning, E2A Exodus/priority protections, E2B fee-accounting semantics (`PAPER_ASSUMED` vs. `UNKNOWN` without fabricating live fees), E3A–E3C closed-candle/provenance binding, and unchanged strategy thresholds.

---

## 2. Exact Baseline & Branch Relationship

```text
6492acd4f39522aef71bb814bb567288f7b5993a  (impl/e3a-candle-eligibility)
  └─▶ 35ef3b77163e302f564e0133c9e4a216490aae1d  (impl/e3b-cache-provenance)
        └─▶ ca01c640ac60477e63a33a86217298573680c5f0  (E3C approved source baseline)
              ├──▶ 1780d0836af205103887c2e5818a3ed817922c86  (impl/e3c-parser-cache-integrity: E4 audit report-only)
              └──▶ design/e4-entry-execution  (THIS DESIGN — branched directly from ca01c64)
```

- **Source baseline:** `ca01c640ac60477e63a33a86217298573680c5f0` (excludes report-only commit `1780d08`).
- **Separate branches preserved untouched:** `dev_1` (`d88e0f3`, E1 + E2A), `dev_2` (`19104e3`, E2B fee accounting), and `impl/e3a*`, `impl/e3b*`, `impl/e3c*`.

---

## 3. Current Entry Lifecycle & Failure Boundaries

1. **`scanner_node.py`**: Selects highest-liquidity pool (`pair_address`, `dex_id`) per mint and persists to DuckDB `watchlist`.
2. **`safety_gate_node.py`**: Checks `jup_is_sus`, then skips RugCheck + Jupiter Shield if `cand["safety_passed"]`, `watchlist_safety[mint]`, or `_safety_cache` (300s TTL) hits.
3. **`alpha_node.py`**: Validates E3A–E3C OHLCV provenance, runs `evaluate_scalper_entry()` (`detect_breakout_ignition`), awaits Gemini Decision 1 (`decision_client.decide()`), checks `is_valid_pool_address()`, and emits an in-memory `PendingOrder` **without** `approved_at` or approval price context.
4. **`executor_node.py`**:
   - Checks strategy allowlist and `is_valid_pool_address(order.pair_address)` (`L141-155`).
   - Calls `db.reserve_buy_slot()` (`state_ledger.py:185`) — checks DuckDB `sol_balance` (omitting `tx_fee_sol` and live wallet RPC), lane/global caps, and duplicate mints, with a `60.0s` in-memory TTL. Does **not** re-check cooldowns or `DAILY_MAX_LOSS_PCT`.
   - Calls `fetch_jupiter_order()` (`GET /swap/v2/order`) with no slippage parameter, no pool constraint, no `priceImpactPct` gate, and no drift check.
   - In `live` mode, signs and calls `execute_jupiter_signed_order()` (`POST /swap/v2/execute`, 20s timeout). Any `BaseException` releases `reservation_id` and leaves `focus_state = SCANNING`.
   - Settles via `apply_trade_settlement()`, which drops `ActivePosition` if `token_amount <= 0` while still deducting `sol_delta`.

---

## 4. Detailed Design: Remediation Groups E4A–E4E

### E4A — Approval & Quote Integrity

**Objective:** Ensure `executor_node` only executes quotes that are timely, bounded in price drift and price impact, and produce a valid positive-token fill.

1. **Approval Context on `PendingOrder` (`state.py`, `alpha_node.py`):**
   Add optional backward-compatible fields to `PendingOrder`:
   - `approved_at: Optional[str] = None` (UTC ISO-8601 timestamp captured when `alpha_node` approves the `BUY`).
   - `approval_price_usd: Optional[float] = None` (from `winner_cand["price_usd"]` or latest closed candle close).
   - `approval_price_native: Optional[float] = None` (SOL-denominated price per token at approval, when available).
   - `approval_ohlcv_fetched_at: Optional[str] = None` / `approval_candle_ts: Optional[int] = None` (propagated from E3B/E3C `OhlcvFetchResult` provenance for auditability — never used to reconstruct a new breakout evaluation).
   - *Rule:* For `BREAKOUT_IGNITION` `BUY` orders, `executor_node` fails closed if `approved_at` is missing or invalid, or if approval age `(now - approved_at)` exceeds `MAX_BUY_APPROVAL_AGE_SECONDS`.

2. **Pre-Quote & Post-Quote Drift Enforcement (`executor_node.py`):**
   - Compare effective quote price (`spent_sol / (out_amount / 10**decimals)` vs. `approval_price_native`, or USD-adjusted equivalent using `SOL` price from batched/quote context) against `MAX_ENTRY_PRICE_DRIFT_PCT`.
   - If `approval_price_native` (or `approval_price_usd`) is absent for `BREAKOUT_IGNITION`, fail closed (`REJECTED_MISSING_APPROVAL_PRICE`).
   - Do **not** re-run `detect_breakout_ignition()` in `executor_node` on stale or ad-hoc inputs; E3A–E3C provenance remains authoritative at `alpha_node`, and `executor_node` enforces freshness (`MAX_BUY_APPROVAL_AGE_SECONDS`) and price continuity (`MAX_ENTRY_PRICE_DRIFT_PCT`).

3. **Quote Response Validation, Slippage & `priceImpactPct` (`executor_node.py`):**
   - Pass explicit `slippageBps` (or Jupiter `/swap/v2/order` equivalent once verified — see Section 11) in `fetch_jupiter_order()` for `BUY` orders, and validate `otherAmountThreshold` / `slippageBps` if returned.
   - Parse `extract_raw_price_impact(order_resp)` as `float`; for `BUY` (and optionally `SELL` warning/cap), if `priceImpactPct` is missing/unparseable or `abs(impact_pct) > MAX_ENTRY_PRICE_IMPACT_PCT` in `BREAKOUT_IGNITION`, reject before signing.
   - Enforce quote validity window: measure `quote_fetched_at` before `fetch_jupiter_order()` and verify `(now - quote_fetched_at) <= MAX_QUOTE_AGE_SECONDS` before signing/submitting.

4. **E4-04 Correction — Separate `out_amount <= 0` and Empty `tx_signature`:**
   - **Check 1 (`out_amount <= 0`):** Immediately after `out_amount` is resolved (both after `fetch_jupiter_order` in paper/live and after `execute_jupiter_signed_order` in live), if `out_amount <= 0`:
     - **Paper mode:** Abort before `apply_trade_settlement()`, release reservation, log `BUY_REJECTED_ZERO_OUTPUT`, do not debit SOL.
     - **Live mode:** If `out_amount <= 0` came from `/swap/v2/order`, abort before signing. If `execute_jupiter_signed_order` returned `outputAmountResult <= 0`, do **not** call `apply_trade_settlement()` (which would debit SOL without creating a position); instead transition the durable order intent to `UNRESOLVED` / `RECONCILE_REQUIRED` and verify on-chain token/SOL balance via RPC.
   - **Check 2 (Empty `tx_signature` in live mode):** If `out_amount > 0` in live mode but `exec_resp` returns an empty `tx_signature`:
     - Do **not** discard the position if on-chain execution succeeded! Use the pre-derived `signed_tx_signature` from `VersionedTransaction` (E4D) or mark the durable order intent `UNRESOLVED` for RPC reconciliation (`getSignatureStatuses` / token balance check) before settling or releasing the reservation.

5. **Numerical Thresholds (Not Invented Here):**
   - `MAX_BUY_APPROVAL_AGE_SECONDS`, `MAX_ENTRY_PRICE_DRIFT_PCT`, `MAX_ENTRY_PRICE_IMPACT_PCT`, `ENTRY_SLIPPAGE_BPS`, `MAX_QUOTE_AGE_SECONDS` must be added to `config.py` with evidence-backed defaults selected from paper/soak telemetry (e.g., distribution of `alpha_node -> executor_node` latency, typical `priceImpactPct` on `$25k+` liquidity pools, and Jupiter quote spread).

---

### E4B — Execution Pool Integrity

**Objective:** Prevent Jupiter from executing a `BREAKOUT_IGNITION` `BUY` through an unpinned or thin secondary pool.

1. **API Contract Reality:**
   - Jupiter `/swap/v2/order` does **not** accept a `pair_address` parameter to force a single AMM pool address. (It supports `excludeDexes` / `dexes` on some Jupiter endpoints, which are DEX-program-level, not pool-address-level.)
2. **Route Inspection (`routePlan`) & Fail-Closed Rejection:**
   - Inspect `order_resp.get("routePlan")` returned by `fetch_jupiter_order()`.
   - Each hop in `routePlan` contains `swapInfo.ammKey` (the pool address) and `swapInfo.label` (DEX name), plus `percent`.
   - For `BREAKOUT_IGNITION` `BUY` orders when `REQUIRE_PINNED_POOL_ROUTE = True`:
     - Require `routePlan` to be a non-empty list.
     - Require that **every** hop touching `order.mint` (or single-hop direct route; multi-hop via intermediate like USDC/SOL allowed only if the `mint` leg uses `ammKey == order.pair_address`, or strict 100% single-pool `ammKey == order.pair_address` if configured).
     - If `routePlan` is absent/empty (unless in a synthetic unit test with explicit mock flag, or tests updated to include `routePlan`) or any `mint`-leg hop uses `ammKey != order.pair_address`, **reject the quote** (`BLOCKED: ROUTE_POOL_MISMATCH`) before signing.
3. **Pre-Execution Pinned-Pool Sanity Cross-Check:**
   - Before signing, verify the quoted execution price against the pinned pool's latest DexScreener `priceNative` / `liquidity.usd` (from `batched_price_monitor` or a targeted lookup) if `routePlan` metadata is partial, failing closed if the pinned pool disappeared (`POOL_MIGRATED_OR_MISSING`) or diverged beyond `MAX_ENTRY_PRICE_DRIFT_PCT`.
4. **Continuity Through Monitoring & Exit:**
   - `ActivePosition.pair_address`, `dex_id`, and `pool_identity_status = "PINNED"` remain unchanged and continue driving `position_monitor_node` (`pinned_pools`) and `SELL` telemetry.

---

### E4C — Reservation, Live-Balance & Risk Guard Integrity

**Objective:** Make `reserve_buy_slot()` fee-aware, live-balance-aware, risk-guard-complete, and immune to mid-flight TTL eviction.

1. **Fee-Aware SOL Sufficiency (`state_ledger.py`, `executor_node.py`):**
   - Fix `reserve_buy_slot()` line 242: check `avail_sol + 1e-9 < (required_sol + tx_fee_sol)` (matching the `required_sol + tx_fee_sol` amount actually reserved on line 251).
   - **Paper mode:** `tx_fee_sol = float(config.PAPER_TX_FEE_SOL)`.
   - **Live mode (Reservation vs. E2B Accounting Separation):** Pass a conservative `LIVE_FEE_RESERVATION_BUFFER_SOL` (covering base tx fee + priority fee + ATA rent `~0.002039` SOL if first token account) **solely** to `reserve_buy_slot()` for spendable-SOL headroom. Do **not** store this buffer as `tx_fee_sol` or `entry_fee_sol`; E2B accounting (`tx_fee_opt = None`, `fee_accounting_status = "UNKNOWN"`) remains untouched.

2. **Live Wallet Balance Verification (Addressing External Tx, Fees, Divergence & RPC Unavailability):**
   - **Where to check:** Inside `executor_node` (right before/around `reserve_buy_slot()`, **not** on every `scanner_node` cycle, avoiding unnecessary RPC polling).
   - **Mechanism:** Call Solana JSON-RPC `getBalance(taker_pubkey, commitment="confirmed")`:
     - Compute `spendable_live_sol = wallet_lamports / LAMPORTS_PER_SOL - LIVE_MIN_SOL_RESERVE` (keeping rent-exempt / fee floor).
     - Compare against `required_sol + LIVE_FEE_RESERVATION_BUFFER_SOL + sum(active_inflight_reservations_sol)`.
     - **Ledger/Wallet Divergence:** If `abs(wallet_sol - duck_db_sol_balance) > BALANCE_DIVERGENCE_WARN_SOL`, log a `BALANCE_DIVERGENCE` warning/action, and always use `min(duck_db_avail_sol, spendable_live_sol)` as the effective cap for `BUY` sufficiency (fail closed if either is insufficient; never silently overwrite DuckDB historical PnL).
     - **RPC Unavailability:** If `getBalance` fails or times out in `live` mode, **fail closed** (`BLOCKED: LIVE_RPC_BALANCE_UNAVAILABLE`) rather than trusting potentially stale DuckDB balance.

3. **Atomic Cooldown & Daily-Loss Circuit Breaker (`state_ledger.py`):**
   - Inside `reserve_buy_slot()` under `db._lock`:
     - Check `cooldowns` table (`expires_at > now_iso`) for `mint`; reject with `ON_COOLDOWN` if active.
     - Check `daily_pnl <= -(daily_start * config.DAILY_MAX_LOSS_PCT)`; reject with `DAILY_MAX_LOSS_HIT` if breached.
   - This closes the race where concurrent `whale_lane` closes a losing trade or triggers a cooldown while `alpha_node` is awaiting Gemini/OHLCV.

4. **Mid-Flight Reservation Expiration & Concurrent Execution:**
   - Separate `RESERVATION_TTL_SEC` from active coroutine ownership: add `active_coroutine: bool` / `heartbeat_ts` (or `extend_buy_reservation(db, res_id)` heartbeat / state `PREPARED`/`SUBMITTED`).
   - `_prune_reservations_locked()` must **never** prune a reservation whose durable order intent is in `PREPARED`, `SUBMITTED`, or `UNRESOLVED` state, or whose in-process coroutine is still active (only prune orphaned `RESERVED` entries after `RESERVATION_TTL_SEC` if a coroutine somehow leaked without cleanup).

---

### E4D — Transaction Lifecycle Integrity

**Objective:** Eliminate duplicate live `BUY`s and untracked positions across HTTP timeouts, network errors, and process restarts.

1. **Durable Order Intent (`order_intents` table in DuckDB):**
   Persist an `order_intents` row under `db._lock` **before** external Jupiter broadcast in `live` mode (and optionally in `paper` mode for uniform lifecycle tracking), with explicit states:
   - `PREPARED`: Reservation acquired, quote validated, signed transaction created; `tx_signature` derived deterministically from `VersionedTransaction.signatures[0]` **before** `POST /swap/v2/execute`, along with `jupiter_request_id`.
   - `SUBMITTED`: Written to DuckDB immediately before calling `execute_jupiter_signed_order()`.
   - `UNRESOLVED`: Transitioned on HTTP timeout, connection error, 5xx, empty `tx_signature`, or `outputAmountResult <= 0` after broadcast. **Reservation remains locked** and `mint` remains blocked from new `BUY`s while `UNRESOLVED`.
   - `CONFIRMED`: Confirmed on-chain with positive token delta; proceeds to `apply_trade_settlement()`.
   - `FAILED`: Confirmed failed on-chain (`meta.err != None`) or definitively expired (`lastValidBlockHeight` exceeded with `getSignatureStatuses == None`); safe to release reservation.
   - `RECONCILED`: Terminal recovery state after startup or background reconciliation resolves an `UNRESOLVED` intent (either settled into `ActivePosition` or marked `FAILED` and released).
   - `SETTLEMENT_PENDING_PERSISTENCE`: Special guard state if on-chain `CONFIRMED` succeeded but DuckDB `apply_trade_settlement()` raised an error — prevents releasing reservation or re-entering `mint` and retries persistence on next tick/startup.

2. **Ambiguous Outcome & RPC Reconciliation (`reconcile_unresolved_intents`):**
   - On timeout/error during `execute_jupiter_signed_order()`: transition intent to `UNRESOLVED` (do **not** call `release_buy_reservation()`).
   - Run reconciliation (and on startup in `main.py` before starting `run_autonomous_loop` / `run_whale_lane_loop`):
     1. Query RPC `getSignatureStatuses([tx_signature], searchTransactionHistory=True)`.
     2. If `confirmationStatus in ("confirmed", "finalized")` and `err is None`:
        - Query `getTransaction` or `getTokenAccountsByOwner(taker, mint)` to obtain actual acquired `token_amount` (and actual lamport delta if needed), create `ActivePosition` via `apply_trade_settlement()`, mark intent `RECONCILED`, and release reservation.
     3. If `err is not None`: mark intent `FAILED`, release reservation.
     4. If status is `None`: check if transaction blockhash expired (`isBlockhashValid` / `getBlockHeight > last_valid_block_height`); if expired, mark `FAILED` and release; if still valid, keep `UNRESOLVED` and re-check next tick.

3. **Duplicate-Submission Prevention:**
   - `reserve_buy_slot()` checks `order_intents WHERE status IN ('PREPARED', 'SUBMITTED', 'UNRESOLVED', 'SETTLEMENT_PENDING_PERSISTENCE')` under `db._lock` and blocks any new `BUY` for the same `mint` (or exceeding lane/global caps / SOL).

---

### E4E — Safety Freshness

**Objective:** Prevent stale safety approvals (`300s` `_safety_cache` or unbounded `watchlist.safety_passed`) from allowing a `BUY` after LP unlock or Jupiter Shield `isSus` changes.

1. **Watchlist `safety_passed` TTL:**
   - Store `safety_checked_at` alongside `safety_passed` in `watchlist` (or stop treating `cand["safety_passed"] == True` / `watchlist_safety[mint] == True` as permanently valid).
   - `safety_gate_node()` must re-verify if `(now - safety_checked_at) > SAFETY_CACHE_TTL_SECONDS` (e.g., 300s for watchlist soak, and much tighter at execution).
2. **Pre-Execution Safety Check at `executor_node` (`BUY` only):**
   - Immediately before `fetch_jupiter_order()` (or before signing) for `BUY` orders, run an inexpensive pre-execution safety check:
     - **Jupiter Shield (`/ultra/v1/shield`) + Jupiter Token v2 `isSus`** (lightweight single-mint check) and/or `check_token_safety()` if cached safety age exceeds `PRE_EXECUTION_SAFETY_TTL_SECONDS`.
     - **Fail-closed:** In `live` mode (and optionally configurable in `paper`), if pre-execution safety fails or the safety API is unavailable/errors, abort the `BUY`, release reservation (since no swap was broadcast), and evict/cooldown on confirmed safety failure.

---

## 5. State & Database Schema Changes, Migration & Restart Behavior

- **`PendingOrder` (`state.py`)**: Add optional `approved_at`, `approval_price_usd`, `approval_price_native`, `approval_ohlcv_fetched_at`, `approval_candle_ts` (defaults `None` for backward compatibility with existing tests/`SELL` orders).
- **`watchlist` table (`db.py`)**: Additive idempotent migration `ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS safety_checked_at VARCHAR`. Existing rows with `NULL` `safety_checked_at` are treated as stale and re-checked on next `safety_gate_node` / pre-execution check.
- **New `order_intents` table (`db.py`)**:
  - Columns: `intent_id VARCHAR PRIMARY KEY`, `lane VARCHAR`, `mint VARCHAR`, `symbol VARCHAR`, `pair_address VARCHAR`, `dex_id VARCHAR`, `side VARCHAR`, `strategy VARCHAR`, `amount_atomic BIGINT`, `status VARCHAR`, `tx_signature VARCHAR`, `jupiter_request_id VARCHAR`, `last_valid_block_height BIGINT`, `reserved_sol DOUBLE`, `error_detail VARCHAR`, `created_at VARCHAR`, `updated_at VARCHAR`.
- **Restart Behavior (`main.py`)**:
  - On startup, before launching scanner/alpha loops, query `order_intents WHERE status IN ('PREPARED', 'SUBMITTED', 'UNRESOLVED', 'SETTLEMENT_PENDING_PERSISTENCE')`:
    - `PREPARED` (never broadcast): mark `FAILED` (`ABORTED_ON_RESTART`).
    - `SUBMITTED` / `UNRESOLVED`: promote to `UNRESOLVED` and run RPC signature/balance reconciliation before allowing new `BUY`s.
    - `SETTLEMENT_PENDING_PERSISTENCE`: retry `apply_trade_settlement()` from persisted intent + RPC token balance.

---

## 6. Paper vs. Live Behavioral Differences

| Dimension | Paper Mode (`EXECUTION_MODE="paper"`) | Live Mode (`EXECUTION_MODE="live"`) |
|---|---|---|
| **Wallet Balance Check** | DuckDB `sol_balance - reserved_sol` (including `PAPER_TX_FEE_SOL`) | `min(DuckDB avail, RPC getBalance - LIVE_MIN_SOL_RESERVE - reserved)` (fail closed if RPC unavailable) |
| **Fee Reservation** | `PAPER_TX_FEE_SOL` (`0.001`) | `LIVE_FEE_RESERVATION_BUFFER_SOL` (headroom only; E2B `tx_fee_sol` stays `None`/`UNKNOWN`) |
| **Route Pool Check** | Inspect `routePlan` if present (configurable strictness for mocks) | Strict fail-closed `routePlan` pinned-pool verification |
| **Tx Signing & Intent** | Synthetic `paper-buy-...`; no RPC broadcast | `VersionedTransaction` pre-signed, `tx_signature` + `requestId` persisted `SUBMITTED` before `execute` |
| **Ambiguous Outcome** | Quote error aborts cleanly & releases reservation | Timeout/error transitions intent to `UNRESOLVED`, keeps reservation locked, reconciles via RPC |
| **Empty `tx_signature` / `out <= 0`** | `out <= 0` aborts before settlement | `out <= 0` -> `UNRESOLVED`/reconcile (never debit SOL without position); empty `tx_signature` -> use pre-derived sig + RPC reconcile |

---

## 7. Concurrency & Atomicity Analysis

- **Shared-process `chart_lane` + `whale_lane`:** Both run in one `asyncio` event loop sharing `db._lock`.
- **Atomic boundary (`reserve_buy_slot` under `db._lock`):** Checks `agent_state` + `order_intents` (active `PREPARED`/`SUBMITTED`/`UNRESOLVED`) + `_inflight_buy_reservations` + `cooldowns` + `DAILY_MAX_LOSS_PCT` + `avail_sol >= required_sol + tx_fee_sol` in one lock acquisition.
- **No mid-flight TTL race:** Active coroutine reservations and `SUBMITTED`/`UNRESOLVED` intents are exempt from TTL pruning.
- **Crash/restart atomicity:** `order_intents` persists across restarts, closing the in-memory-only gap (`E4-09`).

---

## 8. Failure-State Transition Diagram (Live `BUY`)

```text
[alpha_node approves]
        │  (PendingOrder + approved_at + approval_price)
        ▼
[reserve_buy_slot + cooldown + daily_loss + RPC balance]
   ├──(fail)──▶ REJECTED (no reservation, stay SCANNING)
   ▼ (ok: reservation_id)
[E4E pre-exec safety + E4A quote + E4B routePlan + E4A drift/impact]
   ├──(fail)──▶ release_buy_reservation() -> ABORTED (stay SCANNING)
   ▼ (ok: sign tx, derive tx_signature)
[DB: order_intents = PREPARED -> SUBMITTED]
        │
        ▼  POST /swap/v2/execute
   ├──(HTTP 200 & sig & out_amount > 0)──▶ apply_trade_settlement()
   │                                           ├──(ok)──▶ CONFIRMED -> IN_POSITION
   │                                           └──(db err)──▶ SETTLEMENT_PENDING_PERSISTENCE
   │
   └──(Timeout / Net Error / empty sig / out_amount <= 0)──▶ UNRESOLVED (reservation LOCKED!)
                                                                 │
                                                                 ▼  RPC getSignatureStatuses + balance
                                                            ├──(confirmed on-chain)──▶ settle -> RECONCILED
                                                            └──(failed / blockhash expired)──▶ FAILED -> release
```

---

## 9. Narrow Implementation Sequence (Independent Commits & Tests)

1. **Commit 1 — E4A (Approval & Quote Integrity):** `state.py`, `config.py`, `alpha_node.py`, `executor_node.py` + `tests/test_e4a_approval_and_quote.py`.
2. **Commit 2 — E4B (Execution Pool Integrity):** `executor_node.py` (`routePlan` parser & pinned-pool check) + `tests/test_e4b_execution_pool.py`.
3. **Commit 3 — E4C (Reservation, Live-Balance & Risk Guards):** `state_ledger.py`, `executor_node.py`, `config.py` + `tests/test_e4c_reservation_and_balance.py`.
4. **Commit 4 — E4D (Durable Order Intents & RPC Reconciliation):** `db.py`, `executor_node.py`, `main.py` + `tests/test_e4d_tx_lifecycle.py`.
5. **Commit 5 — E4E (Pre-Execution Safety Freshness):** `db.py`, `safety_gate_node.py`, `executor_node.py` + `tests/test_e4e_safety_freshness.py`.

---

## 10. Regression Tests Preserving E1, E2A, E2B, and E3A–E3C

Every E4 commit must run and pass the full existing regression suite:
- **E1**: `tests/test_e1_pool_identity.py` (pool pinning, invalid pool rejection, multi-pool isolation).
- **E2A**: `tests/test_e2a_exit_integrity.py`, `tests/test_e2a_watching_reentry.py` (Exodus priority & confirmation/warm-up).
- **E2B**: `tests/test_e2b_fee_accounting.py` (paper vs. live `UNKNOWN` fees, net breakeven; note `test_e2b_fee_accounting.py` lives on `dev_2` and should be verified when E2B/E4 are integrated).
- **E3A–E3C**: `tests/test_breakout_ignition.py`, `tests/test_scalper_entry_engine.py`, `tests/test_breakout_isolation.py`, and E3A/E3B/E3C OHLCV provenance tests.
- **E4-04 specific**: Separate tests for (a) `out_amount <= 0` aborting/reconciling without debiting SOL and dropping position, and (b) empty `tx_signature` with `out_amount > 0` triggering signature/RPC reconciliation without discarding a valid position.

---

## 11. Explicit Assumptions, API Uncertainties & Unresolved Decisions

1. **Solana RPC Endpoint (`SOLANA_RPC_URL`):** `config.py` currently has no `SOLANA_RPC_URL` field. Live `getBalance`, `getSignatureStatuses`, and `getTokenAccountsByOwner` require adding `SOLANA_RPC_URL` (or deriving a Helius RPC URL when `HELIUS_API_KEY` is set).
2. **Jupiter `/swap/v2/order` Exact Schema:** `routePlan` (`swapInfo.ammKey`), `slippageBps`, `otherAmountThreshold`, and `lastValidBlockHeight` fields in `/swap/v2/order` responses must be verified against a live dry-run `/swap/v2/order` response before finalizing E4A/E4B/E4D field names.
3. **Threshold Calibration:** Exact numerical values for `MAX_BUY_APPROVAL_AGE_SECONDS`, `MAX_ENTRY_PRICE_DRIFT_PCT`, `MAX_ENTRY_PRICE_IMPACT_PCT`, `ENTRY_SLIPPAGE_BPS`, `LIVE_FEE_RESERVATION_BUFFER_SOL`, and `PRE_EXECUTION_SAFETY_TTL_SECONDS` are intentionally deferred to evidence-based calibration from soak/telemetry logs.
4. **Multi-Hop Routes:** Decision required before E4B implementation on whether to require 100% single-pool direct routes (`len(routePlan) == 1 and ammKey == pair_address`) or allow multi-hop routes as long as the target-mint hop uses `pair_address`.

---

## 12. Acceptance Criteria

- **E4A:** Stale approvals, missing approval price, excessive drift, excessive `priceImpactPct`, and `out_amount <= 0` are rejected before settlement; empty `tx_signature` with `out_amount > 0` enters reconciliation rather than discarding the position.
- **E4B:** `BREAKOUT_IGNITION` `BUY` rejects any quote whose `routePlan` cannot be verified against `order.pair_address`.
- **E4C:** `reserve_buy_slot()` enforces `required_sol + tx_fee_sol`, `cooldowns`, `DAILY_MAX_LOSS_PCT`, live RPC spendable balance (failing closed on RPC outage), and never evicts active/unresolved reservations mid-flight.
- **E4D:** Live `BUY` persists `PREPARED -> SUBMITTED` with pre-derived `tx_signature` before `/swap/v2/execute`; timeouts transition to `UNRESOLVED` (blocking duplicate `BUY`s) and reconcile via RPC on next tick/startup.
- **E4E:** Expired `watchlist.safety_passed` or stale `_safety_cache` triggers revalidation before execution and fails closed on safety failure or API unavailability.
