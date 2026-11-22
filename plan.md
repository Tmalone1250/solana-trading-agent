# GeckoTerminal OHLCV Global Rate-Limit Discipline & Whale Radar Breakout Telemetry Plan

## 1. Fix `ohlcv_client.py` Rate-Limit Discipline (`ohlcv_client.py`, `config.py`, `.env.example`)
- **No `1m` Fallback on `429` or Network Error**:
  - Track `last_ok: bool` and `last_status: int` per pool in `_ohlcv_cache`.
  - Add `should_fetch_1m_fallback(pair_address, candles_5m, now_ts)` returning `True` **only** when not in global backoff, the `5m` request succeeded (`200 OK`), and `len(candles_5m) < 12`.
  - Enforce this guard in `fetch_ohlcv_1m_batch`, `alpha_node.py`, and `whale_lane.py` so a `429` or network error on `5m` never fires a `1m` request.
- **Global GeckoTerminal Pacing & Backoff**:
  - Default `OHLCV_PACING_DELAY_SECONDS=6.5` in `config.py` and `.env.example` (~9.2 req/min max, safely under GeckoTerminal's ~10 req/min keyless limit).
  - Add a shared async rate-limiter lock across all GeckoTerminal OHLCV calls enforcing minimum `6.5s` spacing globally.
  - Replace per-pool-only `429` backoff with a **global** `429` cooldown (`30s`, doubling up to `120s`) so a `429` on Pool A immediately pauses requests for Pool B and all other pools.
- **Stale-While-Revalidate Cache**:
  - When global backoff is active, serve the last cached candles for each pool without making network calls.
  - Add bounded shared `breakout_telemetry` cache (`cache_breakout_telemetry` / `get_cached_breakout_telemetry`) keyed by `mint`.

## 2. Ensure Whale Radar Populates `breakout_telemetry` (`whale_lane.py`, `alpha_node.py`, `telemetry_api.py`)
- Share `_ohlcv_cache` and the bounded `breakout_telemetry` cache between watchlist evaluation (`alpha_node.py`) and Whale Radar (`whale_lane.py`).
- Preserve/attach cached `breakout_telemetry` on Whale Radar confluence rows even when `COOLDOWN`, `STALE > 60s`, or `PENDING` in `whale_lane.py` and `telemetry_api.py`.

## 3. Verification
- Add unit tests in `tests/test_scalper_entry_engine.py` verifying:
  1. A `5m` `429` does NOT trigger a `1m` fallback call (while a `200 OK` with `< 12` candles does).
  2. A `429` on Pool A activates global backoff for Pool B and serves stale cached candles while in backoff.
  3. Whale Radar confluence rows include `breakout_telemetry` from the shared cache even when `COOLDOWN` or `STALE > 60s`.
- Run `./venv/bin/pytest -v`.
