# Phase E3 — Candle Integrity & Cache Freshness Read-Only Audit Report

- **Repository:** `Tmalone1250/solana-trading-agent`
- **Source Branch:** `dev_2`
- **Source Commit SHA:** `19104e32e0ac619a19f8586ae3e27d8707af62c0`
- **Audit Mode:** Strictly read-only (no application source, configuration, database, or deployment modifications)
- **Target Scope:** Complete OHLCV candle retrieval, caching, validation, and decision data path for `BREAKOUT_IGNITION` (plus shared callers)

---

## 1. Executive Summary

The `BREAKOUT_IGNITION` pipeline retrieves 5m and 1m OHLCV bars from GeckoTerminal (`ohlcv_client.py`), caches them in memory (`_ohlcv_cache`), normalizes them in `scalper_entry_engine.py`, and evaluates `candles[-1]` as the ignition bar in `breakout_ignition.py`.

**Core finding:** The system has **no candle-closure enforcement, no candle-timestamp freshness validation, and no stale-cache distinction** anywhere in the retrieval, caching, or decision layers. In particular:
1. `detect_breakout_ignition()` evaluates `candles[-1]` without verifying whether that bar has closed, so a still-forming 5m or 1m candle can trigger a `BUY` decision.
2. `ohlcv_client.py` serves stale cached candles (up to 600s old under `OHLCV_CACHE_MAX_TTL_SECONDS`, or within the 90s `OHLCV_CACHE_SECONDS` window) during HTTP 429 backoff or network errors, and `fetch_ohlcv_batch()` / `fetch_ohlcv_1m_batch()` strip all cache/freshness metadata—returning plain `list[dict[str, float]]` indistinguishable from fresh 200 OK data.
3. Neither `ohlcv_client.py`, `scalper_entry_engine.py`, nor `breakout_ignition.py` checks `candles[-1]["timestamp"]` against `now_ts`, checks for missing/gapped intervals, or deduplicates timestamps.

As a result, the system **cannot distinguish** between:
- Fresh closed candles
- Fresh still-forming candles
- Stale cached candles (from 90s TTL cache or 429/error fallback)

Only **missing/empty** candles (`len(candles) < 12` for 5m and `< 15` for 1m) are rejected by `detect_breakout_ignition()`.

---

## 2. Files and Functions Inspected

| File | Functions / Symbols Inspected | Purpose in Audit |
| :--- | :--- | :--- |
| `ohlcv_client.py` | `_ohlcv_cache` (`L27`), `_breakout_telemetry_cache` (`L30`), `clear_ohlcv_cache` (`L51-58`), `cache_breakout_telemetry` (`L66-83`), `get_cached_breakout_telemetry` (`L86-105`), `get_cached_ohlcv_for_pool` (`L108-115`), `should_fetch_1m_fallback` (`L118-135`), `_prune_ohlcv_cache` (`L137-144`), `parse_geckoterminal_ohlcv` (`L147-168`), `_fetch_ohlcv_interval` (`L171-289`), `fetch_ohlcv_5m` (`L291-301`), `fetch_ohlcv_1m` (`L304-314`), `fetch_ohlcv_batch` (`L317-331`), `fetch_ohlcv_1m_batch` (`L334-352`) | GeckoTerminal OHLCV fetching, global pacing, 429 backoff, caching, and 1m fallback gating |
| `breakout_ignition.py` | `BreakoutTelemetry` (`L10-19`), `BreakoutIgnitionResult` (`L22-39`), `compute_ema_series` (`L42-50`), `compute_vwap` (`L53-66`), `_aggregate_to_15m` (`L69-91`), `_is_htf_15m_bullish` (`L94-104`), `_extract_flow_and_accel` (`L107-121`), `detect_breakout_ignition` (`L124-365`) | Breakout setup scoring, hard vetoes, and candle index usage |
| `scalper_entry_engine.py` | `_normalize_candles` (`L25-52`), `compute_rsi` (`L55-77`), `compute_ema` (`L80-90`), `evaluate_scalper_entry` (`L185-321`) | Candle normalization, 5m vs. 1m fallback routing, RSI/order-flow/soak gates |
| `alpha_node.py` | `_resolve_pool_identity` (`L56-85`), `alpha_node` (`L88-360+`) | Batch OHLCV fetch, `evaluate_scalper_entry` invocation, `BREAKOUT_IGNITION` approval & telemetry caching |
| `alpha_helpers.py` | `compute_live_momentum_and_action` (`L100-156`), `build_batch_entry_prompt` (`L159-180+`) | Live momentum/action decision using `scalper.eligible` and `breakout_telemetry` |
| `scanner_node.py` | Watchlist refresh & backfill (`L515-590`) | Discovery/watchlist persistence (no OHLCV attached) |
| `safety_gate_node.py` | Candidate safety filtering (`L315-365`) | Passes `safe_candidates` without OHLCV |
| `whale_lane.py` | Whale confluence OHLCV & breakout gating (`L349-430`) | Shared OHLCV consumer for `WHALE_SCALP` breakout gating |
| `memory_guard.py` | `prune_timestamped_cache` (`L35-74`) | TTL & max-entry eviction for `_ohlcv_cache` and `_breakout_telemetry_cache` |
| `config.py` | OHLCV & breakout defaults (`L113-123`) | `OHLCV_CACHE_SECONDS=90.0`, `OHLCV_PACING_DELAY_SECONDS=6.5`, `BREAKOUT_*` thresholds |
| `telemetry_api.py` & `db.py` | `breakout_telemetry` surfacing (`telemetry_api.py:23,188,220-246`; `db.py:356-357,1353-1383`) | Telemetry API and DuckDB persistence of `breakout_telemetry` |
| `tests/test_breakout_ignition.py` | Full file (`L1-335`) | Breakout setup, 1m fallback, coiling retention, CSV/API telemetry tests |
| `tests/test_scalper_entry_engine.py` | OHLCV & scalper tests (`L180-450`) | Cache hit, 429 global backoff, 1m fallback gating, scalper entry tests |

