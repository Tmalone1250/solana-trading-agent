# Solana Trading Agent — Post-Soak Telemetry & Audit Report

## 1. Header & Executive Summary

- **Generated At (UTC):** `2026-11-26T20:32:03.405391+00:00`
- **Soak Cutoff (`--since`):** `2026-11-26T03:15:45+00:00`
- **Latest Telemetry Timestamp (UTC):** `2026-11-26T00:55:36.019490+00:00`
- **Active Soak Duration Elapsed:** **0h 0m** (`0.00` hours)
- **Input Source:** CSV Directory (`/home/tmalone1250/solana-trading-agent/reports`)

### Headline Active Soak KPIs
| KPI | Active Soak Value |
| :--- | ---: |
| Soak Trade Events (`BUY` / Exits) | 0 (0 Entries / 0 Exits) |
| Soak Win Rate (Closed/Partial Exits) | 0.0% (0/0) |
| Soak Net Realized PnL (SOL) | +0.0000 SOL |
| Non-NONE Setups / Breakouts Triggered | 0 |
| Unique Tokens Evaluated by Alpha | 0 |
| Active Soak Whale Events Recorded | 0 |

## 2. Table Inventory & Schema Overview

| Table Name | Present | Total Rows | Pre-Soak Rows | Active Soak Rows | Earliest Timestamp (UTC) | Latest Timestamp (UTC) | Columns |
| :--- | :--- | ---: | ---: | ---: | :--- | :--- | :--- |
| `agent_state` | `YES` | 1 | 1 | 0 | `2026-11-26T00:00:00.624865+00:00` | `2026-11-26T00:00:00.624865+00:00` | `id`, `focus_state`, `sol_balance`, `daily_start_sol`, `daily_pnl_sol`, `daily_reset_date`, `active_position_json`, `last_decision_rationale`, `updated_at`, `strategy`, `triggering_wallets_json`, `whale_shadow_exit_requested`, `whale_position_json`, `chart_positions_json`, `whale_positions_json` |
| `active_watchlist` | `YES` | 4 | 4 | 0 | `2026-11-25T23:44:04.632240+00:00` | `2026-11-26T00:08:32.830082+00:00` | `mint`, `symbol`, `pair_address`, `added_at`, `safety_passed`, `entry_liquidity_usd`, `peak_mc`, `low_mc`, `leg2_score`, `leg2_breakdown_json`, `vamp_strikes`, `rsi_5m`, `setup_type`, `scalper_eligible`, `dist_from_1h_high_pct`, `last_evaluated_at`, `latest_scores_json`, `candidate_json`, `discovery_source`, `jup_organic_score`, `jup_meta_json` |
| `cooldowns` | `YES` | 12 | 12 | 0 | `2026-11-26T00:06:56.332475+00:00` | `2026-11-26T00:55:36.019490+00:00` | `mint`, `expires_at`, `reason` |
| `trades` | `YES` | 339 | 339 | 0 | `2026-11-20T02:06:04.484551+00:00` | `2026-11-25T23:55:36.067475+00:00` | `id`, `timestamp`, `mode`, `side`, `mint`, `symbol`, `in_amount`, `out_amount`, `sol_value`, `pnl_sol`, `pnl_pct`, `reason`, `tx_signature`, `rationale`, `strategy`, `triggering_wallets_json`, `opened_at`, `jupiter_price_impact_raw` |
| `decisions_log` | `YES` | 1089 | 1089 | 0 | `2026-11-25T23:27:36.410111+00:00` | `2026-11-26T00:08:54.252411+00:00` | `id`, `timestamp`, `mint`, `symbol`, `phase`, `action`, `confidence`, `scores_json`, `rationale` |
| `action_log` | `YES` | 20095 | 20095 | 0 | `2026-11-25T06:21:54.499082+00:00` | `2026-11-26T00:08:53.371887+00:00` | `id`, `timestamp_utc`, `bot`, `mode`, `action`, `symbol`, `asset_id`, `qty`, `notional`, `unit`, `price_or_value`, `pnl_amount`, `pnl_pct`, `strategy`, `reason`, `sol_equity`, `daily_realized_sol`, `circuit_breaker`, `mc_usd`, `liq_usd`, `soak_minutes`, `rsi`, `leg2_score`, `narrative_score`, `momentum_score`, `vamp_score`, `buy_confidence`, `peak_pnl_pct`, `hold_seconds`, `triggering_whales`, `whale_sol_flow`, `breakout_score`, `breakout_state`, `breakout_timeframe`, `breakout_vol_ratio`, `breakout_ema_ext_pct`, `breakout_failed_gates`, `jupiter_price_impact_raw`, `monitor_sol_value`, `price_source`, `position_mfe_pct`, `position_mae_pct` |
| `candidate_outcomes` | `YES` | 3000 | 3000 | 0 | `2026-11-22T19:03:15.032461+00:00` | `2026-11-26T00:08:53.371887+00:00` | `id`, `mint`, `symbol`, `source`, `discovered_at`, `decision`, `decision_reason`, `strategy`, `entry_price_usd`, `entry_mc_usd`, `entry_liq_usd`, `breakout_score`, `breakout_state`, `leg2_score`, `vamp_score`, `rsi`, `whale_sol_flow`, `triggering_wallet`, `price_1m_pct`, `price_5m_pct`, `price_15m_pct`, `price_30m_pct`, `mfe_pct`, `mae_pct`, `finalized` |
| `whale_events` | `YES` | 633 | 633 | 0 | `2026-11-24T00:04:24+00:00` | `2026-11-26T00:08:19+00:00` | `signature`, `wallet`, `mint`, `side`, `sol_amount`, `token_amount`, `timestamp_utc` |
| `whale_signals` | `YES` | 196 | 196 | 0 | `2026-11-24T00:04:24+00:00` | `2026-11-26T00:08:19+00:00` | `mint`, `buy_count`, `unique_whales`, `total_sol_spent`, `sell_count`, `last_seen_utc` |
| `whale_decisions` | `YES` | 124 | 124 | 0 | `2026-11-24T00:05:25.627851+00:00` | `2026-11-26T00:08:53.820358+00:00` | `mint`, `status`, `reason`, `updated_at`, `breakout_telemetry_json` |
| `social_signals` | `YES` | 48 | 48 | 0 | `2026-11-24T02:39:52.454167+00:00` | `2026-11-24T17:11:49.422046+00:00` | `mint`, `mention_count`, `unique_authors`, `total_likes`, `top_snippets_json`, `last_seen_utc` |
| `x_seen_tweets` | `YES` | 585 | 585 | 0 | `2026-11-20T23:19:01.215713+00:00` | `2026-11-24T17:11:49.422046+00:00` | `tweet_id`, `fetched_at_utc`, `author_username`, `author_followers`, `like_count`, `text`, `extracted_mints` |
| `x_worker_state` | `YES` | 1 | 1 | 0 | `2026-11-25T23:52:00.446515+00:00` | `2026-11-25T23:52:00.446515+00:00` | `id`, `last_poll_utc` |

