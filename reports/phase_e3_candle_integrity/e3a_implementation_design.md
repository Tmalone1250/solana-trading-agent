# Phase E3A — Candle Eligibility Implementation Design (Final)

- **Repository:** `Tmalone1250/solana-trading-agent`
- **Audited Source Branch / Commit:** `dev_2` @ `19104e32e0ac619a19f8586ae3e27d8707af62c0`
- **Audit Branch:** `audit/e3-candle-integrity-20261127`
- **Mode:** Documentation/design correction only (no application source, configuration, test, database, or runtime changes)
- **Prerequisite Audit:** [e3_readonly_audit_report.md](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/reports/phase_e3_candle_integrity/e3_readonly_audit_report.md)

---

## Change Summary (Narrowly Scoped Fallback-Contract Pass)

1. **`should_fetch_1m_fallback()` Normalized Closed-Bar Counting:**
   - Replaced the simplified `float(c["timestamp"]) + 300 <= ref_ts` sketch with the exact E3A normalization contract (calling `normalize_and_filter_candles()` from `breakout_ignition.py`, which is acyclic with `ohlcv_client.py`).
   - Safely handles missing timestamps, `bool`, non-numeric/malformed entries, non-finite (`NaN`/`inf`), non-positive (`<= 0`), invalid OHLCV, millisecond timestamps (`>= 1e12` -> seconds), and deduplicates by unique eligible closed timestamp so duplicate/malformed rows cannot falsely satisfy `>= 12` and suppress a needed 1m fallback, while preserving `_global_backoff_until`, `last_ok`, and HTTP `200` guards.
2. **Pre-Attached 5m Candidate & `whale_lane.py` Resolution:**
   - Explicitly traced pre-attached `cand["candles_5m"]` in `alpha_node.py:123,135,156-159` and `pair.get("candles_5m")` in `whale_lane.py:350-356,378-381`.
   - Documented that pre-attached-5m-only candidates in `alpha_node.py` are **intentionally not eligible** for a 1m network fallback request (no 5m fetch/cache state `last_ok=True, last_status=200` is established) and safely evaluate to `eligible=False` (`insufficient 5m candles`) when `< 12` closed 5m bars exist without pre-attached `candles_1m`.
   - Documented the `whale_lane.py` difference (fetches 5m for all `pairAddress` entries regardless of pre-attached `candles_5m`, while preferring pre-attached `candles_5m`/`candles_1m` if present as lists).
3. **Updated Test Matrix & Limitations:**
   - Added `T6c` (malformed/millisecond/duplicate cached 5m timestamps cannot suppress 1m fallback) and `T6d` (pre-attached 5m-only candidate triggers no 1m network request and fails safely on `< 12` closed bars).

---

## 1. Executive Summary

In `19104e32e0ac619a19f8586ae3e27d8707af62c0` (`dev_2`), `BREAKOUT_IGNITION` (along with `SWEEP_RECLAIM` and `DISPLACEMENT_RETEST`) evaluates `candles[-1]` without verifying whether that bar's interval has elapsed, without validating `"timestamp"` presence or OHLCV sanity, and without deduplicating timestamps. Under the codebase's interval-start assumption for GeckoTerminal's `/ohlcv/minute` endpoint, an unclosed interval returned at the newest position is placed at `candles[-1]` by `parse_geckoterminal_ohlcv()`, which would allow intrabar price spikes or partial-bar volume to drive `BUY` decisions.

