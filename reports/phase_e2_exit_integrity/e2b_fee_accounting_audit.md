# Directive 23 — E2B Fee Accounting & Net Breakeven Audit (Read-Only)

**Repository:** `Tmalone1250/solana-trading-agent`  
**Branch:** `dev_1`  
**Inspected Commit SHA:** `94a0c3e8835d19fc3d93f9db1d1a71ebcafd622b`  
**Mode:** Strictly Read-Only Forensic Audit  
**Deliverable:** `reports/phase_e2_exit_integrity/e2b_fee_accounting_audit.md`

---

## 1. Files & Functions Inspected

- `config.py`: `AgentConfig` (`PAPER_TX_FEE_SOL`, `BREAKEVEN_ARM_PCT`, `BREAKEVEN_FLOOR_PCT`, `TRADE_SIZE_SOL`, `PARTIAL_TP_*`)
- `executor_node.py`: `executor_node`, `fetch_jupiter_order`, `execute_jupiter_signed_order`, `extract_raw_price_impact`
- `state.py`: `ActivePosition` (`entry_sol`, `initial_entry_sol`, `remaining_cost_basis_sol`, `realized_partial_pnl_sol`)
- `state_ledger.py`: `reserve_buy_slot`, `apply_trade_settlement`
- `db.py`: `trades` schema, `log_trade`, `log_action`
- `chart_exit_engine.py`: `evaluate_chart_position_exit`, `_mk_order`
- `batched_price_monitor.py`: `active_cost_basis_sol`, `needs_exact_jupiter_quote`, `resolve_position_sol_value`
- `whale_discovery_scoring.py`: confirmed only `getBalance` RPC call exists (`https://mainnet.helius-rpc.com`); no `getTransaction` fee-lookup exists anywhere in the repository.

---

## 2. Current Fee-Source Inventory

| Cost / Fee Component | Paper Mode (`EXECUTION_MODE="paper"`) | Live Mode (`EXECUTION_MODE="live"`) | Evidence & Classification |
|---|---|---|---|
| **Transaction / Priority Fee (`tx_fee_sol`)** | Fixed `PAPER_TX_FEE_SOL` (`0.001 SOL` default in `config.py:50`) applied per `BUY`, `PARTIAL_SELL`, and `SELL`. | Hardcoded `0.0` (`executor_node.py:126`: `float(config.PAPER_TX_FEE_SOL) if config.EXECUTION_MODE == "paper" else 0.0`). | **Paper:** Assumed constant (`0.001`). **Live:** **UNKNOWN** silently represented as `0.0`. |
| **AMM Pool Fee & Jupiter Platform Fee** | Baked into Jupiter `/swap/v2/order` `outAmount` quote. | Baked into Jupiter `/swap/v2/execute` `outputAmountResult` / `outAmount`. | **Known** (implicitly reflected in output amounts; not separately itemized). |
| **Quote Price Impact (`priceImpactPct`)** | Logged raw to `trades.jupiter_price_impact_raw` and `action_log`. | Logged raw to `trades.jupiter_price_impact_raw` and `action_log`. | **Execution slippage/impact**, NOT a transaction fee. |
| **Solana RPC `getTransaction` Fee (`meta.fee`)** | Not applicable. | **Not implemented.** `execute_jupiter_signed_order` reads only `signature`/`txid` and `outputAmountResult` (`executor_node.py:251-253`). | **Unavailable** in current execution path (Jupiter `/swap/v2/execute` does not reliably return `meta.fee`; Helius RPC `getTransaction` is not wired). |

---

## 3. `BUY` / `PARTIAL_SELL` / `SELL` Accounting Flow

In `executor_node.py`:
1. **`BUY` (L273–334):**
   - `spent_sol = order.amount_atomic / LAMPORTS_PER_SOL`
   - `sol_delta = -(spent_sol + tx_fee_sol)`
   - `pnl_delta = -tx_fee_sol` (applied to `daily_pnl_sol` via `db.apply_trade_settlement`)
   - `ActivePosition` sets `entry_sol = spent_sol`, `initial_entry_sol = spent_sol`, `remaining_cost_basis_sol = spent_sol`, `realized_partial_pnl_sol = 0.0`.
   - **Gap:** Entry `tx_fee_sol` is deducted from `sol_balance` and `daily_pnl_sol`, **but is NOT stored on `ActivePosition` or in `trades`**.
