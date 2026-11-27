# Phase E4 Implementation Design: `BREAKOUT_IGNITION` Entry Execution & TOCTOU Safeguards

**Document Type:** Implementation Design Only (No Code Changes)  
**Repository:** `Tmalone1250/solana-trading-agent`  
**Design Branch:** `design/e4-entry-execution`  
**Exact Application-Source Baseline:** `ca01c640ac60477e63a33a86217298573680c5f0` (`impl/e3c-parser-cache-integrity`)  
**Audit Reference:** `reports/phase_e4_entry_execution/e4_breakout_ignition_entry_audit.md` (report-only commit `1780d0836af205103887c2e5818a3ed817922c86`)  
**Revision:** Rev 4 (Final `jsonParsed` Account-Key Resolution Corrective Pass)  
**Date:** 2026-11-27

---

## 0. Independent-Review Corrective Log

### Rev 4 Resolutions
| # | Finding | Resolution in Rev 4 |
|---|---|---|
| 1 | E4D mixed `encoding="jsonParsed"` with `meta.loadedAddresses` concatenation (`message.accountKeys + meta.loadedAddresses...`). | Standardized E4D strictly on `encoding="jsonParsed"` with `maxSupportedTransactionVersion=0`: `transaction.message.accountKeys` already contains the complete resolved key list (including ALT-loaded accounts) and `meta.loadedAddresses` is omitted. Extract `pubkey` directly from each parsed `message.accountKeys` entry; never append `meta.loadedAddresses`. Added explicit `accountIndex` bounds/type validation and E4D v0/ALT regression tests. |

### Rev 3 Resolutions
| # | Finding | Resolution in Rev 3 |
|---|---|---|
| 1 | E4A allowed either DexScreener pinned-pool `priceNative` or a Jupiter mint-level quote. | Restricted `BREAKOUT_IGNITION` to **one** authoritative source explicitly tied to `order.pair_address` (`dexscreener_pinned_pool_native`), recording `approval_pair_address`, `approval_price_source`, `approval_price_observed_at`, `approval_price_native`, and `token_decimals`. Removed mint-level Jupiter quote as an authorization/drift fallback; missing/invalid pinned-pool price rejects `BUY` before quoting. |
| 2 | E4D marked any `getSignatureStatuses` `err != None` as `FAILED` without checking commitment. | Required configured `confirmed`/`finalized` commitment before classifying `err != None` as terminal `FAILED`. Processed-only errors, missing/inconsistent statuses, or `getSignatureStatuses`/`getTransaction` disagreements remain `UNRESOLVED` with reservation locked. |
| 3 | E4D token-delta attribution needed ALT key resolution, multi-account aggregation, new/closed ATA rules, and missing `owner`/`programId` handling. | Specified full `accountKeys + meta.loadedAddresses` resolution, aggregation across all taker-owned accounts for `order.mint`, strict new-ATA zero-pre-balance proof requirements, closed/multi-account handling, and ownership resolution when `owner`/`programId` is omitted (never assuming ownership or falling back to wallet balance). |
| 4 | E4D needed explicit separation of swap input principal from total wallet lamport delta (`preBalances - postBalances`). | Prohibited using total wallet lamport delta as swap cost basis; specified attributing swap principal from verified wSOL deltas or parsed swap instructions, network fee from `meta.fee` + fee payer, and rent/other transfers separately while preserving E2B `UNKNOWN` fee status when exact attribution is unavailable. |

### Rev 2 Resolutions

| # | Review Finding | Resolution in Rev 2 |
|---|---|---|
| 1 | E4B assumed every `/swap/v2/order` route exposes `routePlan[].swapInfo.ammKey` equal to `pair_address`. | Verified official Jupiter Swap API v2 (`dev.jup.ag`) meta-aggregator router contract (`metis`, `jupiterz`, `dflow`, `okx`). Added router-specific verification policy: only `metis` with exact `ammKey == pair_address` on target-mint legs is verifiable; `jupiterz` (RFQ), `dflow`, and `okx` are explicitly unverifiable for pinned-pool execution and fail closed in live mode. Documented why assembled-tx inspection is unsafe. |
| 2 | E4A conflated approval timestamp, price observation timestamp, and candle close fallback. | Separated `approved_at`, `approval_price_observed_at`, and audit-only OHLCV provenance timestamps. Established one authoritative live native price source (no silent fallback to candle close), explicit `decimals` validation, synchronized USD rule, and exact `MAX_ENTRY_PRICE_DRIFT_PCT` formula and sign convention. |
| 3 | E4A started quote-age clock before request, ignored `lastValidBlockHeight`/`expireAt`, and assumed unconditional `slippageBps`/`priceImpactPct`. | Moved quote-age clock to `quote_received_at` with 3 checkpoints (receipt, pre-sign, pre-submit); added router-specific `lastValidBlockHeight` vs. `expireAt` validation; documented RTSE vs. `manual` mode when passing `slippageBps`; verified `priceImpact` vs. deprecated `priceImpactPct` and `otherAmountThreshold` handling per router. |
| 4 | E4A depended on E4D's future durable intents for post-broadcast ambiguous outcomes. | Strictly scoped E4A to **pre-broadcast** validation only. All post-broadcast live recovery (`outputAmountResult <= 0`, empty `tx_signature`, HTTP timeouts) is deferred to E4D. Live unattended execution remains explicitly blocked until E4D is complete. |
| 5 | E4C referenced E4D's `order_intents` table before E4D existed. | Split cleanly: E4C implements self-contained in-process coroutine reservation pinning (no E4D dependency); E4D then extends reservation & startup protection to persisted `order_intents`. |
| 6 | E4D relied on wallet token balance to infer swap success/amount and lacked strict `FAILED` and partial-signing rules. | Made `getTransaction` (exact signature `preTokenBalances`/`postTokenBalances` + `preBalances`/`postBalances` + `meta.err`) primary; demoted wallet balance to supporting evidence only. Added strict `FAILED` classification (`searchTransactionHistory=true` + verified expiry), full restart-reconciliation schema (zero secrets), and `gasless`/`signatureFeePayer`/partial-signing verification before pre-deriving `signatures[0]`. |
| 7 | E4C needed explicit spendable-balance accounting, RPC-outside-lock concurrency, and divergence rules. | Defined exact formula relating DuckDB `sol_balance`, RPC `getBalance`, `reserved_sol`, `LIVE_MIN_SOL_RESERVE`, and `LIVE_FEE_RESERVATION_BUFFER_SOL` (base + priority + ATA rent). RPC executes outside `db._lock`; final check executes atomically under `db._lock` without double-counting reservations or altering historical PnL / E2B fee semantics. |
| 8 | Sequence & acceptance criteria needed per-phase safety boundaries and deferred thresholds. | Added explicit prerequisites, "safe after commit", and "remains unsafe/disabled" tables for every phase; kept all numerical thresholds deferred to telemetry evidence. |

