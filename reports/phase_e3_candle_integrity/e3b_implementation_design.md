# Phase E3B — Cache Provenance & Stale-Data Handling Implementation Design

- **Repository:** `Tmalone1250/solana-trading-agent`
- **Design Branch:** `design/e3b-cache-provenance`
- **Source Baseline:** `impl/e3a-candle-eligibility` @ `6492acd4f39522aef71bb814bb567288f7b5993a`
- **Scope:** Strictly Phase E3B (cache provenance, stale-fallback blocking, pre-attached provenance enforcement, whale-lane & telemetry-cache provenance). Excludes E3C parser hardening, empty-200 handling, timestamp parsing, deduplication, interval-spacing validation, and strategy threshold tuning.

---

## 1. Current Behavior & Confirmed Defects

1. **`_fetch_ohlcv_interval()` (`ohlcv_client.py:179-296`) drops provenance on stale returns:**
   - Returns `(prev_candles, False)` during `_global_backoff_until` (`L202-209`, `L228-229`)—identical in signature to a `< 90s` fresh cache hit (`L212-219`).
   - Returns `(prev_candles, True)` on HTTP `429` (`L246-267`) and provider/network exceptions (`L281-296`)—identical in signature to a live `200 OK` fetch (`L280`).
   - Preserves `_ohlcv_cache` entries up to `max(600.0, 2 * OHLCV_CACHE_SECONDS)` = `600s` (`L19, L145-152`) for stale-while-revalidate serving, conflating cache retention with entry eligibility.
2. **`fetch_ohlcv_batch()` (`L325-339`) and `fetch_ohlcv_1m_batch()` (`L342-360`) discard all fetch-status metadata:**
   - Both drop `did_network` and return `dict[str, list[dict[str, float]]]`, making `network_200`, `cache_fresh`, `stale_backoff`, `stale_429`, and `stale_error` indistinguishable downstream.
3. **`fetch_ohlcv_1m_batch()` (`L354-357`) silently serves unflagged stale `c1_cached`:**
   - When `should_fetch_1m_fallback(addr, c5_cached, now_ts=now_ts)` returns `False` (active backoff or failed 5m fetch), `L356` returns `c1_cached` directly from `_ohlcv_cache` without verifying freshness or eligibility.
4. **`alpha_node()` (`alpha_node.py:123, 156-168`) bypasses fetching and provenance for pre-attached candles:**
   - Candidates with `cand.get("candles_5m") is not None` skip `fetch_ohlcv_batch()` (`L123`) and pass directly into `evaluate_scalper_entry()` (`L161-168`) with no provenance validation.
5. **`whale_lane.py` (`whale_lane.py:350-436`) treats stale cached candles as live OHLCV:**
   - Sets `has_ohlcv = bool(c5 or c1)` (`L380, L416`) without checking whether `c5`/`c1` came from a stale fallback, gating `WHALE_SCALP` (`L416-436`) and overwriting `_breakout_telemetry_cache` (`L391`) with stale-derived telemetry.
6. **`_breakout_telemetry_cache` (`ohlcv_client.py:31, 67-106`, `telemetry_api.py:239`) hides age and provenance:**
   - `get_cached_breakout_telemetry()` returns a plain `dict` across a `1800s` (`30m`) TTL with no `fetched_at`, `age_seconds`, or `source` metadata.

---

## 2. Proposed API & Data Contract

### 2.1 Minimal Typed Provenance Contract (`ohlcv_client.py`)
```python
OhlcvSource = Literal[
    "network_200",
    "cache_fresh",
    "stale_backoff",
    "stale_429",
    "stale_error",
    "unavailable",
    "pre_attached_verified",
    "pre_attached_unknown",
]

@dataclass(frozen=True)
class OhlcvFetchResult:
    candles: list[dict[str, float]]
    source: OhlcvSource
    fetched_at: Optional[float]       # timestamp of this cache/fetch record
    last_ok_at: Optional[float]       # timestamp of last successful 200 OK fetch
    last_status: Optional[int]        # 200, 429, 0 (exception), or None
    cache_age_seconds: Optional[float] # max(0.0, ref_ts - last_ok_at) when last_ok_at is not None
    is_entry_eligible: bool           # True iff source in ("network_200", "cache_fresh", "pre_attached_verified")
```

- **Cache entry (`_ohlcv_cache[cache_key]`):** Track `last_ok_at: Optional[float]` alongside `fetched_at`, `candles`, `last_ok`, `last_status`. On `200 OK`, set `last_ok_at = check_ts`. On `429` or `Exception`, preserve prior `last_ok_at` (or `None` if never succeeded).
- **Interval & batch APIs:**
  - Add `_fetch_ohlcv_interval_with_provenance(...) -> OhlcvFetchResult`, `fetch_ohlcv_batch_with_provenance(...) -> dict[str, OhlcvFetchResult]`, and `fetch_ohlcv_1m_batch_with_provenance(...) -> dict[str, OhlcvFetchResult]`.
  - Keep `fetch_ohlcv_5m()`, `fetch_ohlcv_1m()`, `fetch_ohlcv_batch()`, and `fetch_ohlcv_1m_batch()` intact for backward compatibility with existing unit tests.
  - In `fetch_ohlcv_1m_batch_with_provenance()`, when `should_fetch_1m_fallback(addr, c5_cached, now_ts=now_ts)` is `False`, return `OhlcvFetchResult(candles=[], source="unavailable", fetched_at=None, last_ok_at=None, last_status=None, cache_age_seconds=None, is_entry_eligible=False)`—never return unflagged `c1_cached`.

