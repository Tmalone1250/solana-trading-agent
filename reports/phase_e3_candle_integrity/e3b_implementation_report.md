# Phase E3B — Implementation Report

- **Implementation Branch:** `impl/e3b-cache-provenance`
- **Source Baseline Commit:** `6492acd4f39522aef71bb814bb567288f7b5993a` (`impl/e3a-candle-eligibility`)
- **Approved Design Commit:** `e40c4f2928019b0346d9985fed4ebe06ba005c83` (`design/e3b-cache-provenance`)
- **Full Diff Patch:** [e3b_full_diff.patch](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/reports/phase_e3_candle_integrity/e3b_full_diff.patch)
- **Test Results:** [e3b_test_results.md](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/reports/phase_e3_candle_integrity/e3b_test_results.md)

---

## 1. Changed-File Inventory

- **Application Source (5):**
  - [ohlcv_client.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/ohlcv_client.py) — `OhlcvSource`, `ELIGIBLE_OHLCV_SOURCES`, `OhlcvFetchResult`, `BreakoutTelemetryCacheEntry`, `evaluate_ohlcv_provenance()`, `select_ohlcv_interval_provenance()`, `_fetch_ohlcv_interval_with_provenance()`, `fetch_ohlcv_batch_with_provenance()`, `fetch_ohlcv_1m_batch_with_provenance()`, `cache_breakout_telemetry()`, `get_cached_breakout_telemetry_with_meta()`
  - [scalper_entry_engine.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/scalper_entry_engine.py) — `ohlcv_provenance` enforcement via `evaluate_ohlcv_provenance()` + candle-payload binding check (`OHLCV provenance payload mismatch`) in `evaluate_scalper_entry()`
  - [alpha_node.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/alpha_node.py) — provenance-aware batch fetching, runtime `_ohlcv_*_result` / `pre_attached_unknown` handling, `select_ohlcv_interval_provenance()`, direct `res_5m.candles` / `res_1m.candles` binding, and gated `cache_breakout_telemetry()`
  - [whale_lane.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/whale_lane.py) — provenance-aware batch fetching, direct `res_5m.candles` / `res_1m.candles` binding, `has_ohlcv = bool((c5 or c1) and prov_ok)` so stale OHLCV neither gates `WHALE_SCALP` nor overwrites `_breakout_telemetry_cache`
  - [telemetry_api.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/telemetry_api.py) — exposes `telemetry_age_seconds`, `telemetry_source`, and `telemetry_is_historical` via `get_cached_breakout_telemetry_with_meta()`
- **New E3B Tests (1):**
  - [tests/test_e3b_cache_provenance.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/tests/test_e3b_cache_provenance.py) (`B1`–`B16` acceptance, integration, and payload-binding coverage)
- **Updated Fixture Tests (11):** `tests/test_alpha_and_executor.py`, `tests/test_breakout_ignition.py`, `tests/test_breakout_isolation.py`, `tests/test_e1_pool_identity.py`, `tests/test_e3a_candle_eligibility.py`, `tests/test_helius_webhook.py`, `tests/test_jupiter_token_client.py`, `tests/test_leg2_predictor.py`, `tests/test_scalper_entry_engine.py`, `tests/test_soak_readiness.py`, `tests/test_x_discovery_worker.py`
- **Deliverables (3):** `e3b_implementation_report.md`, `e3b_full_diff.patch`, `e3b_test_results.md`

---

## 2. Provenance-to-Payload Binding Corrective Pass

- **Original Defect (`b59a2fc`):** In `alpha_node.py` and `whale_lane.py`, `candles_5m` / `candles_1m` were read from `cand["candles_5m"]` / `pair["candles_5m"]` separately from `cand.get("_ohlcv_5m_result")` / `pair.get("_ohlcv_5m_result")`, and `evaluate_scalper_entry()` did not verify that the evaluated candle array matched `ohlcv_provenance.candles`. A valid `OhlcvFetchResult` could therefore authenticate an unrelated breakout-triggering candle array.
- **Correction Applied:**
  1. `alpha_node.py` and `whale_lane.py` now always pass `res_5m.candles` and `res_1m.candles` (from the resolved `OhlcvFetchResult`) into `evaluate_scalper_entry()`, never independently supplied `cand["candles_*"]` / `pair["candles_*"]` arrays when `_ohlcv_*_result` is used.
  2. `evaluate_scalper_entry()` in `scalper_entry_engine.py` enforces exact payload equality between the evaluated interval (`candles_5m` when `len(closed_5m) >= 12`, or `candles_1m` on the 1m fallback path when `len(closed_1m) >= 15`) and `ohlcv_provenance.candles`, failing closed with `rejection_reason="OHLCV provenance payload mismatch"` on any mismatch.
- **Regression Coverage (`test_b16_provenance_payload_binding_blocks_mismatched_5m_1m_alpha_and_whale`):** Verifies 5m mismatch rejection, 1m fallback mismatch rejection, matching 5m/1m evaluations, `alpha_node` integration, and `whale_lane` integration.

---

## 3. Provenance Contract, 5m/1m Selection, & Telemetry Behavior

1. **Authoritative Provenance (`OhlcvFetchResult` + `evaluate_ohlcv_provenance()`):**
   - `OhlcvFetchResult` has no independently settable eligibility boolean. `evaluate_ohlcv_provenance()` validates `isinstance(..., OhlcvFetchResult)`, `source in {"network_200", "cache_fresh", "pre_attached_verified"}`, `last_status == 200`, finite positive `last_ok_at`, and `0 <= decision_timestamp - last_ok_at <= OHLCV_CACHE_SECONDS` (`90.0s`, with `5.0s` clock-skew tolerance).
   - `fetch_ohlcv_1m_batch_with_provenance()` returns `source="unavailable", candles=[]` when `should_fetch_1m_fallback()` is `False` (never returning unflagged `c1_cached`).
2. **5m/1m Selection (`select_ohlcv_interval_provenance()`):**
   - `ok_5m` and `>= 12` closed 5m bars → uses 5m.
   - `ok_5m` and `< 12` closed 5m bars → uses 1m only if permitted and `ok_1m` (`>= 15` closed 1m bars); otherwise fails closed on `err_1m`.
   - `not ok_5m` (`stale_*` / `unavailable`) → never uses stale 5m; only an independently supplied `pre_attached_verified` 1m `OhlcvFetchResult` (`ok_1m` + `>= 15` closed 1m bars) qualifies, otherwise fails closed.
3. **Cache & Telemetry Timestamp Behavior:**
   - `OHLCV_CACHE_SECONDS` (`90s`), `OHLCV_CACHE_MAX_TTL_SECONDS` (`600s`), 429 backoff, pacing, and strategy thresholds are unchanged.
   - `_breakout_telemetry_cache` stores `cached_at`, `ohlcv_last_ok_at`, and `ohlcv_source`; `get_cached_breakout_telemetry_with_meta()` computes `data_age_seconds` from `ohlcv_last_ok_at` and marks missing/invalid/future (`> now_ts + 5s`) timestamps as `data_age_seconds=None, is_trusted=False` without clamping future timestamps to `0.0`.
4. **E3A Intact:** All E3A closed-candle filtering, `validate_decision_timestamp()`, `normalize_and_filter_candles()`, and 900s-bucketed `_aggregate_to_15m()` behavior remain intact (`63 passed` across focused suites; `161 passed, 3 failed` in full `tests/` suite with the 3 pre-existing baseline failures).