---

## 3. Actual Candle Lifecycle and Data Flow

```text
scanner_node.py (L533-588)
  └─ Upserts watchlist entries (mint, pair_address, candidate JSON) — NO OHLCV attached
        │
        ▼
safety_gate_node.py (L320-365)
  └─ Produces safe_candidates — NO OHLCV attached
        │
        ▼
alpha_node.py (L120-165)
  ├─ Collects pairs where cand.get("candles_5m") is None (L123)
  ├─ fetch_ohlcv_batch(pairs_to_fetch, config, client, now_ts) (L134)
  │     └─ _fetch_ohlcv_interval(..., aggregate="5", limit="30") (ohlcv_client.py:171-289)
  │           ├─ If now_ts < _global_backoff_until -> returns prev_candles, False (L194-201)
  │           ├─ If entry.last_ok and age < OHLCV_CACHE_SECONDS (90s) -> returns prev_candles, False (L204-211)
  │           ├─ Acquires _get_gecko_lock(), enforces 6.5s spacing (L217-228)
  │           ├─ HTTP 429 -> sets global backoff 30s..120s, returns prev_candles, True (L238-259)
  │           ├─ HTTP 200 -> parse_geckoterminal_ohlcv() -> sorts oldest-first, caches, returns list, True (L262-272)
  │           └─ Exception -> returns prev_candles, True (L273-288)
  ├─ should_fetch_1m_fallback() + fetch_ohlcv_1m_batch() (L135-137)
  │     └─ Only fetches 1m if 5m entry.last_ok==True, last_status==200, len(5m)<12, no backoff
  ├─ evaluate_scalper_entry(candles_5m, cand, time_in_slot_min, config, candles_1m) (L161)
  │     ├─ _normalize_candles() -> sorts oldest-first (scalper_entry_engine.py:25-52)
  │     ├─ If len(5m) < 12 and len(1m) >= 15 -> detect_breakout_ignition(..., bar_minutes=1) (L208-236)
  │     ├─ Else if len(5m) < 12 -> eligible=False ("insufficient 5m candles") (L238-252)
  │     └─ Else -> detect_breakout_ignition(candles_5m, ..., bar_minutes=5) (L266)
  │           ├─ Evaluates curr = candles[-1] as the ignition candle (breakout_ignition.py:174)
  │           ├─ Uses candles[-5:-1] / [-4:-1] for coil, candles[-(lookback+1):-1] for swing high,
  │           │   candles[-6:-1] for avg_vol_5, all closes for EMA9/21, last 30 for VWAP,
  │           │   and _aggregate_to_15m(candles) for HTF 15m
  │           └─ Returns detected=True if score>=75, res_passed, vol_passed, !over_extended, rsi<=78
  └─ If (candles_5m or candles_1m) and sc_entry.breakout_telemetry -> cache_breakout_telemetry() (L162-163)
```

---

## 4. Confirmed Defects (with File Paths & Line References)

