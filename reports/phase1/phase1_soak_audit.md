# Solana Trading Agent — Post-Soak Telemetry & Audit Report

## 1. Header & Executive Summary

- **Generated At (UTC):** `2026-11-26T20:47:08.203716+00:00`
- **Soak Cutoff (`--since`):** `2026-11-26T03:15:45+00:00`
- **Latest Telemetry Timestamp (UTC):** `2026-11-26T21:02:53.546660+00:00`
- **Active Soak Duration Elapsed:** **17h 47m** (`17.79` hours)
- **Input Source:** DuckDB Snapshot (`solana_meme_agent.duckdb` via `/tmp` copy)

### Headline Active Soak KPIs
| KPI | Active Soak Value |
| :--- | ---: |
| Soak Trade Events (`BUY` / Exits) | 32 (16 Entries / 16 Exits) |
| Soak Win Rate (Closed/Partial Exits) | 18.8% (3/16) |
| Soak Net Realized PnL (SOL) | -0.2992 SOL |
| Non-NONE Setups / Breakouts Triggered | 2 |
| Unique Tokens Evaluated by Alpha | 30 |
| Active Soak Whale Events Recorded | 0 |

## 2. Table Inventory & Schema Overview

| Table Name | Present | Total Rows | Pre-Soak Rows | Active Soak Rows | Earliest Timestamp (UTC) | Latest Timestamp (UTC) | Columns |
| :--- | :--- | ---: | ---: | ---: | :--- | :--- | :--- |
| `agent_state` | `YES` | 1 | 0 | 1 | `2026-11-26T19:15:58.440786+00:00` | `2026-11-26T19:15:58.440786+00:00` | `id`, `focus_state`, `sol_balance`, `daily_start_sol`, `daily_pnl_sol`, `daily_reset_date`, `active_position_json`, `last_decision_rationale`, `updated_at`, `strategy`, `triggering_wallets_json`, `whale_shadow_exit_requested`, `whale_position_json`, `chart_positions_json`, `whale_positions_json` |
| `active_watchlist` | `YES` | 4 | 0 | 4 | `2026-11-26T20:35:20.202340+00:00` | `2026-11-26T20:45:53.311079+00:00` | `mint`, `symbol`, `pair_address`, `added_at`, `safety_passed`, `entry_liquidity_usd`, `peak_mc`, `low_mc`, `leg2_score`, `leg2_breakdown_json`, `vamp_strikes`, `rsi_5m`, `setup_type`, `scalper_eligible`, `dist_from_1h_high_pct`, `last_evaluated_at`, `latest_scores_json`, `candidate_json`, `discovery_source`, `jup_organic_score`, `jup_meta_json` |
| `cooldowns` | `YES` | 15 | 0 | 15 | `2026-11-26T20:45:45.332453+00:00` | `2026-11-26T21:02:53.546660+00:00` | `mint`, `expires_at`, `reason` |
| `trades` | `YES` | 386 | 354 | 32 | `2026-11-20T02:06:04.484551+00:00` | `2026-11-26T19:15:58.457557+00:00` | `id`, `timestamp`, `mode`, `side`, `mint`, `symbol`, `in_amount`, `out_amount`, `sol_value`, `pnl_sol`, `pnl_pct`, `reason`, `tx_signature`, `rationale`, `strategy`, `triggering_wallets_json`, `opened_at`, `jupiter_price_impact_raw` |
| `decisions_log` | `YES` | 1077 | 0 | 1077 | `2026-11-26T19:17:42.795609+00:00` | `2026-11-26T20:46:46.610011+00:00` | `id`, `timestamp`, `mint`, `symbol`, `phase`, `action`, `confidence`, `scores_json`, `rationale` |
| `action_log` | `YES` | 20083 | 2413 | 17670 | `2026-11-26T01:01:57.553117+00:00` | `2026-11-26T20:46:46.213675+00:00` | `id`, `timestamp_utc`, `bot`, `mode`, `action`, `symbol`, `asset_id`, `qty`, `notional`, `unit`, `price_or_value`, `pnl_amount`, `pnl_pct`, `strategy`, `reason`, `sol_equity`, `daily_realized_sol`, `circuit_breaker`, `mc_usd`, `liq_usd`, `soak_minutes`, `rsi`, `leg2_score`, `narrative_score`, `momentum_score`, `vamp_score`, `buy_confidence`, `peak_pnl_pct`, `hold_seconds`, `triggering_whales`, `whale_sol_flow`, `breakout_score`, `breakout_state`, `breakout_timeframe`, `breakout_vol_ratio`, `breakout_ema_ext_pct`, `breakout_failed_gates`, `jupiter_price_impact_raw`, `monitor_sol_value`, `price_source`, `position_mfe_pct`, `position_mae_pct` |
| `candidate_outcomes` | `YES` | 3968 | 3166 | 802 | `2026-11-22T19:03:15.032461+00:00` | `2026-11-26T20:45:55.930731+00:00` | `id`, `mint`, `symbol`, `source`, `discovered_at`, `decision`, `decision_reason`, `strategy`, `entry_price_usd`, `entry_mc_usd`, `entry_liq_usd`, `breakout_score`, `breakout_state`, `leg2_score`, `vamp_score`, `rsi`, `whale_sol_flow`, `triggering_wallet`, `price_1m_pct`, `price_5m_pct`, `price_15m_pct`, `price_30m_pct`, `mfe_pct`, `mae_pct`, `finalized` |
| `whale_events` | `YES` | 365 | 365 | 0 | `2026-11-24T20:48:55+00:00` | `2026-11-26T03:04:18+00:00` | `signature`, `wallet`, `mint`, `side`, `sol_amount`, `token_amount`, `timestamp_utc` |
| `whale_signals` | `YES` | 88 | 88 | 0 | `2026-11-24T20:55:36+00:00` | `2026-11-26T03:04:18+00:00` | `mint`, `buy_count`, `unique_whales`, `total_sol_spent`, `sell_count`, `last_seen_utc` |
| `whale_decisions` | `YES` | 73 | 73 | 0 | `2026-11-24T20:59:43.927227+00:00` | `2026-11-26T03:05:16.041040+00:00` | `mint`, `status`, `reason`, `updated_at`, `breakout_telemetry_json` |
| `social_signals` | `YES` | 0 | 0 | 0 | `—` | `—` | `mint`, `mention_count`, `unique_authors`, `total_likes`, `top_snippets_json`, `last_seen_utc` |
| `x_seen_tweets` | `YES` | 585 | 585 | 0 | `2026-11-20T23:19:01.215713+00:00` | `2026-11-24T17:11:49.422046+00:00` | `tweet_id`, `fetched_at_utc`, `author_username`, `author_followers`, `like_count`, `text`, `extracted_mints` |
| `x_worker_state` | `YES` | 1 | 0 | 1 | `2026-11-26T20:46:07.748478+00:00` | `2026-11-26T20:46:07.748478+00:00` | `id`, `last_poll_utc` |