## 3. Trades Performance (`trades`)

### Side-by-Side Window Summary
| Metric | Pre-Soak Baseline | Active Soak Window |
| :--- | ---: | ---: |
| Total Trade Events (Rows) | 339 | 0 |
| Entries (`BUY`) | 162 | 0 |
| Exits (`PARTIAL_SELL` + `SELL`) | 177 | 0 |
| Currently Open Positions | 0 | 0 |
| Winning Exits / Win Rate | 57 (32.2%) | 0 (0.0%) |
| Net Realized PnL (SOL) | -1.9771 SOL | +0.0000 SOL |
| Average Exit PnL (SOL / %) | -0.0112 SOL (-1.74%) | +0.0000 SOL (+0.00%) |
| Exit PnL % (Min / Median / P90 / Max) | -68.00% / -1.95% / +8.52% / +74.46% | +0.00% / +0.00% / +0.00% / +0.00% |
| Position MFE % (Min / Median / Mean / P90 / Max) | -2.83% / +6.54% / +13.17% / +22.87% / +98.38% | +0.00% / +0.00% / +0.00% / +0.00% / +0.00% |
| Position MAE % (Min / Median / Mean / P90 / Max) | -14.39% / -6.75% / -6.05% / -1.62% / +0.06% | +0.00% / +0.00% / +0.00% / +0.00% / +0.00% |

