# BREAKOUT_IGNITION Historical Replay Report — PAIR (`32rBxcFjCFnyqZ41CEPM5AL67X1dcw8w83vZt6Kxpump`)

## 1. Mandatory Provenance & Isolation Envelope

| Field | Value |
| :--- | :--- |
| **Strategy Scope** | `BREAKOUT_IGNITION` |
| **Replay Modes Executed** | `RECORDED_DECISION_AUDIT`, `DETERMINISTIC_GATE_REPLAY` |
| **Runtime Code Provenance** | `HISTORICAL_CODE_VERSION_UNVERIFIED` |
| **Configuration Profile** | `supplied_phase1_pi_snapshot` (`SUPPLIED_PI_ENV_SNAPSHOT_NOT_VERIFIED_AS_PID_8315_EFFECTIVE_RUNTIME`) |
| **Snapshot Acquisition** | `OFFLINE_LOCK_FREE_SHA256_VERIFIED_SNAPSHOT` (`wal_included=True`) |
| **Source DB SHA-256** | `fd5fe1ef0517eda7a23fe8d0240d8bbec058b5584f3ce4d4a505f1a3e8dc3abd` |
| **Source WAL SHA-256** | `771c2ce29a60edc121b42427317aeab48b665cedcab99ad98fd108eaf7e5252b` |
| **Source Record IDs** | `action_log.id=104876`, `terminal_action_log.id=104877`, `decisions_log.id=PRUNED` |
| **Target Decision Timestamp** | `2026-11-26T15:05:03.504192+00:00` |
| **Episode Context** | `32rBxcFj-ep005` (observation `#1`; mint has `20` episodes / `362` `DECISION_1` rows in window) |

---

## 2. Mode A — `RECORDED_DECISION_AUDIT`

*Reconstructs what the original telemetry records state occurred at the decision timestamp (not an independent recreation of `detect_breakout_ignition()`).*

- **Recorded Decision Class:** `REJECT`
- **Recorded Alpha Action:** `REJECT` (`EXACT_HISTORICAL`)
- **Recorded Strategy Label:** `NONE`
- **Recorded Reason:** `REJECT -> REJECT`
- **Same-Tick Watchlist Eviction:** `True` — `LOW_SCORE (narr=1.95, mom=1.26 < 2.0)`

### Recorded Input & Detector Telemetry (`action_log`)

| Metric | Recorded Value | Provenance Classification |
| :--- | :--- | :--- |
| Market Cap (USD) | \$148391.00 | `EXACT_HISTORICAL` |
| Liquidity (USD) | \$28543.96 | `EXACT_HISTORICAL` |
| Watchlist Soak (min) | `0.05` | `EXACT_HISTORICAL` |
| RSI | `57.95` | `EXACT_HISTORICAL` |
| Breakout Score | `18` | `EXACT_HISTORICAL` |
| Breakout State | `FORMING` | `EXACT_HISTORICAL` |
| Breakout Timeframe | `5m` | `EXACT_HISTORICAL` |
| Breakout Vol Ratio | `0.47x` | `EXACT_HISTORICAL` |
| Breakout EMA9 Extension | `-17.01%` | `EXACT_HISTORICAL` |
| Narrative Score | `1.95` | `EXACT_HISTORICAL` |
| Momentum Score | `1.26` | `EXACT_HISTORICAL` |
| Copycat / Vamp Score | `0.501` | `EXACT_HISTORICAL` |
| Action Confidence (`buy_confidence` col) | `0.800` | `EXACT_HISTORICAL` |
| Leg2 Predictor Score | `39.10` | `EXACT_HISTORICAL` |

### Recorded Breakout Diagnostics Separation (`D1_failed_gates`)

- **Recorded Hard-Veto Diagnostics (2):** `RES(no_break)`, `VOL_SPIKE(0.5x<0.7x)`
- **Recorded Score-Deduction Diagnostics (5):** `COIL(91.6%>7.0%)`, `BUY_RATIO(53%<62%)`, `BUY_ACCEL(0.6x<1.5x)`, `EMA(not_aligned)`, `HTF_15M(bearish)`