---

## 3. Data-Flow Changes & Enforcement Boundaries

### 3.1 Strategy Boundary (`scalper_entry_engine.py:evaluate_scalper_entry`)
- Add `ohlcv_provenance: Optional[OhlcvFetchResult] = None`.
- **Fail-closed enforcement (no silent production bypass):**
  - If `ohlcv_provenance is None` or `not ohlcv_provenance.is_entry_eligible` (including `stale_backoff`, `stale_429`, `stale_error`, `unavailable`, and `pre_attached_unknown`), `evaluate_scalper_entry()` **must** return `eligible=False, setup_type="NONE"`, with `rejection_reason=f"ineligible OHLCV provenance ({source})"`—even if candle timestamps satisfy E3A's closed-bar filter.
  - Note: `breakout_telemetry` may still be computed for observability if candles are present, but `eligible` is strictly `False` and `setup_type` is `"NONE"`.

### 3.2 Pre-Attached Candle Handling & Test Ergonomics (`alpha_node.py`, `whale_lane.py`, `tests/`)
- **Production rule:** Raw pre-attached `cand["candles_5m"]` / `cand["candles_1m"]` without verified provenance are wrapped as `OhlcvFetchResult(candles=..., source="pre_attached_unknown", ..., is_entry_eligible=False)` and **cannot** produce a `BREAKOUT_IGNITION` (or chart setup) `BUY` approval.
- **How tests supply trusted provenance without weakening production validation:**
  - Direct unit tests of `evaluate_scalper_entry()` pass `ohlcv_provenance=make_verified_ohlcv_result(candles)` (where `source="pre_attached_verified", last_status=200, is_entry_eligible=True`).
  - `alpha_node` / `whale_lane` tests that inject pre-attached candles either attach `cand["candles_5m_provenance"]` / `cand["candles_1m_provenance"]` (an explicit `OhlcvFetchResult` with `source="pre_attached_verified", is_entry_eligible=True`) or mock `httpx.MockTransport` so `fetch_ohlcv_batch_with_provenance()` returns `source="network_200", is_entry_eligible=True`.
  - Production scanner/safety pipelines never set `candles_5m_provenance`, ensuring unknown pre-attached data can never bypass production validation.

### 3.3 `alpha_node.py` Boundary
- Call `fetch_ohlcv_batch_with_provenance()` and `fetch_ohlcv_1m_batch_with_provenance()`.
- Resolve active `OhlcvFetchResult` per candidate:
  - Pre-attached `candles_5m`: use `cand.get("candles_5m_provenance")` if valid `OhlcvFetchResult`, else `source="pre_attached_unknown", is_entry_eligible=False`.
  - Fetched 5m: if `len(closed_5m) >= 12`, use `res_5m`. If `< 12` closed 5m bars: use `res_1m` if `p_addr in ohlcv_1m_by_pair` and `res_5m.is_entry_eligible`; otherwise pass `res_5m` (preserving `stale_backoff`/`stale_429`/`stale_error` rather than masking it as `"insufficient 5m candles"`).
- Call `cache_breakout_telemetry()` (`L170`) **only** when `active_provenance.is_entry_eligible is True`.

---

## 4. Freshness Policy & Unresolved Evidence Requirements

- **Preserve 90s network cache (`OHLCV_CACHE_SECONDS = 90.0`):** No evidence justifies changing the 90s network-fetch cache TTL. Retention in `_ohlcv_cache` (`600s` max TTL) is solely for eviction/inspection and **never** grants entry permission.
- **Three distinct temporal concepts:**
  1. **HTTP/cache entry age (`cache_age_seconds = decision_ts - last_ok_at`):** Age of the last `200 OK` provider response.
  2. **Newest closed-candle age (`decision_ts - (closed[-1]["timestamp"] + bar_seconds)`):** Time elapsed since the newest closed bar finished (can be large on illiquid/halted pools even when `cache_age_seconds == 0`).
  3. **E3A closed-candle rule (`timestamp + bar_seconds <= decision_ts`):** Structural invariant ensuring the bar's interval has ended.
- **Conservative fail-closed default until evidence exists:**
  - Require `is_entry_eligible=True` (`source in ("network_200", "cache_fresh", "pre_attached_verified")`) AND `cache_age_seconds <= OHLCV_CACHE_SECONDS` (`90.0s`).
  - Do **not** invent an arbitrary closed-candle-age threshold in E3B. Selecting a closed-candle freshness cutoff requires empirical telemetry on (a) GeckoTerminal indexing lag after bar close, (b) live `alpha_node` batch pacing duration (`6.5s * N` pools), and (c) zero-swap bar omission rates on target Solana pools.

