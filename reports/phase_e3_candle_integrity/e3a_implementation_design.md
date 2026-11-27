# Phase E3A — Candle Eligibility Implementation Design (Revised)

- **Repository:** `Tmalone1250/solana-trading-agent`
- **Audited Source Branch / Commit:** `dev_2` @ `19104e32e0ac619a19f8586ae3e27d8707af62c0`
- **Mode:** Strictly read-only audit & documentation-only design revision (no application source, configuration, test, database, branch, or commit modifications)
- **Prerequisite Audit:** [e3_readonly_audit_report.md](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/reports/phase_e3_candle_integrity/e3_readonly_audit_report.md)

---

## Change Summary (Review Corrective Pass)

1. **Fail-Closed Direct Detector Contract:** Replaced optional `decision_timestamp=None` bypass risk with a fail-closed contract on both `detect_breakout_ignition()` and `evaluate_scalper_entry()`: `None` defaults internally to `time.time()` (UTC seconds), and any non-finite, non-positive, or non-numeric `decision_timestamp` fails closed (`detected=False` / `eligible=False`).
2. **Dependency-Safe Helper Placement:** Placed `filter_eligible_candles()` in `breakout_ignition.py` (which `scalper_entry_engine.py` already imports), preventing any circular import back from `breakout_ignition.py` into `scalper_entry_engine.py`.
3. **GeckoTerminal Timestamp Convention Evidence:** Separated what the repository proves (internal convention/assumptions) from external CoinGecko/GeckoTerminal API v2 documentation (`ohlcv_list` timestamps denote interval start in Unix epoch seconds, and zero-swap intervals are omitted by default), and explicitly stated that `timestamp + bar_seconds <= decision_timestamp` depends on the provider's interval-start invariant.
4. **Gap Rejection & Fallback Semantics:** Defined explicit data-integrity rejection propagation: a 5m gap rejection does **not** fall back to 1m (1m fallback is strictly for `< 12` closed 5m bars on young pools), and `ScalperEntryResult.rejection_reason` / `BreakoutTelemetry.failed_gates` preserve the gap reason without being masked by `"insufficient 5m candles"`.
5. **Test Matrix (`T3a` / `T3b` / `T4`):** Split `T3` into separate boundary tests (`T3a`: `timestamp + bar_seconds == decision_timestamp` eligible; `T3b`: `timestamp + bar_seconds == decision_timestamp + 1` ineligible) and kept `T4` (stale-but-closed) distinct.

---

## 1. Executive Summary

In `19104e32e0ac619a19f8586ae3e27d8707af62c0` (`dev_2`), `BREAKOUT_IGNITION` (along with `SWEEP_RECLAIM` and `DISPLACEMENT_RETEST`) evaluates `candles[-1]` without verifying whether that bar has closed, without validating `"timestamp"` presence or OHLCV sanity, and without deduplicating or checking interval continuity. Because GeckoTerminal's `/ohlcv/minute` endpoint returns the currently forming interval as the newest entry, `parse_geckoterminal_ohlcv()` places that unclosed bar at `candles[-1]`, allowing intrabar spikes or partial-bar volume to trigger `BUY` decisions.

This design defines a minimal, fail-closed **E3A Candle Eligibility** contract that:
1. Captures a single UTC `decision_timestamp` per evaluation cycle and passes it to both 5m and 1m evaluations.
2. Enforces strict closed-candle filtering (`timestamp + 300 <= decision_timestamp` for 5m; `timestamp + 60 <= decision_timestamp` for 1m) inside **both** `evaluate_scalper_entry()` and `detect_breakout_ignition()`, defaulting to `time.time()` when omitted and failing closed on invalid timestamps.
3. Hardens normalization against missing timestamps (removing `float(c.get("timestamp", idx))`), duplicate timestamps, out-of-order rows, invalid/non-finite OHLCV values, and severe recent-window gaps.
4. Preserves existing `ohlcv_client.py` cache behavior, fetcher return types, and strategy thresholds.
5. Explicitly separates **closed-candle validation** (implemented in E3A) from **freshness/staleness validation** (threshold left undecided pending empirical telemetry).

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
       │    └─ breakout_ignition.py:detect_breakout_ignition(candles, pair_data, bar_minutes=5, config=config) (L238)
       └─ Else (len(candles) >= 12) (L254-321):
            ├─ compute_rsi(closes, period=14) (L255)
            ├─ breakout_ignition.py:detect_breakout_ignition(candles, pair_data, bar_minutes=5, rsi_val=rsi_5m, config=config) (L266)
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