## 3. Trades Performance (`trades`)

### Side-by-Side Window Summary
| Metric | Pre-Soak Baseline | Active Soak Window |
| :--- | ---: | ---: |
| Total Trade Events (Rows) | 354 | 32 |
| Entries (`BUY`) | 168 | 16 |
| Exits (`PARTIAL_SELL` + `SELL`) | 186 | 16 |
| Currently Open Positions | 0 | 0 |
| Winning Exits / Win Rate | 63 (33.9%) | 3 (18.8%) |
| Net Realized PnL (SOL) | -1.8763 SOL | -0.2992 SOL |
| Average Exit PnL (SOL / %) | -0.0101 SOL (-1.40%) | -0.0187 SOL (-3.74%) |
| Exit PnL % (Min / Median / P90 / Max) | -68.00% / -1.90% / +11.60% / +74.46% | -13.22% / -2.50% / +1.72% / +3.46% |
| Position MFE % (Min / Median / Mean / P90 / Max) | -3.35% / +12.16% / +10.94% / +20.68% / +22.79% | -3.14% / +0.23% / +2.19% / +9.25% / +14.03% |
| Position MAE % (Min / Median / Mean / P90 / Max) | -11.41% / -5.55% / -6.41% / -3.86% / -3.14% | -13.02% / -3.74% / -5.09% / -0.46% / -0.18% |

### Breakdown by Strategy / Setup Type
| Strategy / Setup | Pre-Soak Exits | Pre-Soak Net SOL | Active Soak Exits | Active Soak Net SOL |
| :--- | ---: | ---: | ---: | ---: |
| `BREAKOUT_IGNITION` | 9 | +0.0397 | 16 | -0.2992 |
| `DISPLACEMENT_RETEST` | 9 | -0.3868 | 0 | +0.0000 |
| `LEG2_ENTRY` | 47 | -0.8702 | 0 | +0.0000 |
| `SCALPER` | 7 | +0.0364 | 0 | +0.0000 |
| `SWEEP_RECLAIM` | 34 | -0.2531 | 0 | +0.0000 |
| `WHALE_SCALP` | 80 | -0.4424 | 0 | +0.0000 |

### Breakdown by Exit Reason
| Exit Reason | Pre-Soak Count | Active Soak Count |
| :--- | ---: | ---: |
| `BREAKEVEN_SHIELD` | 10 | 2 |
| `EXODUS_LIQ_DROP` | 2 | 0 |
| `EXODUS_SELL_FLOW` | 30 | 10 |
| `HARD_STOP_LOSS` | 51 | 3 |
| `PARTIAL_TP_50PCT` | 8 | 0 |
| `TRAILING_TP` | 19 | 1 |
| `WHALE_BREAKEVEN_SHIELD` | 4 | 0 |
| `WHALE_HARD_STOP` | 2 | 0 |
| `WHALE_PARTIAL_TP_50PCT` | 3 | 0 |
| `WHALE_PEAK_TRAIL` | 15 | 0 |
| `WHALE_SHADOW_EXIT` | 9 | 0 |
| `WHALE_TIME_STOP` | 33 | 0 |

