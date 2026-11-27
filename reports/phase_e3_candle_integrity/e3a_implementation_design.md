# Phase E3A — Candle Eligibility Implementation Design

- **Repository:** `Tmalone1250/solana-trading-agent`
- **Audited Source Branch / Commit:** `dev_2` @ `19104e32e0ac619a19f8586ae3e27d8707af62c0`
- **Mode:** Strictly read-only audit & design (no source, configuration, test, database, branch, or commit modifications)
- **Prerequisite Audit:** [e3_readonly_audit_report.md](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/reports/phase_e3_candle_integrity/e3_readonly_audit_report.md)

---

## 1. Executive Summary

In the `19104e32e0ac619a19f8586ae3e27d8707af62c0` (`dev_2`) codebase, `BREAKOUT_IGNITION` (and the sibling chart setups `SWEEP_RECLAIM` and `DISPLACEMENT_RETEST`) evaluate `candles[-1]` without checking whether that bar has closed, without validating `"timestamp"` presence or OHLC sanity, and without deduplicating or checking interval continuity. Because GeckoTerminal's `/ohlcv/minute` endpoint returns the currently forming bar as the newest entry, `parse_geckoterminal_ohlcv()` places that unclosed candle at `candles[-1]`, allowing mid-bar price spikes or partial-bar volume to drive `BUY` decisions.

This design defines a minimal, deterministic **E3A Candle Eligibility** contract that:
1. Captures a single UTC `decision_timestamp` per evaluation cycle and propagates it to both 5m and 1m evaluations.
2. Enforces strict closed-candle filtering (`timestamp + bar_seconds <= decision_timestamp`, i.e., `+300` for 5m and `+60` for 1m) before RSI, breakout scoring, or minimum-bar checks.
3. Hardens candle normalization against missing timestamps (removing the `float(c.get("timestamp", idx))` fallback), duplicate timestamps, out-of-order rows, invalid/non-finite OHLCV values, and severe recent-window gaps.
4. Preserves existing `ohlcv_client.py` cache behavior, fetcher return types, and strategy thresholds.
5. Explicitly separates **closed-candle validation** (implemented in E3A) from **staleness/freshness validation** (left undecided pending empirical telemetry on GeckoTerminal indexing lag and batch pacing latency).

---

## 2. Exact Source Call Graph

```text
alpha_node.py:alpha_node() (L115-165)
  ├─ now_dt = now or utc_now() (L115)  [single cycle reference time]
  ├─ Collects pairs_to_fetch where cand.get("candles_5m") is None (L121-126)
  ├─ ohlcv_client.py:fetch_ohlcv_batch(pairs_to_fetch, config, client, now_ts=now_dt.timestamp()) (L134)
  │    └─ fetch_ohlcv_5m() (L291-301) -> _fetch_ohlcv_interval(..., aggregate="5", limit="30") (L171-289)
  │          └─ parse_geckoterminal_ohlcv(resp.json()) (L147-168, called at L262)
  ├─ ohlcv_client.py:should_fetch_1m_fallback(p, ohlcv_by_pair.get(p) or [], now_ts=now_dt.timestamp()) (L135)
  ├─ ohlcv_client.py:fetch_ohlcv_1m_batch(short_pairs, config, client, now_ts=now_dt.timestamp()) (L137)
  │    └─ fetch_ohlcv_1m() (L304-314) -> _fetch_ohlcv_interval(..., aggregate="1", limit="30") (L171-289)
  ├─ Resolves candles_5m / candles_1m (pre-attached cand.get("candles_5m"/"candles_1m") or fetched) (L156-159)
  └─ scalper_entry_engine.py:evaluate_scalper_entry(candles_5m, cand, time_in_slot_min, config=config, candles_1m=candles_1m) (L161)
       ├─ _normalize_candles(candles_5m) (L25-52, called at L202)
       ├─ _normalize_candles(candles_1m) (L25-52, called at L203)
       ├─ If len(candles) < 12 and len(norm_1m) >= 15 (L208-236):
       │    ├─ compute_rsi(closes_1m, period=14) (L211)
       │    └─ breakout_ignition.py:detect_breakout_ignition(norm_1m, pair_data, bar_minutes=1, rsi_val=rsi_1m, config=config) (L212)
       ├─ Else if len(candles) < 12 (L238-252):
       │    └─ detect_breakout_ignition(candles, pair_data, bar_minutes=5, config=config) (L238) [telemetry only; returns eligible=False]
       └─ Else (len(candles) >= 12) (L254-321):
            ├─ compute_rsi(closes, period=14) (L255)
            ├─ detect_breakout_ignition(candles, pair_data, bar_minutes=5, rsi_val=rsi_5m, config=config) (L266)
            ├─ _detect_sweep_reclaim(candles) (L93-112, called at L270)
            └─ _detect_displacement_retest(candles, rsi_5m) (L115-169, called at L272)

whale_lane.py:run_whale_confluence_scan() (L349-430)
  ├─ fetch_ohlcv_batch(pair_addrs, config, client, now_ts=now_dt.timestamp()) (L351)
  ├─ should_fetch_1m_fallback(...) (L352-355) & fetch_ohlcv_1m_batch(...) (L356)
  ├─ Resolves c5 / c1 (pre-attached pair.get("candles_5m"/"candles_1m") or fetched) (L378-379)
  └─ evaluate_scalper_entry(c5, pair, time_in_slot_min=5.0, config=config, candles_1m=c1) (L381)

insider_scanner.py:auto_detect_breakout_ts() (L84-105)
  └─ parse_geckoterminal_ohlcv(resp.json()) (L95)  [picks highest-volume green bar timestamp; not an entry gate]
```

