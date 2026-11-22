# Shadow Outcome Tracker & Realistic Paper Trading Fees Plan

## 1. DuckDB Table & CSV Export (`outcome_tracker.py`, `db.py`, `telemetry_api.py`)
- Create `candidate_outcomes` table in DuckDB via `ensure_outcomes_table` called from `AgentDatabase._init_schema()`:
  - Columns: `id`, `mint`, `symbol`, `source`, `discovered_at`, `decision`, `decision_reason`, `strategy`, `entry_price_usd`, `entry_mc_usd`, `entry_liq_usd`, `breakout_score`, `breakout_state`, `leg2_score`, `vamp_score`, `rsi`, `whale_sol_flow`, `triggering_wallet`, `price_1m_pct`, `price_5m_pct`, `price_15m_pct`, `price_30m_pct`, `mfe_pct`, `mae_pct`, `finalized`.
- Expose `GET /api/export/outcomes-csv` in `telemetry_api.py` exporting the latest 2,000 rows as `candidate_outcomes.csv`.

## 2. Lightweight Batched Tracker (`outcome_tracker.py`, `alpha_node.py`, `helius_webhook.py`, `main.py`)
- Bounded in-memory tracking map capped at `90` active candidates with 15-minute per-mint deduplication.
- Hooks:
  1. `alpha_node.py`: record a snapshot on every evaluated watchlist token (`ENTERED`, `BLOCKED`, `WATCH`, `REJECTED`) with exact decision-time features.
  2. `helius_webhook.py`: record a snapshot on every valid roster `WHALE_BUY` event with `triggering_wallet` and `whale_sol_flow`.
- Background 60s poll (`run_outcome_tracker_loop` in `main.py`) batching active mints in chunks of `30` via DexScreener (`GET /tokens/v1/solana/{mint1,...}`):
  - Updates running `mfe_pct` and `mae_pct`, stamps `price_1m_pct`, `price_5m_pct`, `price_15m_pct`, and `price_30m_pct`, flushes updates to DuckDB, and marks `finalized = True` + evicts from memory once elapsed time `>= 30m`.

## 3. Realistic Paper Trading Fees (`config.py`, `.env.example`, `executor_node.py`)
- Add `PAPER_TX_FEE_SOL=0.001` (`.env` configurable, default `0.001 SOL` per swap side).
- In `EXECUTION_MODE=paper`, deduct `PAPER_TX_FEE_SOL` from `sol_balance` (`sol_equity`) and `daily_pnl_sol` (`daily_realized_sol`) on `BUY`, `PARTIAL_SELL`, and `SELL`.

## 4. Verification
- Add unit tests in `tests/test_outcome_tracker.py` covering:
  1. 15m per-mint deduplication and 90-candidate cap.
  2. Batched DexScreener updates computing `mfe_pct`, `mae_pct`, `price_5m_pct`, `price_30m_pct`, and finalizing/evicting at 30m.
  3. `PAPER_TX_FEE_SOL` deduction on paper `BUY`, `PARTIAL_SELL`, and `SELL`.
  4. `GET /api/export/outcomes-csv` returning valid CSV headers and rows.
- Run `./venv/bin/pytest -v`.