### Active Soak Window Trade Ledger (32 rows)
| Timestamp (UTC) | Side | Symbol | Mint | Setup | SOL Value | PnL % | PnL SOL | MFE % | MAE % | Status | Reason |
| :--- | :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | :--- | :--- |
| `2026-11-26T04:59:02.248617+00:00` | `BUY` | **NORFEATH** | `3gmFG4..pump` | `BREAKOUT_IGNITION` | 0.5000 | — | — | — | — | `ENTRY` | `DECISION_1_ENTRY` |
| `2026-11-26T05:03:38.514414+00:00` | `BUY` | **FIONA** | `B34nXK..pump` | `BREAKOUT_IGNITION` | 0.5000 | — | — | — | — | `ENTRY` | `DECISION_1_ENTRY` |
| `2026-11-26T05:10:43.698069+00:00` | `SELL` | **NORFEATH** | `3gmFG4..pump` | `BREAKOUT_IGNITION` | 0.4349 | -13.22 | -0.0661 | -3.14 | -13.02 | `CLOSED` | `HARD_STOP_LOSS (-13.02% <= -12%)` |
| `2026-11-26T05:15:13.386790+00:00` | `SELL` | **FIONA** | `B34nXK..pump` | `BREAKOUT_IGNITION` | 0.4992 | -0.36 | -0.0018 | +0.91 | -2.49 | `CLOSED` | `EXODUS_SELL_FLOW (5m sells=69.2% >= 65%)` |
| `2026-11-26T06:39:03.068334+00:00` | `BUY` | **PUMPCHAN** | `Fo2HAw..pump` | `BREAKOUT_IGNITION` | 0.5000 | — | — | — | — | `ENTRY` | `DECISION_1_ENTRY` |
| `2026-11-26T07:14:28.931042+00:00` | `SELL` | **PUMPCHAN** | `Fo2HAw..pump` | `BREAKOUT_IGNITION` | 0.5183 | +3.46 | +0.0173 | +14.03 | -0.60 | `CLOSED` | `TRAILING_TP (peak +14.03%, pullback -9.10% >= -6%) [LEG2_ENTRY_DISABLED -> COOLDOWN]` |
| `2026-11-26T07:34:00.837828+00:00` | `BUY` | **Pompom** | `5EmvWD..pump` | `BREAKOUT_IGNITION` | 0.5000 | — | — | — | — | `ENTRY` | `DECISION_1_ENTRY` |
| `2026-11-26T07:50:21.697836+00:00` | `SELL` | **Pompom** | `5EmvWD..pump` | `BREAKOUT_IGNITION` | 0.4949 | -1.23 | -0.0061 | +3.68 | -7.74 | `CLOSED` | `EXODUS_SELL_FLOW (5m sells=65.2% >= 65%)` |
| `2026-11-26T08:12:24.188317+00:00` | `BUY` | **Zero** | `DTSkmo..pump` | `BREAKOUT_IGNITION` | 0.5000 | — | — | — | — | `ENTRY` | `DECISION_1_ENTRY` |
| `2026-11-26T08:25:41.624987+00:00` | `BUY` | **JIMMY** | `38M2z4..pump` | `BREAKOUT_IGNITION` | 0.5000 | — | — | — | — | `ENTRY` | `DECISION_1_ENTRY` |
| `2026-11-26T08:25:47.262624+00:00` | `SELL` | **JIMMY** | `38M2z4..pump` | `BREAKOUT_IGNITION` | 0.4907 | -2.06 | -0.0103 | -0.18 | -0.18 | `CLOSED` | `EXODUS_SELL_FLOW (5m sells=65.5% >= 65%)` |
| `2026-11-26T08:27:29.218371+00:00` | `SELL` | **Zero** | `DTSkmo..pump` | `BREAKOUT_IGNITION` | 0.4394 | -12.31 | -0.0616 | +0.63 | -12.11 | `CLOSED` | `HARD_STOP_LOSS (-12.11% <= -12%)` |
| `2026-11-26T10:10:00.673262+00:00` | `BUY` | **UHI** | `Bnw5JR..pump` | `BREAKOUT_IGNITION` | 0.5000 | — | — | — | — | `ENTRY` | `DECISION_1_ENTRY` |
| `2026-11-26T10:13:23.195377+00:00` | `BUY` | **NORFEATH** | `3gmFG4..pump` | `BREAKOUT_IGNITION` | 0.5000 | — | — | — | — | `ENTRY` | `DECISION_1_ENTRY` |
| `2026-11-26T10:16:11.630613+00:00` | `SELL` | **NORFEATH** | `3gmFG4..pump` | `BREAKOUT_IGNITION` | 0.4394 | -12.32 | -0.0616 | -1.92 | -12.12 | `CLOSED` | `HARD_STOP_LOSS (-12.12% <= -12%)` |
| `2026-11-26T10:31:52.146118+00:00` | `SELL` | **UHI** | `Bnw5JR..pump` | `BREAKOUT_IGNITION` | 0.4779 | -4.62 | -0.0231 | +6.16 | -4.91 | `CLOSED` | `EXODUS_SELL_FLOW (5m sells=98.8% >= 65%)` |
| `2026-11-26T11:43:49.653198+00:00` | `BUY` | **JIMMY** | `38M2z4..pump` | `BREAKOUT_IGNITION` | 0.5000 | — | — | — | — | `ENTRY` | `DECISION_1_ENTRY` |
| `2026-11-26T12:02:23.725813+00:00` | `SELL` | **JIMMY** | `38M2z4..pump` | `BREAKOUT_IGNITION` | 0.5093 | +1.65 | +0.0083 | +9.69 | -2.57 | `CLOSED` | `BREAKEVEN_SHIELD (armed >= +8%, hit floor +1.85% <= +2%)` |
| `2026-11-26T13:41:54.097772+00:00` | `BUY` | **JIMMY** | `38M2z4..pump` | `BREAKOUT_IGNITION` | 0.5000 | — | — | — | — | `ENTRY` | `DECISION_1_ENTRY` |
| `2026-11-26T13:51:13.916732+00:00` | `SELL` | **JIMMY** | `38M2z4..pump` | `BREAKOUT_IGNITION` | 0.5099 | +1.78 | +0.0089 | +8.82 | -1.55 | `CLOSED` | `BREAKEVEN_SHIELD (armed >= +8%, hit floor +1.98% <= +2%)` |
| `2026-11-26T14:23:57.530429+00:00` | `BUY` | **UHI** | `Bnw5JR..pump` | `BREAKOUT_IGNITION` | 0.5000 | — | — | — | — | `ENTRY` | `DECISION_1_ENTRY` |
| `2026-11-26T14:24:35.869542+00:00` | `SELL` | **UHI** | `Bnw5JR..pump` | `BREAKOUT_IGNITION` | 0.4776 | -4.69 | -0.0234 | -1.92 | -5.09 | `CLOSED` | `EXODUS_SELL_FLOW (5m sells=66.0% >= 65%)` |
| `2026-11-26T14:40:48.853453+00:00` | `BUY` | **BASKET** | `3Tg6Yn..JN6f` | `BREAKOUT_IGNITION` | 0.5000 | — | — | — | — | `ENTRY` | `DECISION_1_ENTRY` |
| `2026-11-26T14:41:13.732655+00:00` | `SELL` | **BASKET** | `3Tg6Yn..JN6f` | `BREAKOUT_IGNITION` | 0.4933 | -1.54 | -0.0077 | -0.69 | -1.15 | `CLOSED` | `EXODUS_SELL_FLOW (5m sells=76.0% >= 65%)` |
| `2026-11-26T17:08:32.028104+00:00` | `BUY` | **JIMMY** | `38M2z4..pump` | `BREAKOUT_IGNITION` | 0.5000 | — | — | — | — | `ENTRY` | `DECISION_1_ENTRY` |
| `2026-11-26T17:14:27.283510+00:00` | `BUY` | **Zero** | `DTSkmo..pump` | `BREAKOUT_IGNITION` | 0.5000 | — | — | — | — | `ENTRY` | `DECISION_1_ENTRY` |
| `2026-11-26T17:14:32.908449+00:00` | `SELL` | **Zero** | `DTSkmo..pump` | `BREAKOUT_IGNITION` | 0.4863 | -2.94 | -0.0147 | -0.32 | -0.32 | `CLOSED` | `EXODUS_SELL_FLOW (5m sells=66.4% >= 65%)` |
| `2026-11-26T17:31:38.335166+00:00` | `SELL` | **JIMMY** | `38M2z4..pump` | `BREAKOUT_IGNITION` | 0.4666 | -6.89 | -0.0344 | -1.56 | -9.56 | `CLOSED` | `EXODUS_SELL_FLOW (5m sells=66.0% >= 65%)` |
| `2026-11-26T18:09:08.597959+00:00` | `BUY` | **FIONA** | `B34nXK..pump` | `BREAKOUT_IGNITION` | 0.5000 | — | — | — | — | `ENTRY` | `DECISION_1_ENTRY` |
| `2026-11-26T18:12:58.580468+00:00` | `SELL` | **FIONA** | `B34nXK..pump` | `BREAKOUT_IGNITION` | 0.4994 | -0.33 | -0.0016 | +1.08 | -0.82 | `CLOSED` | `EXODUS_SELL_FLOW (5m sells=66.7% >= 65%)` |
| `2026-11-26T19:05:53.254961+00:00` | `BUY` | **UHI** | `Bnw5JR..pump` | `BREAKOUT_IGNITION` | 0.5000 | — | — | — | — | `ENTRY` | `DECISION_1_ENTRY` |
| `2026-11-26T19:15:58.457557+00:00` | `SELL` | **UHI** | `Bnw5JR..pump` | `BREAKOUT_IGNITION` | 0.4799 | -4.23 | -0.0211 | -0.22 | -7.25 | `CLOSED` | `EXODUS_SELL_FLOW (5m sells=69.0% >= 65%)` |