---

## 3. Mode B — `DETERMINISTIC_GATE_REPLAY`

*Independently reevaluates only gates whose required historical inputs exist at `<= decision_timestamp`. Missing inputs produce `NOT_REPLAYABLE` (never an assumed pass).*

- **Overall Entry Verdict:** `DETERMINISTICALLY_REJECTED_OR_BLOCKED`
- **Eviction Replay Verdict:** `DETERMINISTIC_EVICT (E02_LOW_SCORE_EVICTION_RULE)`
- **Consistent With Recorded Outcome:** `True` — CONSISTENT: Recorded REJECT aligns with 6 failed hard gate(s) (G04_BREAKOUT_VOL_RATIO_THRESHOLD, G05_BREAKOUT_MIN_SCORE_THRESHOLD, G08_WATCHLIST_SOAK_GATE, G11_ALPHA_ACTION_CHOICE, G14_ALPHA_MIN_NARRATIVE_SCORE, G15_ALPHA_MIN_MOMENTUM_SCORE) under 'supplied_phase1_pi_snapshot'.
- **Hard Gate Replayability:** `10` replayable (`4` PASS, `6` FAIL) / `5` `NOT_REPLAYABLE`

### Gate-by-Gate Entry Trace

| Gate ID | Hard Gate? | Status | Input Availability | Observed | Profile Threshold | Explanation |
| :--- | :---: | :--- | :--- | :--- | :--- | :--- |
| `G01_RAW_BREAKOUT_DETECTOR` | `YES` | **`NOT_REPLAYABLE`** | `MISSING` | `None` | `>=12 5m or >=15 1m live candles + raw pair_data` | Raw live-forming 5m/1m OHLCV candles and raw DexScreener pair_data were not persisted in DuckDB; independent detector recreation is NOT_REPLAYABLE. |
| `G02_BREAKOUT_RESISTANCE_AND_BODY` | `YES` | **`NOT_REPLAYABLE`** | `MISSING` | `None` | `close > swing_high AND body_pos_pct >= 0.65` | Prior candle highs and breakout candle open/high/low/close are MISSING; cannot independently recompute res_passed without trusting recorded diagnostic strings. |
| `G03_BREAKOUT_FLOW_AND_ACCEL_SCORE_PILLARS` | `NO` | **`NOT_REPLAYABLE`** | `MISSING` | `None` | `buy_ratio_5m >= 0.62; buys_accel >= 1.5x (score deductions only)` | Raw 5m/1h buy and sell transaction counts are MISSING. Absence of BUY_RATIO or BUY_ACCEL in breakout_failed_gates is never treated as proof of passing. |
| `G04_BREAKOUT_VOL_RATIO_THRESHOLD` | `YES` | **`FAIL`** | `EXACT_HISTORICAL` | `0.47` | `0.7` | Recorded breakout_vol_ratio=0.47x < 0.70x (note: uses recorded detector scalar; raw candle volumes MISSING). |
| `G05_BREAKOUT_MIN_SCORE_THRESHOLD` | `YES` | **`FAIL`** | `EXACT_HISTORICAL` | `18` | `55` | Recorded breakout_score=18 < 55 (uses recorded detector score; raw candles MISSING). |
| `G06_BREAKOUT_ANTI_CHASE_EXTENSION` | `YES` | **`NOT_REPLAYABLE`** | `MISSING` | `{'ema_ext_pct': -17.01, 'vwap_ext_pct': None}` | `12.0` | ema_ext_pct=-17.01 <= +12.0%, but ext_above_vwap_pct is MISSING (decisions_log pruned); refusing to assume VWAP extension passed. |
| `G07_BREAKOUT_RSI_CEILING` | `YES` | **`PASS`** | `EXACT_HISTORICAL` | `57.95` | `78.0` | Recorded RSI=57.95 <= 78.0. |
| `G08_WATCHLIST_SOAK_GATE` | `YES` | **`FAIL`** | `EXACT_HISTORICAL` | `0.05` | `1.0` | Recorded soak_minutes=0.05m < 1.00m. |
| `G09_SCALPER_MIN_TXNS_5M` | `YES` | **`NOT_REPLAYABLE`** | `MISSING` | `None` | `15` | Historical 5m transaction counts (txns.m5.buys + sells) were not persisted in DuckDB; SCALPER_MIN_TXNS_5M cannot be independently evaluated. |
| `G10_CHART_STRATEGY_ALLOWLIST` | `YES` | **`PASS`** | `EXACT_HISTORICAL` | `['BREAKOUT_IGNITION']` | `BREAKOUT_IGNITION` | BREAKOUT_IGNITION is enabled in profile 'supplied_phase1_pi_snapshot'. |
| `G11_ALPHA_ACTION_CHOICE` | `YES` | **`FAIL`** | `EXACT_HISTORICAL` | `REJECT` | `BUY` | Recorded Alpha action='REJECT' != 'BUY'. |
| `G12_ALPHA_MIN_BUY_CONFIDENCE` | `YES` | **`NOT_REPLAYABLE`** | `EXACT_HISTORICAL` | `0.8` | `0.55` | action_log.buy_confidence=0.800 records confidence in 'REJECT', not BUY confidence (Alpha chose non-BUY action). |
| `G13_ALPHA_MAX_VAMP_ENTRY` | `YES` | **`PASS`** | `EXACT_HISTORICAL` | `0.501` | `0.55` | Recorded vamp_score=0.501 <= 0.55. |
| `G14_ALPHA_MIN_NARRATIVE_SCORE` | `YES` | **`FAIL`** | `EXACT_HISTORICAL` | `1.95` | `2.0` | Recorded narrative_score=1.95 < 2.00. |
| `G15_ALPHA_MIN_MOMENTUM_SCORE` | `YES` | **`FAIL`** | `EXACT_HISTORICAL` | `1.26` | `2.0` | Recorded momentum_score=1.26 < 2.00. |
| `G16_PORTFOLIO_SLOT_AND_DUPLICATE_MINT` | `YES` | **`PASS`** | `RECONSTRUCTABLE` | `{'mint_already_open': False, 'open_chart_count': 0, 'open_total_count': 1}` | `{'MAX_CHART_POSITIONS': 3, 'MAX_OPEN_POSITIONS': 6}` | Reconstructed from trades <= decision_timestamp: open_chart=0/3, total=1/6, dup_mint=False. |

