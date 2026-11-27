# Phase E4 Audit: `BREAKOUT_IGNITION` Entry Execution & TOCTOU Safeguards

**Audit Type:** Strictly Read-Only Investigation  
**Repository:** `Tmalone1250/solana-trading-agent`  
**Branch:** `impl/e3c-parser-cache-integrity`  
**Exact Baseline Commit:** `ca01c640ac60477e63a33a86217298573680c5f0`  
**Working Tree State:** Clean (`git status --short` returned empty)  
**Audit Date:** 2026-11-27

---

## 1. Executive Summary & Overall E4 Assessment

**Overall Assessment: NOT SAFE FOR UNATTENDED LIVE `BREAKOUT_IGNITION` EXECUTION WITHOUT REMEDIATION.**

The codebase has strong **slot-capacity, duplicate-mint, and OHLCV provenance** controls at the decision and DuckDB reservation layers:
- Phase E3A–E3C closed-candle filtering, parser validation, and provenance binding (`OhlcvFetchResult`, `evaluate_ohlcv_provenance()`, `select_ohlcv_interval_provenance()`) prevent stale or unverified OHLCV from triggering `BREAKOUT_IGNITION`.
- `db.reserve_buy_slot()` ([state_ledger.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/state_ledger.py#L185-L256)) atomically re-reads DuckDB `agent_state` under `db._lock` before Jupiter I/O, enforcing `MAX_OPEN_POSITIONS`, lane caps, cross-lane duplicate-mint prevention, and SOL balance minus active in-memory reservations.
- Pool identity (`pair_address`) is validated at both `alpha_node` ([alpha_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/alpha_node.py#L338-L340)) and `executor_node` ([executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L145-L153)) and persisted on `ActivePosition` (`pool_identity_status = "PINNED"`).

However, **once `alpha_node` approves a `BREAKOUT_IGNITION` entry, the execution boundary (`executor_node.py`) performs zero market-condition, price-drift, slippage, or price-impact validation, and lacks on-chain confirmation/reconciliation for ambiguous live outcomes**:
1. **No approval timestamp, price context, or entry-drift enforcement:** `PendingOrder` carries no `approved_at` timestamp, no approval price / expected output, and no drift threshold; `executor_node` never revalidates breakout conditions, OHLCV freshness, or price movement before submitting.
2. **No slippage or `priceImpactPct` protection:** `fetch_jupiter_order()` sends only `inputMint`, `outputMint`, `amount`, and `taker`. `priceImpactPct` is logged (`jupiter_price_impact_raw`) but never blocks execution.
3. **Mint-level Jupiter routing vs. pool-pinned decision:** Jupiter routes across any pool for `order.mint` without enforcing `order.pair_address`, while `position_monitor_node` later monitors `order.pair_address`.
4. **Ambiguous live-tx outcomes can cause duplicate BUYs or untracked positions:** `PendingOrder` is never persisted to DuckDB; on `POST /swap/v2/execute` timeout or network error after broadcast, `reservation_id` is released and `focus_state` remains `SCANNING`, allowing a second `BUY` on the next tick.
5. **Reservation & guardrail gaps under concurrency:** `reserve_buy_slot()` omits `tx_fee_sol` from its SOL sufficiency check (and reserves `0.0` in live mode), `RESERVATION_TTL_SEC = 60.0` can expire mid-flight during slow live swaps, and `executor_node` does not re-check cooldowns or `DAILY_MAX_LOSS_PCT` under `db._lock`.

---

## 2. Exact Baseline Commit & Working-Tree Verification

Verified via `git rev-parse HEAD`, `git rev-parse --abbrev-ref HEAD`, and `git status --short`:
- **HEAD SHA:** `ca01c640ac60477e63a33a86217298573680c5f0`
- **Branch:** `impl/e3c-parser-cache-integrity`
- **Working Tree:** Clean (no modified, staged, or untracked files)

---

## 3. Complete `BREAKOUT_IGNITION` Entry Lifecycle Map

```
run_autonomous_loop() [main.py:60-75]
  │  reads db.get_state() -> builds initial_state (no pending_order)
  ▼
StateGraph START -> _route_entry() [graph.py:22-25]
  │  (focus_state == SCANNING and not chart_positions) -> "scanner"
  │  (or position_monitor -> _route_after_position_monitor() -> "scanner" if slots < cap [graph.py:89-97])
  ▼
1. scanner_node() [scanner_node.py]
  │  _parse_pair_to_candidate() [L119-185]: extracts mint, pair_address, dex_id, price_usd, liq, vol
  │  filter_candidates() [L188-220]: cooldown/lane/liq/vol filters; picks highest-liquidity pool per mint
  │  persists to watchlist [L533-587]; returns raw_candidates + active_watchlist [L633-637]
  ▼
2. _route_after_scanner() -> safety_gate_node() [graph.py:28-31, safety_gate_node.py:265-365]
  │  checks jup_is_sus; if cached/watchlist safety_passed==True, skips RugCheck + Jupiter Shield [L319-327]
  │  else calls check_token_safety() (RugCheck + Jupiter /ultra/v1/shield) and caches 300s [L329-330]
  │  returns safe_candidates [L360]
  ▼
3. _route_after_safety() -> alpha_node() [graph.py:34-39, alpha_node.py:90-601]
  │  a) Capacity & balance pre-check [L100-115]
  │  b) OHLCV acquisition [L123-155]: fetch_ohlcv_batch_with_provenance() (5m),
  │     should_fetch_1m_fallback() -> fetch_ohlcv_1m_batch_with_provenance() (1m)
  │  c) Provenance & interval selection [L203-208]: select_ohlcv_interval_provenance() [ohlcv_client.py:103-138]
  │     calls evaluate_ohlcv_provenance() [ohlcv_client.py:77-100] (source, status==200, age <= 90s)
  │  d) BREAKOUT_IGNITION evaluation [L211-219]: evaluate_scalper_entry() [scalper_entry_engine.py:171-353]
  │     verifies payload identity vs active_prov.candles [L216-229], runs detect_breakout_ignition() [breakout_ignition.py:235]
  │     on >=12 closed 5m bars (or >=15 closed 1m bars fallback)
  │  e) Narrative cache / Gemini Decision 1 + live momentum [L237-293]
  │  f) Approval & pool-identity gate [L311-340]:
  │     approved = decision1_wants_buy and scalper.eligible and setup_enabled
  │     if BREAKOUT_IGNITION and not is_valid_pool_address(cand_pair_address, mint): approved = False
  │  g) Open-mint filter, winner sort, PendingOrder creation (in-memory only) [L553-601]
  ▼
4. _route_if_order() -> executor_node() [graph.py:42-47, executor_node.py:91-542]
  │  a) Strategy allowlist & BREAKOUT_IGNITION pool-identity check [L135-153]
  │  b) In-memory caller_open duplicate-mint check [L161-167]
  │  c) Atomic DB reservation [L171-179]: db.reserve_buy_slot() [state_ledger.py:185-256] under db._lock
  │  d) Jupiter quote [L231-235]: fetch_jupiter_order(GET /swap/v2/order, inputMint=SOL, outputMint=mint, amount, taker)
  │  e) Live-only sign & submit [L248-260]: sign_jupiter_transaction() -> execute_jupiter_signed_order(POST /swap/v2/execute)
  │     (Paper mode: synthetic tx_signature = f"paper-buy-{mint[:8]}")
  │     except BaseException: db.release_buy_reservation(reservation_id); raise [L262-266]
  │  f) ActivePosition creation & atomic DB settlement [L290-315]:
  │     db.remove_from_watchlist(order.mint) -> db.apply_trade_settlement() [state_ledger.py:268-379]
  │     persists chart_positions_json, active_position_json, focus_state=IN_POSITION, sol_balance, daily_pnl_sol
  │  g) Telemetry logging [L317-344]: db.log_trade(), db.log_action()
  ▼
5. Subsequent monitoring (next tick) [main.py:75-94, position_monitor_node.py:59-120]
     position_monitor_node() builds pinned_pools = {cp.mint: cp.pair_address} [L108]
     calls fetch_batched_sol_prices(..., pinned_pools=pinned_pools) [batched_price_monitor.py:178]
```

**Data Copy / Transform / Persistence Boundaries:**
1. **DexScreener/Jupiter -> `CandidateToken` -> Watchlist (DuckDB):** Pool chosen by highest `liquidity_usd` per mint (`scanner_node.py:210-213`); persisted in `watchlist` table.
2. **Watchlist -> `safe_candidates` (in-memory dict):** `safety_gate_node` copies candidate dict and sets `safety_passed=True` (`safety_gate_node.py:322-337`).
3. **`alpha_node` -> `PendingOrder` (in-memory dict only):** `PendingOrder` copies `mint`, `pair_address`, `dex_id`, `entry_liquidity_usd`, `entry_mc_usd`, `breakout_telemetry`, `strategy` (`alpha_node.py:580-595`). **Never persisted to DuckDB.**
4. **`executor_node` -> `ActivePosition` (DuckDB `agent_state`):** Created only after `fetch_jupiter_order` (+ `execute_jupiter_signed_order` in live mode) returns (`executor_node.py:290-314`).

---

## 4. Findings Table (Ranked by Severity)

| ID | Severity | Status | Summary | Mode | Location |
|---|---|---|---|---|---|
| E4-01 | **CRITICAL** | CONFIRMED | Zero entry-drift, slippage, price-impact, or approval-age enforcement before execution | Both (highest impact Live) | [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L230-L260), [state.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/state.py#L111-L133) |
| E4-02 | **HIGH** | CONFIRMED | Jupiter executes mint-level route without enforcing `order.pair_address` pinned pool | Both (Live real funds) | [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L47-L69), [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L231-L234) |
| E4-03 | **HIGH** | CONFIRMED | Ambiguous live-tx outcome (timeout/error after broadcast) releases reservation without reconciliation -> duplicate BUY or untracked position | Live | [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L248-L266), [main.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/main.py#L74-L81) |
| E4-04 | **HIGH** | CONFIRMED | `out_amount <= 0` or empty `tx_signature` not rejected in live mode -> SOL debited while position dropped | Live | [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L257-L260), [state_ledger.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/state_ledger.py#L316) |
| E4-05 | **HIGH** | CONFIRMED | `reserve_buy_slot()` omits `tx_fee_sol` from SOL sufficiency check; live mode reserves `0.0` fee | Both | [state_ledger.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/state_ledger.py#L240-L254), [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L126-L131) |
| E4-06 | **HIGH** | CONFIRMED | `RESERVATION_TTL_SEC = 60.0` can expire mid-flight during slow live execution, allowing concurrent double-spend/duplicate entry | Both (Live latency) | [state_ledger.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/state_ledger.py#L13), [state_ledger.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/state_ledger.py#L111-L117) |
| E4-07 | **HIGH** | CONFIRMED | `executor_node()` does not re-check cooldowns or `DAILY_MAX_LOSS_PCT` circuit breaker under `db._lock` | Both | [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L134-L179) |
| E4-08 | **MEDIUM** | CONFIRMED | `safety_gate_node()` trusts 300s/watchlist-cached safety pass without pre-execution revalidation | Both | [safety_gate_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/safety_gate_node.py#L21), [safety_gate_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/safety_gate_node.py#L319-L327) |
| E4-09 | **MEDIUM** | CONFIRMED | Process-local locks & in-memory `PendingOrder` provide no crash/restart recovery during live submission | Live | [state_ledger.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/state_ledger.py#L18), [alpha_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/alpha_node.py#L580-L599) |
| E4-10 | **LOW** | CONFIRMED | `alpha_node` emits only `approved_pairs[0]`; concurrent `whale_lane` fill drops the chart tick without fallback | Both | [alpha_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/alpha_node.py#L569-L595) |

---

## 5. Detailed Evidence for Each Finding

### E4-01 — Zero Entry-Drift, Slippage, Price-Impact, or Approval-Age Enforcement
- **Severity:** CRITICAL | **Status:** CONFIRMED | **Mode:** Both (real-fund exposure in Live)
- **Locations:** [state.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/state.py#L111-L133), [config.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/config.py#L45-L193), [alpha_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/alpha_node.py#L580-L595), [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L27-L35), [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L230-L260)
- **Execution Path:** `alpha_node()` constructs `PendingOrder` without any `approved_at`, `entry_price_usd`, `expected_out_amount`, or drift limit. `executor_node()` calls `fetch_jupiter_order()` (which sends only `inputMint`, `outputMint`, `amount`, and `taker`), extracts `raw_price_impact = extract_raw_price_impact(order_resp)` solely for logging (`db.log_trade`, `db.log_action`), and signs/executes the swap unconditionally.
- **Concrete Failure Scenario:** `alpha_node()` awaits Gemini Decision 1 (`await decision_client.decide(...)` at [alpha_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/alpha_node.py#L274)) or OHLCV/network calls; meanwhile, the token spikes +40% or dumps -30%, or liquidity drains causing 15% Jupiter price impact. `executor_node()` still buys at the degraded quote.
- **Impact:** Bad entry under invalidated breakout conditions or excessive slippage/price impact.
- **Narrowest Remediation:** Record `approved_at` and approval price context (`entry_price_usd` / expected quote reference) on `PendingOrder`, and enforce explicit max approval age, max price drift, and max `priceImpactPct` in `executor_node()` before signing/submitting.

### E4-02 — Jupiter Executes Mint-Level Route Without Pinned-Pool Binding
- **Severity:** HIGH | **Status:** CONFIRMED | **Mode:** Both
- **Locations:** [alpha_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/alpha_node.py#L338-L340), [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L47-L69), [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L145-L153), [position_monitor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/position_monitor_node.py#L108-L120)
- **Execution Path:** `alpha_node` and `executor_node` verify `is_valid_pool_address(order.pair_address, mint=order.mint)`, and `ActivePosition` marks `pool_identity_status = "PINNED"`. However, `fetch_jupiter_order()` never passes `order.pair_address` or `order.dex_id` (nor inspects `routePlan` in `order_resp` to verify the routed pool matches `order.pair_address`).
- **Concrete Failure Scenario:** Token has a primary Raydium pool (`pair_address`) analyzed by OHLCV/Breakout Ignition and a thin/manipulated secondary pool on another DEX. Jupiter routes part or all of the swap through the secondary pool, while `position_monitor_node` monitors and exits against `order.pair_address`.
- **Impact:** Wrong-pool execution and entry/monitor price divergence.
- **Narrowest Remediation:** Inspect `order_resp` route metadata (if provided by Jupiter `/swap/v2/order`) or re-fetch/verify pinned pool price & liquidity right before execution so a divergent route is rejected.

### E4-03 — Ambiguous Live Outcome (Timeout/Error After Broadcast) Causes Duplicate BUY or Untracked Position
- **Severity:** HIGH | **Status:** CONFIRMED | **Mode:** Live
- **Locations:** [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L72-L88), [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L248-L266), [main.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/main.py#L74-L81)
- **Execution Path:** `execute_jupiter_signed_order()` posts the signed transaction with `timeout=20.0`. If the HTTP request times out or raises after Jupiter submits the tx to Solana, `except BaseException:` releases `reservation_id` and re-raises. `run_autonomous_loop()` catches the exception (`L78-81`), leaves `focus_state` unchanged (`SCANNING`), and sleeps `SCAN_POLL_SECONDS`.
- **Concrete Failure Scenario:** Transaction lands on-chain, but `/swap/v2/execute` times out at 20s. Next tick sees `SCANNING` and 0 open positions for `mint`, re-approves or re-enters, and submits a second live `BUY`, while the first position's tokens sit untracked in the wallet.
- **Impact:** Duplicate entry and untracked on-chain position.
- **Narrowest Remediation:** Persist a `SUBMITTED` order intent (including tx signature derived from the signed `VersionedTransaction` or `requestId`) before/at submission, and reconcile on-chain signature status / token balance before releasing the reservation or allowing another `BUY`.

### E4-04 — Live `out_amount <= 0` or Empty `tx_signature` Not Rejected Before Settlement
- **Severity:** HIGH | **Status:** CONFIRMED | **Mode:** Live
- **Locations:** [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L235), [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L257-L260), [state_ledger.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/state_ledger.py#L316)
- **Execution Path:** `tx_signature = str(exec_resp.get("signature") or exec_resp.get("txid") or "")` and `out_amount` are not validated for `bool(tx_signature)` or `out_amount > 0` before `apply_trade_settlement()`. At `state_ledger.py:316`, `is_keep` requires `int(new_pos_dict.get("token_amount") or 0) > 0`.
- **Concrete Failure Scenario:** `/swap/v2/execute` returns a malformed/failed payload with `outputAmountResult: "0"` or no signature. `apply_trade_settlement()` deducts `sol_delta` from `sol_balance` while dropping `new_pos_dict` from `chart_positions`.
- **Impact:** Accounting inconsistency and untracked position.
- **Narrowest Remediation:** Validate `out_amount > 0` (and non-empty `tx_signature` in live mode) inside the `try` block before settlement.

### E4-05 — `reserve_buy_slot()` Omits `tx_fee_sol` from SOL Sufficiency Check (and Reserves `0.0` in Live Mode)
- **Severity:** HIGH | **Status:** CONFIRMED | **Mode:** Both
- **Locations:** [state_ledger.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/state_ledger.py#L240-L254), [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L126-L131), [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L171-L179), [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L280-L282)
- **Execution Path:** `reserve_buy_slot()` checks `avail_sol + 1e-9 < required_sol` without `tx_fee_sol`, yet reserves `required_sol + tx_fee_sol` (`L251`). In `live` mode, `tx_fee_opt` is `None`, so `tx_fee_sol=0.0` is passed.
- **Concrete Failure Scenario:** Wallet has `0.5005` SOL, `TRADE_SIZE_SOL = 0.50`, `PAPER_TX_FEE_SOL = 0.001`. Reservation succeeds; settlement clamps `sol_balance` to `0.0`. In live mode, `0.0` fee is reserved, so tx/priority fees can cause on-chain insufficient-SOL failure or unreserved balance drift.
- **Impact:** Accounting inconsistency / failed live tx.
- **Narrowest Remediation:** Check `avail_sol + 1e-9 < (required_sol + tx_fee_sol)` in `reserve_buy_slot()` and pass a conservative live fee buffer.

### E4-06 — `RESERVATION_TTL_SEC = 60.0` Can Expire Mid-Flight During Slow Live Execution
- **Severity:** HIGH | **Status:** CONFIRMED | **Mode:** Both (Live latency)
- **Locations:** [state_ledger.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/state_ledger.py#L13), [state_ledger.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/state_ledger.py#L111-L117), [main.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/main.py#L148-L150)
- **Execution Path:** `_prune_reservations_locked()` drops reservations older than 60s whenever any lane calls `reserve_buy_slot()`.
- **Concrete Failure Scenario:** A slow live `fetch_jupiter_order` + `execute_jupiter_signed_order` stalls >60s; `run_whale_lane_loop` calls `reserve_buy_slot()`, prunes the chart reservation, and spends the same SOL/slot or enters the same mint.
- **Impact:** Duplicate entry, slot-cap breach, or SOL over-allocation.
- **Narrowest Remediation:** Keep active coroutine reservations pinned until exit of `try/except BaseException` (or raise TTL well above worst-case execution timeout with heartbeat).

### E4-07 — `executor_node()` Does Not Re-Check Cooldowns or `DAILY_MAX_LOSS_PCT` Under `db._lock`
- **Severity:** HIGH | **Status:** CONFIRMED | **Mode:** Both
- **Locations:** [executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L134-L179), [whale_lane.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/whale_lane.py#L317-L318), [whale_lane.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/whale_lane.py#L505)
- **Execution Path:** Cooldowns and daily-loss circuit breaker are checked in `scanner_node` / `whale_lane` / `alpha_node`, never inside `executor_node` or `reserve_buy_slot()`.
- **Concrete Failure Scenario:** While `alpha_node` is awaiting Gemini/OHLCV, `whale_lane` realizes a loss breaching `DAILY_MAX_LOSS_PCT` or puts `mint` on cooldown. `executor_node()` still executes the chart `BUY`.
- **Impact:** Bad entry violating cooldown or daily loss halt.
- **Narrowest Remediation:** Check `db.is_on_cooldown(mint)` and `DAILY_MAX_LOSS_PCT` inside `reserve_buy_slot()` under `db._lock`.

### E4-08 — `safety_gate_node()` Trusts Cached Safety Pass (300s / Watchlist) Without Pre-Execution Revalidation
- **Severity:** MEDIUM | **Status:** CONFIRMED | **Mode:** Both
- **Locations:** [safety_gate_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/safety_gate_node.py#L21), [safety_gate_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/safety_gate_node.py#L319-L327)
- **Execution Path:** `safety_gate_node()` skips `check_token_safety()` if `cand.get("safety_passed") is True`, `watchlist_safety.get(mint) is True`, or `_safety_cache` (300s) hits.
- **Concrete Failure Scenario:** A token passes safety at watchlist admission, sits in watchlist across soak/coiling cycles, and later has LP unlocked or Jupiter Shield flagged before `BREAKOUT_IGNITION` triggers.
- **Impact:** Bad entry into a token whose safety state degraded.
- **Narrowest Remediation:** Re-run or enforce a tighter safety-cache freshness window when a `BUY` is about to execute (or bypass watchlist `safety_passed` cache after TTL).

### E4-09 — Process-Local Locks & In-Memory `PendingOrder` Offer No Restart Recovery
- **Severity:** MEDIUM | **Status:** CONFIRMED | **Mode:** Live
- **Locations:** [state_ledger.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/state_ledger.py#L18), [alpha_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/alpha_node.py#L580-L599)
- **Execution Path:** `_inflight_buy_reservations` and `PendingOrder` live only in Python memory.
- **Concrete Failure Scenario:** Process restarts/crashes while `execute_jupiter_signed_order()` is in flight; on restart, `agent_state` still shows `SCANNING` with no record of the submitted swap.
- **Impact:** Untracked position / duplicate entry on restart.
- **Narrowest Remediation:** Persist pre-submission order intent in DuckDB and reconcile on startup.

### E4-10 — `alpha_node` Single-Winner Emission Drops Tick on Concurrent `whale_lane` Collision
- **Severity:** LOW | **Status:** CONFIRMED | **Mode:** Both
- **Locations:** [alpha_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/alpha_node.py#L569-L595)
- **Execution Path:** `alpha_node` emits only `approved_pairs[0]`; if `whale_lane` enters that mint concurrently before `executor_node` runs, `reserve_buy_slot` blocks it and the tick ends without trying `approved_pairs[1]`.
- **Impact:** Missed eligible entry (no unsafe spend).
- **Narrowest Remediation:** Low priority; optionally allow fallback or clean telemetry logging.

---

## 6. Paper vs. Live Execution Comparison

| Dimension | Paper Mode (`EXECUTION_MODE="paper"`) | Live Mode (`EXECUTION_MODE="live"`) |
|---|---|---|
| **Quote Source** | Live Jupiter `GET /swap/v2/order` (`taker=None`) ([executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L222-L235)) | Live Jupiter `GET /swap/v2/order` (`taker=pubkey`) ([L223-235](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L223-L235)) |
| **Fill Price / Output** | `order_resp["outAmount"]` (indicative quote, no on-chain execution) | `exec_resp["outputAmountResult"]` if present, else `order_resp["outAmount"]` ([L258-259](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L258-L259)) |
| **Slippage & Price Impact** | Neither enforced; `priceImpactPct` logged only | Neither enforced; `priceImpactPct` logged only |
| **Fee Accounting (E2B)** | `tx_fee_opt = PAPER_TX_FEE_SOL` (`0.001`), `fee_accounting_status = "PAPER_ASSUMED"` ([L126-128](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L126-L128)) | `tx_fee_opt = None`, `fee_accounting_status = "UNKNOWN"` ([L129-131](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L129-L131)); `sol_delta` excludes on-chain tx/priority fees |
| **Balance Validation** | `reserve_buy_slot` checks `avail_sol < required_sol` (omits `PAPER_TX_FEE_SOL`) | `reserve_buy_slot` checks `avail_sol < required_sol` (`tx_fee_sol=0.0`); relies on DuckDB `sol_balance`, not live RPC wallet balance |
| **Tx Confirmation** | Synthetic `paper-buy-{mint[:8]}` ([L261](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L261)) | Single `POST /swap/v2/execute` (20s timeout); no RPC `getSignatureStatuses` polling |
| **Failure / Retry** | Raises on quote failure; releases reservation | Raises on quote/sign/execute failure; releases reservation even if tx already broadcast |
| **Position Persistence** | `apply_trade_settlement()` under `db._lock` | Same, but vulnerable to `out_amount == 0` / missing `tx_signature` not being rejected |

*Note on E2B Fee Accounting:* Live mode intentionally marks `fee_accounting_status = "UNKNOWN"` when `tx_fee_opt is None`; however, E4's `reserve_buy_slot()` currently passes `tx_fee_sol=0.0` in live mode and omits `tx_fee_sol` from its sufficiency check in both modes.

---

## 7. TOCTOU & Concurrency Analysis

1. **Approval timestamp & max age:** Not captured on `PendingOrder`; no max age is enforced.
2. **Expired breakout / OHLCV revalidation before submission:** `executor_node` does **not** revalidate `evaluate_ohlcv_provenance()`, `detect_breakout_ignition()`, liquidity, or order-flow before calling Jupiter.
3. **Pinned pool identity:** `pair_address` is verified at `alpha_node` and `executor_node` and saved to `ActivePosition`, **but not enforced in Jupiter's quote/swap request**.
4. **Price drift / second quote:** No approval price is passed to `executor_node`, no second quote or comparison occurs, and no drift threshold exists in `config.py`.
5. **Quote expiration:** `fetch_jupiter_order` is immediately followed by `sign_jupiter_transaction` + `execute_jupiter_signed_order` in `executor_node`, but `priceImpactPct` and `outAmount` are never checked.
6. **Concurrent loops:** `main.py` runs `run_autonomous_loop` (chart) and `run_whale_lane_loop` (whale) concurrently in one asyncio process sharing `db._lock` and `_inflight_buy_reservations`. `reserve_buy_slot()` prevents normal same-process races, **except** when `RESERVATION_TTL_SEC` (60s) expires mid-flight, or for cooldown/circuit-breaker state changes, or across process restarts.
7. **Position creation ordering:** `ActivePosition` is created **after** `execute_jupiter_signed_order` returns (never before execution), which is good—except an ambiguous timeout after broadcast leaves no `ActivePosition` and no pending-order record.

---

## 8. Confirmed Safeguards to Preserve

- `db.reserve_buy_slot()` atomic DuckDB re-read under `db._lock` ([state_ledger.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/state_ledger.py#L185-L256)).
- `db.release_buy_reservation()` on `BaseException` ([executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L262-L266)) and atomic clearance inside `apply_trade_settlement()` ([state_ledger.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/state_ledger.py#L286-L287)).
- `BREAKOUT_IGNITION` `is_valid_pool_address()` check at `alpha_node` ([alpha_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/alpha_node.py#L338-L340)) and `executor_node` ([executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L145-L153)).
- Strategy allowlist enforcement at `executor_node` ([executor_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/executor_node.py#L135-L144)).
- E3A/E3B/E3C closed-candle & provenance binding (`evaluate_ohlcv_provenance`, `select_ohlcv_interval_provenance`, payload match check in `evaluate_scalper_entry`).

---

## 9. Recommended Remediation Order (Narrow, Independently Testable Groups)

1. **Group 1 — Execution-Boundary Quote & Drift Guardrails (Highest Priority):**
   Add approval timestamp & price context to `PendingOrder`; enforce max approval age, max entry price drift, max `priceImpactPct`, and `out_amount > 0` / non-empty `tx_signature` in `executor_node` before signing/settling.
2. **Group 2 — Pinned-Pool Route Verification:**
   Verify Jupiter route / live pinned-pool price & liquidity against `order.pair_address` before executing `BREAKOUT_IGNITION` swaps.
3. **Group 3 — Atomic Reservation Hardening:**
   Include `tx_fee_sol` (plus live fee buffer) in `reserve_buy_slot()` SOL sufficiency check; enforce `db.is_on_cooldown()` and `DAILY_MAX_LOSS_PCT` inside `reserve_buy_slot()`; prevent mid-flight TTL eviction of active coroutines.
4. **Group 4 — Live Submission Persistence & Ambiguous-Outcome Reconciliation:**
   Persist `SUBMITTED` live swap intent (with tx signature) to DuckDB before `/swap/v2/execute`, and reconcile on-chain status on timeout/restart before releasing reservation or allowing re-entry.
5. **Group 5 — Pre-Entry Safety Freshness:**
   Tighten or bypass cached safety pass immediately prior to `BUY` execution.

---

## 10. Explicit Limitations & Unverified Areas

- **No runtime execution or live RPC/Jupiter API calls were performed** (strictly read-only audit). Actual Jupiter `/swap/v2/order` `routePlan` schema fields and `/swap/v2/execute` error/timeout payloads were not tested against live endpoints.
- **No test suite was executed** during this audit, as no files were modified.
- **Deployment / systemd / Raspberry Pi supervisor behavior** (e.g., restart frequency, OOM kills, single-instance locking) was outside the repository scope and not inspected.