---

## 1. Executive Summary & Design Objectives

The Phase E4 audit (`1780d08`) confirmed that while `BREAKOUT_IGNITION` entry decisions are protected by Phase E3A–E3C closed-candle/provenance binding and Phase E1 pool pinning at the decision layer, the execution boundary (`executor_node.py`, `state_ledger.py`, `safety_gate_node.py`) is **not safe for unattended live execution**.

### Design Objectives
1. **Fail closed** whenever approval freshness, price/slippage/impact bounds, pinned-pool route identity, safety freshness, or spendable SOL sufficiency cannot be established.
2. **Eliminate TOCTOU drift** between `alpha_node` approval and `executor_node` submission without altering E3A–E3C OHLCV provenance or strategy thresholds.
3. **Keep E4-04 failure modes separate**: `out_amount <= 0` (which can debit SOL without retaining a position) vs. empty `tx_signature` (a validation/reconciliation defect that must not discard a valid positive-token fill).
4. **Never treat an HTTP timeout or single null RPC response as proof of failure**, and never release a reservation or allow a duplicate `BUY` while an on-chain outcome remains `UNRESOLVED`.
5. **Preserve all prior invariants**: E1 pool pinning, E2A Exodus/priority rules, E2B fee accounting (`PAPER_ASSUMED` vs. `UNKNOWN`, no fabricated live fees), E3A–E3C closed-candle/provenance binding, and unchanged strategy thresholds.

---

## 2. Exact Baseline & Branch Relationship

```text
6492acd4f39522aef71bb814bb567288f7b5993a  (impl/e3a-candle-eligibility)
  └─▶ 35ef3b77163e302f564e0133c9e4a216490aae1d  (impl/e3b-cache-provenance)
        └─▶ ca01c640ac60477e63a33a86217298573680c5f0  (E3C approved source baseline)
              ├──▶ 1780d0836af205103887c2e5818a3ed817922c86  (impl/e3c-parser-cache-integrity: E4 audit report-only)
              └──▶ design/e4-entry-execution  (THIS DESIGN — branched directly from ca01c64)
```

- **Source baseline:** `ca01c640ac60477e63a33a86217298573680c5f0`.
- **Other branches untouched:** `dev_1` (`d88e0f3`), `dev_2` (`19104e3`), and all `impl/e3*` branches.

---

## 3. Current Entry Lifecycle & Failure Boundaries

1. **`scanner_node.py`**: Selects highest-liquidity pool (`pair_address`, `dex_id`) per mint and persists to DuckDB `watchlist`.
2. **`safety_gate_node.py`**: Checks `jup_is_sus`, then skips RugCheck + Jupiter Shield if `cand["safety_passed"]`, `watchlist_safety[mint]`, or `_safety_cache` (300s TTL) hits.
3. **`alpha_node.py`**: Validates E3A–E3C OHLCV provenance, runs `evaluate_scalper_entry()` (`detect_breakout_ignition`), awaits Gemini Decision 1, verifies `is_valid_pool_address()`, and emits an in-memory `PendingOrder` with no approval timestamp or approval price provenance.
4. **`executor_node.py`**:
   - Verifies strategy allowlist and `is_valid_pool_address(order.pair_address)`.
   - Calls `db.reserve_buy_slot()` (`state_ledger.py:185`) — checks DuckDB `sol_balance` (omitting `tx_fee_sol` and live RPC wallet balance), lane/global caps, and duplicate mints, with a `60.0s` in-memory TTL. Does **not** re-check `cooldowns` or `DAILY_MAX_LOSS_PCT`.
   - Calls `fetch_jupiter_order()` (`GET /swap/v2/order`) without route/pool verification, drift checks, quote expiry checks, or `priceImpact` enforcement.
   - In `live` mode, signs via `VersionedTransaction(tx.message, [keypair])` and calls `execute_jupiter_signed_order()` (`POST /swap/v2/execute`, 20s timeout). Any `BaseException` releases `reservation_id` and leaves `focus_state = SCANNING`.
   - Settles via `apply_trade_settlement()`, which drops `ActivePosition` when `token_amount <= 0` while still deducting `sol_delta`.

---

## 4. Detailed Designs: E4A–E4E

### E4A — Approval & Quote Integrity (Pre-Broadcast Scope)

> **Scope Boundary:** E4A covers **pre-broadcast** validation only (approval freshness, price provenance & drift, quote validity, slippage/RTSE, price impact, and pre-sign `out_amount > 0`). All post-broadcast live reconciliation (`outputAmountResult <= 0`, empty `tx_signature`, timeouts) is deferred to **E4D** so no intermediate commit releases a reservation on an unknown on-chain outcome.

#### 4A.1 Approval Timestamps & Authoritative Price Provenance
Separate three distinct timestamps on `PendingOrder` (`state.py`, `alpha_node.py`):
- `approved_at: Optional[str] = None` — UTC ISO-8601 timestamp when `alpha_node` issues the final `BUY` approval.
- `approval_price_observed_at: Optional[str] = None` — UTC ISO-8601 timestamp when the authoritative approval price was actually observed.
- `approval_ohlcv_fetched_at: Optional[str] = None` and `approval_candle_ts: Optional[int] = None` — E3B/E3C OHLCV provenance retained strictly for audit/telemetry (never used as a live execution price or to reconstruct a new breakout evaluation).

**Single Authoritative Approval-Price Source (`BREAKOUT_IGNITION`):**
- **Authoritative source:** Live native SOL-per-token spot price (`priceNative`) from DexScreener `/tokens/v1/solana/{mint}` **matched strictly to the exact pinned `order.pair_address`** (`approval_price_source = "dexscreener_pinned_pool_native"`), recorded with:
  - `approval_pair_address: Optional[str] = None` (must match `order.pair_address` verbatim).
  - `approval_price_native: Optional[float] = None` (SOL per 1 whole token).
  - `approval_price_source: Optional[str] = None` (must equal `"dexscreener_pinned_pool_native"`).
  - `approval_price_observed_at: Optional[str] = None`.
  - `token_decimals: Optional[int] = None` (explicitly obtained from Jupiter Token v2 / batched price v3 / mint metadata and validated as `int` in `0 <= d <= 18`; **never** silently defaulted to `6` when missing).