**All direct callers of `detect_breakout_ignition()` in `19104e32e0ac619a19f8586ae3e27d8707af62c0`:**
- `scalper_entry_engine.py:212` (1m fallback path)
- `scalper_entry_engine.py:238` (`< 12` 5m bars telemetry path)
- `scalper_entry_engine.py:266` (primary 5m path)
- No other source or test files call `detect_breakout_ignition()` directly.

---

## 3. Audit Findings (Answers 1–10)

### Q1. Where OHLCV Timestamps Are Created, Normalized, Transformed, or Discarded
- **Created:** GeckoTerminal `ohlcv_list` rows `[ts, o, h, l, c, v]` (`ohlcv_client.py:149-166`) or synthetic test fixtures (`tests/test_breakout_ignition.py:32,56,66`; `tests/test_scalper_entry_engine.py:57,90,103,194-195`).
- **Parsed:** `parse_geckoterminal_ohlcv()` (`ohlcv_client.py:147-168`) casts `float(row[0])` (`L159`) and sorts ascending (`L167`).
- **Normalized:** `_normalize_candles()` (`scalper_entry_engine.py:25-52`) casts `float(c.get("timestamp", idx))` for dicts (`L32`) or `float(c[0])` for sequences (`L43`), then sorts ascending (`L51`). Missing `"timestamp"` silently fabricates `0.0, 1.0, 2.0, ...`.
- **Transformed:** `_aggregate_to_15m()` (`breakout_ignition.py:69-91`) groups 3×5m or 15×1m bars backward from the newest bar (`L74-76`) and sets `"timestamp": chunk[0]["timestamp"]` (`L83`).
- **Discarded / Ignored:** After sorting, timestamps are never inspected again. `evaluate_scalper_entry()`, `_detect_sweep_reclaim()`, `_detect_displacement_retest()`, and `detect_breakout_ignition()` rely solely on list length and positional slicing (`candles[-1]`, `candles[-5:-1]`, `candles[-6:-1]`).

### Q2. GeckoTerminal Timestamp Convention Evidence
- **What the repository proves (internal assumptions only):**
  - `ohlcv_client.py:213-214` queries `/networks/solana/pools/{addr}/ohlcv/minute` with `aggregate="5"` or `"1"`.
  - `breakout_ignition.py:83` sets aggregated 15m timestamps to `chunk[0]["timestamp"]`, showing the codebase assumes interval-start timestamps.
  - `tests/test_scalper_entry_engine.py:194-195` and `tests/test_breakout_ignition.py:32,56,66` construct synthetic candles with 300s-aligned interval-start Unix epoch seconds.
  - Repository code and synthetic fixtures do **not** independently prove provider semantics or that the newest returned bar is always forming.
- **External authoritative documentation:**
  - CoinGecko / GeckoTerminal API v2 documentation for `/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}` specifies that each `ohlcv_list` element `[timestamp, open, high, low, close, volume]` uses `timestamp` as the **start time of the interval in Unix epoch seconds**, `close` as the closing price (or latest price if the interval is current/forming), and omits zero-swap intervals by default (unless `include_empty_intervals=true` is passed, which `ohlcv_client.py:214` does not pass).
- **Contractual dependency:**
  - The rule `timestamp + bar_seconds <= decision_timestamp` is valid **if and only if** the upstream OHLCV provider timestamps candles at interval start. Any future provider change (e.g., interval-end timestamps) is an implementation prerequisite requiring explicit unit/convention conversion before entering `filter_eligible_candles()`.

### Q3. Where `decision_timestamp` Must Be Passed
- `alpha_node.py:115` captures `now_dt = now or utc_now()` once per cycle and already passes `now_dt.timestamp()` to `fetch_ohlcv_batch()` (`L134`) and `fetch_ohlcv_1m_batch()` (`L137`). It must pass `decision_timestamp=now_dt.timestamp()` to `evaluate_scalper_entry()` (`L161`).
- `whale_lane.py:351,356` already passes `now_dt.timestamp()` to the OHLCV batch fetchers. It must pass `decision_timestamp=now_dt.timestamp()` to `evaluate_scalper_entry()` (`L381`).
- `evaluate_scalper_entry()` (`scalper_entry_engine.py:185`) accepts `decision_timestamp: Optional[float] = None`, resolves a single validated UTC `ref_ts` once at entry, and passes `decision_timestamp=ref_ts` to both the 5m and 1m eligibility checks and all `detect_breakout_ignition()` calls (`L212, L238, L266`).