### Breakdown by Strategy / Setup Type
| Strategy / Setup | Pre-Soak Exits | Pre-Soak Net SOL | Active Soak Exits | Active Soak Net SOL |
| :--- | ---: | ---: | ---: | ---: |
| `BREAKOUT_IGNITION` | 9 | +0.0397 | 0 | +0.0000 |
| `DISPLACEMENT_RETEST` | 7 | -0.4402 | 0 | +0.0000 |
| `LEG2_ENTRY` | 46 | -0.8536 | 0 | +0.0000 |
| `SCALPER` | 7 | +0.0364 | 0 | +0.0000 |
| `SWEEP_RECLAIM` | 29 | -0.3450 | 0 | +0.0000 |
| `WHALE_SCALP` | 79 | -0.4145 | 0 | +0.0000 |

### Breakdown by Exit Reason
| Exit Reason | Pre-Soak Count | Active Soak Count |
| :--- | ---: | ---: |
| `BREAKEVEN_SHIELD` | 9 | 0 |
| `EXODUS_LIQ_DROP` | 2 | 0 |
| `EXODUS_SELL_FLOW` | 29 | 0 |
| `HARD_STOP_LOSS` | 50 | 0 |
| `PARTIAL_TP_50PCT` | 5 | 0 |
| `TRAILING_TP` | 16 | 0 |
| `WHALE_BREAKEVEN_SHIELD` | 4 | 0 |
| `WHALE_HARD_STOP` | 2 | 0 |
| `WHALE_PARTIAL_TP_50PCT` | 3 | 0 |
| `WHALE_PEAK_TRAIL` | 15 | 0 |
| `WHALE_SHADOW_EXIT` | 9 | 0 |
| `WHALE_TIME_STOP` | 33 | 0 |

### Active Soak Window Trade Ledger (0 rows)
| Timestamp (UTC) | Side | Symbol | Mint | Setup | SOL Value | PnL % | PnL SOL | MFE % | MAE % | Status | Reason |
| :--- | :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | :--- | :--- |
| *(no trades in soak window)* | — | — | — | — | — | — | — | — | — | — | — |

## 4. Alpha Decisions & Stage 3 Verification (`decisions_log`)

### Action Breakdown (Pre-Soak vs. Active Soak)
| Action | Pre-Soak Count | Active Soak Count |
| :--- | ---: | ---: |
| `BUY` | 272 | 0 |
| `REJECT` | 17 | 0 |
| `WATCH` | 800 | 0 |

### Setup Type Breakdown (Pre-Soak vs. Active Soak)
| Setup Type | Pre-Soak Count | Active Soak Count |
| :--- | ---: | ---: |
| `DISPLACEMENT_RETEST` | 22 | 0 |
| `NONE` | 1049 | 0 |
| `SWEEP_RECLAIM` | 18 | 0 |

### Score Distributions (Pre-Soak vs. Active Soak)
| Metric | Window | Count | Min | Median | Mean | P90 | Max |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| `narrative` | Pre-Soak | 1089 | 1.13 | 2.31 | 2.26 | 2.84 | 3.15 |
| `narrative` | Active Soak | 0 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| `momentum` | Pre-Soak | 1089 | 0.70 | 2.50 | 2.42 | 3.79 | 4.80 |
| `momentum` | Active Soak | 0 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| `vamp` | Pre-Soak | 1089 | 0.21 | 0.48 | 0.48 | 0.73 | 0.73 |
| `vamp` | Active Soak | 0 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| `leg2` | Pre-Soak | 1089 | 19.10 | 62.00 | 57.57 | 86.30 | 100.00 |
| `leg2` | Active Soak | 0 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| `rsi` | Pre-Soak | 1089 | 26.76 | 58.89 | 59.57 | 75.45 | 100.00 |
| `rsi` | Active Soak | 0 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| `confidence` | Pre-Soak | 1089 | 0.47 | 0.68 | 0.68 | 0.79 | 0.86 |
| `confidence` | Active Soak | 0 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |

### Stage 3 Unfrozen-Momentum Verification Table (Top 25 Soak Tokens)
| Symbol | Mint | Evals | Cache Hits | Distinct Momentum | Momentum Min..Max | Narrative Min..Max | Actions | Non-NONE Setups |
| :--- | :--- | ---: | ---: | ---: | :--- | :--- | :--- | :--- |
| *(no soak decisions)* | — | 0 | 0 | 0 | — | — | — | — |

### Complete Soak Breakout & Setup Log (0 non-NONE evaluations; showing 0)
| Timestamp (UTC) | Symbol | Mint | Setup | Eligible | Action | Approved | Mom | Narr | RSI | Leg2 | BO Score/State |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | :--- |
| *(no non-NONE setups in soak window)* | — | — | — | — | — | — | — | — | — | — | — |

## 5. Candidate Outcomes & Missed-Opportunity Telemetry (`candidate_outcomes`)

### Forward Return & MFE/MAE Distributions (Pre-Soak vs. Active Soak)
| Metric | Window | Count | Min % | Median % | Mean % | P90 % | Max % |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| `mfe_pct` | Pre-Soak | 2935 | +0.00% | +4.89% | +15.26% | +33.51% | +597.81% |
| `mfe_pct` | Active Soak | 0 | +0.00% | +0.00% | +0.00% | +0.00% | +0.00% |
| `mae_pct` | Pre-Soak | 2935 | -99.76% | -5.92% | -13.25% | +0.00% | +0.00% |
| `mae_pct` | Active Soak | 0 | +0.00% | +0.00% | +0.00% | +0.00% | +0.00% |
| `price_1m_pct` | Pre-Soak | 2922 | -99.71% | +0.00% | +0.53% | +5.62% | +174.03% |
| `price_1m_pct` | Active Soak | 0 | +0.00% | +0.00% | +0.00% | +0.00% | +0.00% |
| `price_5m_pct` | Pre-Soak | 2875 | -99.72% | -0.09% | +0.36% | +9.60% | +347.03% |
| `price_5m_pct` | Active Soak | 0 | +0.00% | +0.00% | +0.00% | +0.00% | +0.00% |
| `price_15m_pct` | Pre-Soak | 2805 | -99.72% | -0.50% | -0.22% | +12.99% | +497.57% |
| `price_15m_pct` | Active Soak | 0 | +0.00% | +0.00% | +0.00% | +0.00% | +0.00% |
| `price_30m_pct` | Pre-Soak | 2690 | -99.76% | -1.09% | -1.48% | +17.45% | +520.59% |
| `price_30m_pct` | Active Soak | 0 | +0.00% | +0.00% | +0.00% | +0.00% | +0.00% |

### Top 25 Active Soak Candidates Ranked by MFE % (Highlighting Missed Runners)
| Rank | Discovered At (UTC) | Symbol | Mint | Source | Decision | Status Tag | MFE % | MAE % | 5m % | 15m % | 30m % | Reason |
| ---: | :--- | :--- | :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | :--- |
| — | *(no soak candidate outcomes)* | — | — | — | — | — | — | — | — | — | — | — |

## 6. Pipeline Activity, Slot Churn & Phase 0 Baseline (`action_log`)

### Action Type Counts (Pre-Soak vs. Active Soak)
| Action | Pre-Soak Count | Active Soak Count |
| :--- | ---: | ---: |
| `BUY` | 25 | 0 |
| `DECISION_1` | 19019 | 0 |
| `EVICT` | 798 | 0 |
| `PARTIAL_SELL` | 3 | 0 |
| `SELL` | 25 | 0 |
| `WHALE_CONFLUENCE` | 31 | 0 |
| `WHALE_WEBHOOK` | 194 | 0 |

### Eviction, Gate Rejection & Block Breakdown
| Category | Pre-Soak Count | Active Soak Count |
| :--- | ---: | ---: |
| `DECISION_1_BLOCKED` | 1487 | 0 |
| `LOW_SCORE_EVICT` | 288 | 0 |
| `SAFETY_GATE_REJECT` | 201 | 0 |
| `STALE_WATCH_15M` | 258 | 0 |
| `VAMP_EVICT_OR_STRIKE` | 51 | 0 |

