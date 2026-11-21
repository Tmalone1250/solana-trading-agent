# Live Batched Position Monitor Fix Plan (Jupiter Price V3 + DexScreener `priceNative` Fallback)

## 1. Fix Jupiter `/price/v3` Flat Schema Parsing & SOL Conversion (`batched_price_monitor.py`)
- Always include `SOL_MINT` (`So11111111111111111111111111111111111111112`) in `GET https://api.jup.ag/price/v3?ids=...`.
- Parse the flat V3 response schema (`resp[mint]["usdPrice"]` and `resp[mint]["decimals"]`) and compute:
  `current_sol_value = (token_amount / (10 ** decimals)) * (token_usd / sol_usd)`.

## 2. Mandatory DexScreener `priceNative` Fallback & Live Flow Enrichment (`batched_price_monitor.py`, `state.py`)
- Whenever a mint is omitted from Jupiter `/price/v3` (or `/price/v3` fails), immediately compute `current_sol_value` from the batched DexScreener response using `priceNative` (in SOL) and `token_amount / (10 ** decimals)`.
- Add `decimals`, `pnl_sol`, and `pnl_pct` to `ActivePosition` in `state.py`.
- Always write `last_liquidity_usd`, `last_5m_buy_ratio`, `last_5m_sell_ratio`, `current_sol_value`, `peak_sol_value`, `pnl_sol`, and `pnl_pct` directly onto each position object in `chart_positions` and `whale_positions`.

## 3. In-Place Persistence & 1-Line `[Monitor]` Logging (`position_monitor_node.py`, `whale_lane.py`, `db.py`)
- Upgrade `position_monitor_node.py` (keeping it under 500 lines) and `whale_lane.py` to use `fetch_batched_sol_prices()` + `update_position_from_batch()`, persisting updated `chart_positions` and `whale_positions` in-place to DuckDB (`save_lane_positions` and synced `save_state`) so `/api/telemetry` reflects live PnL and order flow on every tick.
- Log a compact 1-line `[Monitor]` status per position on every monitor tick for `journalctl` visibility.

## 4. Verification (`tests/test_dual_lane_3x3_and_whale_breakout.py`)
- Add unit tests covering:
  1. Flat Jupiter `/price/v3` (`usdPrice` + `decimals`) parsing and SOL value calculation.
  2. DexScreener `priceNative` fallback when Jupiter `/price/v3` omits a meme coin.
  3. `/api/telemetry` returning updated `current_sol_value`, `pnl_sol`, `pnl_pct`, `last_liquidity_usd`, and `last_5m_buy_ratio` inside `chart_positions`.
- Run `./venv/bin/pytest -v`.