---

## 5. Whale-Lane Policy (`whale_lane.py`)

- In `whale_lane.py:416, 427`, OHLCV breakout gating (`WHALE_BLOCK_EXTENDED`, `WHALE_MIN_BREAKOUT_SCORE`) is an **optional quality/anti-chase signal** applied only `if has_ohlcv` (independent whale-confluence entries proceed when OHLCV is cold/absent).
- **E3B rule:** When fetched OHLCV has `is_entry_eligible=False` (or pre-attached OHLCV has `pre_attached_unknown`):
  - Set `has_ohlcv = False` so stale/unknown OHLCV is **not** used as fresh breakout evidence.
  - Do **not** overwrite `_breakout_telemetry_cache` (`L391`) with stale-derived telemetry.
  - Do **not** automatically disable independent whale-confluence entries (distinguishing an unavailable optional quality signal from a failed mandatory gate).

---

## 6. Breakout Telemetry Cache Policy (`ohlcv_client.py`, `telemetry_api.py`)

- Store `{"fetched_at": ts, "source": source, "last_ok_at": last_ok_at, "telemetry": dict(telemetry)}` in `_breakout_telemetry_cache` (only written when `is_entry_eligible=True`).
- Add `BreakoutTelemetryCacheEntry(telemetry, fetched_at, age_seconds, source, is_historical=True)` returned by `get_cached_breakout_telemetry_with_meta(mint, now_ts=...)` (keeping `get_cached_breakout_telemetry()` intact for existing callers).
- Surface `telemetry_age_seconds`, `telemetry_source`, and `is_historical=True` in `whale_lane.py` / `telemetry_api.py` when displaying cached telemetry so historical snapshots (up to `1800s`) are never represented as current evaluations.

---

## 7. Test Plan & Acceptance Criteria

| ID | Scenario | Acceptance Criteria |
| :--- | :--- | :--- |
| `B1` | Successful `200 OK` network fetch | `source="network_200"`, `last_status=200`, `is_entry_eligible=True`; `BUY` allowed |
| `B2` | Fresh cache hit (`< 90s`, `last_ok=True`) | `source="cache_fresh"`, `cache_age_seconds < 90`, `is_entry_eligible=True`; `BUY` allowed |
| `B3` | Expired cache (`> 90s`) + `200 OK` refresh | `source="network_200"`, `last_ok_at` updated, `is_entry_eligible=True` |
| `B4` | HTTP `429` with cached breakout candles | `source="stale_429"`, `last_status=429`, `is_entry_eligible=False`; `alpha_node` blocks `BUY` |
| `B5` | Provider/network error or timeout with cached candles | `source="stale_error"`, `last_status=0`, `is_entry_eligible=False`; `alpha_node` blocks `BUY` |
| `B6` | Global backoff (`_global_backoff_until`) with cached candles | `source="stale_backoff"`, `is_entry_eligible=False`; `alpha_node` blocks `BUY` |
| `B7` | `fetch_ohlcv_1m_batch_with_provenance()` when `should_fetch_1m_fallback()` is `False` (with `c1_cached`) | Returns `source="unavailable"`, `candles=[]`, `is_entry_eligible=False` (never returns stale `c1_cached`) |
| `B8` | Pre-attached trusted candles (`pre_attached_verified`) | `is_entry_eligible=True`; tests pass without weakening production validation |
| `B9` | Pre-attached unknown-provenance candles (`pre_attached_unknown` / omitted `ohlcv_provenance`) | `is_entry_eligible=False`; `evaluate_scalper_entry()` and `alpha_node()` fail closed |
| `B10` | `whale_lane.py` with stale OHLCV (`stale_429` / `stale_backoff`) | `has_ohlcv=False`, `_breakout_telemetry_cache` not overwritten, independent whale entry not falsely blocked |
| `B11` | `_breakout_telemetry_cache` age & provenance | `get_cached_breakout_telemetry_with_meta()` exposes `age_seconds`, `source`, `is_historical=True` |

---

## 8. Smallest File Set & Explicit Out-of-Scope Items

- **Smallest file set:** `ohlcv_client.py`, `scalper_entry_engine.py`, `alpha_node.py`, `whale_lane.py`, `telemetry_api.py`, plus focused E3B tests (and updating test fixtures that call `evaluate_scalper_entry()` / `alpha_node()` with pre-attached candles to supply `pre_attached_verified`).
- **Explicit Out-of-Scope Items:**
  - E3C parser hardening, empty-`200` negative caching, timestamp parsing, deduplication, and interval-spacing rules.
  - Changing `OHLCV_CACHE_SECONDS` (`90s`), `OHLCV_CACHE_MAX_TTL_SECONDS` (`600s`), `429` backoff, or strategy thresholds (`BREAKOUT_*`, `SCALPER_*`, `WHALE_*`).
  - Any source code implementation in this design pass.