## 4. Alpha Decisions & Stage 3 Verification (`decisions_log`)

### Action Breakdown (Pre-Soak vs. Active Soak)
| Action | Pre-Soak Count | Active Soak Count |
| :--- | ---: | ---: |
| `BUY` | 0 | 5 |
| `REJECT` | 0 | 54 |
| `WATCH` | 0 | 1018 |

### Setup Type Breakdown (Pre-Soak vs. Active Soak)
| Setup Type | Pre-Soak Count | Active Soak Count |
| :--- | ---: | ---: |
| `BREAKOUT_IGNITION` | 0 | 2 |
| `NONE` | 0 | 1075 |

### Score Distributions (Pre-Soak vs. Active Soak)
| Metric | Window | Count | Min | Median | Mean | P90 | Max |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| `narrative` | Pre-Soak | 0 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| `narrative` | Active Soak | 1077 | 1.27 | 2.10 | 2.16 | 2.43 | 3.32 |
| `momentum` | Pre-Soak | 0 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| `momentum` | Active Soak | 1077 | 0.70 | 2.15 | 2.47 | 4.43 | 4.80 |
| `vamp` | Pre-Soak | 0 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| `vamp` | Active Soak | 1077 | 0.21 | 0.47 | 0.47 | 0.62 | 0.78 |
| `leg2` | Pre-Soak | 0 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| `leg2` | Active Soak | 1077 | 18.80 | 62.50 | 60.09 | 90.00 | 100.00 |
| `rsi` | Pre-Soak | 0 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| `rsi` | Active Soak | 1077 | 22.10 | 51.32 | 50.80 | 66.28 | 100.00 |
| `confidence` | Pre-Soak | 0 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| `confidence` | Active Soak | 1077 | 0.39 | 0.66 | 0.68 | 0.81 | 0.91 |

