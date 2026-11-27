# Phase E3A — Candle Eligibility Implementation Design (Final)

- **Repository:** `Tmalone1250/solana-trading-agent`
- **Audited Source Branch / Commit:** `dev_2` @ `19104e32e0ac619a19f8586ae3e27d8707af62c0`
- **Mode:** Strictly read-only audit & documentation-only design revision (no application source, configuration, test, database, branch, or commit modifications)
- **Prerequisite Audit:** [e3_readonly_audit_report.md](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/reports/phase_e3_candle_integrity/e3_readonly_audit_report.md)

---

## Change Summary (Final Corrective Pass)

1. **GeckoTerminal Documentation Traceability:**
   - Added the exact authoritative CoinGecko/GeckoTerminal API v2 documentation URL (`https://docs.coingecko.com/reference/pool-ohlcv-contract-address`) and identified the specific documented sections (`data.attributes.ohlcv_list` response schema, `before_timestamp` pagination, and `include_empty_intervals` query parameter).
   - Corrected the Executive Summary so it does not categorically assert that the newest returned candle is always forming; instead states that `/ohlcv/minute` *can* include a currently forming interval and that the system currently cannot distinguish forming from closed candles.
   - Explicitly stated that `timestamp + bar_seconds <= decision_timestamp` depends on interval-start Unix-second timestamps and that this provider contract must be verified before implementation.
2. **Diagnostic-Only Recent Candle Gap Policy:**
   - Removed hard eligibility rejection on `> 3 * bar_seconds` gaps because the cutoff is not empirically established and GeckoTerminal omits zero-swap intervals by default.
   - Changed recent-window gap detection to **diagnostic-only** in E3A: valid closed candles are **not** rejected on gaps, 5m/1m fallback remains governed strictly by `len(closed_5m) < 12`, and diagnostic gaps never become a generic `"insufficient 5m candles"` rejection.
   - Updated Test Matrix `T7` to verify diagnostic gap reporting without eligibility rejection or spurious fallback.

---

## 1. Executive Summary

In `19104e32e0ac619a19f8586ae3e27d8707af62c0` (`dev_2`), `BREAKOUT_IGNITION` (along with `SWEEP_RECLAIM` and `DISPLACEMENT_RETEST`) evaluates `candles[-1]` without verifying whether that bar has closed, without validating `"timestamp"` presence or OHLCV sanity, and without deduplicating timestamps. Because GeckoTerminal's `/ohlcv/minute` endpoint *can* include the currently forming interval alongside historical bars, `parse_geckoterminal_ohlcv()` can place an unclosed bar at `candles[-1]`, allowing intrabar spikes or partial-bar volume to drive `BUY` decisions when the latest interval has not yet elapsed.

This design defines a minimal, fail-closed **E3A Candle Eligibility** contract that:
1. Captures a single UTC `decision_timestamp` per evaluation cycle and passes it to both 5m and 1m evaluations.
2. Enforces strict closed-candle filtering (`timestamp + 300 <= decision_timestamp` for 5m; `timestamp + 60 <= decision_timestamp` for 1m) inside **both** `evaluate_scalper_entry()` and `detect_breakout_ignition()`, defaulting to `time.time()` when omitted and failing closed on invalid timestamps.
3. Hardens normalization against missing timestamps (removing `float(c.get("timestamp", idx))`), duplicate timestamps, out-of-order rows, and invalid/non-finite OHLCV values, while recording recent-window interval gaps as **diagnostic-only** telemetry.
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
  - Repository code and synthetic fixtures do **not** independently prove provider semantics or whether the newest returned bar is always forming.
- **Authoritative external documentation:**
  - **URL:** `https://docs.coingecko.com/reference/pool-ohlcv-contract-address` (CoinGecko On-Chain API / GeckoTerminal `GET /onchain/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}` reference).
  - **Documented fields & sections:**
    - **Response schema (`data.attributes.ohlcv_list`):** Documents each element as `[timestamp, open, high, low, close, volume]`, where `timestamp` is the **interval start time in Unix epoch seconds** and `close` is the closing price (or latest price for the current interval).
    - **Query Parameter `before_timestamp`:** Documents integer Unix epoch seconds pagination using `oldest_timestamp - 1` to avoid repeating a candle, corroborating interval-start semantics.
    - **Query Parameter `include_empty_intervals`:** Documents that zero-swap intervals are omitted by default unless `include_empty_intervals=true` is passed (`ohlcv_client.py:214` does not pass this parameter).
  - *Note:* No live market-data API calls were executed during this audit.