---

## 3. Audit Findings (Answers 1–10)

### Q1. Where OHLCV Timestamps Are Created, Normalized, Transformed, or Discarded
- **Created:**
  - External GeckoTerminal API payload `data.attributes.ohlcv_list` as `[ts, open, high, low, close, volume]` (`ohlcv_client.py:149-166`).
  - Synthetic test fixtures (`tests/test_breakout_ignition.py:32,56,66`; `tests/test_scalper_entry_engine.py:57,90,103,194-195`).
- **Parsed:** `parse_geckoterminal_ohlcv()` (`ohlcv_client.py:147-168`) casts `float(row[0])` to `"timestamp"` (`L159`) and sorts ascending (`L167`).
- **Normalized:** `_normalize_candles()` (`scalper_entry_engine.py:25-52`) casts `float(c.get("timestamp", idx))` for dict inputs (`L32`) or `float(c[0])` for sequence inputs (`L43`), then sorts ascending (`L51`). **Defect:** missing `"timestamp"` silently fabricates `0.0, 1.0, 2.0, ...`.
- **Transformed:** `_aggregate_to_15m()` (`breakout_ignition.py:69-91`) groups 3×5m or 15×1m bars backward from the newest bar (`L74-76`) and assigns `"timestamp": chunk[0]["timestamp"]` (`L83`).
- **Discarded / Ignored:** After sorting, timestamps are never inspected again. `evaluate_scalper_entry()` (`scalper_entry_engine.py:185-321`), `_detect_sweep_reclaim()` (`L93-112`), `_detect_displacement_retest()` (`L115-169`), and `detect_breakout_ignition()` (`breakout_ignition.py:124-365`) rely solely on list length and positional slicing (`candles[-1]`, `candles[-5:-1]`, `candles[-6:-1]`).

### Q2. GeckoTerminal Interval-Start Timestamp Convention
- **Conclusion:** Yes, GeckoTerminal timestamps each candle at its **interval start**, and existing code and tests already assume this:
  - `ohlcv_client.py:213-214` queries `/networks/solana/pools/{addr}/ohlcv/minute` with `aggregate="5"` or `"1"`.
  - `breakout_ignition.py:83` sets aggregated 15m bar timestamps to `chunk[0]["timestamp"]` (the start of the first constituent bar in the 15m bucket).
  - `tests/test_scalper_entry_engine.py:194-195` mocks `ohlcv_list` with exact 300s-aligned Unix epoch timestamps `[1700000300, ...]` and `[1700000600, ...]`.
  - `tests/test_breakout_ignition.py:32,56,66` generates bars at `t0 + i * bar_seconds`.
- **Gap:** No runtime check verifies that a bar's interval `[timestamp, timestamp + bar_seconds)` has elapsed before evaluation.