### Stage 3 Unfrozen-Momentum Verification Table (Top 25 Soak Tokens)
| Symbol | Mint | Evals | Cache Hits | Distinct Momentum | Momentum Min..Max | Narrative Min..Max | Actions | Non-NONE Setups |
| :--- | :--- | ---: | ---: | ---: | :--- | :--- | :--- | :--- |
| **FIONA** | `B34nXK..pump` | 179 | 177 | 58 | `0.70..4.30` | `2.34..2.50` | `WATCH:178, REJECT:1` | `NONE` |
| **JIMMY** | `38M2z4..pump` | 169 | 166 | 78 | `0.70..4.49` | `2.15..2.31` | `WATCH:169` | `NONE` |
| **LENDR** | `AGazYi..Lend` | 112 | 109 | 52 | `0.70..3.88` | `1.69..2.05` | `WATCH:111, REJECT:1` | `NONE` |
| **Investor** | `J9eBkC..pump` | 94 | 91 | 45 | `0.70..3.73` | `1.93..2.06` | `WATCH:92, REJECT:2` | `NONE` |
| **SIDOOR** | `zcRvzm..door` | 56 | 54 | 27 | `0.70..4.20` | `1.42..2.08` | `WATCH:53, REJECT:2, BUY:1` | `NONE` |
| **GTA6** | `4kjruq..zDnz` | 52 | 50 | 11 | `1.58..4.79` | `2.94..3.32` | `WATCH:50, REJECT:2` | `NONE` |
| **Dogeball** | `EMhHvd..pump` | 50 | 49 | 17 | `1.40..4.30` | `2.21..2.21` | `WATCH:50` | `NONE` |
| **Snowball** | `9TKbd3..pump` | 49 | 47 | 15 | `1.39..2.29` | `1.91..2.08` | `WATCH:48, REJECT:1` | `NONE` |
| **PI** | `GEUULM..pump` | 37 | 36 | 15 | `1.40..4.54` | `2.25..2.25` | `WATCH:36, BUY:1` | `NONE` |
| **DCTY** | `9hjBLB..pump` | 33 | 31 | 13 | `0.70..3.68` | `1.65..1.65` | `WATCH:24, REJECT:9` | `NONE` |
| **George** | `FvitVJ..pump` | 28 | 27 | 16 | `1.40..4.69` | `1.97..1.97` | `WATCH:26, REJECT:2` | `NONE` |
| **JANTA** | `9tERTs..pump` | 27 | 27 | 13 | `1.40..4.00` | `2.16..2.16` | `WATCH:27` | `NONE` |
| **1T** | `HYFbbf..pump` | 26 | 25 | 6 | `1.40..4.80` | `1.89..1.89` | `WATCH:24, BUY:1, REJECT:1` | `NONE` |
| **securititty** | `Gvo9wi..pump` | 24 | 23 | 8 | `1.40..4.80` | `2.10..2.10` | `WATCH:23, BUY:1` | `NONE` |
| **Huss** | `CUg7Az..pump` | 19 | 17 | 10 | `1.42..4.70` | `1.48..1.72` | `WATCH:16, REJECT:3` | `NONE` |
| **NORFEATH** | `3gmFG4..pump` | 17 | 17 | 10 | `1.40..4.65` | `1.97..1.97` | `WATCH:16, REJECT:1` | `NONE` |
| **Hoppy** | `CFX1gH..pump` | 16 | 15 | 9 | `1.40..4.80` | `2.53..2.53` | `WATCH:16` | `NONE` |
| **攀豹豹** | `6ZbWPY..pump` | 15 | 14 | 8 | `1.10..4.30` | `1.69..1.69` | `WATCH:13, REJECT:2` | `NONE` |
| **duk** | `DG2EJS..pump` | 15 | 12 | 11 | `0.90..3.57` | `1.67..1.85` | `WATCH:10, REJECT:5` | `NONE` |
| **Zero** | `DTSkmo..pump` | 11 | 9 | 7 | `1.54..3.16` | `1.83..2.03` | `WATCH:9, REJECT:2` | `NONE` |
| **DIGS** | `9QT8K6..pump` | 10 | 8 | 10 | `0.78..1.76` | `1.89..1.96` | `REJECT:10` | `NONE` |
| **Rings** | `65iH8k..ring` | 8 | 7 | 5 | `2.39..4.59` | `1.64..1.64` | `WATCH:8` | `NONE` |
| **JOB** | `9Pq7aL..pump` | 8 | 6 | 7 | `1.71..4.43` | `1.95..2.05` | `WATCH:6, REJECT:2` | `BREAKOUT_IGNITION:2` |
| **BASKET** | `3Tg6Yn..JN6f` | 6 | 3 | 6 | `1.40..2.10` | `1.78..2.00` | `WATCH:4, REJECT:2` | `NONE` |
| **ZBRA** | `DrBCGJ..pump` | 6 | 5 | 5 | `3.12..3.32` | `2.13..2.13` | `WATCH:6` | `NONE` |

### Complete Soak Breakout & Setup Log (2 non-NONE evaluations; showing 2)
| Timestamp (UTC) | Symbol | Mint | Setup | Eligible | Action | Approved | Mom | Narr | RSI | Leg2 | BO Score/State |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | :--- |
| `2026-11-26T20:32:34.156624+00:00` | **JOB** | `9Pq7aL..pump` | `BREAKOUT_IGNITION` | `False` | `WATCH` | `False` | 2.68 | 2.05 | 46.0 | 84.3 | `82/IGNITION` |
| `2026-11-26T20:32:51.609268+00:00` | **JOB** | `9Pq7aL..pump` | `BREAKOUT_IGNITION` | `False` | `WATCH` | `False` | 4.43 | 2.05 | 46.0 | 73.7 | `90/IGNITION` |

## 5. Candidate Outcomes & Missed-Opportunity Telemetry (`candidate_outcomes`)