### Defect 1 (Critical): Latest Candle (`candles[-1]`) Is Not Guaranteed to Be Closed
- **Locations:**
  - `ohlcv_client.py:147-168` (`parse_geckoterminal_ohlcv`)
  - `scalper_entry_engine.py:208-266` (`evaluate_scalper_entry`)
  - `breakout_ignition.py:174-201` (`detect_breakout_ignition`)
- **Behavior:** GeckoTerminal's `/ohlcv/minute` endpoint returns the currently forming (unclosed) candle alongside historical closed bars. `parse_geckoterminal_ohlcv()` sorts bars oldest-first, placing the live/forming bar at `candles[-1]`. `detect_breakout_ignition()` directly evaluates `curr = candles[-1]` (`L174`) for `close > swing_high`, `body_pos_pct >= 0.65`, and `vol_ratio >= 1.5x` (`L191-201`).
- **Impact:** A mid-bar price spike on an unclosed 5m or 1m candle can satisfy `RES`, `VOL_SPIKE`, and `EXT`, producing a `BREAKOUT_IGNITION` `BUY` decision before the candle closes (allowing intrabar wick-backs to become false breakout entries). Conversely, early in a new bar when partial volume is low, `vol_ratio` is artificially depressed.

### Defect 2 (Critical): No Candle Timestamp Freshness Validation Against Current Time
- **Locations:**
  - `ohlcv_client.py:147-168, 171-289`
  - `scalper_entry_engine.py:25-52, 185-321`
  - `breakout_ignition.py:124-365`
- **Behavior:** `detect_breakout_ignition()` and `evaluate_scalper_entry()` do not accept `now_ts` and never inspect `c["timestamp"]` for staleness. `_fetch_ohlcv_interval()` only checks cache age (`current_ts - fetched_at < OHLCV_CACHE_SECONDS`), not the timestamp of the newest candle returned by the API or served from cache.
- **Impact:** Candles whose latest bar timestamp is hours or days old (e.g., an illiquid/halted pool or stale cached snapshot) are evaluated identically to live candles and can produce `BUY` decisions if paired with current `pair_data` flow metrics.

### Defect 3 (High): Stale Cached Candles Are Indistinguishable from Fresh Candles Downstream
- **Locations:**
  - `ohlcv_client.py:194-211, 238-288, 317-352`
  - `alpha_node.py:134-164`
  - `whale_lane.py:351-382`
- **Behavior:**
  - `fetch_ohlcv_5m()` / `fetch_ohlcv_1m()` return `(candles, did_network)` where `did_network` is `True` on **both** a fresh `200 OK` fetch (`L272`) and a `429` (`L259`) or exception (`L288`) that falls back to `prev_candles`.
  - `fetch_ohlcv_batch()` (`L329-330`) and `fetch_ohlcv_1m_batch()` (`L350-351`) discard even `did_network` and return `dict[str, list[dict[str, float]]]`.
  - `_ohlcv_cache` entries remain eligible for stale-while-revalidate serving up to `max(600.0, 2 * OHLCV_CACHE_SECONDS)` = **600 seconds (10 minutes)** (`ohlcv_client.py:18, 138`).
- **Impact:** During HTTP 429 backoff or transient API failures, `alpha_node.py` and `whale_lane.py` evaluate `BREAKOUT_IGNITION` on candles up to 10 minutes old without knowing they are stale, and can approve a `BUY` (`alpha_node.py:271`) or gate `WHALE_SCALP` (`whale_lane.py:410-429`) on stale OHLCV.

### Defect 4 (High): 1m Fallback Batch Silently Serves Stale Cached 1m Candles During Backoff / 5m Failure
- **Locations:** `ohlcv_client.py:118-135` (`should_fetch_1m_fallback`), `ohlcv_client.py:334-352` (`fetch_ohlcv_1m_batch`)
- **Behavior:** In `fetch_ohlcv_1m_batch()`, if `should_fetch_1m_fallback(addr, c5_cached, now_ts=now_ts)` returns `False` (for example, because `_global_backoff_until` is active or 5m failed), `L348` returns `c1_cached` directly from `_ohlcv_cache`.
- **Impact:** Stale 1m candles from an earlier cycle can be fed into `evaluate_scalper_entry(..., candles_1m=...)` even when 5m fetching failed or global backoff is active.