### Q3. Where `decision_timestamp` Must Be Passed
- `alpha_node.py:115` captures `now_dt = now or utc_now()` once per cycle and already passes `now_dt.timestamp()` to `fetch_ohlcv_batch()` (`L134`) and `fetch_ohlcv_1m_batch()` (`L137`). It must pass `decision_timestamp=now_dt.timestamp()` to `evaluate_scalper_entry()` at `alpha_node.py:161`.
- `whale_lane.py:351,356` already passes `now_dt.timestamp()` to the OHLCV batch fetchers. It must pass `decision_timestamp=now_dt.timestamp()` to `evaluate_scalper_entry()` at `whale_lane.py:381`.
- `evaluate_scalper_entry()` (`scalper_entry_engine.py:185`) must accept `decision_timestamp: Optional[float] = None` and forward the exact same `decision_timestamp` to both the 1m fallback path (`L212`) and the 5m primary path (`L266`, plus `L238`).
- `detect_breakout_ignition()` (`breakout_ignition.py:124`) must accept `decision_timestamp: Optional[float] = None` so direct callers also enforce the same reference time.

### Q4. Exact Closed-Candle Rule & Timestamp Normalization
- **Closed-candle rules:**
  - **5m (`bar_minutes=5`, `bar_seconds=300`):** eligible iff `timestamp + 300 <= decision_timestamp`
  - **1m (`bar_minutes=1`, `bar_seconds=60`):** eligible iff `timestamp + 60 <= decision_timestamp`
- **Timezone, seconds, and millisecond handling:**
  - `decision_timestamp` must be a UTC Unix epoch timestamp in **seconds** (`float`). Callers using `datetime` must pass timezone-aware UTC `datetime.timestamp()` (as `utc_now()` in `alpha_node.py:115` produces); naive `datetime.timestamp()` is prohibited because Python interprets naive datetimes in local OS time.
  - GeckoTerminal returns Unix seconds (`~1.7e9`). If a caller or pre-attached payload provides millisecond epoch timestamps (detected deterministically as `ts >= 1e12`, which separates post-2001 ms timestamps from seconds through year 2286), normalize `ts = ts / 1000.0` during candle normalization rather than mixing units. Non-positive (`ts <= 0`) or non-finite timestamps are rejected.

### Q5. Latest Closed Candle vs. Minimum Closed Candles
- **Both** are required, preserving existing strategy semantics:
  - Minimum closed bar count: `>= 12` closed 5m bars (`scalper_entry_engine.py:208`, `breakout_ignition.py:139`) or `>= 15` closed 1m bars (`scalper_entry_engine.py:209`, `breakout_ignition.py:139`).
  - Latest closed candle: after filtering out unclosed bars, `closed_candles[-1]` is the latest closed candle and remains the ignition/confirmation bar evaluated by `detect_breakout_ignition()`, `_detect_sweep_reclaim()`, and `_detect_displacement_retest()`.

### Q6. Handling Missing, Duplicate, Out-of-Order, Invalid OHLC, and Gapped Candles
- **Missing timestamps:** Drop the bar immediately. Remove `float(c.get("timestamp", idx))` (`scalper_entry_engine.py:32`); any dict missing `"timestamp"`, or with `None`/non-numeric/non-finite/`<= 0` timestamp, is invalid and excluded.
- **Duplicate timestamps:** Deduplicate by normalized `timestamp` (keeping the latest entry in input order) before sorting, preventing double-counting in `EMA`, `VWAP`, `coil_bars`, and `avg_vol_5`.
- **Out-of-order candles:** Sort ascending (oldest-first) by `timestamp` after validation and deduplication.
- **Invalid OHLCV values:** Drop any bar where `open`, `high`, `low`, or `close` is non-finite (`NaN`/`inf`) or `<= 0`, where `high < low`, `high < max(open, close)`, `low > min(open, close)`, or where `volume` is non-finite or `< 0`.
- **Gaps in candle intervals:** Low-liquidity Solana pools can omit zero-volume bars on GeckoTerminal, so strict contiguity across all 30 historical bars would cause excessive false rejections. Instead, validate that consecutive closed bars in the active lookback/coil/volume window (`closed[-6:]`) do not contain a severe gap (`delta > 3 * bar_seconds`); if a severe gap is present in `closed[-6:]`, reject eligibility with an explicit gap reason.