- **No silent mint-level or candle-close substitution:** Never substitute a mint-level Jupiter quote (which is not pool-pinned) or an older candle close as the drift reference. Any alternative/diagnostic value (e.g. `approval_candle_close_usd`) is strictly non-authoritative and cannot authorize execution or serve as the drift reference.
- **Optional USD comparison rule:** Prefer native SOL-per-token directly to avoid cross-feed clock skew. If USD comparison is ever enabled, require both `approval_price_usd` (from the exact pinned pool) and a synchronized `sol_usd_price` with `abs(sol_usd_observed_at - approval_price_observed_at) <= MAX_SOL_USD_SKEW_SECONDS`, failing closed if either timestamp is missing or skewed.
- **Fail-closed validation:** If `approved_at`, `approval_price_observed_at`, `approval_pair_address != order.pair_address`, `approval_price_source != "dexscreener_pinned_pool_native"`, `approval_price_native`, or `token_decimals` is missing, non-finite (`NaN`/`Inf`), `<= 0`, or stale (`now - approved_at > MAX_BUY_APPROVAL_AGE_SECONDS` or `now - approval_price_observed_at > MAX_APPROVAL_PRICE_AGE_SECONDS`), `executor_node` rejects the `BUY` before quoting.

#### 4A.2 Precise Entry-Drift Formula & Sign Convention
Given `spent_sol = order.amount_atomic / LAMPORTS_PER_SOL` and quote `out_amount` (atomic units) with validated `token_decimals`:
```text
quoted_tokens       = out_amount / (10 ** token_decimals)
quote_price_native  = spent_sol / quoted_tokens        (SOL paid per 1 token)
entry_drift_pct     = ((quote_price_native - approval_price_native) / approval_price_native) * 100.0
```
- **Sign convention:**
  - `entry_drift_pct > 0`: **adverse** drift (paying *more* SOL per token than at approval; price ran up / worse fill). Rejected when `entry_drift_pct > MAX_ENTRY_PRICE_DRIFT_PCT`.
  - `entry_drift_pct < 0`: **favorable** drift (paying *less* SOL per token). Optionally bounded by `MAX_NEGATIVE_ENTRY_DRIFT_PCT` if extreme downward collapse indicates liquidity rugging; otherwise adverse drift is the primary upper bound.
- Compute the same check against `otherAmountThreshold` (when present) to verify the worst-case guaranteed on-chain execution price does not breach `MAX_WORST_CASE_ENTRY_DRIFT_PCT`.

#### 4A.3 Quote Timing, Validity Fields, RTSE/Slippage & Price Impact
1. **Quote Age Clock & 3 Checkpoints:**
   - Start clock at `quote_received_at = utc_now()` immediately when `fetch_jupiter_order()` returns (not before sending the HTTP request).
   - **Checkpoint 1 (Quote Receipt):** Validate `out_amount > 0`, router validity field present, `entry_drift_pct`, `priceImpact`, `otherAmountThreshold`, and E4B route identity.
   - **Checkpoint 2 (Immediately Before Signing):** Verify `(now - quote_received_at) <= MAX_QUOTE_AGE_SECONDS` and router validity not expired.
   - **Checkpoint 3 (Immediately Before Submission):** If any meaningful delay occurred between signing and `POST /swap/v2/execute` (e.g., DB persistence or pre-exec safety), re-verify `(now - quote_received_at) <= MAX_QUOTE_AGE_SECONDS` and expiry before broadcasting.
2. **Router-Specific Validity Fields (Verified via `dev.jup.ag`):**
   - **Aggregator routes (`metis`, `dflow`, `okx`):** Validate `lastValidBlockHeight` (positive integer). In live mode, verify against recent RPC block height (`current_block_height < lastValidBlockHeight - MIN_BLOCK_HEIGHT_HEADROOM`) or fail closed if `lastValidBlockHeight` is missing.
   - **RFQ routes (`jupiterz`):** Validate `expireAt` (timestamp). Reject if missing or `now >= expireAt - MIN_RFQ_EXPIRY_HEADROOM_SECONDS`.
   - If a router omits its expected validity field, **fail closed** (`REJECTED_MISSING_QUOTE_VALIDITY`) in live mode.
3. **RTSE vs. Explicit `slippageBps` (`ultra` vs. `manual` Mode):**
   - Per official Jupiter Swap API v2 docs (`dev.jup.ag`), leaving `slippageBps` unset runs `/swap/v2/order` in `ultra` mode with **RTSE (Real-Time Slippage Estimator)** active. Passing explicit `slippageBps` switches the request into **`manual` mode**, overriding RTSE and potentially altering router eligibility.
   - **Design policy:** Do **not** blindly force a fixed `slippageBps` request parameter. Default to RTSE (`ultra` mode) unless an explicit telemetry-backed config toggle enables `manual` mode, and enforce **post-quote client-side bounds** on the response's `slippageBps` (`<= MAX_ACCEPTED_SLIPPAGE_BPS`) and `otherAmountThreshold` (minimum guaranteed output). If a router omits `otherAmountThreshold` on a `BUY`, fail closed in live mode unless verified as a fixed-price RFQ quote (`jupiterz` where `outAmount` is exact — which is itself rejected by E4B for `BREAKOUT_IGNITION`).
4. **`priceImpact` / `priceImpactPct` Per-Router Enforcement:**
   - Jupiter Swap API v2 returns `priceImpact` (float percentage, e.g. `-0.1` for `-0.1%`) alongside deprecated string `priceImpactPct`. Parse `priceImpact` first, falling back to `priceImpactPct` only if `priceImpact` is absent.
   - If neither field is present/parseable (e.g., external/RFQ routers), do **not** assume `0%`: **fail closed** (`REJECTED_MISSING_PRICE_IMPACT`) for `BREAKOUT_IGNITION` `BUY` in live mode.
   - Reject if `abs(impact_pct) > MAX_ENTRY_PRICE_IMPACT_PCT`.
5. **Pre-Broadcast `out_amount <= 0` Check:**
   - Reject any quote with `out_amount <= 0` immediately at Checkpoint 1 (before signing or settlement) in both paper and live modes.

