# BREAKOUT_IGNITION Setup & Coiling Base Retention Plan

## Objective
Add a third chart setup—`BREAKOUT_IGNITION`—to the Scalper Expert Entry Engine without modifying `SWEEP_RECLAIM` or `DISPLACEMENT_RETEST`, so tokens breaking out of tight coiling bases aren't rejected by `near 1h high (< 5%)`, `RSI > 70`, or `< 12 x 5m candles`.

## 1. `BREAKOUT_IGNITION` Detector (`breakout_ignition.py`)
Trigger when all 5 conditions pass (on 5m candles, or 1m fallback when `< 12` 5m bars exist and `>= 15` 1m bars are available):
1. **Coil / Base**: `>= 3` prior candles forming higher lows or a tight range (`range_pct <= 8%`) under local resistance.
2. **Resistance Breakout + Body Conviction**: Latest candle closes strictly above the prior 15m–30m swing high with close in the top 35% of the candle range (`(close - low) / (high - low) >= 0.65`), blocking upper-wick fakeouts.
3. **Volume & Flow Spike**: Latest candle volume `>= 2.0x` rolling 5-bar average, 5m `buy_ratio >= 62%`, and `m5_buys >= 1.5x` the 1h 5-minute average (`buys_1h / 12`).
4. **MA / VWAP Alignment**: `EMA9 > EMA21` (sloping up) and `price > VWAP`.
5. **HTF Context**: 15m trend is bullish (`price > 15m EMA21` or higher-low structure on 15m aggregated bars).

## 2. Breakout-Aware Entry Gates (`scalper_entry_engine.py` & `ohlcv_client.py`)
Only when `BREAKOUT_IGNITION` is evaluated:
- **Bypass** `near 1h high (< 5%)` block; enforce anti-chase extension cap: reject if price is `> 12%` above `EMA9` or `VWAP`.
- **RSI ceiling**: Allow RSI up to `78.0` (instead of `70.0`) when `BREAKOUT_IGNITION` + volume spike are confirmed.
- **1m OHLCV Fallback**: Add `fetch_ohlcv_1m` / `fetch_ohlcv_1m_batch` to `ohlcv_client.py` (90s cache + 429 backoff) and invoke it in `alpha_node.py` when `< 12` 5m candles exist.

## 3. Coiling Base Retention (`alpha_node.py` & `scanner_node.py`)
For watchlist tokens with `liquidity_usd >= $40,000`, `vamp_score < 0.65`, and `dist_from_1h_high_pct <= 5%`:
- Require **2 consecutive** `LOW_SCORE` ticks before evicting (tracked via `low_score_strikes` in `latest_scores`).
- Extend `STALE_WATCH` timeout from `15m` to `25m`.

## 4. Telemetry & Unit Tests
- Log `strategy=BREAKOUT_IGNITION` and breakout/extension metrics (`vol_x`, `ext_ema9`, `ext_vwap`, `tf`) in DuckDB `action_log` and `solana_actions.csv`.
- Add `tests/test_breakout_ignition.py` covering valid breakout ignition, over-extended chase rejection (`> 12%` above EMA9/VWAP), upper-wick fakeout rejection, 1m candle fallback, coiling base retention, and CSV logging.
- Run `./venv/bin/pytest -v`.