### Q7. Impact on RSI, Breakout Detection, 12-Bar 5m Minimum, and 15-Bar 1m Fallback
- **RSI (`compute_rsi`, `scalper_entry_engine.py:55-77, 255`):** Computed strictly over `closed` candle closes, eliminating intrabar spike distortion.
- **Breakout detection (`detect_breakout_ignition`, `breakout_ignition.py:174-220`):** `curr = candles[-1]` is the newest closed bar; `coil_bars`, `swing_window`, `avg_vol_5`, `EMA9`/`EMA21`, `VWAP`, and `_aggregate_to_15m()` all operate exclusively on closed bars.
- **12-candle 5m minimum (`scalper_entry_engine.py:208`):** Evaluated *after* closed-candle filtering. If 12 raw 5m bars were fetched (11 closed + 1 forming), dropping the forming bar leaves `11 < 12` closed 5m bars, triggering the 1m fallback path.
- **15-candle 1m fallback (`scalper_entry_engine.py:209`):** Evaluated *after* closed-candle filtering on `candles_1m`.
- **Important `alpha_node.py` / `whale_lane.py` 1m-fetch interaction:** Currently `alpha_node.py:135` and `whale_lane.py:354` call `should_fetch_1m_fallback(p, ohlcv_by_pair.get(p) or [], ...)` using **raw** 5m bar count (`< 12`). If GeckoTerminal returns 12 raw 5m bars where `candles[-1]` is unclosed, `len(raw_5m) == 12`, so `should_fetch_1m_fallback()` would return `False` and `alpha_node` would not fetch 1m bars even though only 11 closed 5m bars exist. Counting only **closed** 5m bars when deciding whether to trigger `should_fetch_1m_fallback()` (without changing `ohlcv_client.py`'s signature or return types) resolves this edge case cleanly.

### Q8. Entry Paths That Could Bypass Eligibility Validation
1. **Pre-attached candidate candles:** `alpha_node.py:123,156-159` (`cand.get("candles_5m")`, `cand.get("candles_1m")`) and `whale_lane.py:378-379` (`pair.get("candles_5m")`, `pair.get("candles_1m")`). Placing validation inside `evaluate_scalper_entry()` covers both fetched and pre-attached candles.
2. **Direct `detect_breakout_ignition()` calls:** Any caller invoking `detect_breakout_ignition()` directly without going through `evaluate_scalper_entry()`. Placing the shared eligibility filter inside `detect_breakout_ignition()` as well closes this path.
3. **Direct `ohlcv_client.parse_geckoterminal_ohlcv()` callers:** `insider_scanner.py:95` calls `parse_geckoterminal_ohlcv()` directly (not an entry gate, but hardening `parse_geckoterminal_ohlcv()` for OHLCV validity/dedup protects it too).

### Q9. Required Test Scenarios
Covered in Section 6 (Test Matrix) below.

### Q10. Closed-Candle Validation vs. Freshness Validation
- **Closed-candle validation** answers: *"Has this bar completed its `[timestamp, timestamp + bar_seconds)` interval?"* (`timestamp + bar_seconds <= decision_timestamp`). This is a pure structural/temporal invariant and **is** enforced by E3A.
- **Freshness validation** answers: *"Is the newest closed bar too old relative to `decision_timestamp`?"* (`decision_timestamp - (closed[-1]["timestamp"] + bar_seconds) <= max_age`).
- **Why the freshness threshold is left undecided:**
  1. `ohlcv_client.py` caches in memory only (`_ohlcv_cache`, `OHLCV_CACHE_SECONDS=90.0`, `OHLCV_CACHE_MAX_TTL_SECONDS=600.0`) and raw candles are not persisted in DuckDB (`solana_meme_agent.duckdb`), so there is no historical telemetry of normal GeckoTerminal bar-publication lag.
  2. `alpha_node.py` uses global 6.5s pacing (`OHLCV_PACING_DELAY_SECONDS=6.5`), meaning a 5-pool batch with 1m fallbacks can take 30–65s before the last pool is evaluated, and low-cap pools can legitimately omit zero-volume bars.
  3. Without empirical distribution data on (a) GeckoTerminal indexing delay after bar close and (b) zero-volume bar omission rates on target Solana pools, choosing a hard freshness cutoff in E3A would risk arbitrary false rejections. Freshness policy and cache-provenance gating should be decided separately once telemetry is collected.

---

## 4. Proposed Minimal Implementation Design

All changes are localized to candle validation and `decision_timestamp` propagation—**no** strategy threshold tuning, **no** `ohlcv_client.py` cache-TTL or return-type changes.

1. **Shared Helper (`scalper_entry_engine.py` or `breakout_ignition.py`):**
   - Add `filter_eligible_candles(raw_candles, *, bar_minutes: int, decision_timestamp: Optional[float] = None) -> tuple[list[dict[str, float]], Optional[str]]`:
     - Normalizes dict/sequence rows, converts `ts >= 1e12` ms to seconds, drops rows with missing/invalid timestamps or non-finite/non-positive/inverted OHLCV.
     - Deduplicates by `timestamp` (last-wins), sorts ascending.
     - If `decision_timestamp is not None`: filters to `c["timestamp"] + (bar_minutes * 60) <= decision_timestamp`.
     - Checks `closed[-6:]` for consecutive gaps `> 3 * (bar_minutes * 60)`; if found, returns `([], "candle interval gap in recent window")`.
     - Returns `(closed_candles, None)`.
2. **`scalper_entry_engine.py`:**
   - Replace `_normalize_candles()`'s `float(c.get("timestamp", idx))` with strict timestamp extraction (no index substitution).
   - Add `decision_timestamp: Optional[float] = None` to `evaluate_scalper_entry()`, defaulting to `time.time()` when `None` (fail-closed for production callers; explicit `decision_timestamp` supported for deterministic tests).
   - Run `filter_eligible_candles()` on `candles_5m` (`bar_minutes=5`) and `candles_1m` (`bar_minutes=1`) *before* `len(candles) < 12` / `len(norm_1m) >= 15` / `compute_rsi()` / setup detectors.
   - Pass `decision_timestamp=ref_ts` to `detect_breakout_ignition()`.
3. **`breakout_ignition.py`:**
   - Add `decision_timestamp: Optional[float] = None` to `detect_breakout_ignition()`. If provided, filter `candles` to closed bars (`c["timestamp"] + bar_seconds <= decision_timestamp`) before `len(candles) < min_bars` (`L140`) and `curr = candles[-1]` (`L174`).
4. **`alpha_node.py` & `whale_lane.py`:**
   - Pass `decision_timestamp=now_dt.timestamp()` to `evaluate_scalper_entry()` (`alpha_node.py:161`, `whale_lane.py:381`).
   - When computing `short_pairs` / `short_addrs` for `should_fetch_1m_fallback()` (`alpha_node.py:135`, `whale_lane.py:353`), pass the closed-filtered 5m list (or check closed count `< 12`) so 11 closed + 1 unclosed 5m bars still trigger 1m fallback.

---

## 5. Data Contract & Failure Behavior

| Input Condition | Behavior under E3A Contract | `ScalperEntryResult` / `BreakoutIgnitionResult` Output |
| :--- | :--- | :--- |
| Forming 5m bar (`ts + 300 > decision_ts`) | Excluded from `closed_5m` | Evaluated on `closed_5m[-1]`; if `< 12` closed 5m bars remain, routes to 1m fallback or `eligible=False` |
| Forming 1m bar (`ts + 60 > decision_ts`) | Excluded from `closed_1m` | Evaluated on `closed_1m[-1]`; if `< 15` closed 1m bars remain, `eligible=False` |
| Exact boundary (`ts + bar_seconds == decision_ts`) | **Eligible** (closed) | Included as `closed[-1]` |
| Missing `"timestamp"` or `ts <= 0` / `NaN` | Row dropped (never `idx`) | Excluded; if remaining bars `< min_bars`, `eligible=False` |
| Duplicate `timestamp` | Last row kept, sorted ascending | Single bar at that `timestamp` |
| Out-of-order bars | Sorted ascending by `timestamp` | Evaluated oldest-first |
| Invalid OHLC (`<= 0`, `NaN`/`inf`, `high < low`, `vol < 0`) | Row dropped | Excluded; if remaining bars `< min_bars`, `eligible=False` |
| Severe gap (`> 3 * bar_seconds`) in `closed[-6:]` | Series rejected | `eligible=False`, `setup_type="NONE"`, explicit gap `rejection_reason` |
| Millisecond timestamp (`ts >= 1e12`) | Scaled to `ts / 1000.0` | Normalized to UTC seconds before closed-bar check |

---

## 6. Test Matrix

| ID | Scenario | Target Module(s) | Key Assertion |
| :--- | :--- | :--- | :--- |
| `T1` | Forming 5m candle would trigger breakout (`ts + 300 > decision_ts`) | `scalper_entry_engine`, `breakout_ignition` | Forming bar dropped; `detected=False`, `eligible=False` |
| `T2` | Forming 1m candle would trigger breakout (`ts + 60 > decision_ts`) | `scalper_entry_engine`, `breakout_ignition` | Forming 1m bar dropped; `detected=False`, `eligible=False` |
| `T3` | Exact interval boundary (`ts + 300 == decision_ts` and `ts + 300 == decision_ts + 1`) | `scalper_entry_engine`, `breakout_ignition` | `==` is eligible; `+ 1` is dropped |
| `T4` | Stale but closed candles (`ts + 300 <= decision_ts`, older than 1 bar) | `scalper_entry_engine` | Closed-candle filter retains them (freshness policy undecided in E3A) |
| `T5` | Missing `"timestamp"`, duplicate timestamps, out-of-order, `NaN`/zero OHLC, ms timestamps | `scalper_entry_engine`, `ohlcv_client` | Missing/invalid dropped (no `idx` fallback); deduped; sorted; ms scaled |
| `T6` | 12 raw 5m bars (11 closed + 1 forming) + 15 closed 1m bars | `alpha_node`, `scalper_entry_engine` | Triggers 1m fallback and evaluates closed 1m bars |
| `T7` | Severe gap (`> 3 * bar_seconds`) in `closed[-6:]` | `scalper_entry_engine` | `eligible=False` with gap rejection reason |
| `T8` | Existing callers & fixtures (`alpha_node`, `whale_lane`, `test_breakout_ignition.py`, `test_scalper_entry_engine.py`, `test_breakout_isolation.py`) | All callers & test suites | Callers pass `decision_timestamp`; existing suites pass with explicit `decision_timestamp` aligned to fixtures |

---

## 7. Risks & Unresolved Questions

1. **Existing unit test compatibility:** Existing tests ([test_breakout_ignition.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/tests/test_breakout_ignition.py#L32), [test_scalper_entry_engine.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/tests/test_scalper_entry_engine.py#L57), [test_breakout_isolation.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/tests/test_breakout_isolation.py)) use hardcoded historical timestamps (`1_700_000_000`, `1_760_000_000`) and omit `decision_timestamp`. If `decision_timestamp` defaults to `time.time()`, those tests must pass `decision_timestamp` aligned to their fixtures (whereas `ts + 300 <= time.time()` is already true for historical timestamps, any future freshness check would fail them unless `decision_timestamp` is passed).
2. **Low-liquidity bar gaps:** GeckoTerminal omits zero-volume intervals; the `> 3 * bar_seconds` threshold in `closed[-6:]` should be validated against real pool data before tightening.
3. **Unresolved freshness threshold:** Optimal `max_close_age` for 5m and 1m bars cannot be determined from current code alone due to missing GeckoTerminal indexing-lag and live pacing telemetry.

---

## 8. Explicit Out-of-Scope Items

- Implementing any code, configuration, or test changes.
- Modifying `ohlcv_client.py` cache TTLs, 429 backoff logic, or fetcher return types.
- Defining or enforcing a numeric candle-freshness / staleness threshold (deferred until empirical telemetry is available).
- Tuning `BREAKOUT_IGNITION`, `SWEEP_RECLAIM`, `DISPLACEMENT_RETEST`, RSI, or order-flow thresholds.
- Making live network requests or modifying DuckDB schemas/data.
