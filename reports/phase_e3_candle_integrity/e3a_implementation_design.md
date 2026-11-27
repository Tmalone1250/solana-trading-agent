# Phase E3A — Candle Eligibility Implementation Design (Final)

- **Repository:** `Tmalone1250/solana-trading-agent`
- **Audited Source Branch / Commit:** `dev_2` @ `19104e32e0ac619a19f8586ae3e27d8707af62c0`
- **Audit Branch:** `audit/e3-candle-integrity-20261127`
- **Mode:** Documentation/design correction only (no application source, configuration, test, database, or runtime changes)
- **Prerequisite Audit:** [e3_readonly_audit_report.md](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/reports/phase_e3_candle_integrity/e3_readonly_audit_report.md)

---

## Change Summary (Independent-Review Corrective Pass)

1. **GeckoTerminal Timestamp Contract Reconciled & Marked `UNVERIFIED`:**
   - Reconciled the CoinGecko `/onchain` reference (`https://docs.coingecko.com/reference/pool-ohlcv-contract-address`) with the actual audited endpoint (`https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute`, `ohlcv_client.py:213`).
   - Removed unsupported claims that official documentation explicitly proves interval-start semantics or forming-candle `close` semantics; marked both as **UNVERIFIED** from documentation alone and retained live/sample-response verification as a **hard implementation prerequisite**.
2. **Complete 1m Fallback Call-Path Fix Specified End-to-End:**
   - Audited `alpha_node.py:135,158`, `whale_lane.py:354,379`, `should_fetch_1m_fallback()` (`ohlcv_client.py:118-135`), and `fetch_ohlcv_1m_batch()` (`ohlcv_client.py:334-352`).
   - Identified that `fetch_ohlcv_1m_batch()` re-reads cached raw `c5_cached` and calls `should_fetch_1m_fallback()`, and `alpha_node.py:158` gates on raw `len(candles_5m) < 12`. Specified minimal updates to `should_fetch_1m_fallback()` (using closed 5m count while preserving backoff and `last_ok`/`200` guards) and `alpha_node.py:158` so the 12-raw / 11-closed 5m -> 1m fallback path works end-to-end.
3. **Time-Aligned 15m Aggregation (`_aggregate_to_15m`):**
   - Replaced positional `group_size` slicing in `_aggregate_to_15m()` (`breakout_ignition.py:69-91`) with wall-clock 15m bucket alignment (`bucket_ts = int(ts // 900) * 900`), requiring a complete contiguous set of sub-bars per bucket and excluding incomplete/gapped buckets so `_is_htf_15m_bullish()` fails safely (`False`) without rejecting valid 5m/1m candles.

---

## 1. Executive Summary

In `19104e32e0ac619a19f8586ae3e27d8707af62c0` (`dev_2`), `BREAKOUT_IGNITION` (along with `SWEEP_RECLAIM` and `DISPLACEMENT_RETEST`) evaluates `candles[-1]` without verifying whether that bar's interval has elapsed, without validating `"timestamp"` presence or OHLCV sanity, and without deduplicating timestamps. Under the codebase's interval-start assumption for GeckoTerminal's `/ohlcv/minute` endpoint, an unclosed interval returned at the newest position is placed at `candles[-1]` by `parse_geckoterminal_ohlcv()`, which would allow intrabar price spikes or partial-bar volume to drive `BUY` decisions.

