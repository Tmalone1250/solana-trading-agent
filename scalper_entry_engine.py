"""Deterministic Scalper Expert Entry Engine (scalper_entry_engine.py)."""

from __future__ import annotations

from typing import Any, Optional, Sequence

from pydantic import BaseModel

from breakout_ignition import detect_breakout_ignition
from config import VALID_CHART_STRATEGIES, AgentConfig


class ScalperEntryResult(BaseModel):
    """Deterministic 4-gate Scalper Expert Entry Engine verdict."""

    eligible: bool
    setup_type: str = "NONE"  # SWEEP_RECLAIM | DISPLACEMENT_RETEST | BREAKOUT_IGNITION | NONE
    rsi_5m: float = 50.0
    dist_from_1h_high_pct: float = 0.0
    rejection_reason: Optional[str] = None
    breakout_metrics: Optional[dict[str, Any]] = None
    breakout_telemetry: Optional[dict[str, Any]] = None


def _normalize_candles(raw_candles: Sequence[Any]) -> list[dict[str, float]]:
    """Normalize candle dicts or [ts, o, h, l, c, v] rows into oldest-first dicts."""
    normalized: list[dict[str, float]] = []
    for idx, c in enumerate(raw_candles):
        if isinstance(c, dict):
            normalized.append(
                {
                    "timestamp": float(c.get("timestamp", idx)),
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                    "volume": float(c.get("volume", 0.0)),
                }
            )
        elif isinstance(c, (list, tuple)) and len(c) >= 5:
            normalized.append(
                {
                    "timestamp": float(c[0]),
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5]) if len(c) > 5 else 0.0,
                }
            )
    normalized.sort(key=lambda x: x["timestamp"])
    return normalized


def compute_rsi(closes: Sequence[float], period: int = 14) -> float:
    """Compute Wilder 14-period RSI from oldest-first close prices."""
    if len(closes) < 2:
        return 50.0

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    if len(deltas) <= period:
        avg_gain = sum(gains) / len(deltas)
        avg_loss = sum(losses) / len(deltas)
    else:
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        for g, l in zip(gains[period:], losses[period:]):
            avg_gain = (avg_gain * (period - 1) + g) / period
            avg_loss = (avg_loss * (period - 1) + l) / period

    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def compute_ema(values: Sequence[float], period: int = 9) -> float:
    """Compute exponential moving average (EMA) from oldest-first values."""
    if not values:
        return 0.0
    if len(values) <= period:
        return sum(values) / len(values)
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v * k) + (ema * (1.0 - k))
    return ema


def _detect_sweep_reclaim(candles: list[dict[str, float]]) -> bool:
    """ICT/CRT Liquidity Sweep: one of last 3 candles wicks below prior 10-candle swing low
    and closes back above it, with current candle confirming green."""
    if len(candles) < 6:
        return False

    prior_window = candles[-13:-3] if len(candles) >= 13 else candles[:-3]
    if not prior_window:
        return False
    swing_low = min(c["low"] for c in prior_window)
    curr = candles[-1]

    # Current candle must confirm green and hold above the swept level
    if not (curr["close"] > curr["open"] and curr["close"] > swing_low):
        return False

    for c in candles[-3:]:
        if c["low"] < swing_low and c["close"] > swing_low:
            return True
    return False


def _detect_displacement_retest(candles: list[dict[str, float]], rsi_5m: float) -> bool:
    """Impulse + Pullback: recent bullish displacement candle (body >= 1.5x avg 10-candle body)
    followed by a controlled -10% to -30% pullback into displacement midpoint / 9 EMA,
    with rsi_5m in [42, 65] and a green curl closing above the prior candle's high."""
    if len(candles) < 8:
        return False
    if not (42.0 <= rsi_5m <= 65.0):
        return False

    curr = candles[-1]
    prev = candles[-2]
    # Green curl closing above prior candle's high
    if not (curr["close"] > curr["open"] and curr["close"] > prev["high"]):
        return False

    closes = [c["close"] for c in candles]
    ema_9 = compute_ema(closes, period=9)

    n = len(candles)
    start_idx = max(1, n - 8)
    end_idx = n - 2  # at least 1 pullback candle + current curl candle after displacement

    for idx in range(start_idx, end_idx):
        d = candles[idx]
        d_body = d["close"] - d["open"]
        if d_body <= 0:
            continue

        prior_10 = candles[max(0, idx - 10) : idx]
        if not prior_10:
            continue
        avg_body = sum(abs(c["close"] - c["open"]) for c in prior_10) / len(prior_10)
        if avg_body <= 0 or d_body < 1.5 * avg_body:
            continue

        after_disp = candles[idx + 1 :]
        if len(after_disp) < 2:
            continue

        impulse_high = max(c["high"] for c in candles[idx : n - 1])
        pullback_low = min(c["low"] for c in after_disp)
        if impulse_high <= 0:
            continue

        pullback_pct = (impulse_high - pullback_low) / impulse_high
        if not (0.10 <= pullback_pct <= 0.30):
            continue

        disp_midpoint = (d["open"] + d["close"]) / 2.0
        # Pullback reached into displacement midpoint or 9 EMA zone (allowing 3% tolerance)
        retest_zone_top = max(disp_midpoint, ema_9) * 1.03
        if pullback_low <= retest_zone_top:
            return True

    return False


