# DIRECTIVE 07 — PAPER FEE & BREAKOUT BUY-RATIO PROVENANCE AUDIT

**Project:** Solana Trading Agent (`solana-trading-agent`)  
**Branch / Commit Inspected:** `dev_1` @ `eb776074ad498185e31b23574fc983e5edd24f0d`  
**Audit Date:** `2026-11-26`  
**Scope:** Strictly read-only forensic audit of `PAPER_TX_FEE_SOL` and `SCALPER_MIN_BUY_RATIO_5M` for `BREAKOUT_IGNITION` and the Historical Decision Replay Simulator (Phase A support).  

---

## 1. Executive Summary

| Item | Code default | Effective behavior | Historical status | Evidence |
|---|---|---|---|---|
| **PAPER_TX_FEE_SOL** | `0.001` SOL (`config.py:50`) | Deducted once per `BUY`, `PARTIAL_SELL`, and `SELL` in `executor_node.py` (`EXECUTION_MODE == "paper"` only; `0.0` in `live`), reducing both `sol_balance` and `daily_pnl_sol` / `pnl_sol`. | `EXPECTED_FROM_CODE_DEFAULT` (empirically corroborated at `0.001` SOL in Phase 1 DuckDB `action_log` & `trades`; exact PID 8315 `.env` remains `HISTORICAL_RUNTIME_VALUE_UNVERIFIED`) | `config.py:50`, `executor_node.py:126,266-267,329-331,391-397`, `state_ledger.py:236,296-297`, Phase 1 DuckDB `trades` & `action_log` |
| **General buy ratio** (`SCALPER_MIN_BUY_RATIO_5M`) | `0.58` (`config.py:114`) | Hard rejection gate (`buy_ratio_5m < min_buy_ratio`, i.e., `< 58%` passes at `>= 58%`) on the 5m path for `SWEEP_RECLAIM`, `DISPLACEMENT_RETEST`, and `NONE` when `total_5m >= min_txns`. | `EXPECTED_FROM_CODE_DEFAULT` (`HISTORICAL_RUNTIME_VALUE_UNVERIFIED`) | `config.py:114`, `scalper_entry_engine.py:198,307-310` |
| **BREAKOUT_IGNITION buy ratio** | `0.62` hardcoded in `breakout_ignition.py:252` (vs `docs/SPEC.md:215` claiming a hard 62% gate) | **Exempt** from `SCALPER_MIN_BUY_RATIO_5M` (`58%`) in `scalper_entry_engine.py:307` (and never checked on 1m fallback `L208-236`). In `breakout_ignition.py:252-258`, `buy_ratio_5m >= 0.62` is a **score-only** 8-point criterion (`4` pts if `>= 0.55`), **NOT** a hard veto (`L354-360`). | `EXPECTED_FROM_CODE_DEFAULT` (behavior confirmed in `dev_1` code; Phase 1 `action_log` confirms `BUY_RATIO(...%<62%)` in `failed_gates`) | `scalper_entry_engine.py:208-236,307`, `breakout_ignition.py:252-258,354-360`, `docs/SPEC.md:215` |

---

## 2. Exact Source Locations and Line References

### `PAPER_TX_FEE_SOL`
- **Configuration definition**: `config.py:32-36` (`SettingsConfigDict`), `config.py:50` (`PAPER_TX_FEE_SOL: float = Field(default=0.001, ge=0.0)`).
- **Template documentation**: `.env.example:25` (`PAPER_TX_FEE_SOL=0.001`).
- **Executor read & mode gate**: `executor_node.py:126` (`tx_fee_sol = float(config.PAPER_TX_FEE_SOL) if config.EXECUTION_MODE == "paper" else 0.0`).
- **Pre-trade BUY slot reservation**: `executor_node.py:157-161` -> `db.py:596` -> `state_ledger.py:170-240` (`L236`: reserves `required_sol + tx_fee_sol`).
- **BUY settlement**: `executor_node.py:266-267, 290-296` -> `db.py:602` -> `state_ledger.py:253-364`.
- **PARTIAL_SELL settlement**: `executor_node.py:329-331, 339, 350-365`.
- **Final SELL settlement**: `executor_node.py:391, 397, 438-453`.
- **Tests**: `tests/test_outcome_tracker.py:161-229`, `tests/test_sprint2a_accounting.py:41,69,85-86,114`, `tests/test_soak_readiness.py:46,110,176,232,339`, `tests/test_sprint1_instrumentation.py:304`, `tests/test_whale_control_separation.py:177`, `tests/test_two_slot_and_whale_hardening.py:34`.