This design defines a minimal, fail-closed **E3A Candle Eligibility** contract that:
1. Captures a single UTC `decision_timestamp` per evaluation cycle and passes it to both 5m and 1m evaluations.
2. Enforces strict closed-candle filtering (`timestamp + 300 <= decision_timestamp` for 5m; `timestamp + 60 <= decision_timestamp` for 1m) inside **both** `evaluate_scalper_entry()` and `detect_breakout_ignition()`, defaulting to `time.time()` when omitted and failing closed on invalid timestamps (subject to verifying the provider's interval-start contract as a hard prerequisite).
3. Makes `should_fetch_1m_fallback()`, `fetch_ohlcv_1m_batch()`, `alpha_node.py`, `whale_lane.py`, and `evaluate_scalper_entry()` count **unique eligible closed 5m bars** identically after normalization and deduplication, without broadening network requests during backoff, after 5m failures, or for pre-attached-5m-only candidates in `alpha_node.py`.
4. Hardens normalization against missing timestamps (removing `float(c.get("timestamp", idx))`), duplicate timestamps, out-of-order rows, and invalid/non-finite OHLCV values, records recent-window gaps as **diagnostic-only** telemetry, and fixes `_aggregate_to_15m()` to use time-aligned 15m buckets rather than positional grouping across noncontiguous bars.
5. Preserves existing `ohlcv_client.py` cache TTLs, 429/backoff semantics, fetcher return types, and strategy thresholds, while leaving freshness thresholds undecided pending empirical telemetry.

---

## 2. Exact Source Call Graph

```text
alpha_node.py:alpha_node() (L115-165)
  ├─ now_dt = now or utc_now() (L115)  [single cycle reference time]
  ├─ Collects pairs_to_fetch ONLY where cand.get("candles_5m") is None (L121-126)
  │    └─ Pre-attached cand["candles_5m"] candidates bypass fetch_ohlcv_batch & 1m fallback
  ├─ ohlcv_client.py:fetch_ohlcv_batch(pairs_to_fetch, config, client, now_ts=now_dt.timestamp()) (L134)
  │    └─ fetch_ohlcv_5m() (L291-301) -> _fetch_ohlcv_interval(..., aggregate="5", limit="30") (L171-289)
  │          └─ parse_geckoterminal_ohlcv(resp.json()) (L147-168, called at L262)
  ├─ ohlcv_client.py:should_fetch_1m_fallback(p, ohlcv_by_pair.get(p) or [], now_ts=now_dt.timestamp()) (L135)
  │    └─ [E3A] Calls normalize_and_filter_candles(..., bar_minutes=5, decision_timestamp=ref_ts)
  │         Checks len(unique_closed_5m) < 12 + no backoff + entry_5m last_ok & status==200
  ├─ ohlcv_client.py:fetch_ohlcv_1m_batch(short_pairs, config, client, now_ts=now_dt.timestamp()) (L137)
  │    └─ Re-reads c5_cached from _ohlcv_cache (L346) -> calls should_fetch_1m_fallback(addr, c5_cached, now_ts=now_ts) (L347)
  │         └─ fetch_ohlcv_1m() (L304-314) -> _fetch_ohlcv_interval(..., aggregate="1", limit="30") (L171-289)
  ├─ Resolves candles_5m / candles_1m (L156-159)
  │    └─ [E3A] Remove raw len(candles_5m) < 12 guard at L158 when assigning ohlcv_1m_by_pair.get(p_addr, [])
  └─ scalper_entry_engine.py:evaluate_scalper_entry(candles_5m, cand, time_in_slot_min, config=config, candles_1m=candles_1m, decision_timestamp=now_dt.timestamp()) (L161)
       ├─ breakout_ignition.py:validate_decision_timestamp(decision_timestamp)
       ├─ breakout_ignition.py:normalize_and_filter_candles(candles_5m, bar_minutes=5, decision_timestamp=ref_ts)
       ├─ If len(closed_5m) < 12:
       │    ├─ breakout_ignition.py:normalize_and_filter_candles(candles_1m, bar_minutes=1, decision_timestamp=ref_ts)
       │    └─ If len(closed_1m) >= 15 -> detect_breakout_ignition(closed_1m, ..., bar_minutes=1, decision_timestamp=ref_ts) (L212)
       │    └─ Else -> detect_breakout_ignition(closed_5m, ..., bar_minutes=5, decision_timestamp=ref_ts) (L238) [eligible=False]
       └─ Else (len(closed_5m) >= 12) (L254-321):
            ├─ compute_rsi(closes_5m, period=14) (L255)
            ├─ breakout_ignition.py:detect_breakout_ignition(closed_5m, ..., bar_minutes=5, rsi_val=rsi_5m, decision_timestamp=ref_ts) (L266)
            │    └─ _aggregate_to_15m(closed_candles, bar_minutes) (L69-91) -> _is_htf_15m_bullish(...) (L94-104)
            ├─ _detect_sweep_reclaim(closed_5m) (L93-112, called at L270)
            └─ _detect_displacement_retest(closed_5m, rsi_5m) (L115-169, called at L272)

whale_lane.py:run_whale_confluence_scan() (L349-430)
  ├─ Collects pair_addrs for ALL pairs with pairAddress (L350) [does not skip pre-attached candles_5m]
  ├─ fetch_ohlcv_batch(pair_addrs, config, client, now_ts=now_dt.timestamp()) (L351)
  ├─ should_fetch_1m_fallback(a, ohlcv_5m.get(a) or [], now_ts=now_dt.timestamp()) (L352-355)
  ├─ fetch_ohlcv_1m_batch(short_addrs, config, client, now_ts=now_dt.timestamp()) (L356)
  ├─ Resolves c5 / c1 (pre-attached pair.get("candles_5m"/"candles_1m") if list, else ohlcv_5m/ohlcv_1m.get(p_addr, [])) (L378-379)
  └─ evaluate_scalper_entry(c5, pair, time_in_slot_min=5.0, config=config, candles_1m=c1, decision_timestamp=now_dt.timestamp()) (L381)

insider_scanner.py:auto_detect_breakout_ts() (L84-105)
  └─ parse_geckoterminal_ohlcv(resp.json()) (L95)  [picks highest-volume green bar timestamp; not an entry gate]
```

**All direct callers of `detect_breakout_ignition()` in `19104e32e0ac619a19f8586ae3e27d8707af62c0`:**
- `scalper_entry_engine.py:212`, `L238`, `L266`. No other source or test files call `detect_breakout_ignition()` directly.

---

## 3. Audit Findings (Answers 1–10)

### Q1. Where OHLCV Timestamps Are Created, Normalized, Transformed, or Discarded
- **Created:** GeckoTerminal `ohlcv_list` rows `[ts, o, h, l, c, v]` (`ohlcv_client.py:149-166`) or synthetic test fixtures (`tests/test_breakout_ignition.py:32,56,66`; `tests/test_scalper_entry_engine.py:57,90,103,194-195`).
- **Parsed:** `parse_geckoterminal_ohlcv()` (`ohlcv_client.py:147-168`) casts `float(row[0])` (`L159`) and sorts ascending (`L167`).
- **Normalized:** `_normalize_candles()` (`scalper_entry_engine.py:25-52`) casts `float(c.get("timestamp", idx))` for dicts (`L32`) or `float(c[0])` for sequences (`L43`), then sorts ascending (`L51`). Missing `"timestamp"` silently fabricates `0.0, 1.0, 2.0, ...`.
- **Transformed:** `_aggregate_to_15m()` (`breakout_ignition.py:69-91`) groups 3×5m or 15×1m bars backward from the newest bar by positional index (`L74-77`) and sets `"timestamp": chunk[0]["timestamp"]` (`L83`).
- **Discarded / Ignored:** After sorting, timestamps are never inspected again by `evaluate_scalper_entry()`, `_detect_sweep_reclaim()`, `_detect_displacement_retest()`, or `detect_breakout_ignition()`.

### Q2. GeckoTerminal Timestamp Convention Evidence (`UNVERIFIED` Provider Contract)
- **What the repository proves (internal assumptions only):**
  - `ohlcv_client.py:213` queries `https://api.geckoterminal.com/api/v2/networks/solana/pools/{addr}/ohlcv/minute` (`L213-214`, `aggregate="5"` or `"1"`, `limit="30"`, `currency="usd"`).
  - `breakout_ignition.py:83` sets aggregated 15m timestamps to `chunk[0]["timestamp"]`, and test fixtures (`tests/test_scalper_entry_engine.py:194-195`, `tests/test_breakout_ignition.py:32,56,66`) use 300s-aligned Unix epoch seconds.
  - Repository code and synthetic fixtures reflect the codebase's **internal assumption**; they do **not** prove GeckoTerminal's live API semantics.
- **Official public documentation review:**
  - The CoinGecko On-Chain API v2 mirror of this endpoint (`https://docs.coingecko.com/reference/pool-ohlcv-contract-address`, endpoint `GET /onchain/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}`) documents:
    - `data.attributes.ohlcv_list` element order `[timestamp, open, high, low, close, volume]`
    - Unix epoch integer-second timestamps
    - `before_timestamp` pagination (`oldest_timestamp - 1`)
    - `include_empty_intervals` (zero-swap intervals omitted by default)
  - However, the cited documentation does **not** explicitly state in normative schema text whether `timestamp` is strictly interval-start vs. interval-end or whether the newest entry's `close` is always a live forming price.
- **Status & Hard Implementation Prerequisite:**
  - **UNVERIFIED** from authoritative documentation alone. Because `timestamp + bar_seconds <= decision_timestamp` is valid **only if** the provider timestamps candles at interval start, verifying a real/sample `https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute` response against wall-clock time is a **hard implementation prerequisite** before E3A rollout.

### Q3. Where `decision_timestamp` Must Be Passed & Complete 1m Fallback Path
- `alpha_node.py:115` captures `now_dt = now or utc_now()` once per cycle and passes `now_dt.timestamp()` to `fetch_ohlcv_batch()` (`L134`), `should_fetch_1m_fallback()` (`L135`), `fetch_ohlcv_1m_batch()` (`L137`), and must pass `decision_timestamp=now_dt.timestamp()` to `evaluate_scalper_entry()` (`L161`).
- `whale_lane.py:351,354,356` already passes `now_dt.timestamp()` to `fetch_ohlcv_batch()`, `should_fetch_1m_fallback()`, and `fetch_ohlcv_1m_batch()`, and must pass `decision_timestamp=now_dt.timestamp()` to `evaluate_scalper_entry()` (`L381`).
- **Consistent closed-5m counting in `should_fetch_1m_fallback()` (`ohlcv_client.py:118-135`):**
  - Instead of raw `len(candles_5m) >= 12` (`L126`) or naive `float(c["timestamp"]) + 300 <= ref_ts`, `should_fetch_1m_fallback()` must call `normalize_and_filter_candles(candles_5m, bar_minutes=5, decision_timestamp=ref_ts)` (wrapping non-sequence input safely so it never throws on malformed cached data).
  - Because `normalize_and_filter_candles()` drops missing/malformed/`bool`/non-finite/non-positive timestamps and invalid OHLCV, normalizes `>= 1e12` ms timestamps to seconds, and deduplicates by unique `timestamp`, `should_fetch_1m_fallback()` counts the **exact same unique eligible closed 5m bars** that `evaluate_scalper_entry()` will evaluate. Duplicate or malformed cached entries cannot falsely reach `12` and suppress a needed 1m fallback.
  - Because `fetch_ohlcv_1m_batch()` (`L346-347`) re-reads `c5_cached` from `_ohlcv_cache` and calls `should_fetch_1m_fallback(addr, c5_cached, now_ts=now_ts)`, both `alpha_node.py:135` / `whale_lane.py:354` and `fetch_ohlcv_1m_batch():347` evaluate the identical normalized closed-5m count while preserving `_global_backoff_until`, `entry_5m["last_ok"]`, and `last_status == 200` guards (`L128-134`).
- **Pre-attached 5m candles in `alpha_node.py` vs. `whale_lane.py`:**
  - In `alpha_node.py:123`, candidates with pre-attached `cand["candles_5m"]` are excluded from `pairs_to_fetch`, so `fetch_ohlcv_batch()` is not called for them and `_ohlcv_cache` has no `last_ok=True, last_status=200` 5m state for them. Consequently, a pre-attached-5m-only candidate (with `< 12` closed 5m bars and no pre-attached `candles_1m`) is **intentionally not eligible** for a 1m network fallback request; `evaluate_scalper_entry()` evaluates the pre-attached bars and safely returns `eligible=False` (`insufficient 5m candles`).
  - `alpha_node.py:158` still removes the raw `len(candles_5m) < 12` guard when reading `ohlcv_1m_by_pair.get(p_addr, [])` so fetched 1m bars for `short_pairs` (e.g., 12 raw / 11 closed 5m bars) are always passed to `evaluate_scalper_entry()`.
  - **Meaningful difference in `whale_lane.py`:** `whale_lane.py:350` fetches 5m OHLCV for *all* pairs with `pairAddress` (it does not skip pre-attached `candles_5m`), while `L378-379` prefers `pair.get("candles_5m")` / `pair.get("candles_1m")` if present as a `list`. Thus `whale_lane.py` only triggers 1m fallback based on the fetched/cached 5m bars in `_ohlcv_cache`, not pre-attached `pair["candles_5m"]`.

### Q4. Exact Closed-Candle Rule, Fail-Closed Contract, & Timestamp Normalization
- **Closed-candle rules:**
  - **5m (`bar_minutes=5`, `bar_seconds=300`):** eligible iff `timestamp + 300 <= decision_timestamp`
  - **1m (`bar_minutes=1`, `bar_seconds=60`):** eligible iff `timestamp + 60 <= decision_timestamp`
- **Fail-closed `decision_timestamp` contract (no silent bypass):**
  - Both `evaluate_scalper_entry()` and `detect_breakout_ignition()` accept `decision_timestamp: Optional[float] = None`.
  - When `decision_timestamp is None`, both **default internally to `time.time()`** (UTC Unix epoch seconds) and **always** run closed-candle filtering.
  - When `decision_timestamp` is explicitly provided, it must not be `bool`, and must be numeric, finite (`math.isfinite`), and strictly positive (`> 0`). Otherwise both functions **fail closed immediately** (`detected=False` / `eligible=False`, `rejection_reason="invalid decision_timestamp"`, `failed_gates=["INVALID_DECISION_TS"]`).
- **Timezone, seconds, and millisecond handling:**
  - `decision_timestamp` is UTC Unix epoch seconds (`float`).
  - Candle timestamps `>= 1e12` (milliseconds) are normalized via `ts / 1000.0`; non-positive (`<= 0`) or non-finite (`NaN`/`inf`) timestamps are dropped.

### Q5. Latest Closed Candle vs. Minimum Closed Candles
- **Both** are required, preserving existing strategy semantics: `>= 12` closed 5m bars (`scalper_entry_engine.py:208`, `breakout_ignition.py:139`) or `>= 15` closed 1m bars (`scalper_entry_engine.py:209`, `breakout_ignition.py:139`), with `closed_candles[-1]` evaluated as the latest closed bar.

### Q6. Handling Missing, Duplicate, Out-of-Order, Invalid OHLC, and Gapped Candles
- **Missing timestamps:** Drop the bar immediately (remove `float(c.get("timestamp", idx))` at `scalper_entry_engine.py:32`).
- **Duplicate timestamps / out-of-order:** Deduplicate by normalized `timestamp` (last-wins), then sort ascending.
- **Invalid OHLCV values:** Drop any bar with non-finite or `<= 0` OHLC, `high < low`, `high < max(open, close)`, `low > min(open, close)`, or non-finite / `< 0` volume.
- **Gaps in candle intervals (Diagnostic-Only Policy in E3A):**
  - Because GeckoTerminal omits zero-swap intervals by default, gaps can reflect legitimate inactivity rather than malformed data, and no empirical threshold exists in the codebase.
  - E3A records recent-window gaps (`closed[-6:]`, `recent_gap_count`, `max_recent_gap_seconds`) as **diagnostic-only** telemetry and **does NOT reject** otherwise valid closed candles due to gaps. Diagnostic gaps never alter `eligible`, `detected`, or `len(closed_5m) < 12` fallback routing, and never become `"insufficient 5m candles"`.

### Q7. Impact on RSI, Breakout Detection, 12-Bar 5m Minimum, 15-Bar 1m Fallback, and 15m Aggregation
- **RSI (`compute_rsi`) & Breakout Detection (`detect_breakout_ignition`):** Computed strictly on closed bars.
- **12-bar 5m minimum & 15-bar 1m fallback:** Evaluated *after* closed-candle filtering, with `should_fetch_1m_fallback()`, `fetch_ohlcv_1m_batch()`, and `alpha_node.py:158` aligned on unique normalized closed 5m count `< 12`.
- **15m Aggregation (`_aggregate_to_15m()`, `breakout_ignition.py:69-91`) & HTF (`_is_htf_15m_bullish()`, `L94-104`):**
  - Positional slicing (`candles[i : i + group_size]`) would falsely combine noncontiguous bars when zero-swap intervals are skipped.
  - **Fix:** Group closed bars by wall-clock 15m bucket (`bucket_ts = int(c["timestamp"] // 900) * 900`, `900s = 15m`) in ascending order. Emit a 15m bar **only** when the bucket contains a complete contiguous set of sub-bars (`{bucket_ts, bucket_ts + 300, bucket_ts + 600}` for 5m; `{bucket_ts + k * 60 for k in range(15)}` for 1m).
  - Incomplete or noncontiguous buckets are excluded from `candles_15m` without rejecting the 5m/1m series; if `candles_15m == []`, `_is_htf_15m_bullish()` safely returns `False` (`L96-97`).

### Q8. Entry Paths That Could Bypass Eligibility Validation
1. **Pre-attached candidate candles** (`alpha_node.py:123,156-159`, `whale_lane.py:378-379`): covered by validating inside `evaluate_scalper_entry()`.
2. **Direct `detect_breakout_ignition()` calls**: covered by fail-closed validation inside `detect_breakout_ignition()`.

### Q9. Required Test Scenarios
Covered in Section 6 (Test Matrix).

### Q10. Closed-Candle Validation vs. Freshness Validation
- **Closed-candle validation** (`timestamp + bar_seconds <= decision_timestamp`) checks interval completion and **is** enforced in E3A.
- **Freshness validation** (`decision_timestamp - (closed[-1]["timestamp"] + bar_seconds) <= max_age`) is deferred (no DuckDB raw-candle telemetry or empirical GeckoTerminal indexing-lag distribution exists in current code).

---

## 4. Proposed Minimal Implementation Design

### 4.1 Dependency-Safe Module & Import Direction
- **`scalper_entry_engine.py` already imports from `breakout_ignition.py` (`L9`).** Do **not** import from `scalper_entry_engine.py` into `breakout_ignition.py`.
- **`ohlcv_client.py`** imports `normalize_and_filter_candles` from `breakout_ignition` (acyclic: `breakout_ignition.py` uses only stdlib + `pydantic` and has zero imports from `ohlcv_client.py`, `scalper_entry_engine.py`, `alpha_node.py`, or `whale_lane.py`).
- **Exact dependency direction:**
  - `breakout_ignition.py`: defines `validate_decision_timestamp()`, `normalize_and_filter_candles()`, `_aggregate_to_15m()`, `detect_breakout_ignition()`.
  - `ohlcv_client.py`: imports `normalize_and_filter_candles` from `breakout_ignition` for `should_fetch_1m_fallback()`.
  - `scalper_entry_engine.py`: imports `detect_breakout_ignition, normalize_and_filter_candles, validate_decision_timestamp` from `breakout_ignition`.
  - `alpha_node.py` & `whale_lane.py`: pass `decision_timestamp=now_dt.timestamp()` to `evaluate_scalper_entry()`, and `alpha_node.py:158` removes the raw `len(candles_5m) < 12` guard when reading `ohlcv_1m_by_pair.get(p_addr, [])`.

### 4.2 Function Contracts
1. **`validate_decision_timestamp(decision_timestamp: Optional[float]) -> tuple[Optional[float], Optional[str]]`** (`breakout_ignition.py`):
   - `None` -> `(time.time(), None)`; invalid (`bool`, non-numeric, non-finite, `<= 0`) -> `(None, "invalid decision_timestamp")`; else `(float(decision_timestamp), None)`.
2. **`normalize_and_filter_candles(raw_candles: Sequence[Any], *, bar_minutes: int, decision_timestamp: Optional[float] = None) -> tuple[list[dict[str, float]], Optional[str], dict[str, Any]]`** (`breakout_ignition.py`):
   - Returns `([], err, {})` safely if `raw_candles` is not a sequence or `decision_timestamp` is invalid; parses without `idx` substitution; drops `bool`/non-numeric/non-finite/`<= 0` timestamps and invalid OHLCV; scales `ts >= 1e12` ms to seconds; deduplicates by unique `timestamp` (last-wins); sorts ascending; filters `ts + bar_minutes * 60 <= ref_ts`; computes diagnostic-only `recent_gap_count` and `max_recent_gap_seconds` over `closed[-6:]`.
3. **`_aggregate_to_15m(candles: Sequence[dict[str, float]], bar_minutes: int) -> list[dict[str, float]]`** (`breakout_ignition.py`):
   - Buckets closed bars by `int(c["timestamp"] // 900) * 900` and emits a 15m bar **only** when all contiguous sub-bars (`3` for 5m, `15` for 1m) are present.
4. **`detect_breakout_ignition(..., decision_timestamp: Optional[float] = None)`** (`breakout_ignition.py`):
   - Runs `normalize_and_filter_candles()` at entry; fails closed on `"invalid decision_timestamp"`.
5. **`evaluate_scalper_entry(..., decision_timestamp: Optional[float] = None)`** (`scalper_entry_engine.py`):
   - Validates `decision_timestamp` once; filters 5m and (when `len(closed_5m) < 12`) 1m via `normalize_and_filter_candles()`; passes `decision_timestamp=ref_ts` to `detect_breakout_ignition()`.
6. **`should_fetch_1m_fallback(pair_address, candles_5m, *, now_ts=None)`** (`ohlcv_client.py`):
   - Safely runs `closed_5m, err, _ = normalize_and_filter_candles(candles_5m or [], bar_minutes=5, decision_timestamp=now_ts)` (never throws on malformed input); returns `False` if `err` or `len(closed_5m) >= 12`; then checks `_global_backoff_until` and `entry_5m["last_ok"] and int(entry_5m["last_status"] or 0) == 200` unchanged.

---

## 5. Data Contract and Failure Behavior

| Input Condition | Behavior under E3A Contract | Output / Telemetry |
| :--- | :--- | :--- |
| `decision_timestamp=None` | Defaults to `time.time()` (UTC seconds); filtering active | Closed-bar filter enforced |
| Invalid `decision_timestamp` (`NaN`, `inf`, `<=0`, `bool`, non-numeric) | Fails closed immediately | `eligible=False`, `detected=False`, `rejection_reason="invalid decision_timestamp"`, `failed_gates=["INVALID_DECISION_TS"]` |
| Forming 5m / 1m bar (`ts + bar_seconds > decision_ts`) | Excluded from `closed` | Evaluated on `closed[-1]`; routes to 1m fallback if `len(closed_5m) < 12` |
| Exact boundary (`ts + bar_seconds == decision_ts`) | **Eligible** (closed) | Included as `closed[-1]` |
| One second before close (`ts + bar_seconds == decision_ts + 1`) | **Ineligible** (forming) | Excluded from `closed` |
| Missing/`bool`/non-finite/`<=0` timestamp or invalid OHLCV | Row dropped safely (no `idx` fallback, no exception) | Excluded before `min_bars` and fallback checks |
| Duplicate `timestamp` / millisecond `ts >= 1e12` | Ms scaled to seconds; last-wins dedup on unique timestamp | Cannot inflate bar count to `>= 12` |
| Interval gap in `closed[-6:]` | **Diagnostic-only** (not rejected) | Valid closed candles evaluated normally; `recent_gap_count` / `max_recent_gap_seconds` recorded diagnostically |
| Noncontiguous / incomplete 15m bucket | Bucket excluded from `candles_15m` | `_is_htf_15m_bullish()` returns `False` if no complete 15m buckets exist; 5m/1m series not rejected |
| 12 raw 5m bars (11 closed + 1 forming) with 5m `200 OK` & no backoff | `should_fetch_1m_fallback()` returns `True` | `fetch_ohlcv_1m_batch()` fetches 1m; `alpha_node` / `whale_lane` pass 1m to `evaluate_scalper_entry()` |
| Pre-attached 5m-only candidate in `alpha_node.py` (`< 12` closed 5m, no `candles_1m`) | Not in `pairs_to_fetch` -> no 5m cache state -> **no 1m request** | `evaluate_scalper_entry()` returns `eligible=False` (`insufficient 5m candles`) |

---

## 6. Test Matrix

| ID | Scenario | Target Module(s) | Key Assertion |
| :--- | :--- | :--- | :--- |
| `T1` | Forming 5m candle would trigger breakout (`ts + 300 > decision_ts`) | `scalper_entry_engine`, `breakout_ignition` | Forming bar dropped; `detected=False`, `eligible=False` |
| `T2` | Forming 1m candle would trigger breakout (`ts + 60 > decision_ts`) | `scalper_entry_engine`, `breakout_ignition` | Forming 1m bar dropped; `detected=False`, `eligible=False` |
| `T3a` | Exact interval boundary: `timestamp + bar_seconds == decision_timestamp` (5m & 1m) | `scalper_entry_engine`, `breakout_ignition` | Candle **is eligible** and evaluated as `closed[-1]` |
| `T3b` | One second before close: `timestamp + bar_seconds == decision_timestamp + 1` (5m & 1m) | `scalper_entry_engine`, `breakout_ignition` | Candle is **not yet eligible** and is dropped |
| `T4` | Stale-but-closed candles (`timestamp + 300 <= decision_timestamp - 600`) | `scalper_entry_engine`, `breakout_ignition` | Retained by closed-candle filter (distinct from `T3a`/`T3b`; freshness threshold out of scope) |
| `T5` | Missing/`bool` timestamp, duplicate timestamps, out-of-order, `NaN`/zero OHLC, ms timestamps, invalid `decision_timestamp` | `scalper_entry_engine`, `breakout_ignition` | Missing/invalid rows dropped (no `idx` fallback); deduped; sorted; ms scaled; invalid `decision_timestamp` fails closed |
| `T6a` | 12 raw 5m bars (11 closed + 1 forming) with `200 OK` & no backoff + 15 closed 1m bars | `ohlcv_client`, `alpha_node`, `whale_lane`, `scalper_entry_engine` | `should_fetch_1m_fallback()` and `fetch_ohlcv_1m_batch()` fetch 1m; `alpha_node`/`whale_lane` pass 1m to `evaluate_scalper_entry()` |
| `T6b` | 12 raw 5m bars (11 closed + 1 forming) during `_global_backoff_until` or after 5m `429`/error | `ohlcv_client`, `alpha_node`, `whale_lane` | `should_fetch_1m_fallback()` and `fetch_ohlcv_1m_batch()` return `False` / make **zero** 1m network calls |
| `T6c` | Cached 5m list with 12+ raw rows containing duplicates, ms timestamps (`>= 1e12`), or malformed/`bool`/`NaN` timestamps leaving `< 12` unique closed 5m bars | `ohlcv_client`, `scalper_entry_engine` | `should_fetch_1m_fallback()` does not throw, counts `< 12` unique closed 5m bars, and permits 1m fallback (when `200 OK` & no backoff) |
| `T6d` | Pre-attached 5m-only candidate in `alpha_node.py` (`< 12` closed 5m bars, `candles_1m=None`) | `alpha_node`, `scalper_entry_engine` | Makes **zero** 5m/1m network requests (no `200 OK` 5m cache state) and returns `eligible=False` (`insufficient 5m candles`) |
| `T7` | Interval gap in 5m `closed[-6:]` with `>= 12` valid closed 5m bars | `scalper_entry_engine`, `breakout_ignition` | Diagnostic gap reported (`recent_gap_count > 0`); valid closed 5m candles **not rejected**, no 1m fallback, no `"insufficient 5m candles"` |
| `T8a` | `_aggregate_to_15m()` with skipped 5m interval (e.g., `12:00`, `12:10`, `12:25`) | `breakout_ignition` | Noncontiguous bars are **not** combined into a synthetic 15m bar |
| `T8b` | `_aggregate_to_15m()` with misaligned timestamps or `< 3` bars in a 15m bucket | `breakout_ignition` | Incomplete 15m bucket excluded; if `candles_15m == []`, `_is_htf_15m_bullish()` returns `False` safely without rejecting 5m series |
| `T9` | Existing callers & fixtures (`alpha_node`, `whale_lane`, `test_breakout_ignition.py`, `test_scalper_entry_engine.py`, `test_breakout_isolation.py`) | All callers & test suites | Callers pass `decision_timestamp`; existing suites pass with explicit `decision_timestamp` aligned to fixtures |

---

## 7. Risks & Unresolved Questions

1. **Hard Implementation Prerequisite — GeckoTerminal Timestamp Contract (`UNVERIFIED`):** `timestamp + bar_seconds <= decision_timestamp` depends on interval-start Unix-second timestamps on `https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute`, which is **UNVERIFIED** from documentation alone (`https://docs.coingecko.com/reference/pool-ohlcv-contract-address`). Live/sample response inspection against wall-clock time is required before implementation.
2. **Pre-attached 5m-only limitation (`alpha_node.py`):** Candidates arriving with pre-attached `candles_5m` (`< 12` closed) and no `candles_1m` do not trigger a 1m network fallback in `alpha_node.py` because no 5m fetch/cache state is established; callers supplying pre-attached partial 5m data must also supply `candles_1m` if 1m fallback is desired.
3. **Existing unit test compatibility:** Existing fixture tests ([test_breakout_ignition.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/tests/test_breakout_ignition.py#L32), [test_scalper_entry_engine.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/tests/test_scalper_entry_engine.py#L57), [test_breakout_isolation.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/tests/test_breakout_isolation.py)) use historical timestamps (`1_700_000_000`, `1_760_000_000`) and omit `decision_timestamp`; passing explicit `decision_timestamp` in tests is recommended for determinism.
4. **Unresolved freshness & gap thresholds:** Optimal `max_close_age` and whether any gap threshold should ever become gating cannot be determined without live telemetry on GeckoTerminal indexing lag, low-cap zero-swap bar omission rates, and `alpha_node` batch pacing duration.

---

## 8. Explicit Out-of-Scope Items

- Implementing any code, configuration, or test changes.
- Defining or enforcing a numeric candle-freshness / staleness threshold or a gating candle-gap threshold.
- Modifying `ohlcv_client.py` cache TTLs, 429 backoff behavior, fetcher return types, or OHLCV providers.
- Tuning `BREAKOUT_IGNITION`, `SWEEP_RECLAIM`, `DISPLACEMENT_RETEST`, RSI, or order-flow thresholds.
- Making live market-data network requests or modifying DuckDB schemas/data.