---

### E4B — Execution Pool Integrity (Router-Specific Contract)

#### 4B.1 Official Jupiter Swap API v2 Meta-Aggregator Contract (`dev.jup.ag`)
`GET /swap/v2/order` competes four routers and returns `"router": "metis" | "jupiterz" | "dflow" | "okx"`:

| Router | Execution Model | `routePlan` / `swapInfo.ammKey` | Validity Field | Can Prove Pinned `pair_address`? |
|---|---|---|---|---|
| **`metis`** | Jupiter on-chain AMM aggregator | Returns `routePlan[]` with `swapInfo.ammKey` (pool/market account pubkey), `label`, `inputMint`, `outputMint`, `inAmount`, `outAmount`, `percent` | `lastValidBlockHeight` | **YES — ONLY when** `routePlan` is non-empty AND every hop involving `order.mint` has `swapInfo.ammKey == order.pair_address` (exact base58 pubkey match) |
| **`jupiterz`** | Off-chain / market-maker RFQ | Fills from MM inventory, not an on-chain DexScreener AMM pool | `expireAt` | **NO (Unverifiable / Non-Pool)** — does not execute against pinned AMM pool |
| **`dflow`** | External liquidity partner | External routing; `ammKey` pool provenance is not guaranteed to map 1:1 to DexScreener `pair_address` | `lastValidBlockHeight` | **NO (Unverifiable)** unless live schema verification proves exact AMM pool `ammKey` semantics |
| **`okx`** | External liquidity partner | External routing; `ammKey` pool provenance is not guaranteed to map 1:1 to DexScreener `pair_address` | `lastValidBlockHeight` | **NO (Unverifiable)** unless live schema verification proves exact AMM pool `ammKey` semantics |

#### 4B.2 Why Assembled Transaction Inspection Is Unsafe as a Fallback
Inspecting raw `VersionedTransaction` bytes to infer pool identity is **not** safe because:
1. V0 transactions rely on Address Lookup Tables (ALTs) requiring extra on-chain RPC fetches to resolve account keys.
2. Each DEX program encodes pool/market account indices differently in instruction data.
3. The mere presence of `order.pair_address` in `accountKeys` does not prove 100% of the swap executed through that pool (split routes or multi-hop CPIs can still route liquidity elsewhere).

#### 4B.3 Router-Specific Policy for `BREAKOUT_IGNITION`
- **Never equate** `swapInfo.label` (DEX name), AMM program ID, or route label with `order.pair_address`.
- **Do not assume** `excludeRouters` or `excludeDexes` can enforce a specific pool address (they only filter routers/DEX programs and shift `/order` into `manual` mode).
- **Live-mode authorization (fail closed):**
  - Require `router == "metis"`, non-empty `routePlan`, and **every** hop where `inputMint == order.mint` or `outputMint == order.mint` has `swapInfo.ammKey == order.pair_address`.
  - If `router in ("jupiterz", "dflow", "okx")`, or `routePlan` is missing/empty, or any target-mint hop has `ammKey != order.pair_address`, **reject the quote** before signing (`BLOCKED: ROUTE_POOL_UNVERIFIABLE` or `BLOCKED: ROUTE_POOL_MISMATCH`).
- **Paper-mode observability vs. enforcement:**
  - Always log `router`, `route_pool_keys`, and `pool_route_status` (`VERIFIED_PINNED_POOL` | `UNVERIFIABLE_ROUTER` | `POOL_MISMATCH` | `MISSING_ROUTE_PLAN`) in paper telemetry to collect empirical data before live enablement.
  - Enforce fail-closed rejection in paper mode behind an explicit config flag (`REQUIRE_PINNED_POOL_ROUTE_IN_PAPER`) so existing mock tests remain explicit while live mode is unconditionally fail-closed.
- **Post-entry continuity:** `ActivePosition.pair_address`, `dex_id`, and `pool_identity_status = "PINNED"` remain intact through monitoring and exits.

---

### E4C — Reservation, Spendable Balance & Risk Guard Integrity

> **Independence from E4D:** E4C works completely standalone using in-process coroutine ownership and DuckDB `agent_state` + `cooldowns`, without requiring E4D's `order_intents` table. E4D then extends `reserve_buy_slot()` to also honor durable `order_intents`.

#### 4C.1 Spendable Balance & Fee Reservation Formula
Clearly separate **reservation headroom** from **E2B fee accounting**:
- **Paper mode:**
  - `fee_reserve_sol = float(config.PAPER_TX_FEE_SOL)`
  - `spendable_sol = duck_db_sol_balance - reserved_sol`
  - Require `spendable_sol + 1e-9 >= required_sol + fee_reserve_sol`.
- **Live mode:**
  - Fetch `wallet_sol_snapshot = getBalance(taker_pubkey, commitment="confirmed") / LAMPORTS_PER_SOL` **outside `db._lock`** (only at `executor_node` `BUY` boundary — **never** on every `scanner_node` tick).
  - If RPC `getBalance` fails or times out: **fail closed** (`BLOCKED: LIVE_RPC_BALANCE_UNAVAILABLE`). Do not fall back to potentially stale DuckDB `sol_balance`.
  - Define `fee_reserve_sol = LIVE_FEE_RESERVATION_BUFFER_SOL` (covering base tx fee + priority fee + potential first-time ATA rent `~0.002039` SOL). This buffer is used **only** for reservation headroom and is **never** stored as `tx_fee_sol` or `entry_fee_sol` (preserving E2B `UNKNOWN` fee semantics).
  - Inside `reserve_buy_slot()` under `db._lock` (using all currently active in-flight reservations `reserved_sol` so in-flight reservations are **neither omitted nor double-counted**):
    ```text
    duck_avail_sol   = duck_db_sol_balance - reserved_sol
    wallet_avail_sol = wallet_sol_snapshot - LIVE_MIN_SOL_RESERVE - reserved_sol
    effective_avail  = min(duck_avail_sol, wallet_avail_sol)
    ```
    Require `effective_avail + 1e-9 >= required_sol + fee_reserve_sol`.
  - **Divergence handling:** If `abs(wallet_sol_snapshot - duck_db_sol_balance) > BALANCE_DIVERGENCE_WARN_SOL` (e.g., external wallet transfers or accumulated unrecorded network fees), log a `BALANCE_DIVERGENCE` alert/action and cap spending at `effective_avail`. **Never** overwrite DuckDB historical `daily_pnl_sol` or fabricate E2B fee rows to force reconciliation.