### `SCALPER_MIN_BUY_RATIO_5M` & `BREAKOUT_IGNITION` Buy Ratio
- **Configuration definition**: `config.py:32-36` (`SettingsConfigDict`), `config.py:114` (`SCALPER_MIN_BUY_RATIO_5M: float = Field(default=0.58, ge=0.0, le=1.0)`).
- **Scalper engine assignment & gate**: `scalper_entry_engine.py:172-182` (`_extract_order_flow`), `scalper_entry_engine.py:198` (`min_buy_ratio = config.SCALPER_MIN_BUY_RATIO_5M if config else 0.58`), `scalper_entry_engine.py:208-236` (1m fallback), `scalper_entry_engine.py:304-310` (Gate 4: Order-Flow Imbalance).
- **Breakout Ignition scoring & diagnostic**: `breakout_ignition.py:107-121` (`_extract_flow_and_accel`), `breakout_ignition.py:202, 252-258` (`FLOW` / `BUY_RATIO`), `breakout_ignition.py:327` (`buy_ratio_5m`), `breakout_ignition.py:354-360` (`detected`).
- **Downstream live-momentum usage (separate from `SCALPER_MIN_BUY_RATIO_5M`)**: `alpha_helpers.py:77-81, 102, 123, 136` (`compute_live_momentum_and_action`).
- **Production callers of `evaluate_scalper_entry`**: `alpha_node.py:128` (Chart lane), `whale_lane.py:381` (Whale lane).
- **Documentation**: `docs/SPEC.md:215`.
- **Tests**: `tests/test_scalper_entry_engine.py:260-299`, `tests/test_breakout_ignition.py:77-83, 284-313`.

---

## 3. `PAPER_TX_FEE_SOL` Definition and Complete Production Call Chain

### A. Configuration Declaration (`config.py:32-55`)
- **Declaration (`config.py:50`)**: `PAPER_TX_FEE_SOL: float = Field(default=0.001, ge=0.0)`.
- **Default & Constraints**: Default is `0.001` SOL; constrained to `>= 0.0` (`ge=0.0`).
- **Settings Lookup (`config.py:32-36`)**: `AgentConfig` inherits from `pydantic_settings.BaseSettings` with `SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")`. There are **no** Pydantic `AliasChoices` or custom field aliases. Process environment variables take precedence over `.env`, which takes precedence over the class default (`0.001`).
- **Local `.env` / `.env.example` Discrepancy**:
  - Current laptop `.env` (checked via non-secret key match) contains **no** `PAPER_TX_FEE_SOL=` line.
  - Current `.env.example:25` **does** contain `PAPER_TX_FEE_SOL=0.001` (added in commit `d2ad41b` on `2026-11-22 12:58:50 -0600`).
  - Supplied Pi `.env` snapshot (per audit premise) does not contain `PAPER_TX_FEE_SOL`. Therefore, both laptop and Pi `.env` rely on the `config.py:50` default (`0.001`) unless overridden by process environment.

### B. Complete Production Call Chain
`PAPER_TX_FEE_SOL` is read in **exactly one production file** (`executor_node.py`):

1. **Read & Mode Gate (`executor_node.py:126`)**:
   ```python
   tx_fee_sol = float(config.PAPER_TX_FEE_SOL) if config.EXECUTION_MODE == "paper" else 0.0
   ```
   - In non-paper mode (`EXECUTION_MODE == "live"`), `tx_fee_sol` is strictly `0.0` (live on-chain network/priority fees come out of actual wallet balance).
2. **Pre-Trade BUY Slot Reservation (`executor_node.py:157-161` -> `db.reserve_buy_slot` -> `state_ledger.py:170-240`)**:
   - `executor_node.py:158` passes `tx_fee_sol=tx_fee_sol`.
   - Note a subtle detail at `state_ledger.py:227`: the `INSUFFICIENT_SOL` check compares `avail_sol + 1e-9 < required_sol` (excluding `tx_fee_sol`), whereas `state_ledger.py:236` records `"sol_amount": float(required_sol + tx_fee_sol)` in `_inflight_buy_reservations` for concurrent buys.