This design defines a minimal, fail-closed **E3A Candle Eligibility** contract that:
1. Captures a single UTC `decision_timestamp` per evaluation cycle and passes it to both 5m and 1m evaluations.
2. Enforces strict closed-candle filtering (`timestamp + 300 <= decision_timestamp` for 5m; `timestamp + 60 <= decision_timestamp` for 1m) inside **both** `evaluate_scalper_entry()` and `detect_breakout_ignition()`, defaulting to `time.time()` when omitted and failing closed on invalid timestamps (subject to verifying the provider's interval-start contract as a hard prerequisite).
3. Makes the 12-raw / 11-closed 5m -> 1m fallback path coherent across `should_fetch_1m_fallback()`, `fetch_ohlcv_1m_batch()`, `alpha_node.py`, `whale_lane.py`, and `evaluate_scalper_entry()` without broadening requests during backoff or 5m failures.
4. Hardens normalization against missing timestamps (removing `float(c.get("timestamp", idx))`), duplicate timestamps, out-of-order rows, and invalid/non-finite OHLCV values, records recent-window gaps as **diagnostic-only** telemetry, and fixes `_aggregate_to_15m()` to use time-aligned 15m buckets rather than positional grouping across noncontiguous bars.
5. Preserves existing `ohlcv_client.py` cache TTLs, 429/backoff semantics, fetcher return types, and strategy thresholds, while leaving freshness thresholds undecided pending empirical telemetry.

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
  │    └─ [E3A] Checks closed 5m count < 12 + _global_backoff_until + entry_5m last_ok & status==200
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
  ├─ fetch_ohlcv_batch(pair_addrs, config, client, now_ts=now_dt.timestamp()) (L351)
  ├─ should_fetch_1m_fallback(a, ohlcv_5m.get(a) or [], now_ts=now_dt.timestamp()) (L352-355)
  ├─ fetch_ohlcv_1m_batch(short_addrs, config, client, now_ts=now_dt.timestamp()) (L356)
  ├─ Resolves c5 / c1 (pre-attached pair.get("candles_5m"/"candles_1m") or ohlcv_5m/ohlcv_1m.get(p_addr, [])) (L378-379)
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

### Q3. Where `decision_timestamp` Must Be Passed (Including Complete 1m Fallback Path)
- `alpha_node.py:115` captures `now_dt = now or utc_now()` once per cycle and passes `now_dt.timestamp()` to `fetch_ohlcv_batch()` (`L134`), `should_fetch_1m_fallback()` (`L135`), `fetch_ohlcv_1m_batch()` (`L137`), and must pass `decision_timestamp=now_dt.timestamp()` to `evaluate_scalper_entry()` (`L161`).
- `whale_lane.py:351,354,356` already passes `now_dt.timestamp()` to `fetch_ohlcv_batch()`, `should_fetch_1m_fallback()`, and `fetch_ohlcv_1m_batch()`, and must pass `decision_timestamp=now_dt.timestamp()` to `evaluate_scalper_entry()` (`L381`).
- **Complete 1m fallback path consistency (`12` raw 5m bars = `11` closed + `1` forming):**
  1. `should_fetch_1m_fallback(pair_address, candles_5m, *, now_ts=None)` (`ohlcv_client.py:118-135`): currently checks raw `len(candles_5m) >= 12` (`L126`). Because `fetch_ohlcv_1m_batch()` (`L346-347`) re-reads raw `c5_cached` from `_ohlcv_cache` and calls `should_fetch_1m_fallback(addr, c5_cached, now_ts=now_ts)`, updating `should_fetch_1m_fallback()` to count **closed** 5m bars (`timestamp + 300 <= ref_ts`, where `ref_ts = now_ts if now_ts is not None else time.time()`) ensures `alpha_node.py:135`, `whale_lane.py:354`, and `fetch_ohlcv_1m_batch():347` all agree, while strictly preserving `_global_backoff_until` and `entry_5m["last_ok"] and last_status == 200` guards (`L128-134`).
  2. `alpha_node.py:158`: currently checks `if candles_1m is None and len(candles_5m) < 12:` using raw `len(candles_5m)`. If `candles_5m` has 12 raw bars (11 closed + 1 forming), `L158` would skip reading `ohlcv_1m_by_pair.get(p_addr, [])`! Changing `L158` to `if candles_1m is None: candles_1m = ohlcv_1m_by_pair.get(p_addr, [])` (safe because `ohlcv_1m_by_pair` only holds pools in `short_pairs`, and `evaluate_scalper_entry()` only uses `candles_1m` when `len(closed_5m) < 12`) completes the end-to-end 1m fallback path. (`whale_lane.py:379` already reads `ohlcv_1m.get(p_addr, [])` without a raw-length guard.)

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
- **12-bar 5m minimum & 15-bar 1m fallback:** Evaluated *after* closed-candle filtering, with `should_fetch_1m_fallback()`, `fetch_ohlcv_1m_batch()`, and `alpha_node.py:158` aligned on closed 5m count `< 12`.
- **15m Aggregation (`_aggregate_to_15m()`, `breakout_ignition.py:69-91`) & HTF (`_is_htf_15m_bullish()`, `L94-104`):**
  - Because diagnostic-only gaps allow valid closed 5m/1m candles with skipped intervals to enter `detect_breakout_ignition()`, positional slicing (`candles[i : i + group_size]`) would falsely combine noncontiguous bars (e.g., `[12:00, 12:10, 12:25]`) into a single synthetic 15m bar.
  - **Fix:** Group closed bars by wall-clock 15m bucket (`bucket_ts = int(c["timestamp"] // 900) * 900`, `900s = 15m`) in ascending order. A 15m bucket is emitted **only** if it contains a complete contiguous set of sub-bars:
    - `bar_minutes == 5`: exact timestamps `{bucket_ts, bucket_ts + 300, bucket_ts + 600}` (3 bars).
    - `bar_minutes == 1`: exact timestamps `{bucket_ts + k * 60 for k in range(15)}` (15 bars).
  - Incomplete or noncontiguous buckets are **excluded** from `candles_15m` (without rejecting the underlying 5m/1m series).
  - If no complete 15m bars can be formed (`candles_15m == []`), `_is_htf_15m_bullish(candles_15m, price)` safely returns `False` (`L96-97`), withholding the 7-point `HTF` bonus rather than fabricating a bullish 15m signal from gapped bars.

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
- Place `validate_decision_timestamp()` and `normalize_and_filter_candles()` in **`breakout_ignition.py`** (stdlib + `pydantic` only).
- **Exact dependency direction:**
  - `breakout_ignition.py`: defines `validate_decision_timestamp()`, `normalize_and_filter_candles()`, `_aggregate_to_15m()`, `detect_breakout_ignition()`.
  - `scalper_entry_engine.py`: imports `detect_breakout_ignition, normalize_and_filter_candles, validate_decision_timestamp` from `breakout_ignition`.
  - `ohlcv_client.py`: `should_fetch_1m_fallback()` counts closed 5m bars inline (`float(c["timestamp"]) + 300.0 <= ref_ts` for valid finite timestamps) to avoid adding cross-module imports into `ohlcv_client.py` while keeping `fetch_ohlcv_1m_batch()` and `alpha_node.py` / `whale_lane.py` consistent.
  - `alpha_node.py` & `whale_lane.py`: pass `decision_timestamp=now_dt.timestamp()` to `evaluate_scalper_entry()`, and `alpha_node.py:158` removes the raw `len(candles_5m) < 12` guard when reading `ohlcv_1m_by_pair.get(p_addr, [])`.

### 4.2 Function Contracts
1. **`validate_decision_timestamp(decision_timestamp: Optional[float]) -> tuple[Optional[float], Optional[str]]`** (`breakout_ignition.py`):
   - `None` -> `(time.time(), None)`; invalid (`bool`, non-numeric, non-finite, `<= 0`) -> `(None, "invalid decision_timestamp")`; else `(float(decision_timestamp), None)`.
2. **`normalize_and_filter_candles(raw_candles: Sequence[Any], *, bar_minutes: int, decision_timestamp: Optional[float] = None) -> tuple[list[dict[str, float]], Optional[str], dict[str, Any]]`** (`breakout_ignition.py`):
   - Validates `decision_timestamp`; parses without `idx` substitution; scales `ts >= 1e12` ms to seconds; drops invalid OHLCV; deduplicates by `timestamp` (last-wins); sorts ascending; filters `ts + bar_minutes * 60 <= ref_ts`; computes diagnostic-only `recent_gap_count` and `max_recent_gap_seconds` over `closed[-6:]`.
3. **`_aggregate_to_15m(candles: Sequence[dict[str, float]], bar_minutes: int) -> list[dict[str, float]]`** (`breakout_ignition.py`):
   - Buckets closed bars by `int(c["timestamp"] // 900) * 900` and emits a 15m bar **only** when all contiguous sub-bars (`3` for 5m, `15` for 1m) are present for that bucket.
4. **`detect_breakout_ignition(..., decision_timestamp: Optional[float] = None)`** (`breakout_ignition.py`):
   - Runs `normalize_and_filter_candles()` at entry; fails closed on `"invalid decision_timestamp"`.
5. **`evaluate_scalper_entry(..., decision_timestamp: Optional[float] = None)`** (`scalper_entry_engine.py`):
   - Validates `decision_timestamp` once; filters 5m and (when `len(closed_5m) < 12`) 1m via `normalize_and_filter_candles()`; passes `decision_timestamp=ref_ts` to `detect_breakout_ignition()`.
6. **`should_fetch_1m_fallback(pair_address, candles_5m, *, now_ts=None)`** (`ohlcv_client.py`):
   - Counts valid closed 5m bars (`ts + 300.0 <= ref_ts`) instead of raw `len(candles_5m)`, preserving `_global_backoff_until` and `entry_5m["last_ok"] and last_status == 200` checks.

---

## 5. Data Contract and Failure Behavior

| Input Condition | Behavior under E3A Contract | Output / Telemetry |
| :--- | :--- | :--- |
| `decision_timestamp=None` | Defaults to `time.time()` (UTC seconds); filtering active | Closed-bar filter enforced |
| Invalid `decision_timestamp` (`NaN`, `inf`, `<=0`, `bool`, non-numeric) | Fails closed immediately | `eligible=False`, `detected=False`, `rejection_reason="invalid decision_timestamp"`, `failed_gates=["INVALID_DECISION_TS"]` |
| Forming 5m / 1m bar (`ts + bar_seconds > decision_ts`) | Excluded from `closed` | Evaluated on `closed[-1]`; routes to 1m fallback if `len(closed_5m) < 12` |
| Exact boundary (`ts + bar_seconds == decision_ts`) | **Eligible** (closed) | Included as `closed[-1]` |
| One second before close (`ts + bar_seconds == decision_ts + 1`) | **Ineligible** (forming) | Excluded from `closed` |
| Missing `"timestamp"` or invalid OHLCV (`<=0`, `NaN`, `high < low`, `vol < 0`) | Row dropped (no `idx` fallback) | Excluded before `min_bars` check |
| Duplicate `timestamp` / out-of-order rows | Last-wins dedup, sorted ascending | Deterministic oldest-first series |
| Interval gap in `closed[-6:]` | **Diagnostic-only** (not rejected) | Valid closed candles evaluated normally; `recent_gap_count` / `max_recent_gap_seconds` recorded diagnostically |
| Noncontiguous / incomplete 15m bucket | Bucket excluded from `candles_15m` | `_is_htf_15m_bullish()` returns `False` if no complete 15m buckets exist; 5m/1m series not rejected |
| 12 raw 5m bars (11 closed + 1 forming) with 5m `200 OK` & no backoff | `should_fetch_1m_fallback()` returns `True` | `fetch_ohlcv_1m_batch()` fetches 1m; `alpha_node` / `whale_lane` pass 1m to `evaluate_scalper_entry()` |

---

## 6. Test Matrix

| ID | Scenario | Target Module(s) | Key Assertion |
| :--- | :--- | :--- | :--- |
| `T1` | Forming 5m candle would trigger breakout (`ts + 300 > decision_ts`) | `scalper_entry_engine`, `breakout_ignition` | Forming bar dropped; `detected=False`, `eligible=False` |
| `T2` | Forming 1m candle would trigger breakout (`ts + 60 > decision_ts`) | `scalper_entry_engine`, `breakout_ignition` | Forming 1m bar dropped; `detected=False`, `eligible=False` |
| `T3a` | Exact interval boundary: `timestamp + bar_seconds == decision_timestamp` (5m & 1m) | `scalper_entry_engine`, `breakout_ignition` | Candle **is eligible** and evaluated as `closed[-1]` |
| `T3b` | One second before close: `timestamp + bar_seconds == decision_timestamp + 1` (5m & 1m) | `scalper_entry_engine`, `breakout_ignition` | Candle is **not yet eligible** and is dropped |
| `T4` | Stale-but-closed candles (`timestamp + 300 <= decision_timestamp - 600`) | `scalper_entry_engine`, `breakout_ignition` | Retained by closed-candle filter (distinct from `T3a`/`T3b`; freshness threshold out of scope) |
| `T5` | Missing `"timestamp"`, duplicate timestamps, out-of-order, `NaN`/zero OHLC, ms timestamps, invalid `decision_timestamp` | `scalper_entry_engine`, `breakout_ignition` | Missing/invalid rows dropped (no `idx` fallback); deduped; sorted; ms scaled; invalid `decision_timestamp` fails closed |
| `T6a` | 12 raw 5m bars (11 closed + 1 forming) with `200 OK` & no backoff + 15 closed 1m bars | `ohlcv_client`, `alpha_node`, `whale_lane`, `scalper_entry_engine` | `should_fetch_1m_fallback()` and `fetch_ohlcv_1m_batch()` fetch 1m; `alpha_node`/`whale_lane` pass 1m to `evaluate_scalper_entry()` |
| `T6b` | 12 raw 5m bars (11 closed + 1 forming) during `_global_backoff_until` or after 5m `429`/error | `ohlcv_client`, `alpha_node`, `whale_lane` | `should_fetch_1m_fallback()` and `fetch_ohlcv_1m_batch()` still return `False` / make **zero** 1m network calls |
| `T7` | Interval gap in 5m `closed[-6:]` with `>= 12` valid closed 5m bars | `scalper_entry_engine`, `breakout_ignition` | Diagnostic gap reported (`recent_gap_count > 0`); valid closed 5m candles **not rejected**, no 1m fallback, no `"insufficient 5m candles"` |
| `T8a` | `_aggregate_to_15m()` with skipped 5m interval (e.g., `12:00`, `12:10`, `12:25`) | `breakout_ignition` | Noncontiguous bars are **not** combined into a synthetic 15m bar |
| `T8b` | `_aggregate_to_15m()` with misaligned timestamps or `< 3` bars in a 15m bucket | `breakout_ignition` | Incomplete 15m bucket excluded; if `candles_15m == []`, `_is_htf_15m_bullish()` returns `False` safely without rejecting 5m series |
| `T9` | Existing callers & fixtures (`alpha_node`, `whale_lane`, `test_breakout_ignition.py`, `test_scalper_entry_engine.py`, `test_breakout_isolation.py`) | All callers & test suites | Callers pass `decision_timestamp`; existing suites pass with explicit `decision_timestamp` aligned to fixtures |

---

## 7. Risks & Unresolved Questions

1. **Hard Implementation Prerequisite — GeckoTerminal Timestamp Contract (`UNVERIFIED`):** `timestamp + bar_seconds <= decision_timestamp` depends on interval-start Unix-second timestamps on `https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute`, which is **UNVERIFIED** from documentation alone (`https://docs.coingecko.com/reference/pool-ohlcv-contract-address`). Live/sample response inspection against wall-clock time is required before implementation.
2. **Existing unit test compatibility:** Existing fixture tests ([test_breakout_ignition.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/tests/test_breakout_ignition.py#L32), [test_scalper_entry_engine.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/tests/test_scalper_entry_engine.py#L57), [test_breakout_isolation.py](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/tests/test_breakout_isolation.py)) use historical timestamps (`1_700_000_000`, `1_760_000_000`) and omit `decision_timestamp`; passing explicit `decision_timestamp` in tests is recommended for determinism.
3. **Unresolved freshness & gap thresholds:** Optimal `max_close_age` and whether any gap threshold should ever become gating cannot be determined without live telemetry on GeckoTerminal indexing lag, low-cap zero-swap bar omission rates, and `alpha_node` batch pacing duration.

---

## 8. Explicit Out-of-Scope Items

- Implementing any code, configuration, or test changes.
- Defining or enforcing a numeric candle-freshness / staleness threshold or a gating candle-gap threshold.
- Modifying `ohlcv_client.py` cache TTLs, 429 backoff behavior, fetcher return types, or OHLCV providers.
- Tuning `BREAKOUT_IGNITION`, `SWEEP_RECLAIM`, `DISPLACEMENT_RETEST`, RSI, or order-flow thresholds.
- Making live market-data network requests or modifying DuckDB schemas/data.