#### 4C.2 Atomic Risk Guards Under `db._lock`
Inside `reserve_buy_slot()` under `db._lock`:
1. Check `cooldowns` table (`expires_at > now_iso`) for `mint`; reject with `ON_COOLDOWN` if active.
2. Check `daily_pnl <= -(daily_start * config.DAILY_MAX_LOSS_PCT)`; reject with `DAILY_MAX_LOSS_HIT` if breached.
3. Preserve all existing checks: duplicate mint across `chart_positions`, `whale_positions`, `active_position`, `whale_position`, and `_inflight_buy_reservations`, plus `MAX_CHART_POSITIONS`, `MAX_WHALE_POSITIONS`, and `MAX_OPEN_POSITIONS`.

#### 4C.3 Mid-Flight Coroutine Ownership (Solving `RESERVATION_TTL_SEC` Without E4D)
- Each in-memory reservation record stores `active_coroutine: bool = True` and `owner_task_id`.
- `_prune_reservations_locked()` **never** prunes any reservation with `active_coroutine == True` (even if a live swap exceeds `60.0s`).
- On normal exit or pre-broadcast abort, `executor_node` clears `active_coroutine` and releases the reservation in `finally` / `except` (until E4D takes over post-broadcast retention).
- **Restart limitation (explicitly acknowledged until E4D):** In-memory reservations do not survive process crash/restart; E4D closes this gap via DuckDB `order_intents`.

---

### E4D — Transaction Lifecycle Integrity & Transaction-Specific Reconciliation

#### 4D.1 Partial-Signing & Signature Derivation Verification
Before relying on pre-derived `tx_signature`:
1. Check `order_resp.get("gasless")`: if `True`, or `order_resp.get("signatureFeePayer") != taker_pubkey`, the transaction uses a third-party/market-maker fee payer (partial signing).
2. Inspect deserialized `VersionedTransaction`:
   - Verify `tx.message.header.num_required_signatures == 1` and `str(tx.message.account_keys[0]) == taker_pubkey`.
   - After signing with `keypair`, extract `sig = str(signed_tx.signatures[0])` and validate `sig` is non-empty, not the all-zero signature (`"1" * 64`), and valid 64-byte base58.
3. **Policy if `gasless == True` or `num_required_signatures > 1` or `signatureFeePayer != taker_pubkey`:** Reject before signing for `BREAKOUT_IGNITION` live `BUY` (`BLOCKED: UNSUPPORTED_PARTIAL_SIGN_OR_GASLESS`) so the agent never broadcasts a live transaction whose signature cannot be deterministically derived and reconciled.

#### 4D.2 Durable `order_intents` Persistence (No Secrets)
Persist in DuckDB `order_intents` under `db._lock` **before** calling `execute_jupiter_signed_order()`:
- **Persisted fields:** `intent_id`, `lane`, `mint`, `symbol`, `pair_address`, `dex_id`, `side`, `strategy`, `router`, `order_mode` (`ultra`/`manual`), `jupiter_request_id`, `tx_signature`, `last_valid_block_height`, `expire_at`, `input_mint`, `output_mint`, `amount_atomic`, `expected_out_amount`, `other_amount_threshold`, `token_decimals`, `taker_pubkey`, `reserved_sol`, `status`, `error_detail`, `created_at`, `updated_at`. **Never persist private keys, signed transaction bytes, or API keys.**
- **Explicit States:**
  - `PREPARED`: Reservation acquired, E4A/E4B/E4E passed, tx signed, `tx_signature` verified.
  - `SUBMITTED`: Written to DuckDB immediately before `POST /swap/v2/execute`.
  - `UNRESOLVED`: Transitioned on HTTP timeout, network/5xx error, empty `exec_resp` signature, or `outputAmountResult <= 0` after broadcast. **Reservation remains locked; `mint` remains blocked from new `BUY`s.**
  - `CONFIRMED`: Confirmed on-chain (`meta.err is None`) with verified positive token delta; ready for `apply_trade_settlement()`.
  - `SETTLEMENT_PENDING_PERSISTENCE`: On-chain `CONFIRMED` succeeded, but `apply_trade_settlement()` raised a DuckDB error; reservation stays locked and persistence is retried on next tick/startup.
  - `FAILED`: Definitively failed on-chain or expired; safe to release reservation.
  - `RECONCILED`: Terminal recovery state after an `UNRESOLVED` or `SETTLEMENT_PENDING_PERSISTENCE` intent is settled or confirmed failed.

#### 4D.3 Transaction-Specific Reconciliation (`getTransaction` Primary)
Wallet token balance (`getTokenAccountsByOwner` / `getBalance`) is **supporting evidence only** — never proof that a specific swap succeeded or how many tokens it acquired (existing holdings, external transfers, or other txs would corrupt the inference).

**Reconciliation procedure for `UNRESOLVED` / `SUBMITTED` intents:**
1. Call `getSignatureStatuses([tx_signature], searchTransactionHistory=True)` on a healthy RPC endpoint.
2. **Committed Failure Policy (`err != None`):**
   - A `processed`-only error is **not** sufficient to release a reservation (it can be fork-dependent or incomplete).
   - Require `status.confirmationStatus` to meet the configured `RECONCILE_FAILURE_COMMITMENT` policy (`"confirmed"` or `"finalized"`). If `status.err is not None` at `processed` only, or if status is missing/inconsistent, keep `UNRESOLVED` and keep the mint/reservation locked.
   - Cross-check `getTransaction.meta.err` when `getTransaction` is available. If `getSignatureStatuses` and `getTransaction` disagree (e.g., one reports `err != None` and the other reports `None`, or `getTransaction` is `None`/lagging), retain `UNRESOLVED` until consistent committed evidence is observed.
   - Only when committed `status.err is not None` and cross-checked `getTransaction` (if present) agrees on failure → transition to `FAILED` and release reservation.