### Forward Return & MFE/MAE Distributions (Pre-Soak vs. Active Soak)
| Metric | Window | Count | Min % | Median % | Mean % | P90 % | Max % |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| `mfe_pct` | Pre-Soak | 3093 | +0.00% | +4.96% | +16.10% | +35.18% | +597.81% |
| `mfe_pct` | Active Soak | 802 | +0.00% | +4.24% | +10.12% | +25.10% | +156.75% |
| `mae_pct` | Pre-Soak | 3093 | -99.76% | -5.94% | -13.41% | +0.00% | +0.00% |
| `mae_pct` | Active Soak | 802 | -99.90% | -5.62% | -12.87% | -0.41% | +0.00% |
| `price_1m_pct` | Pre-Soak | 3080 | -99.71% | +0.00% | +0.83% | +5.85% | +416.15% |
| `price_1m_pct` | Active Soak | 801 | -99.39% | +0.00% | -0.07% | +4.47% | +58.82% |
| `price_5m_pct` | Pre-Soak | 3009 | -99.72% | -0.10% | +0.67% | +10.12% | +378.80% |
| `price_5m_pct` | Active Soak | 795 | -99.62% | -0.35% | -0.64% | +7.62% | +64.18% |
| `price_15m_pct` | Pre-Soak | 2925 | -99.72% | -0.48% | -0.03% | +13.83% | +497.57% |
| `price_15m_pct` | Active Soak | 788 | -99.62% | -0.88% | -2.91% | +10.20% | +72.26% |
| `price_30m_pct` | Pre-Soak | 2815 | -99.76% | -1.12% | -1.26% | +18.16% | +520.59% |
| `price_30m_pct` | Active Soak | 779 | -99.90% | -1.53% | -5.17% | +13.06% | +109.37% |

### Top 25 Active Soak Candidates Ranked by MFE % (Highlighting Missed Runners)
| Rank | Discovered At (UTC) | Symbol | Mint | Source | Decision | Status Tag | MFE % | MAE % | 5m % | 15m % | 30m % | Reason |
| ---: | :--- | :--- | :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | :--- |
| 1 | `2026-11-26T16:59:28.130949+00:00` | **KURO** | `AaY6Xt..pump` | `CHART_ALPHA` | `WATCH` | **MISSED_RUNNER** | +156.75% | -10.83% | +2.67% | +31.81% | +18.31% | `WATCH -> WATCH` |
| 2 | `2026-11-26T15:49:10.778340+00:00` | **DUO** | `CC1uvr..pump` | `CHART_ALPHA` | `BLOCKED` | **MISSED_RUNNER** | +152.50% | -45.52% | -9.05% | -9.65% | +109.37% | `BLOCKED: soak 0.0m < 1m / BREAKOUT_IGNITION(1m): no close ab` |
| 3 | `2026-11-26T17:08:47.809961+00:00` | **MSTR** | `57sCZ8..pump` | `CHART_ALPHA` | `WATCH` | **MISSED_RUNNER** | +132.20% | -99.26% | -50.85% | -63.54% | -99.26% | `WATCH -> WATCH` |
| 4 | `2026-11-26T14:04:07.280865+00:00` | **PAIR** | `32rBxc..pump` | `CHART_ALPHA` | `REJECTED` | **MISSED_RUNNER** | +110.27% | -26.96% | +14.97% | +23.13% | +95.78% | `REJECT -> REJECT` |
| 5 | `2026-11-26T16:07:04.323816+00:00` | **DUO** | `CC1uvr..pump` | `CHART_ALPHA` | `WATCH` | **MISSED_RUNNER** | +109.28% | -72.39% | +64.18% | +45.78% | -72.39% | `WATCH -> WATCH` |
| 6 | `2026-11-26T08:01:48.017612+00:00` | **NORFEATH** | `3gmFG4..pump` | `CHART_ALPHA` | `WATCH` | **MISSED_RUNNER** | +105.62% | +0.00% | +11.71% | +39.81% | +12.90% | `WATCH -> WATCH` |
| 7 | `2026-11-26T14:19:25.662651+00:00` | **PAIR** | `32rBxc..pump` | `CHART_ALPHA` | `WATCH` | **MISSED_RUNNER** | +96.21% | -2.40% | -0.46% | +57.65% | +87.40% | `WATCH -> WATCH` |
| 8 | `2026-11-26T15:05:03.504192+00:00` | **PAIR** | `32rBxc..pump` | `CHART_ALPHA` | `REJECTED` | **MISSED_RUNNER** | +95.42% | +0.00% | +24.09% | +41.72% | +94.28% | `REJECT -> REJECT` |
| 9 | `2026-11-26T08:30:36.144378+00:00` | **Elonmas** | `6B5X3k..pump` | `CHART_ALPHA` | `WATCH` | **MISSED_RUNNER** | +93.46% | +0.00% | +20.56% | +22.80% | +93.46% | `WATCH -> WATCH` |
| 10 | `2026-11-26T17:03:10.944249+00:00` | **Kuro** | `7sxuap..pump` | `CHART_ALPHA` | `WATCH` | **MISSED_RUNNER** | +83.33% | -66.10% | +6.92% | +24.91% | -58.37% | `WATCH -> WATCH` |
| 11 | `2026-11-26T08:50:56.776311+00:00` | **Elonmas** | `6B5X3k..pump` | `CHART_ALPHA` | `WATCH` | **MISSED_RUNNER** | +75.13% | +0.00% | +10.85% | +70.74% | +35.42% | `WATCH -> WATCH` |
| 12 | `2026-11-26T05:59:24.234525+00:00` | **Elonmas** | `6B5X3k..pump` | `CHART_ALPHA` | `WATCH` | **MISSED_RUNNER** | +74.01% | -5.41% | +24.64% | +17.42% | +54.86% | `WATCH -> WATCH` |
| 13 | `2026-11-26T16:42:59.789632+00:00` | **duk** | `DG2EJS..pump` | `CHART_ALPHA` | `REJECTED` | **MISSED_RUNNER** | +72.26% | -0.79% | +45.44% | +72.26% | +15.59% | `REJECT -> REJECT` |
| 14 | `2026-11-26T16:49:38.334897+00:00` | **Kuro** | `8LbBWe..pump` | `CHART_ALPHA` | `WATCH` | **MISSED_RUNNER** | +69.20% | -58.26% | -2.41% | +39.70% | -58.26% | `WATCH -> WATCH` |
| 15 | `2026-11-26T16:27:52.689714+00:00` | **duk** | `DG2EJS..pump` | `CHART_ALPHA` | `REJECTED` | **MISSED_RUNNER** | +69.02% | -11.92% | -6.80% | -2.66% | +69.02% | `REJECT -> REJECT` |
| 16 | `2026-11-26T03:15:57.382710+00:00` | **MCAP** | `AYij72..pump` | `CHART_ALPHA` | `BLOCKED` | **MISSED_RUNNER** | +68.62% | -55.64% | +14.11% | -27.99% | -34.68% | `BLOCKED: soak 0.6m < 1m / insufficient 5m candles (0 < 12); ` |
| 17 | `2026-11-26T05:48:08.085337+00:00` | **Elonmas** | `DMXm1g..pump` | `CHART_ALPHA` | `WATCH` | **MISSED_RUNNER** | +64.68% | -42.24% | +46.97% | +60.10% | -21.47% | `WATCH -> WATCH` |
| 18 | `2026-11-26T14:59:07.575813+00:00` | **MDAQ** | `8Xefdy..pump` | `CHART_ALPHA` | `WATCH` | **MISSED_RUNNER** | +64.56% | -16.25% | +60.15% | +29.69% | +32.47% | `WATCH -> WATCH` |
| 19 | `2026-11-26T19:36:42.595358+00:00` | **duk** | `DG2EJS..pump` | `CHART_ALPHA` | `WATCH` | **MISSED_RUNNER** | +61.58% | -2.60% | -2.52% | +22.46% | +59.84% | `WATCH -> WATCH` |
| 20 | `2026-11-26T08:16:50.017926+00:00` | **NORFEATH** | `3gmFG4..pump` | `CHART_ALPHA` | `WATCH` | **MISSED_RUNNER** | +58.75% | -23.07% | +49.11% | -12.83% | -22.10% | `WATCH -> WATCH` |
| 21 | `2026-11-26T10:57:32.066693+00:00` | **Grodia** | `6HS1ae..pump` | `CHART_ALPHA` | `REJECTED` | **MISSED_RUNNER** | +56.79% | +0.00% | +22.79% | +38.39% | +31.16% | `REJECT -> REJECT` |
| 22 | `2026-11-26T08:10:05.182545+00:00` | **Elonmas** | `6B5X3k..pump` | `CHART_ALPHA` | `WATCH` | **MISSED_RUNNER** | +53.57% | -1.46% | +6.52% | +17.57% | +53.57% | `WATCH -> WATCH` |
| 23 | `2026-11-26T16:50:44.291642+00:00` | **攀豹豹** | `6ZbWPY..pump` | `CHART_ALPHA` | `REJECTED` | **MISSED_RUNNER** | +52.18% | +0.00% | +2.55% | +16.95% | +52.18% | `REJECT -> REJECT` |
| 24 | `2026-11-26T19:42:28.007855+00:00` | **George** | `FvitVJ..pump` | `CHART_ALPHA` | `WATCH` | **MISSED_RUNNER** | +47.83% | -99.74% | +30.46% | -26.66% | -99.73% | `WATCH -> WATCH` |
| 25 | `2026-11-26T18:02:52.174152+00:00` | **KURO** | `AaY6Xt..pump` | `CHART_ALPHA` | `WATCH` | **MISSED_RUNNER** | +47.79% | -92.79% | +33.66% | +47.79% | -90.99% | `WATCH -> WATCH` |