### Watchlist Eviction Rule Trace

| Rule ID | Status | Input Availability | Observed | Threshold | Explanation |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `E01_VAMP_EVICTION_RULE` | **`EVICT_NOT_TRIGGERED`** | `EXACT_HISTORICAL` | `0.501` | `0.85` | vamp_score=0.501 below eviction thresholds. |
| `E02_LOW_SCORE_EVICTION_RULE` | **`EVICT_TRIGGERED`** | `EXACT_HISTORICAL` | `{'narrative_score': 1.95, 'momentum_score': 1.26}` | `2.0` | LOW_SCORE eviction triggered: narr=1.95 < 2.0 AND mom=1.26 < 2.0 (coiling_base_eligible=False or strikes=1 >= 2). |
| `E03_STALE_WATCH_EVICTION_RULE` | **`EVICT_NOT_TRIGGERED`** | `EXACT_HISTORICAL` | `0.05` | `15` | soak_minutes=0.05m <= 15m. |

---

## 4. Historical Execution Evidence (`execution_analyzer`)

- **Traded in `BREAKOUT_IGNITION`:** `False`
- **Executable Quote Availability:** `UNAVAILABLE`
- **Assessment:** Candidate was NOT traded in BREAKOUT_IGNITION. Historical Jupiter order quotes, route price impact, and executable slippage are UNAVAILABLE; chart/DexScreener prices are never treated as executable fills.

