"""Timestamp-bounded DecisionSnapshot builder with strict anti-look-ahead enforcement.

Outcome fields (`price_1m_pct`, `price_5m_pct`, `price_15m_pct`, `price_30m_pct`, `mfe_pct`,
`mae_pct`, `finalized`, and any observation after `decision_timestamp`) are stripped before
constructing a `DecisionSnapshot`, and accessing them raises `LookAheadViolationError`.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from simulator import (
    EXACT_HISTORICAL,
    INFERRED,
    MISSING,
    PARTIALLY_AVAILABLE,
    RECONSTRUCTABLE,
)
from simulator.data_loader import HistoricalDataset, parse_iso_utc


def _get_dataset_index(dataset: HistoricalDataset) -> dict[str, Any]:
    """Build sorted lookup indices once per HistoricalDataset (O(N log N) one-time setup)."""
    cached = getattr(dataset, "_sim_lookup_index", None)
    if cached is not None:
        return cached

    # 1. Pre-parse sol_equity timeline (`timestamp_epoch`, `sol_equity`)
    sol_epochs: list[float] = []
    sol_vals: list[float] = []
    # 2. Per-mint action_log sequence (`id`, `epoch`, `action`, `reason`)
    actions_by_mint: dict[str, list[tuple[int, float, str, str]]] = {}
    for r in dataset.action_log:
        dt = parse_iso_utc(r.get("timestamp_utc"))
        if dt is None:
            continue
        ep = dt.timestamp()
        if r.get("sol_equity") is not None:
            sol_epochs.append(ep)
            sol_vals.append(float(r["sol_equity"]))
        m = str(r.get("asset_id") or "").strip()
        if m:
            actions_by_mint.setdefault(m, []).append(
                (
                    int(r.get("id") or 0),
                    ep,
                    str(r.get("action") or "").upper(),
                    str(r.get("reason") or ""),
                )
            )

    # 3. Per-mint decisions_log index (`epoch`, `row_dict`)
    decisions_by_mint: dict[str, tuple[list[float], list[dict[str, Any]]]] = {}
    tmp_dec: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    for d in dataset.decisions_log:
        m = str(d.get("mint") or "").strip()
        dt = parse_iso_utc(d.get("timestamp"))
        if m and dt is not None:
            tmp_dec.setdefault(m, []).append((dt.timestamp(), d))
    for m, items in tmp_dec.items():
        items.sort(key=lambda x: x[0])
        decisions_by_mint[m] = ([x[0] for x in items], [x[1] for x in items])

    # 4. Pre-parsed trades (`epoch`, `side`, `mint`, `is_whale`, `ts_str`)
    parsed_trades: list[tuple[float, str, str, bool, str]] = []
    for t in dataset.trades:
        dt = parse_iso_utc(t.get("timestamp"))
        m = str(t.get("mint") or "").strip()
        if dt is not None and m:
            parsed_trades.append(
                (
                    dt.timestamp(),
                    str(t.get("side") or "").upper(),
                    m,
                    str(t.get("strategy") or "").upper() == "WHALE_SCALP",
                    str(t.get("timestamp") or ""),
                )
            )
    parsed_trades.sort(key=lambda x: x[0])
    trade_epochs = [x[0] for x in parsed_trades]

    idx_obj: dict[str, Any] = {
        "sol_epochs": sol_epochs,
        "sol_vals": sol_vals,
        "actions_by_mint": actions_by_mint,
        "decisions_by_mint": decisions_by_mint,
        "parsed_trades": parsed_trades,
        "trade_epochs": trade_epochs,
    }
    setattr(dataset, "_sim_lookup_index", idx_obj)
    return idx_obj


class LookAheadViolationError(AttributeError):
    """Raised if decision-replay code attempts to access forbidden forward-outcome attributes."""


FORBIDDEN_OUTCOME_FIELDS: frozenset[str] = frozenset(
    {
        "price_1m_pct",
        "price_5m_pct",
        "price_15m_pct",
        "price_30m_pct",
        "mfe_pct",
        "mae_pct",
        "finalized",
        "future_observations",
        "subsequent_trades",
        "forward_returns",
    }
)


@dataclass(frozen=True)
class TimestampBoundedRecordMap:
    """Read-only dict wrapper that forbids access to forward-looking outcome keys."""

    _data: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        if key in FORBIDDEN_OUTCOME_FIELDS:
            raise LookAheadViolationError(
                f"Look-ahead violation: '{key}' is forbidden during decision evaluation."
            )
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        if key in FORBIDDEN_OUTCOME_FIELDS:
            raise LookAheadViolationError(
                f"Look-ahead violation: '{key}' is forbidden during decision evaluation."
            )
        return self._data.get(key, default)

    def keys(self) -> tuple[str, ...]:
        return tuple(self._data.keys())

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)


@dataclass(frozen=True)
class DecisionSnapshot:
    """Strictly time-bounded (`<= decision_timestamp`) input snapshot for a single `DECISION_1` tick."""

    mint: str
    symbol: str
    decision_timestamp: str
    decision_dt: datetime
    action_log_id: int
    episode_id: Optional[str]
    observation_index_in_episode: int

    # Recorded scalar values from `action_log` (`DECISION_1` at `decision_timestamp`)
    recorded_strategy: str
    recorded_reason: str
    mc_usd: Optional[float]
    liq_usd: Optional[float]
    soak_minutes: Optional[float]
    rsi: Optional[float]
    leg2_score: Optional[float]
    narrative_score: Optional[float]
    momentum_score: Optional[float]
    vamp_score: Optional[float]
    buy_confidence: Optional[float]
    breakout_score: Optional[int]
    breakout_state: Optional[str]
    breakout_timeframe: Optional[str]
    breakout_vol_ratio: Optional[float]
    breakout_ema_ext_pct: Optional[float]
    breakout_failed_gates_raw: Optional[str]
    breakout_failed_gates: tuple[str, ...]

    # Same-timestamp (`<= decision_timestamp`) terminal action in `action_log`, if any (`EVICT` / `BUY`)
    same_tick_terminal_action: Optional[str]
    same_tick_terminal_reason: Optional[str]
    same_tick_terminal_id: Optional[int]

    # Optional unpruned `decisions_log` entry at `decision_timestamp` (last ~48m of soak only)
    decisions_log_id: Optional[int]
    decisions_log_action: Optional[str]
    decisions_log_confidence: Optional[float]
    decisions_log_scores: Optional[TimestampBoundedRecordMap]

    # Prior observations in the SAME episode strictly `< decision_timestamp` (for strike tracking)
    prior_episode_d1_count: int
    consecutive_prior_vamp_warn_strikes: int
    consecutive_prior_low_score_strikes: int

    # Reconstructed portfolio/slot state strictly `<= decision_timestamp` from `trades`
    open_chart_mints_at_decision: tuple[str, ...]
    open_whale_mints_at_decision: tuple[str, ...]
    open_chart_count: int
    open_total_count: int
    estimated_sol_balance_at_decision: Optional[float]

    # Raw OHLCV & order-flow inputs (never persisted in Phase 1 DuckDB -> None / MISSING)
    raw_candles_5m: Optional[tuple[dict[str, float], ...]]
    raw_candles_1m: Optional[tuple[dict[str, float], ...]]
    raw_txns_5m_total: Optional[int]
    raw_buy_ratio_5m: Optional[float]

    # Field-level provenance map
    input_availability: TimestampBoundedRecordMap

    def __getattr__(self, name: str) -> Any:
        if name in FORBIDDEN_OUTCOME_FIELDS:
            raise LookAheadViolationError(
                f"Look-ahead violation: forward outcome attribute '{name}' is inaccessible on DecisionSnapshot."
            )
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")


def _reconstruct_open_positions_indexed(
    idx_obj: dict[str, Any],
    decision_epoch: float,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Reconstruct open Chart and Whale mints at `decision_epoch` using ONLY trades `<= decision_epoch`.

    Complexity:
    - Timestamp boundary lookup (`bisect_right`): O(log N)
    - Historical trade-prefix traversal (`parsed_trades[:hi]`): O(N) in the number of preceding trade records
    """
    open_chart: list[tuple[str, str]] = []
    open_whale: list[tuple[str, str]] = []
    hi = bisect.bisect_right(idx_obj["trade_epochs"], decision_epoch)
    for _, side, mint, is_whale, ts_str in idx_obj["parsed_trades"][:hi]:
        bucket = open_whale if is_whale else open_chart
        if side == "BUY":
            bucket.append((mint, ts_str))
        elif side == "SELL":
            for i, (om, _) in enumerate(bucket):
                if om == mint:
                    bucket.pop(i)
                    break
    return tuple(m for m, _ in open_chart), tuple(m for m, _ in open_whale)


