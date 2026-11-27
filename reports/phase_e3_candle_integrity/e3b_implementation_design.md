# Phase E3B — Cache Provenance & Stale-Data Handling Implementation Design (Revised)

- **Repository:** `Tmalone1250/solana-trading-agent`
- **Design Branch:** `design/e3b-cache-provenance`
- **Source Baseline:** `impl/e3a-candle-eligibility` @ `6492acd4f39522aef71bb814bb567288f7b5993a`
- **Scope:** Strictly Phase E3B (cache provenance, stale-fallback blocking, pre-attached provenance enforcement, whale-lane & telemetry-cache provenance). Excludes E3C parser hardening, empty-200 handling, timestamp parsing, deduplication, interval-spacing validation, and strategy threshold tuning.

---

## Change Summary (Corrective Revision)

1. **Authoritative Eligibility Calculation:** Replaced independently settable `is_entry_eligible` with a centralized `evaluate_ohlcv_provenance()` validator (and derived check) based strictly on `source`, `last_ok_at`, `last_status`, and `decision_timestamp - last_ok_at <= OHLCV_CACHE_SECONDS`, preventing contradictory `source` + `is_entry_eligible` states.
2. **Independent 1m Provenance Enforcement:** Required `alpha_node.py` to independently validate the 1m `OhlcvFetchResult` via `evaluate_ohlcv_provenance()` when 1m fallback is selected; a fresh 5m result never authorizes stale, unavailable, or unknown-provenance 1m candles.
3. **Trusted Pre-Attached Provenance:** Prohibited trusting candidate-supplied booleans or arbitrary serialized dict metadata; required an in-memory `OhlcvFetchResult` instance preserving `source`, `last_ok_at`, and `last_status=200`, subject to the exact same `last_ok_at` age validation as fetched candles.
4. **Telemetry Source-Data Timestamp:** Separated `cached_at` (insertion time) from `ohlcv_last_ok_at` and `ohlcv_source` in `_breakout_telemetry_cache`, calculating `data_age_seconds` from `ohlcv_last_ok_at` so telemetry computed from older candles is never labeled current even if inserted recently.

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
6. **`_breakout_telemetry_cache` (`ohlcv_client.py:31, 67-106`, `telemetry_api.py:239`) hides age and source-data provenance:**
   - `get_cached_breakout_telemetry()` returns a plain `dict` across a `1800s` (`30m`) TTL with no `ohlcv_last_ok_at`, `data_age_seconds`, or `ohlcv_source` metadata.

---

## 2. Proposed API & Authoritative Provenance Contract

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

ELIGIBLE_OHLCV_SOURCES = frozenset({"network_200", "cache_fresh", "pre_attached_verified"})

@dataclass(frozen=True)
class OhlcvFetchResult:
    candles: list[dict[str, float]]
    source: OhlcvSource
    fetched_at: Optional[float]   # timestamp of this cache/fetch record
    last_ok_at: Optional[float]   # timestamp of last successful 200 OK fetch
    last_status: Optional[int]    # 200, 429, 0 (exception), or None

    def age_seconds(self, decision_timestamp: float) -> Optional[float]:
        if self.last_ok_at is None or isinstance(self.last_ok_at, bool):
            return None
        if not math.isfinite(self.last_ok_at) or self.last_ok_at <= 0.0:
            return None
        return decision_timestamp - self.last_ok_at