### Q4. Exact Closed-Candle Rule, Fail-Closed Contract, & Timestamp Normalization
- **Closed-candle rules:**
  - **5m (`bar_minutes=5`, `bar_seconds=300`):** eligible iff `timestamp + 300 <= decision_timestamp`
  - **1m (`bar_minutes=1`, `bar_seconds=60`):** eligible iff `timestamp + 60 <= decision_timestamp`
- **Fail-closed `decision_timestamp` contract (no silent bypass):**
  - Both `evaluate_scalper_entry()` and `detect_breakout_ignition()` accept `decision_timestamp: Optional[float] = None`.
  - When `decision_timestamp is None`, both functions **default internally to `time.time()`** (UTC Unix epoch seconds) and **always** run closed-candle filtering. Direct callers cannot silently disable closed-candle validation by omitting `decision_timestamp`.
  - When `decision_timestamp` is explicitly provided, it must not be `bool`, and must be numeric, finite (`math.isfinite`), and strictly positive (`> 0`). If invalid (e.g., `NaN`, `inf`, `<= 0`, string/object, `bool`), both functions **fail closed immediately**:
    - `detect_breakout_ignition()` returns `BreakoutIgnitionResult(detected=False, timeframe=tf, rejection_reason="invalid decision_timestamp", telemetry=BreakoutTelemetry(score=0, state="FORMING", timeframe=tf_label, failed_gates=["INVALID_DECISION_TS"]))`.
    - `evaluate_scalper_entry()` returns `ScalperEntryResult(eligible=False, setup_type="NONE", rejection_reason="invalid decision_timestamp")`.
  - Deterministic tests pass an explicit `decision_timestamp` aligned to their fixture clock.
- **Timezone, seconds, and millisecond handling:**
  - `decision_timestamp` must be UTC Unix epoch seconds (`float`, e.g., from UTC-aware `datetime.timestamp()` or `time.time()`).
  - If a candle timestamp is `>= 1e12` (millisecond epoch), normalize `ts = ts / 1000.0` during candle normalization. Non-positive (`ts <= 0`) or non-finite (`NaN`/`inf`) candle timestamps are dropped.

### Q5. Latest Closed Candle vs. Minimum Closed Candles
- **Both** are required, preserving existing strategy semantics:
  - Minimum closed bar count: `>= 12` closed 5m bars (`scalper_entry_engine.py:208`, `breakout_ignition.py:139`) or `>= 15` closed 1m bars (`scalper_entry_engine.py:209`, `breakout_ignition.py:139`).
  - Latest closed candle: after filtering out unclosed bars, `closed_candles[-1]` is the latest closed candle and remains the signal/ignition bar for `detect_breakout_ignition()`, `_detect_sweep_reclaim()`, and `_detect_displacement_retest()`.

### Q6. Handling Missing, Duplicate, Out-of-Order, Invalid OHLC, and Gapped Candles
- **Missing timestamps:** Drop the bar immediately. Remove `float(c.get("timestamp", idx))` (`scalper_entry_engine.py:32`); any dict missing `"timestamp"` or having `None`/bool/non-numeric/non-finite/`<= 0` timestamp is dropped.
- **Duplicate timestamps:** Deduplicate by normalized `timestamp` (keeping the latest occurrence in input order) before sorting.
- **Out-of-order candles:** Sort ascending (oldest-first) by `timestamp` after validation and deduplication.
- **Invalid OHLCV values:** Drop any bar where `open`, `high`, `low`, or `close` is non-finite or `<= 0`, where `high < low`, `high < max(open, close)`, `low > min(open, close)`, or where `volume` is non-finite or `< 0`.
- **Gaps in candle intervals:** Because GeckoTerminal omits zero-swap intervals by default, historical gaps outside the active window are tolerated; however, any consecutive closed-bar delta `> 3 * bar_seconds` inside the active coil/volume window (`closed[-6:]`) is rejected as a data-integrity gap.

