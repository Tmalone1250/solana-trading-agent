# Dual-Lane 3+3 Concurrency, Batched Price Polling & Whale Breakout Integration Plan

## 1. Dual-Lane 3+3 Concurrency (`config.py`, `db.py`, `state.py`, `executor_node.py`, `main.py`)
- **Configurable Lane Caps (`.env`):** `MAX_CHART_POSITIONS=3`, `MAX_WHALE_POSITIONS=3`, `MAX_OPEN_POSITIONS=6`.
- **Multi-Position Persistence (`db.py`):** Add `chart_positions_json` and `whale_positions_json` on `agent_state` (with automatic `ALTER TABLE` migration) plus `breakout_telemetry_json` on `whale_decisions`, keeping legacy `active_position` and `whale_position` fields synchronized for backward compatibility.
- **Continuous Watchlist Scanning:** Allow the 5-Slot Rotating Watchlist to keep scanning whenever `len(chart_positions) < MAX_CHART_POSITIONS` and total open `< MAX_OPEN_POSITIONS`.
- **Shared Safety Guards:**
  - **No Duplicate Mint** across all 6 open positions and active `WATCHING_REENTRY` trackers.
  - **Global Circuit Breaker & SOL Equity:** All 6 positions share `sol_balance`, `daily_pnl_sol`, and `DAILY_MAX_LOSS_PCT`; block entries when free SOL is insufficient.

## 2. Batched Price Polling (`batched_price_monitor.py`)
- Replace per-position 5s Jupiter swap quotes with a single batched `GET /price/v3?ids=...` call (with batched DexScreener fallback) every 5s across all open mints (`12 req/min` total) with exponential backoff on HTTP `429`.
- Fire an individual Jupiter Swap Quote only on entry, sell execution, or when batched unrealized PnL is within `2%` of Hard Stop, Breakeven Shield, Trailing TP, or Whale TP / Shadow Exit / Time Stop.

## 3. Whale Breakout Gate & Telemetry (`whale_lane.py`, `telemetry_api.py`, `executor_node.py`)
- Add `WHALE_MIN_BREAKOUT_SCORE=50` and `WHALE_BLOCK_EXTENDED=true`.
- Evaluate active Whale Radar mints and `WHALE_SCALP` triggers through `breakout_telemetry` (with `1m_fallback` when `< 12` 5m candles exist).
- Reject `WHALE_SCALP` entries when `breakout_state == "EXTENDED"` (if `WHALE_BLOCK_EXTENDED`) or `breakout_score < WHALE_MIN_BREAKOUT_SCORE`, logging exact `breakout_failed_gates` to DuckDB and CSV.
- Tag `IGNITION` (`score >= 75`) whale entries with reason `WHALE_BREAKOUT_CONFLUENCE`.
- Populate `breakout_score`, `breakout_state`, `breakout_timeframe`, `breakout_vol_ratio`, `breakout_ema_ext_pct`, and `breakout_failed_gates` on `WHALE_CONFLUENCE`, `WHALE_SCALP` `BUY`, and `WHALE_SCALP` `SELL` CSV rows.

## 4. Frontend Updates (`dashboard/`)
- Extract shared `BreakoutMeter.tsx` and render it in both `WatchlistTable.tsx` and `WhaleListCard.tsx` (Confluence tab).
- Upgrade `ActivePositionHero.tsx` to render grouped **Chart Positions (`X / 3`)** and **Whale Positions (`Y / 3`)** cards with strategy badges, entry/exit SOL, unrealized PnL, and Risk Gauges while remaining backward-compatible.

## 5. Verification
- Add `tests/test_dual_lane_3x3_and_whale_breakout.py` covering 3+3 lane caps, duplicate-mint rejection across lanes, batched price polling + on-demand quote trigger near stop/TP, and `WHALE_SCALP` `EXTENDED` / low-score rejection + CSV columns.
- Run `./venv/bin/pytest -v` and `cd dashboard && npm run build`.