- **Implementation prerequisite:**
  - The closed-candle formula `timestamp + bar_seconds <= decision_timestamp` is valid **only if** the upstream provider timestamps candles at interval start in Unix seconds. Before implementing E3A, this provider contract must be verified against the live/sandboxed GeckoTerminal endpoint response shape (and any future provider change must normalize to interval-start Unix seconds).

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
- **Gaps in candle intervals (Diagnostic-Only Policy in E3A):**
  - Because GeckoTerminal omits zero-swap intervals by default (`include_empty_intervals` is not enabled in `ohlcv_client.py:214`), gaps can represent legitimate market inactivity on low-cap Solana pools rather than malformed data, and no empirical threshold is established in the codebase.
  - Therefore, E3A records recent-window interval gaps (`closed[-6:]`, e.g., `max_gap_seconds` and `gap_count` where `delta > bar_seconds`) as **diagnostic-only** metadata (e.g., debug/warning log or non-gating telemetry) and **does NOT reject** otherwise valid closed candles due to gaps.
  - Diagnostic gaps never alter `eligible`, `detected`, or `len(closed_5m) < 12` fallback routing, and never turn into a generic `"insufficient 5m candles"` rejection.

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
- Place `validate_decision_timestamp()` and `normalize_and_filter_candles()` in **`breakout_ignition.py`** using only stdlib (`math`, `time`, `typing`) and `pydantic`.
- **Exact dependency direction:**
  - `breakout_ignition.py` defines `validate_decision_timestamp(...)`, `normalize_and_filter_candles(...)`, and `detect_breakout_ignition(...)` (no imports from `scalper_entry_engine.py`, `alpha_node.py`, or `whale_lane.py`).
  - `scalper_entry_engine.py` imports `detect_breakout_ignition, normalize_and_filter_candles, validate_decision_timestamp` from `breakout_ignition` (`L9`).
  - `alpha_node.py` and `whale_lane.py` import `evaluate_scalper_entry` from `scalper_entry_engine` (and `normalize_and_filter_candles` from `breakout_ignition` when checking closed 5m count before `should_fetch_1m_fallback()`).

### 4.2 Function Contracts
1. **`validate_decision_timestamp(decision_timestamp: Optional[float]) -> tuple[Optional[float], Optional[str]]`** (in `breakout_ignition.py`):
   - If `decision_timestamp is None`: returns `(time.time(), None)`.
   - If `isinstance(decision_timestamp, bool)` or not `isinstance(decision_timestamp, (int, float))` or not `math.isfinite(float(decision_timestamp))` or `float(decision_timestamp) <= 0.0`: returns `(None, "invalid decision_timestamp")`.
   - Otherwise returns `(float(decision_timestamp), None)`.
2. **`normalize_and_filter_candles(raw_candles: Sequence[Any], *, bar_minutes: int, decision_timestamp: Optional[float] = None) -> tuple[list[dict[str, float]], Optional[str], dict[str, Any]]`** (in `breakout_ignition.py`):
   - Validates `decision_timestamp` via `validate_decision_timestamp()`. If invalid, returns `([], "invalid decision_timestamp", {})`.
   - Parses dict/sequence rows without `idx` timestamp substitution; scales `ts >= 1e12` ms to seconds; drops non-finite/non-positive/inverted OHLCV rows; deduplicates by `timestamp` (last-wins); sorts ascending.
   - Filters to closed bars: `c["timestamp"] + (bar_minutes * 60) <= ref_ts`.
   - Computes diagnostic gap metrics over `closed[-6:]` (`recent_gap_count`, `max_recent_gap_seconds`) without rejecting valid closed candles.
   - Returns `(closed, None, gap_diag)`.