## 6. Pipeline Activity, Slot Churn & Phase 0 Baseline (`action_log`)

### Action Type Counts (Pre-Soak vs. Active Soak)
| Action | Pre-Soak Count | Active Soak Count |
| :--- | ---: | ---: |
| `BUY` | 3 | 16 |
| `DECISION_1` | 2226 | 16841 |
| `EVICT` | 117 | 797 |
| `PARTIAL_SELL` | 1 | 0 |
| `SELL` | 3 | 16 |
| `WHALE_CONFLUENCE` | 28 | 0 |
| `WHALE_WEBHOOK` | 35 | 0 |

### Eviction, Gate Rejection & Block Breakdown
| Category | Pre-Soak Count | Active Soak Count |
| :--- | ---: | ---: |
| `DECISION_1_BLOCKED` | 301 | 21 |
| `LOW_SCORE_EVICT` | 55 | 484 |
| `SAFETY_GATE_REJECT` | 27 | 103 |
| `STALE_WATCH_15M` | 29 | 176 |
| `VAMP_EVICT_OR_STRIKE` | 6 | 34 |

- **Active Soak Slot Occupancy (`soak_minutes` across evaluations):** count=16841, median=5.89m, mean=6.77m, p90=13.81m, max=24.99m

### Top 20 Most Active Tokens in Active Soak (`action_log`)
| Symbol | Mint | Action Log Events |
| :--- | :--- | ---: |
| **FIONA** | `B34nXK..pump` | 2253 |
| **UHI** | `Bnw5JR..pump` | 2069 |
| **JIMMY** | `38M2z4..pump` | 1867 |
| **Zero** | `DTSkmo..pump` | 1417 |
| **NORFEATH** | `3gmFG4..pump` | 1190 |
| **BASKET** | `3Tg6Yn..JN6f` | 992 |
| **Pompom** | `5EmvWD..pump` | 648 |
| **Investor** | `J9eBkC..pump` | 431 |
| **PUMPCHAN** | `Fo2HAw..pump` | 397 |
| **PAIR** | `32rBxc..pump` | 382 |
| **Grodia** | `6HS1ae..pump` | 356 |
| **GTA6** | `4kjruq..zDnz` | 356 |
| **LENDR** | `AGazYi..Lend` | 331 |
| **JANTA** | `9tERTs..pump` | 283 |
| **RETARDIO** | `CSRRgT..pump` | 232 |
| **SIDOOR** | `zcRvzm..door` | 206 |
| **FEDORA** | `FVWXgq..Loat` | 193 |
| **CYBERLEEK** | `ApZuxd..pbKg` | 182 |
| **NCSP** | `816hX5..pump` | 179 |
| **Rizzmas** | `85cQsF..6mHg` | 175 |