3. **Settlement (`executor_node.py` -> `db.apply_trade_settlement` -> `state_ledger.py:253-364`)**:
   - Fee deductions occur in **one layer only** (`executor_node.py` computes `sol_delta` and `pnl_delta`; `state_ledger.apply_trade_settlement` applies them atomically to `agent_state.sol_balance` and `agent_state.daily_pnl_sol` under `db._lock`).
   - `outcome_tracker.py` tracks DexScreener price percentage changes (`price_*_pct`, `mfe_pct`, `mae_pct`) and does **not** apply `PAPER_TX_FEE_SOL`.

---

## 4. Fee Deduction Behavior and Conditional Fee Calculations

### Verified Deduction Matrix (`EXECUTION_MODE == "paper"`)

| Transaction Side | `sol_balance` (`sol_delta`) | `daily_pnl_sol` (`pnl_delta`) | `trades` Table Persistence | `action_log` Persistence |
|---|---|---|---|---|
| **BUY** (`L266-267, 290-321`) | `-(spent_sol + tx_fee_sol)` | `-tx_fee_sol` | `sol_value = spent_sol` (gross `0.5`), `pnl_sol = None` | `notional = spent_sol`, `sol_equity` & `daily_realized_sol` reflect `-tx_fee_sol` |
| **PARTIAL_SELL** (`L329-331, 350-382`) | `+(received_sol - tx_fee_sol)` | `partial_pnl = round(received_sol - cost_of_sold - tx_fee_sol, 6)` *(also added to `pos_obj.realized_partial_pnl_sol` at `L339`)* | `sol_value = received_sol` (gross), `pnl_sol = partial_pnl` (net of fee) | `notional = received_sol`, `pnl_amount = partial_pnl`, `sol_equity` & `daily_realized_sol` net of fee |
| **SELL** (`L391, 397, 438-474`) | `+(received_sol - tx_fee_sol)` | `pnl_sol = round((received_sol - cost_basis - tx_fee_sol) if cost_basis > 0 else -tx_fee_sol, 6)` | `sol_value = received_sol` (gross), `pnl_sol = pnl_sol` (net of fee) | `notional = received_sol`, `pnl_amount = pnl_sol`, `sol_equity` & `daily_realized_sol` net of fee |

### Conditional Fee Calculations (`TRADE_SIZE_SOL = 0.5` SOL, `PAPER_TX_FEE_SOL = 0.001` SOL)
*Conditional on `EXECUTION_MODE == "paper"` and `PAPER_TX_FEE_SOL = 0.001`:*
- **Per fee-bearing transaction**: `0.001 / 0.50 = 0.002` (**0.20%** of `0.5` SOL).
- **Complete BUY + SELL cycle (2 fee-bearing transactions)**: `2 * 0.001 = 0.002` SOL (**0.40%** of `0.5` SOL).
- **Complete BUY + PARTIAL_SELL + final SELL cycle (3 fee-bearing transactions)**: `3 * 0.001 = 0.003` SOL (**0.60%** of `0.5` SOL).