### Defect 5 (Medium): `_normalize_candles()` Fabricates Sequential Integer Timestamps When `"timestamp"` Is Missing and Does Not Deduplicate or Validate Intervals
- **Locations:** `scalper_entry_engine.py:25-52` (`_normalize_candles`), `ohlcv_client.py:147-168` (`parse_geckoterminal_ohlcv`)
- **Behavior:**
  - `scalper_entry_engine.py:32` uses `float(c.get("timestamp", idx))` when a dict lacks `"timestamp"`.
  - Neither `_normalize_candles()` nor `parse_geckoterminal_ohlcv()` deduplicates duplicate timestamps, validates expected bar spacing (`300s` for 5m / `60s` for 1m), or rejects non-finite (`NaN`/`inf`) or non-positive OHLC prices.
- **Impact:** Gapped or duplicated candles silently distort EMA, VWAP, `coil_bars = candles[-5:-1]`, `swing_window`, and `avg_vol_5 = candles[-6:-1]`.

### Defect 6 (Medium): Empty 200 OK `ohlcv_list` Is Cached as `last_ok=True` for 90 Seconds
- **Locations:** `ohlcv_client.py:261-272` (`_fetch_ohlcv_interval`)
- **Behavior:** If GeckoTerminal returns HTTP 200 with `ohlcv_list: []` (e.g., newly indexed pool), `_ohlcv_cache[cache_key]` records `last_ok: True, last_status: 200, candles: []` (`L265-270`). Subsequent calls within `OHLCV_CACHE_SECONDS` (90s) hit the fresh cache (`L204-211`) and skip network re-fetch.
- **Impact:** A newly listed pool whose OHLCV indexing lags by a few seconds is pinned to empty candles for up to 90s.

---

## 5. Potential Risks (Distinguished from Confirmed Defects)

These are architectural or operational risks observed in the code that are **not** bugs in isolation on synthetic inputs, but can degrade live decision integrity:

1. **Temporal Skew Between `candles` (GeckoTerminal) and `pair_data` (DexScreener):**
   - `alpha_node.py` evaluates `detect_breakout_ignition(candles, pair_data)` where `candles` may be cached up to 90s (or 600s under 429 fallback) while `pair_data` (`txns.m5`, `txns.h1`, `price_usd`) comes from the current scanner cycle. `buy_ratio_5m`, `buys_accel_ratio`, and `dist_from_1h_high_pct` can therefore reflect a different 5-minute window than `candles[-1]`.
2. **Global Rate-Limit Serialization Latency (`_get_gecko_lock`):**
   - `ohlcv_client.py:217-228` enforces `OHLCV_PACING_DELAY_SECONDS = 6.5`s globally per request. For a batch of 10 uncached pools + 1m fallbacks, a single `alpha_node` cycle can take >65–130s, during which `pair_data` and `now_dt` captured at `alpha_node.py:115` age relative to the later pools in the batch.
3. **30-Minute Telemetry Cache (`_breakout_telemetry_cache`):**
   - `ohlcv_client.py:23` sets `BREAKOUT_CACHE_TTL_SECONDS = 1800.0` (30m). While `alpha_node.py` does not use `_breakout_telemetry_cache` for `BUY` decisions, `whale_lane.py:382` and `telemetry_api.py:239` surface it in `WHALE_SCALP` decisions/UI when live OHLCV is unavailable, which can display outdated breakout scores/states in telemetry.
4. **Hardcoded `time_in_slot_min=5.0` in `whale_lane.py`:**
   - `whale_lane.py:381` passes `time_in_slot_min=5.0` to `evaluate_scalper_entry()`, bypassing the soak gate for `whale_lane` breakout telemetry computation (acceptable for whale confluence, but couples `whale_lane` to `scalper_entry_engine` defaults).

---

## 6. Existing Test Coverage and Missing Tests

### Existing Tests Executed (Read-Only)
Command executed after inspection:
```bash
./venv/bin/pytest tests/test_breakout_ignition.py tests/test_scalper_entry_engine.py tests/test_e1_pool_identity.py -q
```
Result: **`19 passed in 6.81s`**.

### What Existing Tests Cover
- **Candle sorting & basic cache/429 backoff (`tests/test_scalper_entry_engine.py:180-228`):** Verifies `parse_geckoterminal_ohlcv` sorts oldest-first, 90s cache hit avoids network, HTTP 429 triggers 30s global backoff and returns cached candles.
- **1m fallback gating (`tests/test_scalper_entry_engine.py:369-433`):** Verifies 5m 429 does not trigger 1m fallback, global backoff serves cached PoolB candles, and 200 OK with `< 12` 5m bars triggers 1m fallback.
- **Breakout setup logic (`tests/test_breakout_ignition.py:86-335`):** Verifies valid 5m breakout, 12% anti-chase rejection, upper-wick fakeout rejection, 1m fallback (`>= 15` bars), score/state capping (`COILING` vs `IGNITION` vs `EXTENDED`), and DuckDB/CSV telemetry serialization.
- **Pool identity validation (`tests/test_e1_pool_identity.py`):** Verifies missing/invalid `pair_address` blocks `BREAKOUT_IGNITION`.