2. **`PARTIAL_SELL` (L335–402):**
   - `received_sol = out_amount / LAMPORTS_PER_SOL`
   - `cost_of_sold = round(pos_obj.initial_entry_sol * frac, 6)`
   - `partial_pnl = round(received_sol - cost_of_sold - tx_fee_sol, 6)`
   - `sol_delta = received_sol - tx_fee_sol`
   - `rem_basis = round(max(0.0, pos_obj.remaining_cost_basis_sol - cost_of_sold), 6)`
   - `pos_obj.realized_partial_pnl_sol = round(pos_obj.realized_partial_pnl_sol + partial_pnl, 6)`
3. **`SELL` (L403–496):**
   - `received_sol = out_amount / LAMPORTS_PER_SOL`
   - `sol_delta = received_sol - tx_fee_sol`
   - `cost_basis = rem_basis_val if (partial_taken and rem_basis_val > 0) else entry_sol`
   - `pnl_sol = round((received_sol - cost_basis - tx_fee_sol) if cost_basis > 0 else -tx_fee_sol, 6)`

### Lifecycle Trace Summary & Account vs. Position Mismatch

| Lifecycle | Account `daily_pnl_sol` Delta | Position `trades.pnl_sol` Sum | Mismatch |
|---|---|---|---|
| `BUY -> SELL` | `-fee_buy + (recv_sell - entry_sol - fee_sell)` | `recv_sell - entry_sol - fee_sell` | Position PnL omits `fee_buy` (`0.001 SOL` in paper). |
| `BUY -> PARTIAL_SELL -> SELL` | `-fee_buy + partial_pnl + final_pnl` | `partial_pnl + final_pnl` | Position PnL omits `fee_buy`. |
| `BUY -> PARTIAL_SELL -> BREAKEVEN/TRAILING` | Same as above | Same as above | Same (`fee_buy` omitted from position-level PnL). |
| `BUY -> SELL -> WATCHING_REENTRY -> Re-entry BUY -> SELL` | Sums all 4 fees + both legs' gross PnL | Two separate lifecycles, each omitting its `BUY` fee | Each leg's `trades` PnL omits its entry fee. |

---

## 4. Current Breakeven Formula & Concrete Numeric Examples

In `chart_exit_engine.py`:
- `cost_basis = active_cost_basis_sol(pos)` (`remaining_cost_basis_sol` if `> 0`, else `entry_sol`).
- `pnl_pct = (current_sol_value - cost_basis) / cost_basis` (**gross** of all transaction fees).
- Arms when `peak_pnl_pct >= BREAKEVEN_ARM_PCT` (`0.08` = `+8%` gross).
- Triggers `BREAKEVEN_SHIELD` when `pnl_pct <= BREAKEVEN_FLOOR_PCT` (`0.02` = `+2%` gross).
- `needs_exact_jupiter_quote` (`batched_price_monitor.py:582-585`) requests an exact Jupiter `/swap/v2/order` quote once within `2%` of the floor, so executable exit proceeds *are* quoted near the floor, **but tx fees are excluded**.

### Concrete Examples (Using Actual Code Formulas)

1. **Full Position, Paper Mode (`entry_sol = 0.50`, `PAPER_TX_FEE_SOL = 0.001`):**
   - Gross `+2%` floor triggers at `current_sol_value = 0.5100 SOL`.
   - `SELL` `trades.pnl_sol` = `0.5100 - 0.5000 - 0.0010 = +0.0090 SOL`.
   - True net lifecycle PnL (including `BUY` fee) = `+0.0080 SOL` (`+1.60%`).
2. **Full Position, Hypothetical Live Priority + Base Fee (`0.006 SOL` per tx = `0.012 SOL` round-trip, `2.4%` on `0.50 SOL`):**
   - *Note: `0.006 SOL/tx` is a hypothetical illustration, not a measured default.*
   - Gross `+2%` floor triggers at `0.5100 SOL` (`+0.0100 SOL` gross).
   - True net lifecycle PnL = `+0.0100 - 0.0120 = -0.0020 SOL` (**closed at nominal "breakeven" while finishing net-negative**).