def _extract_order_flow(pair_data: dict[str, Any]) -> tuple[int, int, float]:
    txns_m5 = (pair_data.get("txns") or {}).get("m5") if isinstance(pair_data.get("txns"), dict) else None
    if isinstance(txns_m5, dict):
        buys = int(txns_m5.get("buys") or 0)
        sells = int(txns_m5.get("sells") or 0)
    else:
        buys = int(pair_data.get("buys_5m") or 0)
        sells = int(pair_data.get("sells_5m") or 0)
    total = buys + sells
    ratio = (buys / total) if total > 0 else 0.0
    return buys, sells, ratio


def evaluate_scalper_entry(
    candles_5m: Sequence[Any],
    pair_data: dict[str, Any],
    time_in_slot_min: float,
    *,
    config: Optional[AgentConfig] = None,
    candles_1m: Optional[Sequence[Any]] = None,
) -> ScalperEntryResult:
    """Run all 4 deterministic gates of the Scalper Expert Entry Engine (+ BREAKOUT_IGNITION)."""
    min_soak = config.MIN_WATCHLIST_SOAK_MINUTES if config else 3.0
    high_buf = config.ANTI_CHASE_HIGH_BUFFER_PCT if config else 0.05
    max_rsi = config.SCALPER_MAX_RSI_5M if config else 70.0
    breakout_max_rsi = 78.0
    min_buy_ratio = config.SCALPER_MIN_BUY_RATIO_5M if config else 0.58
    min_txns = config.SCALPER_MIN_TXNS_5M if config else 15
    enabled_strats = config.chart_enabled_strategies() if config is not None else set(VALID_CHART_STRATEGIES)

    candles = _normalize_candles(candles_5m)
    norm_1m = _normalize_candles(candles_1m) if candles_1m else []
    buys_5m, sells_5m, buy_ratio_5m = _extract_order_flow(pair_data)
    total_5m = buys_5m + sells_5m

    # 1m Candle Fallback: when < 12 5m candles exist, evaluate BREAKOUT_IGNITION on >= 15 1m bars
    if len(candles) < 12:
        if len(norm_1m) >= 15:
            closes_1m = [c["close"] for c in norm_1m]
            rsi_1m = compute_rsi(closes_1m, period=14)
            bi_1m = detect_breakout_ignition(norm_1m, pair_data, bar_minutes=1, rsi_val=rsi_1m, config=config)
            high_1m = max(c["high"] for c in norm_1m)
            curr_p = float(pair_data.get("price_usd") or pair_data.get("priceUsd") or closes_1m[-1])
            dist_1m = max(0.0, (high_1m - curr_p) / high_1m) if high_1m > 0 else 0.0
            fb_reasons: list[str] = []
            bo_enabled = "BREAKOUT_IGNITION" in enabled_strats
            if not bo_enabled:
                fb_reasons.append("BREAKOUT_IGNITION disabled by CHART_ENABLED_STRATEGIES")
            if time_in_slot_min < min_soak:
                fb_reasons.append(f"soak {time_in_slot_min:.1f}m < {min_soak:.0f}m")
            if total_5m < min_txns:
                fb_reasons.append(f"5m txns {total_5m} < {min_txns}")
            if not bi_1m.detected:
                fb_reasons.append(f"BREAKOUT_IGNITION(1m): {bi_1m.rejection_reason}")
            elif rsi_1m > breakout_max_rsi:
                fb_reasons.append(f"RSI={rsi_1m:.1f} > {breakout_max_rsi:.0f}")
            return ScalperEntryResult(
                eligible=bo_enabled and bi_1m.detected and len(fb_reasons) == 0,
                setup_type="BREAKOUT_IGNITION" if (bo_enabled and bi_1m.detected) else "NONE",
                rsi_5m=round(rsi_1m, 2),
                dist_from_1h_high_pct=round(dist_1m, 4),
                rejection_reason=" / ".join(fb_reasons) if fb_reasons else None,
                breakout_metrics=bi_1m.model_dump(),
                breakout_telemetry=bi_1m.telemetry.model_dump(),
            )

        bi_short = detect_breakout_ignition(candles, pair_data, bar_minutes=5, config=config)
        reasons_short: list[str] = []
        if time_in_slot_min < min_soak:
            reasons_short.append(f"soak {time_in_slot_min:.1f}m < {min_soak:.0f}m")
        reasons_short.append(f"insufficient 5m candles ({len(candles)} < 12)")
        rsi_val = compute_rsi([c["close"] for c in candles], period=14) if candles else 50.0
        return ScalperEntryResult(
            eligible=False,
            setup_type="NONE",
            rsi_5m=rsi_val,
            dist_from_1h_high_pct=0.0,
            rejection_reason=" / ".join(reasons_short),
            breakout_metrics=bi_short.model_dump(),
            breakout_telemetry=bi_short.telemetry.model_dump(),
        )

    closes = [c["close"] for c in candles]
    rsi_5m = compute_rsi(closes, period=14)
    high_1h = max(c["high"] for c in candles[-12:])
    current_price = float(pair_data.get("price_usd") or pair_data.get("priceUsd") or 0.0)
    if current_price <= 0:
        current_price = closes[-1]

    dist_from_1h_high_pct = (
        max(0.0, (high_1h - current_price) / high_1h) if high_1h > 0 else 0.0
    )

    # Always compute breakout telemetry for live watchlist Breakout Meter
    breakout_res = detect_breakout_ignition(candles, pair_data, bar_minutes=5, rsi_val=rsi_5m, config=config)

    # Gate 3: Core Market-Physics Setup (only evaluate strategies enabled in CHART_ENABLED_STRATEGIES)
    setup_type = "NONE"
    if "SWEEP_RECLAIM" in enabled_strats and _detect_sweep_reclaim(candles):
        setup_type = "SWEEP_RECLAIM"
    elif "DISPLACEMENT_RETEST" in enabled_strats and _detect_displacement_retest(candles, rsi_5m):
        setup_type = "DISPLACEMENT_RETEST"
    elif "BREAKOUT_IGNITION" in enabled_strats and breakout_res.detected:
        setup_type = "BREAKOUT_IGNITION"

    reasons: list[str] = []

    # Gate 1: Mandatory Soak
    if time_in_slot_min < min_soak:
        reasons.append(f"soak {time_in_slot_min:.1f}m < {min_soak:.0f}m")

    # Gate 2: Breakout-aware Anti-Chase & RSI Cooling
    if setup_type == "BREAKOUT_IGNITION":
        # Bypass near-1h-high (<5%) block (anti-chase <=12% above EMA9/VWAP already enforced); allow RSI <= 78
        if rsi_5m > breakout_max_rsi:
            reasons.append(f"RSI={rsi_5m:.1f} > {breakout_max_rsi:.0f}")
    else:
        if rsi_5m > max_rsi:
            reasons.append(f"RSI={rsi_5m:.1f} > {max_rsi:.0f}")
        if dist_from_1h_high_pct < high_buf:
            reasons.append(
                f"near 1h high ({dist_from_1h_high_pct * 100:.1f}% < {high_buf * 100:.0f}%)"
            )
        if setup_type == "NONE":
            if breakout_res and breakout_res.rejection_reason and (
                "over-extended" in breakout_res.rejection_reason
                or "upper-wick fakeout" in breakout_res.rejection_reason
            ):
                reasons.append(f"BREAKOUT_IGNITION rejected: {breakout_res.rejection_reason}")
            else:
                reasons.append("no SWEEP_RECLAIM, DISPLACEMENT_RETEST, or BREAKOUT_IGNITION setup")

    # Gate 4: Order-Flow Imbalance
    if total_5m < min_txns:
        reasons.append(f"5m txns {total_5m} < {min_txns}")
    elif setup_type != "BREAKOUT_IGNITION" and buy_ratio_5m < min_buy_ratio:
        reasons.append(
            f"5m buy_ratio {buy_ratio_5m * 100:.0f}% < {min_buy_ratio * 100:.0f}%"
        )

    eligible = len(reasons) == 0
    return ScalperEntryResult(
        eligible=eligible,
        setup_type=setup_type,
        rsi_5m=round(rsi_5m, 2),
        dist_from_1h_high_pct=round(dist_from_1h_high_pct, 4),
        rejection_reason=" / ".join(reasons) if reasons else None,
        breakout_metrics=breakout_res.model_dump() if breakout_res is not None else None,
        breakout_telemetry=breakout_res.telemetry.model_dump() if breakout_res is not None else None,
    )