3. **Committed Success & Deterministic Token-Delta Attribution (`status.err is None` and `confirmationStatus in ("confirmed", "finalized")`):**
   - Call `getTransaction(tx_signature, encoding="jsonParsed", maxSupportedTransactionVersion=0, commitment=...)`.
   - If `getTransaction` returns `None`, lacks `meta`, or has `meta.err != None`: retain `UNRESOLVED` (or `FAILED` only under the committed-failure rule above) — never guess.
   - **Complete account-key resolution (`jsonParsed` encoding contract):** Because `getTransaction` is requested with `encoding="jsonParsed"` and `maxSupportedTransactionVersion=0`, `transaction.message.accountKeys` **already contains the complete resolved account-key list**, including address-lookup-table-loaded accounts for v0 transactions (`meta.loadedAddresses` is omitted in `jsonParsed`). Extract each entry's `pubkey` string directly from `transaction.message.accountKeys` (handling parsed key objects `{"pubkey": ..., "source": "transaction" | "lookupTable"}`), and **never** append `meta.loadedAddresses`. Do not mix `jsonParsed` and raw-`json` key composition.
   - **Strict `accountIndex` validation:**
     - Resolve every `preTokenBalances` and `postTokenBalances` `accountIndex` against the exact `all_keys` array extracted from `transaction.message.accountKeys`.
     - Reject any negative, boolean/non-integer, out-of-range (`idx < 0 or idx >= len(all_keys)`), or malformed `accountIndex`, and reject any `accountKeys` entry missing a valid `pubkey`.
     - Verify that resolved pre- and post-token-balance entries map to the expected account addresses and `order.mint`.
     - Never silently repair an index or fall back to a different key ordering; if the RPC response does not provide enough information to prove the mapping, retain `UNRESOLVED`.
   - **Deterministic output token-delta attribution (`order.mint`):**
     - Match `meta.preTokenBalances` and `meta.postTokenBalances` by `accountIndex` (resolved against `all_keys`), `mint == order.mint`, and validated token program (`SPL Token` / `Token-2022`) + `uiTokenAmount.decimals` consistency.
     - **Missing `owner` / `programId`:** Treat omitted `owner` or `programId` in token balance entries as a requirement to resolve account ownership from reliable transaction/account evidence (parsed token-account owner / ATA derivation from `(taker_pubkey, token_program, order.mint)` / decoded instruction accounts) — **never** assume ownership without proof.
     - **Newly created, closed, and multiple token accounts:** Aggregate raw integer token amounts across **all** verified taker-owned accounts for `order.mint`. A missing `preTokenBalances` entry for an account in `postTokenBalances` may be treated as `0` **only** when transaction metadata and account creation/ownership evidence (e.g. `Create` / `CreateIdempotent` ATA instruction for `taker_pubkey` and `order.mint`) establish that the account did not previously hold `order.mint`. Closed accounts (present in `preTokenBalances`, absent in `postTokenBalances`) must be included in the aggregate delta.
     - If ownership, `accountIndex` mapping, `decimals`/program consistency, or pre/post attribution cannot be proven, **retain `UNRESOLVED`** (never infer success or acquired amount from current wallet balance).
   - **Swap Input Principal vs. Total Wallet Lamport Delta (Do Not Conflate):**
     - Never treat total taker lamport delta (`preBalances[taker] - postBalances[taker]`) as swap input principal or cost basis; that aggregate delta conflates swap input, base tx fee, priority fee, ATA rent/account creation, and any other SOL transfers.
     - Determine **actual swap input principal** from verified wSOL (`SOL_MINT`) token-account deltas belonging to the swap or validated parsed/decoded swap instructions and their account relationships.
     - Determine **network fee** from `meta.fee` alongside the verified fee payer (`all_keys[0] == taker_pubkey`).
     - Attribute ATA rent / account creation and other SOL transfers separately when possible; never double-count fees or rent in lifecycle PnL or E2B fee accounting.
     - Preserve `fee_accounting_status = "UNKNOWN"` whenever exact fee/principal attribution is unavailable; if actual input/output attribution required for a valid `ActivePosition` cannot be established safely, keep the intent `UNRESOLVED`.
   - When verified output `token_delta > 0` and principal is reliably attributed: settle `ActivePosition` via `apply_trade_settlement()` → `RECONCILED` (or `SETTLEMENT_PENDING_PERSISTENCE` on DB error).
4. **Safe `FAILED` classification when `getSignatureStatuses` returns `None`:**
   - **Never** classify a single `None` response as `FAILED`.
   - Require a healthy RPC response **AND** verified router-specific expiry:
     - Aggregator routes (`metis`/`dflow`/`okx`): `getBlockHeight(commitment="finalized") > last_valid_block_height`.
     - RFQ routes (`jupiterz`): `now > expire_at + RFQ_RECONCILE_GRACE_SECONDS`.
   - Only when both `searchTransactionHistory=True` returns `None` **and** expiry is proven on a healthy RPC, transition to `FAILED` and release reservation. Otherwise remain `UNRESOLVED`.
5. **Startup Recovery (`main.py`):**
   - Before starting `run_autonomous_loop` or `run_whale_lane_loop`, `db.reserve_buy_slot()` blocks all new `BUY`s while any `order_intents` row is in `PREPARED`/`SUBMITTED`/`UNRESOLVED`/`SETTLEMENT_PENDING_PERSISTENCE`.
   - `PREPARED` (never broadcast) → mark `FAILED` (`ABORTED_BEFORE_BROADCAST`).
   - `SUBMITTED` / `UNRESOLVED` → run transaction-specific RPC reconciliation.
   - `SETTLEMENT_PENDING_PERSISTENCE` → retry `apply_trade_settlement()`.

---

### E4E — Safety Freshness

1. **Persistent Watchlist TTL (`db.py`, `safety_gate_node.py`):**
   - Add `safety_checked_at VARCHAR` to `watchlist` via idempotent `ALTER TABLE`.
   - Stop treating `watchlist.safety_passed == True` as permanently valid; re-check whenever `safety_checked_at` is `NULL` or `(now - safety_checked_at) > SAFETY_CACHE_TTL_SECONDS`.
2. **Pre-Execution Safety Boundary (`executor_node.py`, `BUY` only):**
   - Immediately before quoting/signing a `BUY`, check whether the mint's safety report age exceeds `PRE_EXECUTION_SAFETY_TTL_SECONDS`.
   - Run lightweight single-mint checks (Jupiter `/ultra/v1/shield` + Jupiter Token v2 `isSus`, and full `check_token_safety()` if RugCheck report is older than `PRE_EXECUTION_SAFETY_TTL_SECONDS`).
   - **Fail closed:** On safety failure or API error/timeout, abort the `BUY` before broadcast, release reservation, and evict/cooldown on confirmed safety failure.

