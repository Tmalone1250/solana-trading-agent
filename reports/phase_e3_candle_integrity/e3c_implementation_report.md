# Phase E3C — Implementation Report

- **Implementation Branch:** `impl/e3c-parser-cache-integrity`
- **E3B Baseline Commit:** `35ef3b77163e302f564e0133c9e4a216490aae1d` (`impl/e3b-cache-provenance`)
- **Full Diff Patch:** [e3c_full_diff.patch](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/reports/phase_e3_candle_integrity/e3c_full_diff.patch)
- **Test Results:** [e3c_test_results.md](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/reports/phase_e3_candle_integrity/e3c_test_results.md)

---

## 1. Changed Files & Functions

- **Application Source (1):**
  - [ohlcv_client.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/ohlcv_client.py): `parse_geckoterminal_ohlcv_strict()`, `parse_geckoterminal_ohlcv()`, `_fetch_ohlcv_interval_with_provenance()`
- **Tests (1):**
  - [tests/test_e3c_parser_cache_admission.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/tests/test_e3c_parser_cache_admission.py) (`C1`–`C9` parser, cache-admission, `alpha_node`, `whale_lane`, and `insider_scanner` integration tests)
- **Deliverables (3):** `e3c_implementation_report.md`, `e3c_full_diff.patch`, `e3c_test_results.md`

---

## 2. Parser Validation Rules (`parse_geckoterminal_ohlcv_strict` / `parse_geckoterminal_ohlcv`)

1. **Structural Safety:** Validates `isinstance(..., dict)` on `payload`, `payload["data"]`, and `data["attributes"]`. Distinguishes missing/`None`/`[]` `ohlcv_list` (`struct_ok=True, had_rows=False`) from non-list `ohlcv_list` or non-dict parent objects (`struct_ok=False`).
2. **Per-Row Isolation:** Reuses E3A's `_parse_valid_candle()` from `breakout_ignition.py`:
   - Rejects non-list/tuple rows and rows with `< 6` fields; extra fields beyond index 5 are ignored without shifting indices `0..5`.
   - Rejects `bool`, `None`, non-numeric, `NaN`/`inf`, `<= 0` timestamps; scales `>= 1e12` ms timestamps to seconds.
   - Rejects `bool`, non-numeric, `NaN`/`inf`, `<= 0` OHLC prices, `high < max(open, close, low)`, `low > min(open, close, high)`, and negative/non-finite volumes (preserving `0.0` volume).
   - Malformed individual rows are skipped without aborting valid sibling rows.
3. **Deduplication & Ordering:** Last-valid-row-wins deduplication by normalized `timestamp`, sorted oldest-first.

---

## 3. HTTP 200 Cache Admission Behavior (Before vs. After)

| HTTP 200 Scenario | Before E3C (`35ef3b7`) | After E3C |
| :--- | :--- | :--- |
| Valid rows (or partial valid with 1 bad row) | Single bad row threw `TypeError`/`ValueError` and aborted entire batch to `stale_error` | Bad rows skipped; valid bars deduplicated, sorted, and admitted (`last_ok=True, last_status=200, source="network_200"`) |
| Empty `ohlcv_list: []` or missing `ohlcv_list` | Overwrote `prev_candles` with `[]`, refreshed `last_ok=True, last_ok_at=check_ts`, served `cache_fresh` for `90s` | Rejected from positive cache: preserves `prev_candles` and `prev_ok_at`, sets `last_ok=False`, returns `unavailable` (no prior cache) or `stale_error` (with prior cache), never resets `429` backoff |
| Malformed JSON structure or all-invalid non-empty `ohlcv_list` | Non-dict raised `AttributeError`; corrupted numeric rows (`NaN`/`<=0`/dup) were admitted as `last_ok=True` | Preserves `prev_candles` and `prev_ok_at`, sets `last_ok=False, last_status=0, source="stale_error"`, never resets `429` backoff |

---

## 4. Preserved Contracts & Isolation

- **E3A & E3B Intact:** Closed-candle filtering, `validate_decision_timestamp()`, `normalize_and_filter_candles()`, 900s-bucketed `_aggregate_to_15m()`, `OhlcvFetchResult`, `evaluate_ohlcv_provenance()`, `select_ohlcv_interval_provenance()`, `OHLCV_CACHE_SECONDS` (`90s`), `600s` retention, `429` backoff, `6.5s` pacing, `should_fetch_1m_fallback()`, and all strategy thresholds remain unchanged (`72 passed` in focused suites; `170 passed, 3 failed` in full `tests/` suite with the 3 pre-existing baseline failures).
- **Untouched State:** `dev_1`, `dev_2`, `impl/e3a-candle-eligibility`, `impl/e3b-cache-provenance`, `.env`, databases, and deployment files were not modified.