## 7. Whale Pipeline (`whale_events`, `whale_signals`, `whale_decisions`)

- **Whale Events:** All-Time=365 (Unique Wallets=18) | Active Soak=0 (Unique Wallets=0)
- **Whale Signals Table Rows:** 88 | **Whale Decisions Table Rows:** All-Time=73, Active Soak=0
- **Top Active Soak Whale Mints:** *(none)*

### Active Soak Whale Decisions Breakdown
| Status : Reason | Count |
| :--- | ---: |
| *(none)* | 0 |

### Latest 20 Active Soak Whale Events
| Timestamp (UTC) | Side | Wallet | Mint | SOL Amount | Token Amount |
| :--- | :--- | :--- | :--- | ---: | ---: |
| *(no soak whale events)* | — | — | — | — | — |

## 8. Live State & Social Signals (`agent_state`, `active_watchlist`, `cooldowns`, `social_signals`, `x_worker_state`)

- **`agent_state` Snapshot:** focus=`SCANNING` | sol_balance=`7.064249` | daily_start_sol=`7.284616` | daily_pnl_sol=`-0.220367` | updated_at=`2026-11-26T19:15:58.440786+00:00`
- **`x_worker_state` Snapshot:** last_poll_utc=`2026-11-26T20:46:07.748478+00:00`
- **`active_watchlist` (4 slots):** **PI** (`GEUULM..pump`, setup=`NONE`), **JIMMY** (`38M2z4..pump`, setup=`NONE`), **Hoppy** (`CFX1gH..pump`, setup=`NONE`), **FIONA** (`B34nXK..pump`, setup=`NONE`)
- **`cooldowns` (15 active):** `VVV4Ud..8g8c` (SAFETY_REJECT: RugCheck: Mint Authority still enabled; Jupiter Shield: The authority's owner has the ability to mint more tokens), `3Tg6Yn..JN6f` (LOW_SCORE (narr=1.93, mom=1.59 < 2.0)), `9TKbd3..pump` (SAFETY_REJECT: RugCheck critical risk: Creator history of rugged tokens), `9Pq7aL..pump` (CONSECUTIVE_VAMP (0.78 >= 0.75, 2/2 strikes)), `9hjBLB..pump` (LOW_SCORE (narr=1.65, mom=1.10 < 2.0)), `DG2EJS..pump` (LOW_SCORE (narr=1.85, mom=1.52 < 2.0)), `9QT8K6..pump` (LOW_SCORE (narr=1.94, mom=0.78 < 2.0)), `J9eBkC..pump` (LOW_SCORE (narr=1.93, mom=1.79 < 2.0)), `96egra..XfuN` (SAFETY_REJECT: RugCheck: critical holder concentration (Top 10 holders high ownership); RugCheck critical risk: Single holder ownership; RugCheck critical risk: High ownership), `6ZbWPY..pump` (LOW_SCORE (narr=1.69, mom=1.10 < 2.0)), `EWY4ow..G5dv` (SAFETY_REJECT: RugCheck: Mint Authority still enabled; RugCheck critical risk: Permanent Control Enabled; RugCheck: Freeze Authority still enabled; RugCheck: critical holder concentration (Top 10 holders high ownership); RugCheck critical risk: Single holder ownership; RugCheck critical risk: High ownership; Jupiter Shield: This token's owner has the ability to control this token, even if it is in your wallet; Jupiter Shield: The authority's owner has the ability to freeze your token account, preventing you from further trading; Jupiter Shield: The authority's owner has the ability to mint more tokens), `B34nXK..pump` (STALE_WATCH (15.1m > 15m)), `TSPXcL..d99v` (SAFETY_REJECT: RugCheck: Mint Authority still enabled; RugCheck: Freeze Authority still enabled; RugCheck: Large Amount of LP Unlocked; Jupiter Shield: The authority's owner has the ability to freeze your token account, preventing you from further trading; Jupiter Shield: This token's transfer fees can be updated by the owner; Jupiter Shield: The authority's owner has the ability to mint more tokens), `CUg7Az..pump` (LOW_SCORE (narr=1.48, mom=1.67 < 2.0)), `ChYn9Z..pump` (CONSECUTIVE_VAMP (0.77 >= 0.75, 2/2 strikes))
- **`social_signals`:** Total=0 (Pre-Soak=0, Active Soak=0)

### Latest 10 Social Signals
| Last Seen (UTC) | Mint | Mentions | Unique Authors | Total Likes |
| :--- | :--- | ---: | ---: | ---: |
| *(none)* | — | 0 | 0 | 0 |