---

## 5. Post-Decision Subsequent Outcome Analysis (`outcome_analyzer`)

> **Anti-Look-Ahead & Execution Disclaimer:** Outcome metrics are 60-second DexScreener spot polls (PRICE_ONLY), and trajectory USD prices between snapshots are INFERRED_PRICE_FROM_MARKET_CAP. Neither proves executable Jupiter fill prices or liquidity depth.

- **Matched `candidate_outcomes.id`:** `32rBxcFj-1795705503-0c8e52` (`discovered_at=2026-11-26T15:05:03.504192+00:00`, `finalized=True`)
- **Outcome Evidence Classification:** `PRICE_ONLY` (60-second DexScreener spot polling, NOT an executable Jupiter fill)
- **Baseline Snapshot (`PRICE_ONLY`):** Price `\$0.0001486`, MC `\$148391.00`, Liquidity `\$28543.96`

| Horizon / Excursion Metric | Recorded Value | Evidence Class |
| :--- | ---: | :--- |
| `+1m` Price Change (`price_1m_pct`) | `+5.92%` | `PRICE_ONLY` |
| `+5m` Price Change (`price_5m_pct`) | `+24.09%` | `PRICE_ONLY` |
| `+15m` Price Change (`price_15m_pct`) | `+41.72%` | `PRICE_ONLY` |
| `+30m` Price Change (`price_30m_pct`) | `+94.28%` | `PRICE_ONLY` |
| `30m` Maximum Favorable Excursion (`mfe_pct`) | `+95.42%` | `PRICE_ONLY` |
| `30m` Maximum Adverse Excursion (`mae_pct`) | `+0.00%` | `PRICE_ONLY` |

### Subsequent Watchlist Trajectory (`action_log` Post-Decision Observations)

- **Blind Gap After Decision (Cooldown / Off-Watchlist):** `17.00 min`
- **Subsequent `DECISION_1` Ticks Within 30m:** `45`
- **Peak Recorded Market Cap After Decision:** \$487301.00
- **Subsequent Liquidity Range:** \$25659.31 – \$53493.16