### Q7. Impact on RSI, Breakout Detection, 12-Bar 5m Minimum, and 15-Bar 1m Fallback
- **RSI (`compute_rsi`):** Computed strictly on closed-candle closes.
- **Breakout detection (`detect_breakout_ignition`):** `curr = candles[-1]`, `coil_bars`, `swing_window`, `avg_vol_5`, `EMA9`/`EMA21`, `VWAP`, and `_aggregate_to_15m()` all operate exclusively on closed bars.
- **12-candle 5m minimum & 15-candle 1m fallback:** Evaluated *after* closed-candle filtering.
- **`alpha_node.py:135` / `whale_lane.py:354` 1m-fetch interaction:** `should_fetch_1m_fallback()` currently checks raw `len(candles_5m) < 12`. If 12 raw 5m bars contain 11 closed + 1 forming bar, `alpha_node.py` / `whale_lane.py` must evaluate `should_fetch_1m_fallback()` using the closed-filtered 5m count (`< 12`) so 1m fallback bars are fetched when needed (without modifying `ohlcv_client.py`'s signature or return types).

### Q8. Entry Paths That Could Bypass Eligibility Validation
1. **Pre-attached candidate candles:** `alpha_node.py:123,156-159` and `whale_lane.py:378-379` (`cand.get("candles_5m")` / `candles_1m`). Validating inside `evaluate_scalper_entry()` covers both fetched and pre-attached candles.
2. **Direct `detect_breakout_ignition()` calls:** Validating inside `detect_breakout_ignition()` with fail-closed `decision_timestamp` handling closes direct-call bypasses.

### Q9. Required Test Scenarios
Covered in Section 6 (Test Matrix).

### Q10. Closed-Candle Validation vs. Freshness Validation
- **Closed-candle validation** (`timestamp + bar_seconds <= decision_timestamp`) checks bar completion and **is** enforced in E3A.
- **Freshness validation** (`decision_timestamp - (closed[-1]["timestamp"] + bar_seconds) <= max_age`) checks how old the newest closed bar is.
- **Why the freshness threshold is left undecided:** Current code has no DuckDB raw-candle telemetry (`e3_readonly_audit_report.md:194`), `ohlcv_client.py` caches in memory only (`OHLCV_CACHE_SECONDS=90.0`, `OHLCV_CACHE_MAX_TTL_SECONDS=600.0`), and `alpha_node.py` incurs variable 6.5s global pacing latency across batches. Without empirical data on GeckoTerminal indexing lag and low-cap zero-swap bar omission rates, a freshness cutoff cannot be justified from current code and is deferred.

---

## 4. Proposed Minimal Implementation Design

### 4.1 Dependency-Safe Module & Import Direction
- **`scalper_entry_engine.py` already imports from `breakout_ignition.py` (`L9`).**
- To preserve a strict acyclic DAG, **do not** import anything from `scalper_entry_engine.py` into `breakout_ignition.py`.
- Place `normalize_and_filter_candles()` (shared normalization + closed-bar + gap validation helper) in **`breakout_ignition.py`** using only stdlib (`math`, `time`, `typing`) and `pydantic`.
- **Exact dependency direction:**
  - `breakout_ignition.py` defines `normalize_and_filter_candles(...)` and `detect_breakout_ignition(...)` (no imports from `scalper_entry_engine.py`, `alpha_node.py`, or `whale_lane.py`).
  - `scalper_entry_engine.py` imports `detect_breakout_ignition, normalize_and_filter_candles` from `breakout_ignition` (`L9`).
  - `alpha_node.py` and `whale_lane.py` import `evaluate_scalper_entry` from `scalper_entry_engine` (and `normalize_and_filter_candles` from `breakout_ignition` if checking closed 5m count before `should_fetch_1m_fallback()`).

### 4.2 Function Contracts
1. **`validate_decision_timestamp(decision_timestamp: Optional[float]) -> tuple[Optional[float], Optional[str]]`** (in `breakout_ignition.py`):
   - If `decision_timestamp is None`: returns `(time.time(), None)`.
   - If `isinstance(decision_timestamp, bool)` or not `isinstance(decision_timestamp, (int, float))` or not `math.isfinite(float(decision_timestamp))` or `float(decision_timestamp) <= 0.0`: returns `(None, "invalid decision_timestamp")`.
   - Otherwise returns `(float(decision_timestamp), None)`.
2. **`normalize_and_filter_candles(raw_candles: Sequence[Any], *, bar_minutes: int, decision_timestamp: Optional[float] = None) -> tuple[list[dict[str, float]], Optional[str]]`** (in `breakout_ignition.py`):
   - Validates `decision_timestamp` via `validate_decision_timestamp()`. If invalid, returns `([], "invalid decision_timestamp")`.
   - Parses dict/sequence rows without `idx` timestamp substitution; scales `ts >= 1e12` ms to seconds; drops non-finite/non-positive/inverted OHLCV rows; deduplicates by `timestamp` (last-wins); sorts ascending.
   - Filters to closed bars: `c["timestamp"] + (bar_minutes * 60) <= ref_ts`.
   - Checks `closed[-6:]` for consecutive gaps `> 3 * (bar_minutes * 60)`. If found, returns `([], f"{bar_minutes}m candle interval gap (>{3 * bar_minutes * 60}s) in recent window")`.
   - Returns `(closed, None)`.
3. **`detect_breakout_ignition(..., decision_timestamp: Optional[float] = None)`** (in `breakout_ignition.py`):
   - Calls `normalize_and_filter_candles(candles, bar_minutes=bar_minutes, decision_timestamp=decision_timestamp)` at function entry.
   - If an error reason (`invalid decision_timestamp` or gap reason) is returned, fails closed immediately with `detected=False`, `rejection_reason=err_reason`, and `failed_gates=["INVALID_DECISION_TS"]` or `["CANDLE_GAP"]`.
   - Otherwise evaluates `closed_candles` against `min_bars` (`12` for 5m, `15` for 1m) and proceeds unchanged.
4. **`evaluate_scalper_entry(..., decision_timestamp: Optional[float] = None)`** (in `scalper_entry_engine.py`):
   - Validates `decision_timestamp` once at entry via `validate_decision_timestamp()`; on error, returns `ScalperEntryResult(eligible=False, setup_type="NONE", rejection_reason=err_reason)`.
   - Calls `normalize_and_filter_candles(candles_5m, bar_minutes=5, decision_timestamp=ref_ts)`.
   - **Gap rejection & fallback semantics:**
     - If `candles_5m` fails with a **5m gap rejection**, `evaluate_scalper_entry()` **does NOT fall back to 1m** (1m fallback is strictly for young pools with `< 12` closed 5m bars, not gapped/corrupted older 5m series). It immediately returns `ScalperEntryResult(eligible=False, setup_type="NONE", rsi_5m=50.0, dist_from_1h_high_pct=0.0, rejection_reason=gap_5m_reason)` (prepending soak failure if `time_in_slot_min < min_soak`), preserving the explicit data-integrity rejection rather than `"insufficient 5m candles"`.
     - Only when `gap_5m_reason is None` and `len(closed_5m) < 12` does it evaluate `candles_1m` via `normalize_and_filter_candles(candles_1m, bar_minutes=1, decision_timestamp=ref_ts)`. If `candles_1m` fails with a **1m gap rejection**, it immediately returns `ScalperEntryResult(eligible=False, setup_type="NONE", rejection_reason=gap_1m_reason)` rather than `"insufficient 5m candles"`.
   - Passes `decision_timestamp=ref_ts` to all `detect_breakout_ignition()` calls.

---

## 5. Data Contract and Failure Behavior

| Input Condition | Behavior under E3A Contract | Output / Telemetry |
| :--- | :--- | :--- |
| `decision_timestamp=None` | Defaults to `time.time()` (UTC seconds); filtering active | Closed-bar filter enforced |
| Invalid `decision_timestamp` (`NaN`, `inf`, `<=0`, `bool`, non-numeric) | Fails closed immediately | `eligible=False`, `detected=False`, `rejection_reason="invalid decision_timestamp"`, `failed_gates=["INVALID_DECISION_TS"]` |
| Forming 5m bar (`ts + 300 > decision_ts`) | Excluded from `closed_5m` | Evaluated on `closed_5m[-1]`; if `< 12` closed 5m bars remain, routes to 1m fallback or `insufficient 5m candles` |
| Forming 1m bar (`ts + 60 > decision_ts`) | Excluded from `closed_1m` | Evaluated on `closed_1m[-1]`; if `< 15` closed 1m bars remain, `eligible=False` |
| Exact boundary (`ts + bar_seconds == decision_ts`) | **Eligible** (closed) | Included as `closed[-1]` |
| One second before close (`ts + bar_seconds == decision_ts + 1`) | **Ineligible** (forming) | Excluded from `closed` |
| Missing `"timestamp"` or invalid OHLCV (`<=0`, `NaN`, `high < low`, `vol < 0`) | Row dropped (no `idx` fallback) | Excluded before `min_bars` check |
| Duplicate `timestamp` / out-of-order rows | Last-wins dedup, sorted ascending | Deterministic oldest-first series |
| Severe gap (`> 3 * bar_seconds`) in `closed[-6:]` | **Data-integrity rejection** (no 1m fallback on 5m gap) | `eligible=False`, `setup_type="NONE"`, `rejection_reason` preserves gap reason, `failed_gates=["CANDLE_GAP"]` |
| Millisecond timestamp (`ts >= 1e12`) | Scaled to `ts / 1000.0` | Evaluated in UTC seconds |

---

## 6. Test Matrix

| ID | Scenario | Target Module(s) | Key Assertion |
| :--- | :--- | :--- | :--- |
| `T1` | Forming 5m candle would trigger breakout (`ts + 300 > decision_ts`) | `scalper_entry_engine`, `breakout_ignition` | Forming bar dropped; `detected=False`, `eligible=False` |
| `T2` | Forming 1m candle would trigger breakout (`ts + 60 > decision_ts`) | `scalper_entry_engine`, `breakout_ignition` | Forming 1m bar dropped; `detected=False`, `eligible=False` |
| `T3a` | Exact interval boundary: `timestamp + bar_seconds == decision_timestamp` (5m & 1m) | `scalper_entry_engine`, `breakout_ignition` | Candle **is eligible** and evaluated as `closed[-1]` |
| `T3b` | One second before close: `timestamp + bar_seconds == decision_timestamp + 1` (5m & 1m) | `scalper_entry_engine`, `breakout_ignition` | Candle is **not yet eligible** and is dropped |
| `T4` | Stale-but-closed candles (e.g., `timestamp + 300 <= decision_timestamp - 600`) | `scalper_entry_engine`, `breakout_ignition` | Retained by closed-candle filter (distinct from `T3a`/`T3b`; freshness threshold out of scope) |
| `T5` | Missing `"timestamp"`, duplicate timestamps, out-of-order, `NaN`/zero OHLC, ms timestamps, invalid `decision_timestamp` | `scalper_entry_engine`, `breakout_ignition` | Missing/invalid rows dropped (no `idx` fallback); deduped; sorted; ms scaled; invalid `decision_timestamp` fails closed |
| `T6` | 12 raw 5m bars (11 closed + 1 forming) + 15 closed 1m bars | `alpha_node`, `scalper_entry_engine` | Closed 5m count is 11 (`< 12`), triggers 1m fallback, evaluates closed 1m bars |
| `T7` | Severe gap (`> 3 * 300s`) in 5m `closed[-6:]` even with 15 valid 1m bars present | `scalper_entry_engine`, `breakout_ignition` | Does **not** fall back to 1m; `eligible=False` with explicit 5m gap `rejection_reason` and `CANDLE_GAP` |
| `T8` | Existing callers & fixtures (`alpha_node`, `whale_lane`, `test_breakout_ignition.py`, `test_scalper_entry_engine.py`, `test_breakout_isolation.py`) | All callers & test suites | Callers pass `decision_timestamp`; existing suites pass with explicit `decision_timestamp` aligned to fixtures |

---

## 7. Risks & Unresolved Questions

1. **External GeckoTerminal invariant prerequisite:** `timestamp + bar_seconds <= decision_timestamp` assumes interval-start Unix-second timestamps as documented by CoinGecko/GeckoTerminal API v2. Any alternate provider or schema change must preserve or normalize to this invariant.
2. **Existing unit test compatibility:** Existing fixture tests ([test_breakout_ignition.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/tests/test_breakout_ignition.py#L32), [test_scalper_entry_engine.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/tests/test_scalper_entry_engine.py#L57), [test_breakout_isolation.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/tests/test_breakout_isolation.py)) use historical timestamps (`1_700_000_000`, `1_760_000_000`) and omit `decision_timestamp`; because `time.time()` is newer, closed-candle checks will pass them, though passing explicit `decision_timestamp` in tests is recommended for determinism.
3. **Unresolved freshness threshold:** Optimal `max_close_age` cannot be determined without live telemetry on GeckoTerminal indexing lag, low-cap zero-swap bar omission rates, and `alpha_node` batch pacing duration.

---

## 8. Explicit Out-of-Scope Items

- Implementing any code, configuration, or test changes.
- Defining or enforcing a numeric candle-freshness / staleness threshold.
- Modifying `ohlcv_client.py` cache TTLs, 429 backoff behavior, fetcher return types, or OHLCV providers.
- Tuning `BREAKOUT_IGNITION`, `SWEEP_RECLAIM`, `DISPLACEMENT_RETEST`, RSI, or order-flow thresholds.
- Making live market-data network requests or modifying DuckDB schemas/data.