```
- **No independently settable `is_entry_eligible` field:** Callers cannot construct a contradictory `OhlcvFetchResult` (e.g., `source="stale_429"` with `is_entry_eligible=True`). Eligibility is **always** computed authoritatively by `evaluate_ohlcv_provenance()`.

### 2.2 Centralized Authoritative Validator (`evaluate_ohlcv_provenance`)
```python
def evaluate_ohlcv_provenance(
    result: Any,
    *,
    decision_timestamp: float,
    max_cache_age_seconds: float = 90.0,
    clock_skew_seconds: float = 5.0,
) -> tuple[bool, Optional[str]]:
```
Validation rules (all fail closed):
1. `result is None` → `(False, "missing OHLCV provenance")`.
2. `not isinstance(result, OhlcvFetchResult)` → `(False, "invalid OHLCV provenance type")` (rejects raw dicts/booleans).
3. `result.source not in ELIGIBLE_OHLCV_SOURCES` → `(False, f"ineligible OHLCV source ({result.source})")` (rejects `stale_backoff`, `stale_429`, `stale_error`, `unavailable`, `pre_attached_unknown`, or any unknown string).
4. `result.last_status != 200` → `(False, f"ineligible OHLCV status ({result.last_status})")`.
5. `result.last_ok_at` missing, `bool`, non-finite, or `<= 0` → `(False, "missing or invalid OHLCV last_ok_at")`.
6. `age = decision_timestamp - result.last_ok_at`:
   - If `age < -clock_skew_seconds` → `(False, "future OHLCV last_ok_at")`.
   - If `age > max_cache_age_seconds` (`OHLCV_CACHE_SECONDS`, default `90.0s`) → `(False, f"expired OHLCV cache ({age:.1f}s > {max_cache_age_seconds:.1f}s)")`.
7. Otherwise → `(True, None)`.

### 2.3 Interval & Batch Fetchers (`ohlcv_client.py`)
- Track `last_ok_at: Optional[float]` in `_ohlcv_cache[cache_key]`. On `200 OK`, set `last_ok_at = check_ts`. On `429` or `Exception`, preserve prior `last_ok_at` (or `None` if never succeeded).
- Add `_fetch_ohlcv_interval_with_provenance(...) -> OhlcvFetchResult`, `fetch_ohlcv_batch_with_provenance(...) -> dict[str, OhlcvFetchResult]`, and `fetch_ohlcv_1m_batch_with_provenance(...) -> dict[str, OhlcvFetchResult]` (keeping legacy `fetch_ohlcv_*` signatures intact for existing unit-test compatibility).
- In `fetch_ohlcv_1m_batch_with_provenance()`, when `should_fetch_1m_fallback(addr, c5_cached, now_ts=now_ts)` is `False`, return `OhlcvFetchResult(candles=[], source="unavailable", fetched_at=None, last_ok_at=None, last_status=None)`—never return unflagged `c1_cached`.

---

## 3. Data-Flow Changes & Enforcement Boundaries

### 3.1 Strategy Boundary (`scalper_entry_engine.py:evaluate_scalper_entry`)
- Add `ohlcv_provenance: Optional[OhlcvFetchResult] = None`.
- **Fail-closed enforcement (no silent production bypass):**
  - Validate `ohlcv_provenance` via `evaluate_ohlcv_provenance(ohlcv_provenance, decision_timestamp=ref_ts, max_cache_age_seconds=cache_ttl)`.
  - If invalid (`ok is False`), `evaluate_scalper_entry()` **must** return `eligible=False, setup_type="NONE"`, with `rejection_reason=prov_err`—even if candle timestamps satisfy E3A's closed-bar filter.

### 3.2 Trusted Pre-Attached Provenance (`alpha_node.py`, `whale_lane.py`, `tests/`)
- **Production rule:** Candidate dicts (`cand["candles_5m"]` / `cand["candles_1m"]`) supplied without a runtime `OhlcvFetchResult` instance are wrapped as `OhlcvFetchResult(candles=..., source="pre_attached_unknown", fetched_at=None, last_ok_at=None, last_status=None)`. Any boolean (`is_fresh=True`) or serialized dict metadata attached to a candidate is **ignored** and treated as `pre_attached_unknown` (ineligible).
- **Trusted internal / test attachment:** Trusted internal code or tests attach an actual `OhlcvFetchResult` instance in a dedicated runtime key (e.g., `cand["_ohlcv_5m_result"]` / `cand["_ohlcv_1m_result"]`, or pass `ohlcv_provenance=make_verified_ohlcv_result(candles, last_ok_at=ref_ts)` directly to `evaluate_scalper_entry()`), preserving the original `source` (`"network_200"`, `"cache_fresh"`, or `"pre_attached_verified"`), `last_ok_at`, and `last_status=200`.
- **Mandatory age validation on `pre_attached_verified`:** `evaluate_ohlcv_provenance()` applies the **exact same `last_ok_at` age check** (`<= OHLCV_CACHE_SECONDS`) to `pre_attached_verified` as to fetched candles. A `pre_attached_verified` result with missing or expired `last_ok_at` fails closed.
- Production `scanner_node` / `safety_gate_node` JSON payloads never construct `OhlcvFetchResult` instances, preventing any production bypass.

### 3.3 Explicit 5m/1m Interval Selection & Enforcement in `alpha_node.py`
- Call `fetch_ohlcv_batch_with_provenance()` and `fetch_ohlcv_1m_batch_with_provenance()`.
- Resolve `res_5m` (from `cand.get("_ohlcv_5m_result")` if `cand.get("candles_5m") is not None` and `isinstance(..., OhlcvFetchResult)`, else `pre_attached_unknown` if `candles_5m` is present, else `ohlcv_5m_by_pair[p_addr]`).
- Resolve `res_1m` identically (`cand.get("_ohlcv_1m_result")`, else `pre_attached_unknown` if `candles_1m` is present, else `ohlcv_1m_by_pair.get(p_addr)` or `unavailable`).
- Compute `ok_5m, err_5m = evaluate_ohlcv_provenance(res_5m, ...)` and `ok_1m, err_1m = evaluate_ohlcv_provenance(res_1m, ...)`, plus `closed_5m` via `normalize_and_filter_candles(res_5m.candles, bar_minutes=5, decision_timestamp=ref_ts)`.
- **Deterministic selection rules (never use stale 5m or 1m candles as entry evidence):**
  1. **`ok_5m` and `len(closed_5m) >= 12`:** Use 5m (`active_prov = res_5m`). Stale or unavailable 1m data does not affect this path.
  2. **`ok_5m` and `len(closed_5m) < 12`:** Evaluate 1m fallback **only** if permitted by the existing fallback policy (5m `last_ok=True, last_status=200`, no backoff, or trusted `pre_attached_verified` 1m); independently require `ok_1m` and `len(closed_1m) >= 15`. A fresh 5m result never authorizes stale, `unavailable`, or `pre_attached_unknown` 1m candles; if `not ok_1m`, fail closed with `err_1m`.
  3. **`not ok_5m` (stale or unavailable 5m, regardless of whether `len(closed_5m) >= 12` or `< 12`):**
     - Stale 5m candles are **never** used as entry evidence, even when `len(closed_5m) >= 12`.
     - Do **not** let an ineligible 5m series's candle count suppress an independently eligible 1m fallback **if** the fallback policy permits 1m (note: in live fetching, `should_fetch_1m_fallback()` blocks 1m network requests when 5m failed with `429`/backoff/error, leaving `res_1m` as `unavailable`; however, if 1m is permitted/independently eligible—e.g., `unavailable` 5m or `pre_attached_verified` 1m—`ok_1m` with `len(closed_1m) >= 15` is evaluated on 1m).
     - If `not ok_1m` as well, fail closed immediately (`eligible=False, setup_type="NONE"`), preserving `err_5m` if `res_5m` was `stale_*`, else `err_1m`.
- Call `cache_breakout_telemetry()` (`L170`) **only** when `evaluate_ohlcv_provenance(active_prov, ...)[0]` is `True`.

---

## 4. Freshness Policy & Unresolved Evidence Requirements

- **Preserve 90s network cache (`OHLCV_CACHE_SECONDS = 90.0`):** Cache retention in `_ohlcv_cache` (`600s` max TTL) is solely for eviction/inspection and **never** grants entry permission.
- **Three distinct temporal concepts:**
  1. **HTTP/cache entry age (`decision_ts - last_ok_at`):** Age of the last `200 OK` provider response.
  2. **Newest closed-candle age (`decision_ts - (closed[-1]["timestamp"] + bar_seconds)`):** Time elapsed since the newest closed bar finished.
  3. **E3A closed-candle rule (`timestamp + bar_seconds <= decision_ts`):** Structural invariant ensuring the bar's interval has ended.
- **Conservative fail-closed default until evidence exists:**
  - Require `evaluate_ohlcv_provenance(...)` to pass (`source in ELIGIBLE_OHLCV_SOURCES`, `last_status == 200`, `0 <= decision_ts - last_ok_at <= OHLCV_CACHE_SECONDS`).
  - Do **not** invent an arbitrary closed-candle-age threshold in E3B; live telemetry on GeckoTerminal indexing lag, batch pacing (`6.5s * N`), and zero-swap bar omissions is required first.

---

## 5. Whale-Lane Policy (`whale_lane.py`)

- In `whale_lane.py:416, 427`, OHLCV breakout gating (`WHALE_BLOCK_EXTENDED`, `WHALE_MIN_BREAKOUT_SCORE`) is an **optional quality/anti-chase signal** applied only `if has_ohlcv`.
- **E3B rule:** Resolve `res_5m` / `res_1m` using the same `OhlcvFetchResult` and `evaluate_ohlcv_provenance()` rules as `alpha_node.py`. When the active interval fails `evaluate_ohlcv_provenance()`:
  - Set `has_ohlcv = False` so stale/unknown OHLCV is **not** used as fresh breakout evidence.
  - Do **not** overwrite `_breakout_telemetry_cache` (`L391`).
  - Do **not** automatically disable independent whale-confluence entries.

---

## 6. Breakout Telemetry Cache Policy (`ohlcv_client.py`, `telemetry_api.py`)

- **Distinguish insertion time from originating OHLCV timestamp:**
  - `cache_breakout_telemetry(mint, telemetry, *, ohlcv_source, ohlcv_last_ok_at, now_ts=None)` stores:
    `{"cached_at": ts, "ohlcv_last_ok_at": ohlcv_last_ok_at, "ohlcv_source": ohlcv_source, "telemetry": dict(telemetry)}`.
- **Explicit validation of originating data timestamp (never clamp future timestamps to zero):**
  - `get_cached_breakout_telemetry_with_meta(mint, now_ts=..., clock_skew_seconds=5.0)` returns `BreakoutTelemetryCacheEntry(telemetry, cached_at, ohlcv_last_ok_at, ohlcv_source, data_age_seconds, is_historical=True, is_trusted=...)`:
    - If `ohlcv_last_ok_at` is `None`, `bool`, non-numeric, non-finite (`NaN`/`inf`), `<= 0`, or materially in the future (`ohlcv_last_ok_at > now_ts + clock_skew_seconds`): set `data_age_seconds = None` (unknown/invalid age), `is_historical = True`, and `is_trusted = False` (do **not** clamp future timestamps to `0.0`).
    - Otherwise (`ohlcv_last_ok_at <= now_ts + clock_skew_seconds`): set `data_age_seconds = max(0.0, now_ts - ohlcv_last_ok_at)` (only minor skew within `clock_skew_seconds` clamps to `0.0`), `is_historical = True`, `is_trusted = True`.
  - If telemetry was computed from 15-minute-old candles (`ohlcv_last_ok_at = T - 900`) and inserted 5 seconds ago (`cached_at = T - 5`), `data_age_seconds` is `900.0s` and `is_historical=True`—never labeled as a current evaluation.
- Keep `get_cached_breakout_telemetry()` backward-compatible for legacy callers.

---

## 7. Test Plan & Acceptance Criteria

| ID | Scenario | Acceptance Criteria |
| :--- | :--- | :--- |
| `B1` | Successful `200 OK` network fetch | `source="network_200"`, `last_status=200`, `evaluate_ohlcv_provenance()` returns `(True, None)` |
| `B2` | Fresh cache hit (`< 90s`, `last_ok_at` valid) | `source="cache_fresh"`, `age < 90s`, `evaluate_ohlcv_provenance()` returns `(True, None)` |
| `B3` | Expired cache (`> 90s`) + `200 OK` refresh | `source="network_200"`, `last_ok_at` updated, `(True, None)` |
| `B4` | HTTP `429` with cached breakout candles | `source="stale_429"`, `last_status=429`, `evaluate_ohlcv_provenance()` fails closed; `alpha_node` blocks `BUY` |
| `B5` | Provider/network error or timeout with cached candles | `source="stale_error"`, `last_status=0`, fails closed; `alpha_node` blocks `BUY` |
| `B6` | Global backoff (`_global_backoff_until`) with cached candles | `source="stale_backoff"`, fails closed; `alpha_node` blocks `BUY` |
| `B7` | `fetch_ohlcv_1m_batch_with_provenance()` when `should_fetch_1m_fallback()` is `False` (with `c1_cached`) | Returns `source="unavailable"`, `candles=[]`, fails closed (never returns stale `c1_cached`) |
| `B8` | Pre-attached trusted candles (`pre_attached_verified` with valid `last_ok_at`) | Passes `evaluate_ohlcv_provenance()` without weakening production validation |
| `B9` | Pre-attached unknown-provenance candles (`pre_attached_unknown`, omitted `ohlcv_provenance`, or candidate-supplied dict/bool flags) | Fails closed in `evaluate_scalper_entry()` and `alpha_node()` |
| `B10` | `whale_lane.py` with stale OHLCV (`stale_429` / `stale_backoff`) | `has_ohlcv=False`, `_breakout_telemetry_cache` not overwritten, independent whale entry not falsely blocked |
| `B11` | Fresh 5m (`< 12` closed bars) + stale/ineligible 1m (`stale_429` / `unavailable` / `pre_attached_unknown`) | `alpha_node` fails closed on 1m provenance; fresh 5m never authorizes stale 1m |
| `B12a` | `12+` closed stale 5m candles + independently fresh 1m data (when fallback policy permits 1m) | Stale 5m is rejected and does not suppress eligible 1m (`>= 15` closed 1m bars); when 5m `429`/backoff blocks 1m fetch, fails closed on 5m |
| `B12b` | `12+` closed fresh 5m candles + stale 1m data | 5m is selected (`active_prov = res_5m`) and entry is eligible; stale 1m does not affect 5m |
| `B12c` | Unavailable 5m + independently fresh 1m data (when permitted) | 1m is evaluated (`>= 15` closed 1m bars) rather than blocked by unavailable 5m |
| `B13` | Pre-attached `pre_attached_verified` with missing (`None`) or expired (`> 90s`) `last_ok_at` | `evaluate_ohlcv_provenance()` fails closed (`missing or invalid OHLCV last_ok_at` / `expired OHLCV cache`) |
| `B14a` | Telemetry computed from older candles (`ohlcv_last_ok_at` 900s old) but inserted recently (`cached_at` 5s ago) | `data_age_seconds == 900.0`, `is_historical=True`, never labeled current |
| `B14b` | Telemetry with missing (`None`), `NaN`, `<= 0`, or materially future (`> now_ts + 5s`) `ohlcv_last_ok_at` | `data_age_seconds is None`, `is_historical=True`, `is_trusted=False` (never clamped to `0.0`) |
| `B15` | Contradictory metadata (`source="stale_429"` with `last_status=200`, or `source="network_200"` with `last_status=429` / `last_ok_at=None`) | `evaluate_ohlcv_provenance()` rejects all contradictory combinations fail-closed |

---

## 8. Smallest File Set & Explicit Out-of-Scope Items

- **Smallest file set:** `ohlcv_client.py`, `scalper_entry_engine.py`, `alpha_node.py`, `whale_lane.py`, `telemetry_api.py`, plus focused E3B tests.
- **Explicit Out-of-Scope Items:**
  - E3C parser hardening, empty-`200` negative caching, timestamp parsing, deduplication, and interval-spacing rules.
  - Changing `OHLCV_CACHE_SECONDS` (`90s`), `OHLCV_CACHE_MAX_TTL_SECONDS` (`600s`), `429` backoff, or strategy thresholds (`BREAKOUT_*`, `SCALPER_*`, `WHALE_*`).
  - Any source code implementation in this design pass.