3. **After 50% Partial Sale at `+15%` (`entry_sol = 0.50`, `PAPER_TX_FEE_SOL = 0.001`):**
   - `PARTIAL_SELL` receives `0.2875 SOL` (`cost_of_sold = 0.25`, `partial_pnl = +0.0365 SOL`). `remaining_cost_basis_sol = 0.25 SOL`.
   - Runner `BREAKEVEN_SHIELD` triggers at `0.25 * 1.02 = 0.2550 SOL` (ignoring `realized_partial_pnl_sol`, `entry_fee_sol`, and closing `tx_fee_sol`).
   - True net lifecycle PnL = `0.2875 + 0.2550 - 0.5000 - 0.0030 = +0.0395 SOL`.

---

## 5. Proposed Minimal E2B Implementation Scope

1. **State & Fee-Truthfulness (`state.py`, `executor_node.py`):**
   - Add to `ActivePosition`:
     - `entry_fee_sol: Optional[float] = None`
     - `accumulated_fees_sol: float = 0.0`
     - `fee_accounting_status: str = "UNKNOWN"` (`PAPER_ASSUMED` | `OBSERVED` | `UNKNOWN`)
   - In `executor_node.py`:
     - In `paper` mode: record `tx_fee_sol = float(config.PAPER_TX_FEE_SOL)` with `fee_accounting_status = "PAPER_ASSUMED"`; set `entry_fee_sol = tx_fee_sol` and `accumulated_fees_sol = tx_fee_sol` on `BUY`, and increment `accumulated_fees_sol` on `PARTIAL_SELL`.
     - In `live` mode: **do not** silently hardcode `tx_fee_sol = 0.0`. If an observed fee is not returned by `execute_jupiter_signed_order` (or confirmed RPC lookup), mark `fee_accounting_status = "UNKNOWN"` (keeping `sol_delta` free of fabricated fees while explicitly flagging unknown fee status in `action_log`/`ActivePosition`).
2. **Net-Aware Breakeven (`chart_exit_engine.py`):**
   - Keep `BREAKEVEN_ARM_PCT` and `BREAKEVEN_FLOOR_PCT` thresholds unchanged.
   - Compute `net_current_sol_value = current_sol_value - est_closing_fee_sol - entry_fee_for_basis` (where `est_closing_fee_sol = config.PAPER_TX_FEE_SOL` in paper mode, or `0.0` with `UNKNOWN` tag if live fee is unknown, never fabricating a constant).
   - Compare `net_pnl_pct = (net_current_sol_value - cost_basis) / cost_basis` against `BREAKEVEN_FLOOR_PCT` so paper positions cannot close at a nominal `+2%` floor while losing net SOL after known round-trip fees.
3. **Database Schema:**
   - Optional/additive `tx_fee_sol DOUBLE` and `fee_status VARCHAR` on `trades` (nullable, preserving historical rows as `NULL` / `UNKNOWN`). No changes to existing `BREAKEVEN_*` thresholds or `action_log` column positions unless explicitly authorized.
4. **Focused Regression Tests Needed:**
   - Paper `BUY -> BREAKEVEN_SHIELD` verifies net SOL after entry + exit fees meets floor.
   - Paper `BUY -> PARTIAL_SELL -> BREAKEVEN_SHIELD` verifies `accumulated_fees_sol`, `remaining_cost_basis_sol`, and net lifecycle PnL.
   - Live mode without observed fee marks `fee_accounting_status = "UNKNOWN"` instead of falsely claiming `0.0` verified fee.
   - `WATCHING_REENTRY` + `LEG_2_REENTRY` keeps per-leg `entry_fee_sol` and `accumulated_fees_sol` isolated.

---

## 6. Unresolved Questions & Limitations

1. **Live RPC `getTransaction` Indexing Latency:** Even if Helius `getTransaction` is added later, newly confirmed Solana signatures may take 1–3 seconds to index after `/swap/v2/execute`, so live fee retrieval must tolerate async unavailability (`UNKNOWN`) without blocking exit execution.
2. **Account-Level vs. Position-Level Fee Attribution:** Until `entry_fee_sol` is persisted on `ActivePosition` and `trades`, historical Phase 1 `trades.pnl_sol` sums continue to differ from `daily_pnl_sol` by `PAPER_TX_FEE_SOL` per `BUY`.

---

## 7. Read-Only & Safety Confirmation

Strictly read-only: no production code, tests, `.env`, configuration, DuckDB database, WAL, Raspberry Pi, or systemd service was modified, and no git commit or push was executed.