---

## 5. State & Database Schema Changes, Migration & Restart Behavior

- **`PendingOrder` (`state.py`)**: Optional backward-compatible fields `approved_at`, `approval_price_observed_at`, `approval_pair_address`, `approval_price_native`, `approval_price_source`, `approval_candle_close_usd`, `approval_ohlcv_fetched_at`, `approval_candle_ts`, `token_decimals`.
- **`watchlist` (`db.py`)**: `ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS safety_checked_at VARCHAR` (existing `NULL` rows treated as stale, never backfilled as fresh).
- **`order_intents` (`db.py`, added in E4D)**: New table storing non-secret reconciliation metadata and lifecycle status.
- **Restart behavior:** Startup reconciliation runs before scanner/alpha loops; unresolved intents remain locked in `reserve_buy_slot()` until resolved.

---

## 6. Paper vs. Live Behavioral Differences

| Dimension | Paper Mode (`EXECUTION_MODE="paper"`) | Live Mode (`EXECUTION_MODE="live"`) |
|---|---|---|
| **Wallet Balance** | DuckDB `sol_balance - reserved_sol` | `min(DuckDB avail, RPC getBalance - LIVE_MIN_SOL_RESERVE - reserved_sol)`; fail closed on RPC error |
| **Fee Reservation** | `PAPER_TX_FEE_SOL` (`0.001`) | `LIVE_FEE_RESERVATION_BUFFER_SOL` (headroom only; E2B `tx_fee_sol` remains `None`/`UNKNOWN`) |
| **Route Pool Check** | Logged for observability; strict enforcement configurable | Unconditionally fail-closed (`metis` + exact `ammKey == pair_address`) |
| **Quote Expiry & Signing** | `quote_received_at` age check; synthetic sig | `lastValidBlockHeight`/`expireAt` + `gasless == False` + `signatureFeePayer == taker` + pre-derived `signatures[0]` |
| **Ambiguous Outcome** | Pre-broadcast quote errors release reservation | Post-broadcast timeout/error/empty-sig/`out<=0` → `UNRESOLVED` (locked) → `getTransaction` reconciliation |

---

## 7. Concurrency & Atomicity Analysis

- `run_autonomous_loop` (chart) and `run_whale_lane_loop` (whale) share one `asyncio` event loop and `db._lock`.
- **RPC outside `db._lock`:** `getBalance`, `fetch_jupiter_order`, `execute_jupiter_signed_order`, and `getTransaction` never hold `db._lock`.
- **Atomic check under `db._lock`:** `reserve_buy_slot()` atomically validates `agent_state`, `_inflight_buy_reservations` (active coroutines), `order_intents` (in E4D), `cooldowns`, `DAILY_MAX_LOSS_PCT`, and `min(duck_avail, wallet_avail) >= required_sol + fee_reserve_sol`.
- **No mid-flight TTL eviction:** Reservations owned by active coroutines (E4C) or durable unresolved intents (E4D) are never pruned.

---

## 8. Failure-State Transition Diagram (Live `BUY` After Full E4A–E4E)

```text
[alpha_node approves: approved_at, approval_price_observed_at, approval_price_native, token_decimals]
        │
        ▼
[E4C: RPC getBalance (outside lock) -> reserve_buy_slot (under db._lock: caps, dup-mint, cooldown, daily_loss, fee-aware SOL)]
   ├──(fail / RPC err)──▶ REJECTED (no reservation, stay SCANNING)
   ▼ (ok: active_coroutine reservation pinned)
[E4E pre-exec safety -> E4A quote (quote_received_at) -> E4B router/ammKey -> E4A drift/impact/expiry/out>0]
   ├──(fail)──▶ release_buy_reservation() -> ABORTED (pre-broadcast, stay SCANNING)
   ▼ (ok: verify non-gasless/single-signer, sign tx, verify signatures[0])
[E4D DB: order_intents = PREPARED -> re-check quote age/expiry -> SUBMITTED]
        │
        ▼  POST /swap/v2/execute
   ├──(HTTP 200 & valid sig & out_amount > 0)──▶ apply_trade_settlement()
   │                                               ├──(ok)──▶ CONFIRMED -> IN_POSITION (release res)
   │                                               └──(db err)──▶ SETTLEMENT_PENDING_PERSISTENCE (LOCKED)
   │
   └──(Timeout / Net Error / empty sig / out_amount <= 0)──▶ UNRESOLVED (reservation LOCKED!)
                                                                 │
                                                                 ▼  getSignatureStatuses(searchHistory=true) + getTransaction
                                                            ├──(confirmed & token_delta > 0)──▶ settle -> RECONCILED
                                                            ├──(err != None OR [None & verified expiry])──▶ FAILED -> release
                                                            └──(null/lag/inconsistent meta)──▶ stay UNRESOLVED (LOCKED)
```

---

## 9. Phased Implementation Sequence, Safety Boundaries & Acceptance Criteria

> **Global Live-Safety Warning:** Passing tests for any individual phase (E4A, E4B, E4C, or E4E) does **NOT** make the agent safe for unattended live trading. Unattended live `BREAKOUT_IGNITION` execution remains unsafe until **all five phases (including E4D durable intents & reconciliation)** and telemetry-backed threshold calibration are completed and independently approved.

