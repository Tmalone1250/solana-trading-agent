# `BREAKOUT_IGNITION` Threshold Tuning & `IGNITION` State Badge Hardening Plan

## 1. Configurable `BREAKOUT_IGNITION` Thresholds (`config.py`, `.env.example`)
- Add `.env`-configurable thresholds:
  - `BREAKOUT_MIN_VOL_RATIO=1.5` (lowered from `2.0x` to `1.5x` rolling 5-bar average volume).
  - `BREAKOUT_MAX_COIL_RANGE_PCT=10.0` (raised from `8.0%` to `10.0%` max base range).
  - `BREAKOUT_MIN_SCORE=75` (`0–100` minimum breakout score).

## 2. Weighted-Score Entry Trigger & Non-Veto `BUY_ACCEL` (`breakout_ignition.py`, `scalper_entry_engine.py`)
- Trigger `BREAKOUT_IGNITION` entry (`detected = True`) when all 5 core conditions hold:
  1. `breakout_score >= BREAKOUT_MIN_SCORE` (`75`)
  2. `RES` passed (close above prior 15m–30m swing high in top 35% of candle range)
  3. `VOL_SPIKE` passed (`vol_ratio >= BREAKOUT_MIN_VOL_RATIO`, default `1.5x`)
  4. `breakout_state != "EXTENDED"` (price `<= 12%` above both EMA9 and VWAP)
  5. `RSI <= 78`
- Keep `BUY_ACCEL` (`m5_buys` acceleration) as a weighted score contributor (`+7` pts at `>= 1.5x`, `+3` pts at `>= 1.2x`) and telemetry diagnostic rather than a hard entry veto.

## 3. Tighten `IGNITION` State Badge Logic (`breakout_ignition.py`)
- Only assign `state = "IGNITION"` when `score >= 75` **AND** `RES` passed **AND** `VOL_SPIKE` passed (and not `EXTENDED`).
- If `score >= 75` but either `RES` or `VOL_SPIKE` failed (e.g. `0.12x` volume), cap `state` at `"COILING"`.

## 4. Verification (`tests/test_breakout_ignition.py`)
- Add/update unit tests covering:
  1. Valid `BREAKOUT_IGNITION` fill with `vol_ratio=1.6x`, `coil_range=9.5%`, `score >= 75`, `RES` pass, and `BUY_ACCEL=1.0x`.
  2. `IGNITION` state capped to `COILING` when `score >= 75` but `VOL_SPIKE` or `RES` fails.
  3. `EXTENDED` (`> 12%` above EMA9/VWAP) still hard-blocking entry even when `score >= 75`.
- Run `./venv/bin/pytest -v`.
