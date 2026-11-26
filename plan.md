# Plan — Repo-Native DuckDB Soak Audit Report Exporter

## Files (< 500 lines each)
1. `scripts/soak_report_sections.py`:
   - Section 3: Trades Performance (`trades` + `action_log` MFE/MAE enrichment, Pre-Soak vs. Active Soak side-by-side table, setup & exit-reason breakdowns, MFE/MAE stats, full Active Soak trade ledger).
   - Section 4: Alpha Decisions & Stage 3 Verification (`decisions_log` action/setup counts, score distributions, Top-25 Stage 3 Unfrozen-Momentum Verification table, Complete Breakout Log capped at 150 rows).
   - Section 5: Candidate Outcomes & Missed-Opportunity Telemetry (`candidate_outcomes` Pre-Soak vs. Soak MFE/MAE/1m/5m/15m/30m distributions, Top-25 soak candidates by MFE highlighting missed runners).
   - Section 6: Pipeline Activity, Slot Churn & Phase 0 Baseline (`action_log` event/eviction/block breakdown, Top-20 most active tokens, slot churn & cadence metrics).
   - Section 7: Whale Pipeline (`whale_events`, `whale_signals`, `whale_decisions` counts, unique wallets, top mints, status/reason breakdown, latest 20 soak rows).
   - Section 8: Live State & Social Signals (`agent_state`, `active_watchlist`, `cooldowns`, `x_worker_state`, `social_signals`, `x_seen_tweets`).
2. `scripts/export_soak_report.py`:
   - Read-only CLI supporting `--csv-dir` and `--db` (copies `.duckdb` + `.duckdb.wal` to temporary dir and cleans up in `finally:`), `--since` (default `2026-11-25T20:51:57Z`), and `--out` (default `./soak_audit_report.md`).
   - Renders Section 1 (Header & Executive Summary) and Section 2 (Table Inventory & Schema Overview).
3. `tests/test_export_soak_report.py`:
   - Tests `--csv-dir` and `--db` modes, `--since` splitting, missing/empty tables, and Stage 3 momentum-variance + breakout log rendering.