### Missing Regression Coverage
1. **Unclosed / still-forming latest candle rejection:** No test checks that an in-progress `candles[-1]` (`timestamp + bar_seconds > now_ts`) is excluded or rejected before `detect_breakout_ignition`.
2. **Stale candle timestamp rejection:** No test verifies rejection when `candles[-1]["timestamp"]` is older than a maximum allowed age relative to `now_ts`.
3. **Stale cache vs. fresh candle distinction:** No test verifies that `BREAKOUT_IGNITION` `BUY` approval is blocked when OHLCV came from a 429/error fallback or expired cache.
4. **1m fallback during backoff with stale 1m cache:** No test covers `fetch_ohlcv_1m_batch` returning stale `c1_cached` when `should_fetch_1m_fallback` is `False`.
5. **Gapped, duplicate, or malformed candles:** No test covers duplicate timestamps, missing bar intervals, `NaN`/zero prices, or empty `ohlcv_list: []` caching.

---

## 7. Concrete Remediation Recommendations (Ordered by Severity)

1. **[Critical] Enforce Closed-Candle Filtering Before Breakout Evaluation:**
   - Determine candle closure explicitly using `candle["timestamp"] + bar_seconds <= now_ts` (with a small clock-skew tolerance if needed). Drop or refuse to evaluate an unclosed `candles[-1]` for `BREAKOUT_IGNITION` entry decisions (or separate forming-bar telemetry from closed-bar `detected=True` eligibility).
2. **[Critical] Propagate Explicit Cache/Freshness Metadata from `ohlcv_client.py`:**
   - Return a structured result (e.g., `OhlcvResult(candles, fetched_at, is_fresh, is_stale_fallback, last_status)`) from `fetch_ohlcv_5m`, `fetch_ohlcv_1m`, `fetch_ohlcv_batch`, and `fetch_ohlcv_1m_batch`.
   - Block `BREAKOUT_IGNITION` `BUY` approvals in `alpha_node.py` when `is_stale_fallback` is `True` or `(now_ts - fetched_at)` exceeds a strict entry freshness threshold.
3. **[Critical] Validate Latest Candle Timestamp Freshness Against `now_ts`:**
   - Pass `now_ts` into `evaluate_scalper_entry` / `detect_breakout_ignition` (or validate in `ohlcv_client.py` + `alpha_node.py`) and reject `BREAKOUT_IGNITION` when `now_ts - candles[-1]["timestamp"]` exceeds the maximum allowed staleness for 5m / 1m bars.
4. **[High] Prevent `fetch_ohlcv_1m_batch()` from Returning Unflagged Stale `c1_cached` During Backoff:**
   - When `should_fetch_1m_fallback()` is `False` due to backoff or 5m failure, return an explicit stale/unavailable result rather than silently returning `c1_cached`.
5. **[Medium] Harden Candle Parsing & Normalization:**
   - In `parse_geckoterminal_ohlcv()` and `_normalize_candles()`: validate finite positive OHLC values and non-negative volume, deduplicate by `timestamp` (keeping latest), reject missing timestamps rather than substituting `idx`, and detect/flag large bar gaps.
   - Avoid caching empty `ohlcv_list: []` for the full 90s `OHLCV_CACHE_SECONDS` (or use a short negative-cache TTL, e.g., 15–20s).

---

## 8. Limitations

- **Live External APIs:** No live GeckoTerminal, DexScreener, or Jupiter HTTP requests were made during this read-only audit; GeckoTerminal payload shape and forming-bar behavior were verified from `ohlcv_client.py:parse_geckoterminal_ohlcv` and existing mock-transport tests (`tests/test_scalper_entry_engine.py`).
- **Historical Candle Replay:** Raw OHLCV candles are not persisted in DuckDB (`ohlcv_client.py` uses in-memory `_ohlcv_cache` only), so historical live-run candle closure/staleness incidence could not be measured from `solana_meme_agent.duckdb`.
- **Remote Git Access:** `git ls-remote origin` was not completed in the preflight because `~/.ssh/id_ed25519` requires an interactive passphrase.