| Timestamp (UTC) | +Elapsed (m) | MC (USD) | MC Δ % | Liquidity (USD) | Liq Δ % | Implied USD Price | Price Provenance |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| `2026-11-26T15:22:03.299520+00:00` | `17.00` | \$241593 | `+62.81%` | \$36864.48 | `+29.15%` | \$0.0002419 | `INFERRED_PRICE_FROM_MARKET_CAP` |
| `2026-11-26T15:22:56.106893+00:00` | `17.88` | \$233155 | `+57.12%` | \$36231.77 | `+26.93%` | \$0.0002335 | `INFERRED_PRICE_FROM_MARKET_CAP` |
| `2026-11-26T15:23:46.830344+00:00` | `18.72` | \$224393 | `+51.22%` | \$35584.46 | `+24.67%` | \$0.0002247 | `INFERRED_PRICE_FROM_MARKET_CAP` |
| `2026-11-26T15:24:37.920492+00:00` | `19.57` | \$243317 | `+63.97%` | \$37159.78 | `+30.18%` | \$0.0002437 | `INFERRED_PRICE_FROM_MARKET_CAP` |
| `2026-11-26T15:25:30.806847+00:00` | `20.46` | \$257892 | `+73.79%` | \$37674.31 | `+31.99%` | \$0.0002583 | `INFERRED_PRICE_FROM_MARKET_CAP` |
| `2026-11-26T15:26:21.275984+00:00` | `21.30` | \$251344 | `+69.38%` | \$37200.41 | `+30.33%` | \$0.0002517 | `INFERRED_PRICE_FROM_MARKET_CAP` |
| `2026-11-26T15:27:11.746032+00:00` | `22.14` | \$265104 | `+78.65%` | \$38270.18 | `+34.07%` | \$0.0002655 | `INFERRED_PRICE_FROM_MARKET_CAP` |
| `2026-11-26T15:28:01.678341+00:00` | `22.97` | \$268825 | `+81.16%` | \$38563.79 | `+35.10%` | \$0.0002692 | `INFERRED_PRICE_FROM_MARKET_CAP` |
| `2026-11-26T15:29:00.110396+00:00` | `23.94` | \$291399 | `+96.37%` | \$40241.66 | `+40.98%` | \$0.0002918 | `INFERRED_PRICE_FROM_MARKET_CAP` |
| `2026-11-26T15:29:50.719130+00:00` | `24.79` | \$289958 | `+95.40%` | \$40192.48 | `+40.81%` | \$0.0002904 | `INFERRED_PRICE_FROM_MARKET_CAP` |
| `2026-11-26T15:30:43.077907+00:00` | `25.66` | \$288504 | `+94.42%` | \$40304.35 | `+41.20%` | \$0.0002889 | `INFERRED_PRICE_FROM_MARKET_CAP` |
| `2026-11-26T15:31:33.238031+00:00` | `26.50` | \$272052 | `+83.33%` | \$39141.98 | `+37.13%` | \$0.0002724 | `INFERRED_PRICE_FROM_MARKET_CAP` |
| `2026-11-26T15:32:32.193911+00:00` | `27.48` | \$276001 | `+86.00%` | \$39472.62 | `+38.29%` | \$0.0002764 | `INFERRED_PRICE_FROM_MARKET_CAP` |
| `2026-11-26T15:33:22.058681+00:00` | `28.31` | \$288603 | `+94.49%` | \$40413.98 | `+41.59%` | \$0.0002890 | `INFERRED_PRICE_FROM_MARKET_CAP` |
| `2026-11-26T15:34:16.417132+00:00` | `29.22` | \$288180 | `+94.20%` | \$40417.22 | `+41.60%` | \$0.0002886 | `INFERRED_PRICE_FROM_MARKET_CAP` |
| `2026-11-26T15:34:58.455261+00:00` | `29.92` | \$287919 | `+94.03%` | \$40420.71 | `+41.61%` | \$0.0002883 | `INFERRED_PRICE_FROM_MARKET_CAP` |

### All Watchlist Decision Episodes for PAIR (20 Episodes)