3. **`detect_breakout_ignition(..., decision_timestamp: Optional[float] = None)`** (in `breakout_ignition.py`):
   - Calls `normalize_and_filter_candles(candles, bar_minutes=bar_minutes, decision_timestamp=decision_timestamp)` at function entry.
   - If `err_reason` (`"invalid decision_timestamp"`) is returned, fails closed immediately with `detected=False`, `rejection_reason=err_reason`, and `failed_gates=["INVALID_DECISION_TS"]`.
   - Otherwise evaluates `closed_candles` against `min_bars` (`12` for 5m, `15` for 1m) and proceeds unchanged.
4. **`evaluate_scalper_entry(..., decision_timestamp: Optional[float] = None)`** (in `scalper_entry_engine.py`):
   - Validates `decision_timestamp` once at entry via `validate_decision_timestamp()`; on error, returns `ScalperEntryResult(eligible=False, setup_type="NONE", rejection_reason=err_reason)`.
   - Calls `normalize_and_filter_candles(candles_5m, bar_minutes=5, decision_timestamp=ref_ts)`.
   - If `len(closed_5m) < 12`, calls `normalize_and_filter_candles(candles_1m, bar_minutes=1, decision_timestamp=ref_ts)` and runs 1m fallback if `len(closed_1m) >= 15`; otherwise returns `insufficient 5m candles`.
   - Passes `decision_timestamp=ref_ts` to all `detect_breakout_ignition()` calls.
   - Diagnostic gap metrics from `gap_diag` are logged/surfaced diagnostically without rejecting valid closed candles or altering 5m/1m fallback routing.

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
| Interval gap in `closed[-6:]` | **Diagnostic-only** (not rejected) | Valid closed candles evaluated normally; `recent_gap_count` / `max_recent_gap_seconds` recorded diagnostically |
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
| `T7` | Interval gap in 5m `closed[-6:]` with `>= 12` valid closed 5m bars | `scalper_entry_engine`, `breakout_ignition` | Diagnostic gap reported (`recent_gap_count > 0`); valid closed 5m candles **are not rejected**, do **not** fall back to 1m, and do **not** emit `"insufficient 5m candles"` |
| `T8` | Existing callers & fixtures (`alpha_node`, `whale_lane`, `test_breakout_ignition.py`, `test_scalper_entry_engine.py`, `test_breakout_isolation.py`) | All callers & test suites | Callers pass `decision_timestamp`; existing suites pass with explicit `decision_timestamp` aligned to fixtures |

---

## 7. Risks & Unresolved Questions

1. **External GeckoTerminal contract verification prerequisite:** `timestamp + bar_seconds <= decision_timestamp` depends on interval-start Unix-second timestamps as documented at `https://docs.coingecko.com/reference/pool-ohlcv-contract-address`. This provider contract must be verified before implementation (and any future provider change must normalize to interval-start Unix seconds).
2. **Existing unit test compatibility:** Existing fixture tests ([test_breakout_ignition.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/tests/test_breakout_ignition.py#L32), [test_scalper_entry_engine.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/tests/test_scalper_entry_engine.py#L57), [test_breakout_isolation.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/tests/test_breakout_isolation.py)) use historical timestamps (`1_700_000_000`, `1_760_000_000`) and omit `decision_timestamp`; because `time.time()` is newer, closed-candle checks will pass them, though passing explicit `decision_timestamp` in tests is recommended for determinism.
3. **Unresolved freshness & gap thresholds:** Optimal `max_close_age` and whether any gap threshold should ever become gating cannot be determined without live telemetry on GeckoTerminal indexing lag, low-cap zero-swap bar omission rates, and `alpha_node` batch pacing duration.

---

## 8. Explicit Out-of-Scope Items

- Implementing any code, configuration, or test changes.
- Defining or enforcing a numeric candle-freshness / staleness threshold or a gating candle-gap threshold.
- Modifying `ohlcv_client.py` cache TTLs, 429 backoff behavior, fetcher return types, or OHLCV providers.
- Tuning `BREAKOUT_IGNITION`, `SWEEP_RECLAIM`, `DISPLACEMENT_RETEST`, RSI, or order-flow thresholds.
- Making live market-data network requests or modifying DuckDB schemas/data.
