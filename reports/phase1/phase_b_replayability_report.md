# BREAKOUT_IGNITION Replayability & Historical Input Classification Report

## 1. Provenance Summary

- **Strategy Scope:** `BREAKOUT_IGNITION`
- **Runtime Code Provenance:** `HISTORICAL_CODE_VERSION_UNVERIFIED`
- **Configuration Profile:** `supplied_phase1_pi_snapshot` (`SUPPLIED_PI_ENV_SNAPSHOT_NOT_VERIFIED_AS_PID_8315_EFFECTIVE_RUNTIME`)
- **Snapshot Method:** `OFFLINE_LOCK_FREE_SHA256_VERIFIED_SNAPSHOT` (`db_sha256=fd5fe1ef0517eda7a23fe8d0240d8bbec058b5584f3ce4d4a505f1a3e8dc3abd`)
- **Active-Soak `DECISION_1` Rows Audited:** `18332` across `854` episodes (`101` unique mints)
- **Unpruned `decisions_log` Rows Remaining:** `1018` (1,000-row rolling cap)

## 2. Historical Input & Execution Evidence Classification Matrix

| Component / Field | Classification | Source Table / Artifact | Replayability Impact |
| :--- | :--- | :--- | :--- |
| Live-forming 5m & 1m OHLCV candles | `MISSING` | Not persisted in DuckDB | `detect_breakout_ignition()` candle-level detection is `NOT_REPLAYABLE`. |
| Raw `pair_data` (`txns.m5`, `txns.h1`, `volume`) | `MISSING` | Not persisted in DuckDB | `SCALPER_MIN_TXNS_5M`, `res_passed`, `BUY_RATIO`, and `BUY_ACCEL` are `NOT_REPLAYABLE`. |
| Recorded detector scalars (`breakout_score`, `state`, `timeframe`, `vol_ratio`, `ema_ext_pct`, `failed_gates`) | `EXACT_HISTORICAL` | `action_log` (`100%` of `18332` `DECISION_1` rows) | Supports `RECORDED_DECISION_AUDIT` and scalar threshold checks (`score`, `vol_ratio`, `ema_ext > 12%`). |
| VWAP extension (`ext_above_vwap_pct`) & `dist_from_1h_high_pct` | `PARTIALLY_AVAILABLE` | `decisions_log.scores_json` (`1018` unpruned rows only) | When `ema_ext <= 12%` and `decisions_log` is pruned, anti-chase pass is `NOT_REPLAYABLE`. |
| `mc_usd`, `liq_usd`, `soak_minutes`, `rsi`, `narrative_score`, `momentum_score`, `vamp_score`, `buy_confidence`, `leg2_score` | `EXACT_HISTORICAL` | `action_log` (`100%` of `18332` `DECISION_1` rows) | Full deterministic threshold replay of soak, RSI<=78, narrative, momentum, vamp, and low-score eviction. |
| Open Chart/Whale positions & slot occupancy at `T` | `RECONSTRUCTABLE` | `trades` (`timestamp <= T`) | Deterministic duplicate-mint and slot-cap verification. |
| Historical Pi configuration (`supplied_phase1_pi_snapshot`) | `PARTIALLY_AVAILABLE` / `INFERRED` | User-supplied Pi `.env` snapshot + `config.py` defaults for omitted keys | `50` supplied keys + omitted defaults (`PAPER_TX_FEE_SOL=0.001`); PID 8315 runtime remains `HISTORICAL_CODE_VERSION_UNVERIFIED`. |
| Executed trade entry & `jupiter_exact_quote` exit fills | `OBSERVED_QUOTE` | `trades` + `action_log` | Exact fee and slippage verification on executed trades. |
| `EXODUS_SELL_FLOW` trigger valuations (`jupiter_v3`) | `PRICE_ONLY` | `action_log.monitor_sol_value` | Captures trigger-to-fill slippage against Jupiter `/swap/v2/order` settlement (`OBSERVED_QUOTE`). |
| Untraded candidate executable quotes (e.g., PAIR) | `UNAVAILABLE` | Never requested | Untraded candidates have `UNAVAILABLE` executable quotes; never fabricated. |
| 5-second position monitor `HOLD` sell-ratio progression | `UNAVAILABLE` | Not persisted in DuckDB | Intermediate 5s sell-flow trajectory prior to Exodus exit is `UNAVAILABLE`. |
| `candidate_outcomes` forward horizons (`1m/5m/15m/30m`, `MFE/MAE`) | `PRICE_ONLY` | `candidate_outcomes` (60s DexScreener polling, 15m per-mint dedup) | Post-decision outcome analysis only (strictly blocked from `DecisionSnapshot`). |
| Post-decision trajectory prices derived from `mc_usd` | `INFERRED_PRICE_FROM_MARKET_CAP` | `action_log.mc_usd` anchored to `entry_price_usd` | Diagnostic trajectory only; never presented as exact or executable prices. |

## 3. Per-Gate Replayability & Status Distribution Across Active Soak

| Gate / Rule ID | `PASS` | `FAIL` | `EVICT_TRIGGERED` | `EVICT_NOT_TRIGGERED` | `NOT_REPLAYABLE` |
| :--- | ---: | ---: | ---: | ---: | ---: |
| `E01_VAMP_EVICTION_RULE` | 0 | 0 | 42 | 18290 | 0 |
| `E02_LOW_SCORE_EVICTION_RULE` | 0 | 0 | 283 | 17694 | 355 |
| `E03_STALE_WATCH_EVICTION_RULE` | 0 | 0 | 5 | 17533 | 794 |
| `G01_RAW_BREAKOUT_DETECTOR` | 0 | 0 | 0 | 0 | 18332 |
| `G02_BREAKOUT_RESISTANCE_AND_BODY` | 0 | 0 | 0 | 0 | 18332 |
| `G03_BREAKOUT_FLOW_AND_ACCEL_SCORE_PILLARS` | 0 | 0 | 0 | 0 | 18332 |
| `G04_BREAKOUT_VOL_RATIO_THRESHOLD` | 5183 | 13149 | 0 | 0 | 0 |
| `G05_BREAKOUT_MIN_SCORE_THRESHOLD` | 3579 | 14753 | 0 | 0 | 0 |
| `G06_BREAKOUT_ANTI_CHASE_EXTENSION` | 0 | 1627 | 0 | 0 | 16705 |
| `G07_BREAKOUT_RSI_CEILING` | 17597 | 735 | 0 | 0 | 0 |
| `G08_WATCHLIST_SOAK_GATE` | 15882 | 2450 | 0 | 0 | 0 |
| `G09_SCALPER_MIN_TXNS_5M` | 0 | 0 | 0 | 0 | 18332 |
| `G10_CHART_STRATEGY_ALLOWLIST` | 18332 | 0 | 0 | 0 | 0 |
| `G11_ALPHA_ACTION_CHOICE` | 17 | 18293 | 0 | 0 | 22 |
| `G12_ALPHA_MIN_BUY_CONFIDENCE` | 17 | 10 | 0 | 0 | 18305 |
| `G13_ALPHA_MAX_VAMP_ENTRY` | 14613 | 3719 | 0 | 0 | 0 |
| `G14_ALPHA_MIN_NARRATIVE_SCORE` | 14420 | 3912 | 0 | 0 | 0 |
| `G15_ALPHA_MIN_MOMENTUM_SCORE` | 10520 | 7812 | 0 | 0 | 0 |
| `G16_PORTFOLIO_SLOT_AND_DUPLICATE_MINT` | 18332 | 0 | 0 | 0 | 0 |