def _estimate_sol_balance_indexed(
    idx_obj: dict[str, Any],
    decision_epoch: float,
) -> Optional[float]:
    """Return latest recorded `sol_equity` strictly `<= decision_epoch` in O(log N) via bisect_right."""
    pos = bisect.bisect_right(idx_obj["sol_epochs"], decision_epoch) - 1
    if pos >= 0:
        return float(idx_obj["sol_vals"][pos])
    return None


def build_decision_snapshot(
    dataset: HistoricalDataset,
    *,
    action_row: dict[str, Any],
    episode_id: Optional[str] = None,
    prior_episode_d1_rows: tuple[dict[str, Any], ...] = (),
    vamp_warn_threshold: float = 0.75,
    low_score_threshold: float = 2.0,
) -> DecisionSnapshot:
    """Build a strictly timestamp-bounded `DecisionSnapshot` for one `DECISION_1` row.

    Complexity per observation (after one-time index build):
    - `sol_equity` and `decisions_log` timestamp boundary lookups: O(log N) / O(log M)
    - Open-position reconstruction: O(N) worst-case traversal of the preceding trade prefix
    """
    if str(action_row.get("action") or "").upper() != "DECISION_1":
        raise ValueError(f"Expected DECISION_1 row, got action={action_row.get('action')!r}")

    for forbidden in FORBIDDEN_OUTCOME_FIELDS:
        if forbidden in action_row and action_row[forbidden] is not None:
            raise LookAheadViolationError(
                f"Input action_row unexpectedly carries forbidden outcome field '{forbidden}'."
            )

    mint = str(action_row.get("asset_id") or "").strip()
    symbol = str(action_row.get("symbol") or mint[:8]).strip()
    ts_str = str(action_row.get("timestamp_utc") or "").strip()
    decision_dt = parse_iso_utc(ts_str)
    if not mint or decision_dt is None:
        raise ValueError(f"Invalid mint or timestamp_utc on action_log row: {action_row!r}")
    decision_epoch = decision_dt.timestamp()
    row_id = int(action_row["id"])
    idx_obj = _get_dataset_index(dataset)

    # Match same-tick terminal `EVICT` (`r_epoch == decision_epoch` and `r_id > row_id`) using per-mint list
    same_term_act: Optional[str] = None
    same_term_reason: Optional[str] = None
    same_term_id: Optional[int] = None
    mint_acts = idx_obj["actions_by_mint"].get(mint, [])
    for r_id, r_ep, r_act, r_reason in mint_acts:
        if r_id <= row_id:
            continue
        if r_act == "EVICT" and abs(r_ep - decision_epoch) < 1e-6:
            same_term_act = "EVICT"
            same_term_reason = r_reason
            same_term_id = r_id
            break
        if r_act == "DECISION_1":
            break

    # Match unpruned `decisions_log` row strictly `<= decision_epoch` (within 1.0s) in O(log M)
    dec_id: Optional[int] = None
    dec_act: Optional[str] = None
    dec_conf: Optional[float] = None
    dec_scores_map: Optional[TimestampBoundedRecordMap] = None
    dec_pair = idx_obj["decisions_by_mint"].get(mint)
    if dec_pair is not None:
        d_epochs, d_rows = dec_pair
        lo = bisect.bisect_left(d_epochs, decision_epoch - 1.0)
        hi = bisect.bisect_right(d_epochs, decision_epoch)
        if lo < hi:
            d = d_rows[hi - 1]
            dec_id = int(d["id"])
            dec_act = str(d.get("action") or "")
            dec_conf = float(d["confidence"]) if d.get("confidence") is not None else None
            raw_scores = d.get("scores_json")
            if raw_scores:
                try:
                    parsed = json.loads(raw_scores) if isinstance(raw_scores, str) else dict(raw_scores)
                    clean_scores = {
                        k: v for k, v in parsed.items() if k not in FORBIDDEN_OUTCOME_FIELDS
                    }
                    dec_scores_map = TimestampBoundedRecordMap(clean_scores)
                except (ValueError, TypeError):
                    dec_scores_map = None

    # Compute trailing consecutive strikes strictly `< decision_epoch`
    vamp_strikes = 0
    for pr in reversed(prior_episode_d1_rows):
        pv = float(pr["vamp_score"]) if pr.get("vamp_score") is not None else 0.0
        if pv >= vamp_warn_threshold:
            vamp_strikes += 1
        else:
            break

    low_strikes = 0
    for pr in reversed(prior_episode_d1_rows):
        pn = float(pr["narrative_score"]) if pr.get("narrative_score") is not None else 5.0
        pm = float(pr["momentum_score"]) if pr.get("momentum_score") is not None else 5.0
        if pn < low_score_threshold and pm < low_score_threshold:
            low_strikes += 1
        else:
            break

    open_chart, open_whale = _reconstruct_open_positions_indexed(idx_obj, decision_epoch)
    est_sol = _estimate_sol_balance_indexed(idx_obj, decision_epoch)

    raw_gates_str = action_row.get("breakout_failed_gates")
    gates_tuple = tuple(
        g.strip() for g in str(raw_gates_str or "").split(";") if g.strip()
    )

    avail: dict[str, str] = {
        "mc_usd": EXACT_HISTORICAL if action_row.get("mc_usd") is not None else MISSING,
        "liq_usd": EXACT_HISTORICAL if action_row.get("liq_usd") is not None else MISSING,
        "soak_minutes": EXACT_HISTORICAL if action_row.get("soak_minutes") is not None else MISSING,
        "rsi": EXACT_HISTORICAL if action_row.get("rsi") is not None else MISSING,
        "leg2_score": EXACT_HISTORICAL if action_row.get("leg2_score") is not None else MISSING,
        "narrative_score": EXACT_HISTORICAL if action_row.get("narrative_score") is not None else MISSING,
        "momentum_score": EXACT_HISTORICAL if action_row.get("momentum_score") is not None else MISSING,
        "vamp_score": EXACT_HISTORICAL if action_row.get("vamp_score") is not None else MISSING,
        "buy_confidence": EXACT_HISTORICAL if action_row.get("buy_confidence") is not None else MISSING,
        "breakout_score": EXACT_HISTORICAL if action_row.get("breakout_score") is not None else MISSING,
        "breakout_state": EXACT_HISTORICAL if action_row.get("breakout_state") is not None else MISSING,
        "breakout_timeframe": EXACT_HISTORICAL if action_row.get("breakout_timeframe") is not None else MISSING,
        "breakout_vol_ratio": EXACT_HISTORICAL if action_row.get("breakout_vol_ratio") is not None else MISSING,
        "breakout_ema_ext_pct": EXACT_HISTORICAL if action_row.get("breakout_ema_ext_pct") is not None else MISSING,
        "breakout_failed_gates": EXACT_HISTORICAL if raw_gates_str is not None else MISSING,
        "decisions_log_scores": EXACT_HISTORICAL if dec_scores_map is not None else MISSING,
        "raw_candles_5m": MISSING,
        "raw_candles_1m": MISSING,
        "raw_txns_5m_total": MISSING,
        "raw_buy_ratio_5m": PARTIALLY_AVAILABLE if any(g.startswith("BUY_RATIO(") for g in gates_tuple) else MISSING,
        "open_positions_state": RECONSTRUCTABLE,
        "estimated_sol_balance": RECONSTRUCTABLE if est_sol is not None else INFERRED,
    }

    return DecisionSnapshot(
        mint=mint,
        symbol=symbol,
        decision_timestamp=ts_str,
        decision_dt=decision_dt,
        action_log_id=row_id,
        episode_id=episode_id,
        observation_index_in_episode=len(prior_episode_d1_rows) + 1,
        recorded_strategy=str(action_row.get("strategy") or "NONE"),
        recorded_reason=str(action_row.get("reason") or ""),
        mc_usd=float(action_row["mc_usd"]) if action_row.get("mc_usd") is not None else None,
        liq_usd=float(action_row["liq_usd"]) if action_row.get("liq_usd") is not None else None,
        soak_minutes=float(action_row["soak_minutes"]) if action_row.get("soak_minutes") is not None else None,
        rsi=float(action_row["rsi"]) if action_row.get("rsi") is not None else None,
        leg2_score=float(action_row["leg2_score"]) if action_row.get("leg2_score") is not None else None,
        narrative_score=float(action_row["narrative_score"]) if action_row.get("narrative_score") is not None else None,
        momentum_score=float(action_row["momentum_score"]) if action_row.get("momentum_score") is not None else None,
        vamp_score=float(action_row["vamp_score"]) if action_row.get("vamp_score") is not None else None,
        buy_confidence=float(action_row["buy_confidence"]) if action_row.get("buy_confidence") is not None else None,
        breakout_score=int(action_row["breakout_score"]) if action_row.get("breakout_score") is not None else None,
        breakout_state=str(action_row["breakout_state"]) if action_row.get("breakout_state") is not None else None,
        breakout_timeframe=str(action_row["breakout_timeframe"]) if action_row.get("breakout_timeframe") is not None else None,
        breakout_vol_ratio=float(action_row["breakout_vol_ratio"]) if action_row.get("breakout_vol_ratio") is not None else None,
        breakout_ema_ext_pct=float(action_row["breakout_ema_ext_pct"]) if action_row.get("breakout_ema_ext_pct") is not None else None,
        breakout_failed_gates_raw=str(raw_gates_str) if raw_gates_str is not None else None,
        breakout_failed_gates=gates_tuple,
        same_tick_terminal_action=same_term_act,
        same_tick_terminal_reason=same_term_reason,
        same_tick_terminal_id=same_term_id,
        decisions_log_id=dec_id,
        decisions_log_action=dec_act,
        decisions_log_confidence=dec_conf,
        decisions_log_scores=dec_scores_map,
        prior_episode_d1_count=len(prior_episode_d1_rows),
        consecutive_prior_vamp_warn_strikes=vamp_strikes,
        consecutive_prior_low_score_strikes=low_strikes,
        open_chart_mints_at_decision=open_chart,
        open_whale_mints_at_decision=open_whale,
        open_chart_count=len(open_chart),
        open_total_count=len(open_chart) + len(open_whale),
        estimated_sol_balance_at_decision=est_sol,
        raw_candles_5m=None,
        raw_candles_1m=None,
        raw_txns_5m_total=None,
        raw_buy_ratio_5m=None,
        input_availability=TimestampBoundedRecordMap(avail),
    )