- **Active Soak Slot Occupancy (`soak_minutes` across evaluations):** count=0, median=0.00m, mean=0.00m, p90=0.00m, max=0.00m

### Top 20 Most Active Tokens in Active Soak (`action_log`)
| Symbol | Mint | Action Log Events |
| :--- | :--- | ---: |
| *(none)* | — | 0 |

## 7. Whale Pipeline (`whale_events`, `whale_signals`, `whale_decisions`)

- **Whale Events:** All-Time=633 (Unique Wallets=22) | Active Soak=0 (Unique Wallets=0)
- **Whale Signals Table Rows:** 196 | **Whale Decisions Table Rows:** All-Time=124, Active Soak=0
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

- **`agent_state` Snapshot:** focus=`SCANNING` | sol_balance=`7.284616` | daily_start_sol=`7.284616` | daily_pnl_sol=`0.0` | updated_at=`2026-11-26T00:00:00.624865+00:00`
- **`x_worker_state` Snapshot:** last_poll_utc=`2026-11-25T23:52:00.446515+00:00`
- **`active_watchlist` (4 slots):** **Marvin** (`HkRQ6Q..pump`, setup=`NONE`), **NORFEATH** (`3gmFG4..pump`, setup=`NONE`), **UHI** (`Bnw5JR..pump`, setup=`NONE`), **4CHAN** (`DVdWuN..pump`, setup=`NONE`)
- **`cooldowns` (12 active):** `B34nXK..pump` (EXODUS_SELL_FLOW (5m sells=68.8% >= 65%)), `38M2z4..pump` (EXODUS_SELL_FLOW (5m sells=65.2% >= 65%)), `5EmvWD..pump` (HARD_STOP_LOSS (-13.37% <= -12%)), `2ndz2h..pump` (STALE_WATCH (15.1m > 15m)), `3Tg6Yn..JN6f` (STALE_WATCH (15.3m > 15m)), `4kjruq..zDnz` (STALE_WATCH (15.1m > 15m)), `Bv1a7c..BMmr` (LOW_SCORE (narr=1.65, mom=1.10 < 2.0)), `6HS1ae..pump` (LOW_SCORE (narr=1.79, mom=1.40 < 2.0)), `BQTG5q..TQu6` (STALE_WATCH (15.1m > 15m)), `VVV4Ud..8g8c` (SAFETY_REJECT: RugCheck: Mint Authority still enabled; Jupiter Shield: The authority's owner has the ability to mint more tokens), `AGazYi..Lend` (LOW_SCORE (narr=1.95, mom=1.29 < 2.0)), `9msLfY..pump` (LOW_SCORE (narr=1.27, mom=1.84 < 2.0))
- **`social_signals`:** Total=48 (Pre-Soak=48, Active Soak=0)

### Latest 10 Social Signals
| Last Seen (UTC) | Mint | Mentions | Unique Authors | Total Likes |
| :--- | :--- | ---: | ---: | ---: |
| `2026-11-24T17:11:49.422046+00:00` | `31DZg5..pump` | 3 | 1 | 32 |
| `2026-11-24T17:11:49.422046+00:00` | `6nQCcu..pump` | 7 | 7 | 170 |
| `2026-11-24T17:11:49.422046+00:00` | `F1GiK5..pump` | 1 | 1 | 21 |
| `2026-11-24T16:41:46.994920+00:00` | `6fEQZk..ts6D` | 13 | 1 | 145 |
| `2026-11-24T16:41:46.994920+00:00` | `7VRdgA..sKQh` | 1 | 1 | 6 |
| `2026-11-24T16:41:46.994920+00:00` | `4jPVfK..pump` | 1 | 1 | 8 |
| `2026-11-24T16:41:46.994920+00:00` | `2M3CsC..pump` | 1 | 1 | 59 |
| `2026-11-24T16:41:46.994920+00:00` | `8wbxHY..pump` | 1 | 1 | 15 |
| `2026-11-24T16:41:46.994920+00:00` | `DiaLLB..nqRZ` | 1 | 1 | 18 |
| `2026-11-24T16:11:43.470473+00:00` | `GUtUrP..JxEw` | 2 | 1 | 29 |