| Episode ID | Started At (UTC) | Ended At (UTC) | `DECISION_1` Rows | Peak Breakout Score | Terminal Action | Terminal Reason |
| :--- | :--- | :--- | ---: | ---: | :--- | :--- |
| `32rBxcFj-ep001` | `2026-11-26T13:42:00.971810+00:00` | `2026-11-26T13:49:04.400969+00:00` | 61 | 53 | `EVICT` | `LOW_SCORE (narr=1.86, mom=1.40 < 2.0)` |
| `32rBxcFj-ep002` | `2026-11-26T14:04:07.280865+00:00` | `2026-11-26T14:04:07.280865+00:00` | 1 | 17 | `EVICT` | `LOW_SCORE (narr=1.86, mom=1.40 < 2.0)` |
| `32rBxcFj-ep003` | `2026-11-26T14:19:25.662651+00:00` | `2026-11-26T14:34:39.810349+00:00` | 53 | 66 | `EVICT` | `STALE_WATCH (15.3m > 15m)` |
| `32rBxcFj-ep004` | `2026-11-26T14:45:01.403175+00:00` | `2026-11-26T14:49:50.633097+00:00` | 17 | 21 | `EVICT` | `LOW_SCORE (narr=1.95, mom=1.36 < 2.0)` |
| `32rBxcFj-ep005` | `2026-11-26T15:05:03.504192+00:00` | `2026-11-26T15:05:03.504192+00:00` | 1 | 18 | `EVICT` | `LOW_SCORE (narr=1.95, mom=1.26 < 2.0)` |
| `32rBxcFj-ep006` | `2026-11-26T15:22:03.299520+00:00` | `2026-11-26T15:37:02.597732+00:00` | 51 | 67 | `EVICT` | `STALE_WATCH (15.0m > 15m)` |
| `32rBxcFj-ep007` | `2026-11-26T15:47:06.141430+00:00` | `2026-11-26T16:02:03.997280+00:00` | 50 | 67 | `EVICT` | `STALE_WATCH (15.0m > 15m)` |
| `32rBxcFj-ep008` | `2026-11-26T16:12:20.804318+00:00` | `2026-11-26T16:27:25.640384+00:00` | 49 | 75 | `EVICT` | `STALE_WATCH (15.1m > 15m)` |
| `32rBxcFj-ep009` | `2026-11-26T16:37:52.746254+00:00` | `2026-11-26T16:52:34.437744+00:00` | 48 | 32 | `EVICT` | `LOW_SCORE (narr=1.99, mom=1.89 < 2.0)` |
| `32rBxcFj-ep010` | `2026-11-26T17:08:38.992503+00:00` | `2026-11-26T17:08:38.992503+00:00` | 1 | 22 | `EVICT` | `LOW_SCORE (narr=1.99, mom=1.82 < 2.0)` |
| `32rBxcFj-ep011` | `2026-11-26T17:24:05.680363+00:00` | `2026-11-26T17:24:05.680363+00:00` | 1 | 35 | `EVICT` | `LOW_SCORE (narr=1.81, mom=1.61 < 2.0)` |
| `32rBxcFj-ep012` | `2026-11-26T17:39:22.795291+00:00` | `2026-11-26T17:39:22.795291+00:00` | 1 | 17 | `EVICT` | `LOW_SCORE (narr=1.81, mom=1.63 < 2.0)` |
| `32rBxcFj-ep013` | `2026-11-26T17:54:52.556509+00:00` | `2026-11-26T17:54:52.556509+00:00` | 1 | 28 | `EVICT` | `LOW_SCORE (narr=1.82, mom=1.63 < 2.0)` |
| `32rBxcFj-ep014` | `2026-11-26T18:11:10.863083+00:00` | `2026-11-26T18:13:52.074919+00:00` | 19 | 21 | `EVICT` | `LOW_SCORE (narr=1.82, mom=1.82 < 2.0)` |
| `32rBxcFj-ep015` | `2026-11-26T18:28:57.265588+00:00` | `2026-11-26T18:29:14.627498+00:00` | 2 | 21 | `EVICT` | `LOW_SCORE (narr=1.93, mom=1.74 < 2.0)` |
| `32rBxcFj-ep016` | `2026-11-26T18:47:20.873431+00:00` | `2026-11-26T18:47:20.873431+00:00` | 1 | 13 | `EVICT` | `LOW_SCORE (narr=1.93, mom=1.57 < 2.0)` |
| `32rBxcFj-ep017` | `2026-11-26T19:05:10.438587+00:00` | `2026-11-26T19:05:10.438587+00:00` | 1 | 24 | `EVICT` | `LOW_SCORE (narr=1.75, mom=1.18 < 2.0)` |
| `32rBxcFj-ep018` | `2026-11-26T19:20:23.139328+00:00` | `2026-11-26T19:20:23.139328+00:00` | 1 | 13 | `EVICT` | `LOW_SCORE (narr=1.75, mom=1.03 < 2.0)` |
| `32rBxcFj-ep019` | `2026-11-26T19:35:34.404525+00:00` | `2026-11-26T19:35:34.404525+00:00` | 1 | 30 | `EVICT` | `LOW_SCORE (narr=1.85, mom=1.30 < 2.0)` |
| `32rBxcFj-ep020` | `2026-11-26T19:51:36.356115+00:00` | `2026-11-26T19:51:53.316371+00:00` | 2 | 17 | `EVICT` | `LOW_SCORE (narr=1.85, mom=1.41 < 2.0)` |