### Empirical Phase 1 DuckDB Corroboration (`VERIFIED` in `reports/phase1/solana_meme_agent.duckdb`)
- Across all `17` active-soak `SELL` trades (`0` `PARTIAL_SELL` trades occurred), `ROUND(sol_value - 0.5 - pnl_sol, 6)` equals **`0.001`** on every single trade (e.g., trade #357 `NORFEATH`: `0.434899697 - 0.5 - (-0.0661) = 0.001`).
- In `action_log`, consecutive `BUY` (`FIONA`, `05:03:38Z`) and `SELL` (`NORFEATH`, `05:10:43Z`) rows show exact `0.001` SOL deductions on both `sol_equity` (`6.878438 - 0.501 = 6.377438`) and `daily_realized_sol` (`0.093822 - 0.001 = 0.092822`).

---

## 5. `SCALPER_MIN_BUY_RATIO_5M` Definition and Effective Threshold Logic

### A. Configuration Declaration (`config.py:105-120`)
- **Declaration (`config.py:114`)**: `SCALPER_MIN_BUY_RATIO_5M: float = Field(default=0.58, ge=0.0, le=1.0)`.
- **Override behavior**: Standard `BaseSettings` lookup (`env_file=".env"`, no aliases). Neither `.env` nor `.env.example` defines `SCALPER_MIN_BUY_RATIO_5M`, nor does the supplied Pi `.env`.

### B. `scalper_entry_engine.py:172-321` Logic
1. **Initial Assignment (`L198`)**:
   ```python
   min_buy_ratio = config.SCALPER_MIN_BUY_RATIO_5M if config else 0.58
   ```
2. **Order-Flow Extraction & Missing Data (`_extract_order_flow`, `L172-182`)**:
   - Reads `pair_data["txns"]["m5"]["buys"/"sells"]` or falls back to `pair_data["buys_5m"/"sells_5m"]`.
   - If missing or `0`, `buys = 0`, `sells = 0`, `total = 0`, `ratio = 0.0`.
   - At Gate 4 (`L305-306`), `if total_5m < min_txns:` (`15` default) triggers **first** (`5m txns 0 < 15`), so missing transaction data fails the `min_txns` gate before `buy_ratio_5m` is ever compared.
3. **1m Fallback Path (`L208-236`)**:
   - When `len(candles_5m) < 12` and `len(candles_1m) >= 15`, `evaluate_scalper_entry` runs `detect_breakout_ignition(..., bar_minutes=1)` and checks `bo_enabled`, `time_in_slot_min < min_soak`, `total_5m < min_txns`, `bi_1m.detected`, and `rsi_1m > 78.0`. **`min_buy_ratio` is NOT checked on the 1m fallback path.**
4. **5m Path Gate 4 (`L304-310`)**:
   ```python
   if total_5m < min_txns:
       reasons.append(f"5m txns {total_5m} < {min_txns}")
   elif setup_type != "BREAKOUT_IGNITION" and buy_ratio_5m < min_buy_ratio:
       reasons.append(
           f"5m buy_ratio {buy_ratio_5m * 100:.0f}% < {min_buy_ratio * 100:.0f}%"
       )
   ```
   - **Comparison operator**: Fails when `buy_ratio_5m < min_buy_ratio` (passes at `>= 0.58`).
   - **Effect**: Hard rejection (`eligible=False`, `rejection_reason="5m buy_ratio ...% < ...%"`) **only** when `setup_type != "BREAKOUT_IGNITION"`.

### C. `BREAKOUT_IGNITION` 62% Threshold: Documented vs Actual Code (`VERIFIED`)
- **`docs/SPEC.md:215`** states: `buy_ratio_5m >= 58% (SCALPER_MIN_BUY_RATIO_5M, or >= 62% for BREAKOUT_IGNITION)`.
- **Actual Code (`breakout_ignition.py:252-258, 354-360`)**:
  - `detect_breakout_ignition` checks `if buy_ratio_5m >= 0.62:` -> `+8` pts (`passed_gates.append("FLOW")`), `elif buy_ratio_5m >= 0.55:` -> `+4` pts + `failed_gates.append(f"BUY_RATIO({buy_ratio_5m * 100:.0f}%<62%)")`, else `+0` pts + `failed_gates.append(...)`.
  - **Hard veto check (`L354-360`)**: `detected = (final_score >= score_thresh and res_passed and vol_passed and state != "EXTENDED" and rsi_ok)`.
  - Therefore, **the 62% threshold is NOT a hard veto in `BREAKOUT_IGNITION`**; it is a **score-only** 8-point contribution (with 4-point partial credit at `>= 55%`) and a diagnostic tag in `D1_failed_gates`. A token with `< 62%` (or even `0%`) buy ratio can still achieve `detected=True` and `eligible=True` if its other 92 score points meet `score_thresh` (e.g., `75` default or `55` inferred Pi threshold) and `total_5m >= min_txns`.

---

## 6. `BREAKOUT_IGNITION` Caller / Bypass Analysis

1. **Production Callers of `evaluate_scalper_entry`**:
   - **Chart Lane (`alpha_node.py:128`)**: Called **once** per watchlist candidate per tick (`alpha_node` does not call it again).
   - **Whale Lane (`whale_lane.py:381`)**: Called **once** per fast-track whale trigger (`time_in_slot_min=5.0`) solely to populate `breakout_telemetry` (not used in Chart `BREAKOUT_IGNITION` isolation when `WHALE_STRATEGY_ENABLED=False`).
2. **Does `BREAKOUT_IGNITION` reach or bypass `SCALPER_MIN_BUY_RATIO_5M`?**
   - It **bypasses** `SCALPER_MIN_BUY_RATIO_5M` on **both** the 5m path (`scalper_entry_engine.py:307`: `setup_type != "BREAKOUT_IGNITION"`) and the 1m fallback path (`L208-236`).
3. **Independent Upstream/Downstream `buy_ratio` Checks**:
   - **Upstream**: None in `scanner_node.py` or `safety_gate_node.py`.
   - **Downstream (`alpha_helpers.py:63-156`, `compute_live_momentum_and_action`)**: On narrative-cache hits, live `buy_ratio` (`0.50` default if `total_5m == 0`) contributes `0.0–1.8` pts to `momentum_quality` (`L102`), caps `raw_mom <= 1.40` if `total_5m >= 6 and buy_ratio < 0.35` (`L123`), and forces `action = "REJECT"` if `total_5m >= 10 and buy_ratio < 0.30 and chg_5m <= -10.0` (`L136`). On cache misses, Gemini `gemini-decision-1` receives `5m_buys` / `5m_sells` (`buy_ratio`) in the prompt (`alpha_helpers.py:173-182`).

---

## 7. Relevant Test Coverage and Uncovered Boundaries

| Behavior | Covered by Existing Tests? | Exact Test Reference | Uncovered Boundaries |
|---|---|---|---|
| `PAPER_TX_FEE_SOL=0.001` deducted on paper `BUY`, `PARTIAL_SELL`, `SELL` | **Yes** (`VERIFIED`) | `tests/test_outcome_tracker.py:161-229`, `tests/test_sprint2a_accounting.py:41-105` | Non-paper (`EXECUTION_MODE="live"`) `tx_fee_sol == 0.0` path is not explicitly asserted in `test_outcome_tracker.py` |
| General `SCALPER_MIN_BUY_RATIO_5M` (`58%`) rejection | **Yes** (coarse) | `tests/test_scalper_entry_engine.py:262-298` (`66.7%` pass vs `44%` fail) | Exact boundary at `58.0%` (`0.58`) vs `57.9%`, and missing/zero `txns` behavior |
| `BREAKOUT_IGNITION` `62%` scoring & exemption from `58%` | **Partial** | `tests/test_breakout_ignition.py:77-83, 284-313` (`32/46 = 69.6%`, `20/28 = 71.4%`) | **Uncovered**: `< 62%` (e.g., `50%` or `57%`) passing `BREAKOUT_IGNITION` with `BUY_RATIO(<62%)` in `failed_gates`; `62.0%` vs `61.9%` boundary; 1m fallback buy-ratio exemption |

---

## 8. Current Configuration Versus Historical Runtime Provenance

1. **Current Code Defaults (`dev_1` @ `eb77607`)**:
   - `PAPER_TX_FEE_SOL = 0.001` (introduced in `d2ad41b`, `2026-11-22 12:58:50 -0600`; unchanged through `eb77607`).
   - `SCALPER_MIN_BUY_RATIO_5M = 0.58` (present since initial commit `e1bd02d`, `2026-11-19 19:12:01 -0600`; `BREAKOUT_IGNITION` exemption present in `scalper_entry_engine.py` across `dev_1`).
   - No uncommitted changes exist on any tracked Python or config file.
2. **Current Pi `.env` Overrides**: Supplied Pi `.env` excludes both `PAPER_TX_FEE_SOL` and `SCALPER_MIN_BUY_RATIO_5M` (no overrides present).
3. **Historical PID 8315 Runtime Provenance**:
   - Because exact PID `8315` process environment and Pi filesystem hashes cannot be independently verified from local Git/workspace alone, both settings are formally classified as **`HISTORICAL_RUNTIME_VALUE_UNVERIFIED`** / **`EXPECTED_FROM_CODE_DEFAULT`**.
   - However, Phase 1 DuckDB (`trades` & `action_log`) **empirically corroborates** `PAPER_TX_FEE_SOL = 0.001` on every active-soak `BUY` and `SELL`.

---

## 9. Simulator Implications (`BREAKOUT_IGNITION` ONLY)

- **`PAPER_TX_FEE_SOL`**:
  - Belongs in **Execution/Accounting Replay** and **Outcome/P&L Analysis** (NOT decision-stage replay, except for the `reserve_buy_slot` in-flight reservation sizing if modeling multi-slot concurrency).
  - Must remain parameterized per config profile (`0.001` SOL for Phase 1 `paper` profile; `0.0` for `live`).
- **`SCALPER_MIN_BUY_RATIO_5M`**:
  - Does **NOT** apply as a hard gate to `BREAKOUT_IGNITION` (`scalper_entry_engine.py:307` exempts `BREAKOUT_IGNITION`).
  - For `BREAKOUT_IGNITION` **Recorded-Input Decision Replay**: `buy_ratio_5m` is **only** logged in `action_log.breakout_failed_gates` when `< 62%` (e.g., `BUY_RATIO(53%<62%)`); when `>= 62%` (or when `txns_5m` is missing), the exact `buy_ratio_5m` and `total_5m` are **`MISSING` / `NOT_REPLAYABLE`** at the candle/order-flow layer unless external fixtures are supplied under `DIAGNOSTIC_RECONSTRUCTION`.
  - Must **never** invent missing historical `txns_5m` or reconstruct historical buy ratios from present-day API calls.

---

## 10. Proven Facts, Reasonable Inferences, and Unresolved Questions

- **Proven Facts (`VERIFIED`)**:
  - `PAPER_TX_FEE_SOL` defaults to `0.001` SOL, applies only in `paper` mode, and is deducted once per `BUY`, `PARTIAL_SELL`, and `SELL` in `executor_node.py` -> `state_ledger.py`.
  - Phase 1 DuckDB `trades` and `action_log` show exact `0.001` SOL fee deductions on all 17 active-soak `BUY`+`SELL` pairs.
  - `SCALPER_MIN_BUY_RATIO_5M` (`0.58`) is **bypassed** by `BREAKOUT_IGNITION`; the `62%` threshold in `breakout_ignition.py:252` is a **score-only** 8-point criterion, contradicting `docs/SPEC.md:215`'s description of a hard 62% gate.
- **Reasonable Inferences (`INFERRED`)**:
  - PID `8315` ran with `PAPER_TX_FEE_SOL = 0.001` (code default) and `SCALPER_MIN_BUY_RATIO_5M = 0.58` (unused by `BREAKOUT_IGNITION`).
- **Unresolved Questions**:
  1. Were any process-level environment variables (outside `.env`) passed to PID `8315`'s `systemd` unit?
  2. Should `docs/SPEC.md:215` eventually be updated (in a future documentation directive, not now) to reflect that `62%` is a score-only criterion rather than a hard gate?

---

## 11. Files Inspected

- `config.py`
- `.env.example`
- `.env` (key-existence check only via `grep -nE "^(PAPER_TX_FEE_SOL|SCALPER_MIN_BUY_RATIO_5M)="`; no secrets read or printed)
- `executor_node.py`
- `state_ledger.py`
- `db.py`
- `scalper_entry_engine.py`
- `breakout_ignition.py`
- `alpha_node.py`
- `alpha_helpers.py`
- `whale_lane.py`
- `outcome_tracker.py`
- `docs/SPEC.md`
- `tests/test_outcome_tracker.py`
- `tests/test_sprint2a_accounting.py`
- `tests/test_scalper_entry_engine.py`
- `tests/test_breakout_ignition.py`
- `reports/phase1/solana_meme_agent.duckdb` + `.wal` (read-only `/tmp` snapshot)

---

## 12. Git Commit and Working-Tree Status

- **Commit Inspected**: `eb776074ad498185e31b23574fc983e5edd24f0d` (`dev_1`, `Directive 4`, `2026-11-25 20:53:10 -0600`)
- **Working-Tree Status**: Clean for all tracked files (`## dev_1...origin/dev_1`); only untracked `reports/` artifacts (`reports/phase1/directive_07_fee_and_buy_ratio_audit.md` added by this directive). Zero source, config, test, or database files modified.