| Phase | Files | Prerequisites | Safe After This Commit | Explicitly Remains Unsafe / Disabled |
|---|---|---|---|---|
| **1. E4C** (Reservation, Balance & Risk Guards) | `state_ledger.py`, `executor_node.py`, `config.py`, `tests/test_e4c_*.py` | E3C (`ca01c64`) | Fee-aware SOL check; live RPC `getBalance` check outside lock + atomic check under lock; cooldown & `DAILY_MAX_LOSS_PCT` under lock; active-coroutine reservations immune to 60s TTL eviction. | Post-broadcast timeout/crash recovery; approval drift/impact; pinned-pool route verification; safety freshness. **Not safe for unattended live trading.** |
| **2. E4D** (Durable Intents & Tx Reconciliation) | `db.py`, `state_ledger.py`, `executor_node.py`, `main.py`, `tests/test_e4d_*.py` | E4C | Durable `order_intents` (`PREPARED`/`SUBMITTED`/`UNRESOLVED`/`CONFIRMED`/`FAILED`/`RECONCILED`/`SETTLEMENT_PENDING_PERSISTENCE`); single-signer/non-gasless `signatures[0]` verification; `getTransaction` reconciliation; startup lock/recovery; safe `FAILED` expiry rules. | Quote drift/impact/slippage gates; pinned-pool route check; pre-exec safety freshness. **Not safe for unattended live trading.** |
| **3. E4A** (Pre-Broadcast Approval & Quote Integrity) | `state.py`, `config.py`, `alpha_node.py`, `executor_node.py`, `tests/test_e4a_*.py` | E4D (so live post-broadcast paths are already protected) | `approved_at` & `approval_price_observed_at` freshness; authoritative native price & `decimals` drift gate; `quote_received_at` 3-checkpoint validity; `lastValidBlockHeight`/`expireAt`; RTSE-safe slippage & `priceImpact`/`otherAmountThreshold` checks; pre-broadcast `out_amount > 0`. | Pinned-pool route enforcement; pre-exec safety freshness. |
| **4. E4B** (Execution Pool Integrity) | `executor_node.py`, `config.py`, `tests/test_e4b_*.py` | E4A + live `/swap/v2/order` OpenAPI/payload verification | Router-specific pinned-pool verification (`metis` + exact `ammKey == pair_address`); fail-closed rejection of `jupiterz`/`dflow`/`okx` or mismatched pools in live mode; paper-mode route observability. | Pre-exec safety freshness. |
| **5. E4E** (Pre-Execution Safety Freshness) | `db.py`, `safety_gate_node.py`, `executor_node.py`, `tests/test_e4e_*.py` | E4B | Bounded `watchlist.safety_checked_at` TTL; pre-execution fail-closed safety check before `BUY`. | Live enablement still requires telemetry-backed threshold calibration and final independent review. |

---

## 10. Regression Tests Preserving E1, E2A, E2B, and E3A–E3C

Every E4 commit must pass:
- **E1**: `tests/test_e1_pool_identity.py`
- **E2A**: `tests/test_e2a_exit_integrity.py`, `tests/test_e2a_watching_reentry.py`
- **E2B**: `tests/test_e2b_fee_accounting.py` (when integrated with `dev_2`; preserve `tx_fee_sol=None`/`UNKNOWN` in live mode and never fabricate fees from `LIVE_FEE_RESERVATION_BUFFER_SOL`)
- **E3A–E3C**: `tests/test_breakout_ignition.py`, `tests/test_scalper_entry_engine.py`, `tests/test_breakout_isolation.py`, and E3A/E3B/E3C OHLCV provenance tests
- **E4-specific**: Separate tests for `out_amount <= 0` vs. empty `tx_signature`; `getTransaction` token-delta attribution with pre-existing holdings; v0 transaction with lookup-table-loaded accounts returned via `jsonParsed` where a token-balance `accountIndex` refers to a lookup-table-loaded account and verifying loaded addresses are not appended a second time; malformed or out-of-range `accountIndex` remaining `UNRESOLVED`; `gasless`/`signatureFeePayer != taker` rejection; single null RPC response remaining `UNRESOLVED`; router-specific `metis`/`jupiterz`/`dflow`/`okx` route policies; RPC-outside-lock balance divergence.

---

## 11. Explicit Assumptions, API Uncertainties & Deferred Decisions

1. **Solana RPC Configuration (`SOLANA_RPC_URL`):** `config.py` currently has no `SOLANA_RPC_URL`. E4C/E4D require adding `SOLANA_RPC_URL` (supporting `getBalance`, `getSignatureStatuses`, `getTransaction`, `getBlockHeight`) and confirming RPC history retention for `getTransaction`.
2. **Live `/swap/v2/order` Payload Verification:** Before implementing E4B, capture real `/swap/v2/order` dry-run responses across `metis`, `jupiterz`, `dflow`, and `okx` to confirm exact `routePlan`, `ammKey`, `priceImpact`, `otherAmountThreshold`, `lastValidBlockHeight`, `expireAt`, and `signatureFeePayer` schemas.
3. **Deferred Numerical Thresholds (No Values Invented Here):** All thresholds (`MAX_BUY_APPROVAL_AGE_SECONDS`, `MAX_APPROVAL_PRICE_AGE_SECONDS`, `MAX_ENTRY_PRICE_DRIFT_PCT`, `MAX_ENTRY_PRICE_IMPACT_PCT`, `MAX_ACCEPTED_SLIPPAGE_BPS`, `MAX_QUOTE_AGE_SECONDS`, `LIVE_MIN_SOL_RESERVE`, `LIVE_FEE_RESERVATION_BUFFER_SOL`, `BALANCE_DIVERGENCE_WARN_SOL`, `PRE_EXECUTION_SAFETY_TTL_SECONDS`, `MIN_BLOCK_HEIGHT_HEADROOM`) must be calibrated from paper/soak telemetry before enabling live trading.
4. **Multi-Hop `metis` Routes:** Decide prior to E4B whether to require single-hop `len(routePlan) == 1 and ammKey == pair_address` or allow multi-hop routes (e.g., `SOL -> USDC -> mint`) provided every hop involving `order.mint` uses `ammKey == order.pair_address`.

---

## 12. Acceptance Criteria Summary

- **E4C:** Fee-aware `reserve_buy_slot()`, live RPC `getBalance` outside `db._lock` + atomic check under `db._lock`, cooldown & `DAILY_MAX_LOSS_PCT` under lock, active-coroutine reservation pinning.
- **E4D:** Non-secret `order_intents` persisted before broadcast; single-signer/non-gasless `signatures[0]` verified; timeouts/empty-sig/`out<=0` remain `UNRESOLVED` (locked); `getTransaction` delta attribution; strict `FAILED` expiry proof; startup recovery blocks new `BUY`s until reconciled.
- **E4A:** Separate `approved_at` / `approval_price_observed_at` / OHLCV audit timestamps; authoritative live native price + validated `decimals`; `quote_received_at` 3-checkpoint timing; `lastValidBlockHeight`/`expireAt`; RTSE-safe slippage & `priceImpact`/`otherAmountThreshold` gates; pre-broadcast `out_amount > 0`.
- **E4B:** Live `BREAKOUT_IGNITION` accepts only `metis` routes with verified `ammKey == pair_address` on target-mint hops and fails closed on unverifiable routers (`jupiterz`, `dflow`, `okx`) or mismatches.
- **E4E:** Watchlist `safety_checked_at` TTL enforced; pre-execution safety check fails closed on safety failure or API unavailability.