def find_snapshot_for_observation(
    dataset: HistoricalDataset,
    *,
    mint: str,
    decision_timestamp: str,
    vamp_warn_threshold: float = 0.75,
    low_score_threshold: float = 2.0,
) -> DecisionSnapshot:
    """Locate a specific `(mint, decision_timestamp)` observation and build its `DecisionSnapshot`."""
    target_dt = parse_iso_utc(decision_timestamp)
    if target_dt is None:
        raise ValueError(f"Invalid decision_timestamp: {decision_timestamp!r}")

    for ep in dataset.episodes_by_mint.get(mint, []):
        for idx, row in enumerate(ep.decision1_rows):
            r_dt = parse_iso_utc(row.get("timestamp_utc"))
            if row.get("timestamp_utc") == decision_timestamp or (
                r_dt is not None and abs((r_dt - target_dt).total_seconds()) < 1e-6
            ):
                return build_decision_snapshot(
                    dataset,
                    action_row=row,
                    episode_id=ep.episode_id,
                    prior_episode_d1_rows=ep.decision1_rows[:idx],
                    vamp_warn_threshold=vamp_warn_threshold,
                    low_score_threshold=low_score_threshold,
                )

    # Fallback scan over all action_log rows if outside episode filter window
    for row in dataset.action_log:
        if str(row.get("action") or "").upper() != "DECISION_1":
            continue
        if str(row.get("asset_id") or "").strip() != mint:
            continue
        r_dt = parse_iso_utc(row.get("timestamp_utc"))
        if row.get("timestamp_utc") == decision_timestamp or (
            r_dt is not None and abs((r_dt - target_dt).total_seconds()) < 1e-6
        ):
            return build_decision_snapshot(
                dataset,
                action_row=row,
                episode_id=None,
                prior_episode_d1_rows=(),
                vamp_warn_threshold=vamp_warn_threshold,
                low_score_threshold=low_score_threshold,
            )

    raise KeyError(f"No DECISION_1 observation found for mint={mint!r} at {decision_timestamp!r}")
