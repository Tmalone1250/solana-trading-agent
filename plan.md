# Breakout Telemetry Logging & 5-Slot Watchlist Breakout Meter Plan

## 1. Backend: Breakout Scoring & Telemetry (`breakout_ignition.py`, `scalper_entry_engine.py`)
Return a structured `breakout_telemetry` object for every watchlist mint on every tick:
- **`score` (0–100 integer)** from 5 weighted pillars:
  1. Coil / Base (20 pts)
  2. Resistance Break & Body Conviction (25 pts)
  3. Volume & Flow Spike (25 pts)
  4. MA / VWAP / RSI Alignment (15 pts)
  5. HTF 15m Context & Anti-Chase Extension (15 pts)
- **`state`**: `EXTENDED` (`> 12%` above EMA9/VWAP), `IGNITION` (`>= 75`), `COILING` (`50–74`), `FORMING` (`< 50`).
- **`timeframe`**: `"5m"` or `"1m_fallback"`.
- **`vol_ratio`** (float), **`ema_ext_pct`** (float %), **`passed_gates`** (`list[str]`), **`failed_gates`** (`list[str]` with actual vs threshold, e.g., `VOL_SPIKE(1.7x<2.0x)`).

## 2. DuckDB, CSV Export & Telemetry API (`action_export.py`, `db.py`, `alpha_node.py`, `executor_node.py`, `telemetry_api.py`)
- Add 6 columns to `ACTION_LOG_COLUMNS` and automatic `ALTER TABLE` migrations:
  `breakout_score`, `breakout_state`, `breakout_timeframe`, `breakout_vol_ratio`, `breakout_ema_ext_pct`, `breakout_failed_gates`.
- Populate these 6 columns on `DECISION_1`, `BUY`, and `EVICT` rows in DuckDB and `/api/export/csv`.
- Include `breakout_telemetry` inside each mint object in `active_watchlist[]` returned by `GET /api/telemetry`.

## 3. Frontend: 5-Slot Watchlist Breakout Meter (`dashboard/`)
- Add `BreakoutTelemetry` interface to `dashboard/src/lib/types.ts`.
- Render a compact **Breakout Meter** in each row of `dashboard/src/components/WatchlistTable.tsx`:
  - Color-coded 0–100 progress bar (`FORMING` slate, `COILING` amber, `IGNITION` emerald, `EXTENDED` rose) with `score/100 • STATE • tf`.
  - Compact single-line pillar pill strip (`COIL`, `RES`, `VOL 2.3x`, `EMA`, `EXT +4.1%`) color-coded green (passed) vs rose/muted (failed).

## 4. Verification
- Add unit tests in `tests/test_breakout_ignition.py` covering `breakout_score` calculation, `failed_gates` formatting, DuckDB/CSV serialization of the 6 new columns, and `/api/telemetry` `active_watchlist[].breakout_telemetry`.
- Run `./venv/bin/pytest -v` and `cd dashboard && npm run build`.
