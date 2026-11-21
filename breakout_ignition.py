"""BREAKOUT_IGNITION chart setup detector (5m primary + 1m fallback)."""

from __future__ import annotations

from typing import Any, Optional, Sequence

from pydantic import BaseModel


class BreakoutTelemetry(BaseModel):
    """Real-time 0-100 breakout scoring telemetry surfaced in watchlist API & CSV."""

    score: int = 0
    state: str = "FORMING"  # FORMING | COILING | IGNITION | EXTENDED
    timeframe: str = "5m"  # 5m | 1m_fallback
    vol_ratio: float = 0.0
    ema_ext_pct: float = 0.0
    passed_gates: list[str] = []
    failed_gates: list[str] = []


class BreakoutIgnitionResult(BaseModel):
    """Structured telemetry & verdict for the BREAKOUT_IGNITION setup."""

    detected: bool = False
    timeframe: str = "5m"
    ema9: float = 0.0
    ema21: float = 0.0
    vwap: float = 0.0
    ext_above_ema9_pct: float = 0.0
    ext_above_vwap_pct: float = 0.0
    vol_ratio: float = 0.0
    buy_ratio_5m: float = 0.0
    buys_accel_ratio: float = 0.0
    body_pos_pct: float = 0.0
    coil_range_pct: float = 0.0
    htf_bullish: bool = False
    rejection_reason: Optional[str] = None
    telemetry: BreakoutTelemetry = BreakoutTelemetry()


def compute_ema_series(values: Sequence[float], period: int) -> list[float]:
    """Compute oldest-first EMA series for `values`."""
    if not values:
        return []
    k = 2.0 / (period + 1)
    series: list[float] = [float(values[0])]
    for v in values[1:]:
        series.append((float(v) * k) + (series[-1] * (1.0 - k)))
    return series


def compute_vwap(candles: Sequence[dict[str, float]]) -> float:
    """Compute volume-weighted average price over `candles`."""
    if not candles:
        return 0.0
    pv_sum = 0.0
    v_sum = 0.0
    for c in candles:
        typical = (c["high"] + c["low"] + c["close"]) / 3.0
        vol = max(0.0, c.get("volume", 0.0))
        pv_sum += typical * vol
        v_sum += vol
    if v_sum > 0:
        return pv_sum / v_sum
    return sum((c["high"] + c["low"] + c["close"]) / 3.0 for c in candles) / len(candles)


def _aggregate_to_15m(candles: Sequence[dict[str, float]], bar_minutes: int) -> list[dict[str, float]]:
    """Aggregate 5m (3 bars) or 1m (15 bars) candles into 15m OHLCV bars."""
    group_size = max(1, 15 // max(1, bar_minutes))
    bars: list[dict[str, float]] = []
    # Align from the newest candle backward so the latest 15m bar is complete/current
    n = len(candles)
    remainder = n % group_size
    start = remainder if remainder > 0 and n >= group_size * 2 else 0
    for i in range(start, n, group_size):
        chunk = candles[i : i + group_size]
        if not chunk:
            continue
        bars.append(
            {
                "timestamp": chunk[0]["timestamp"],
                "open": chunk[0]["open"],
                "high": max(c["high"] for c in chunk),
                "low": min(c["low"] for c in chunk),
                "close": chunk[-1]["close"],
                "volume": sum(c.get("volume", 0.0) for c in chunk),
            }
        )
    return bars


def _is_htf_15m_bullish(candles_15m: Sequence[dict[str, float]], current_price: float) -> bool:
    """Return True if 15m price > 15m EMA21 or 15m bars show higher-low structure."""
    if not candles_15m:
        return False
    closes_15m = [c["close"] for c in candles_15m]
    ema21_15m = compute_ema_series(closes_15m, period=21)[-1]
    if ema21_15m > 0 and current_price > ema21_15m:
        return True
    if len(candles_15m) >= 2 and candles_15m[-1]["low"] >= candles_15m[-2]["low"] and candles_15m[-1]["close"] >= candles_15m[-1]["open"]:
        return True
    return False


def _extract_flow_and_accel(pair_data: dict[str, Any]) -> tuple[int, int, float, float]:
    """Return (buys_5m, sells_5m, buy_ratio_5m, buys_accel_ratio vs 1h 5m-avg)."""
    txns = pair_data.get("txns") if isinstance(pair_data.get("txns"), dict) else {}
    m5 = txns.get("m5") if isinstance(txns.get("m5"), dict) else None
    h1 = txns.get("h1") if isinstance(txns.get("h1"), dict) else None

    buys_5m = int(m5.get("buys") or 0) if m5 is not None else int(pair_data.get("buys_5m") or 0)
    sells_5m = int(m5.get("sells") or 0) if m5 is not None else int(pair_data.get("sells_5m") or 0)
    buys_1h = int(h1.get("buys") or 0) if h1 is not None else int(pair_data.get("buys_1h") or 0)

    total_5m = buys_5m + sells_5m
    buy_ratio_5m = (buys_5m / total_5m) if total_5m > 0 else 0.0
    avg_5m_from_1h = (buys_1h / 12.0) if buys_1h > 0 else 0.0
    accel = (buys_5m / avg_5m_from_1h) if avg_5m_from_1h > 0 else (2.0 if buys_5m >= 15 else 0.0)
    return buys_5m, sells_5m, buy_ratio_5m, accel


def detect_breakout_ignition(
    candles: Sequence[dict[str, float]],
    pair_data: dict[str, Any],
    *,
    bar_minutes: int = 5,
    max_extension_pct: float = 0.12,
    rsi_val: Optional[float] = None,
    config: Optional[Any] = None,
    min_vol_ratio: Optional[float] = None,
    max_coil_range_pct: Optional[float] = None,
    min_score: Optional[int] = None,
) -> BreakoutIgnitionResult:
    """Detect BREAKOUT_IGNITION on 5m (>=12 bars) or 1m fallback (>=15 bars) with 0-100 telemetry."""
    tf = "5m" if bar_minutes == 5 else "1m"
    tf_label = "5m" if bar_minutes == 5 else "1m_fallback"
    min_bars = 12 if bar_minutes == 5 else 15
    if len(candles) < min_bars:
        fail_msg = f"BARS({len(candles)}<{min_bars})"
        return BreakoutIgnitionResult(
            detected=False,
            timeframe=tf,
            rejection_reason=f"insufficient {tf} bars ({len(candles)} < {min_bars})",
            telemetry=BreakoutTelemetry(
                score=0,
                state="FORMING",
                timeframe=tf_label,
                vol_ratio=0.0,
                ema_ext_pct=0.0,
                passed_gates=[],
                failed_gates=[fail_msg],
            ),
        )

    vol_thresh = float(
        min_vol_ratio
        if min_vol_ratio is not None
        else (getattr(config, "BREAKOUT_MIN_VOL_RATIO", None) or 1.5)
    )
    raw_coil_max = float(
        max_coil_range_pct
        if max_coil_range_pct is not None
        else (getattr(config, "BREAKOUT_MAX_COIL_RANGE_PCT", None) or 10.0)
    )
    coil_max_frac = (raw_coil_max / 100.0) if raw_coil_max > 1.0 else raw_coil_max
    score_thresh = int(
        min_score
        if min_score is not None
        else (getattr(config, "BREAKOUT_MIN_SCORE", None) or 75)
    )

    curr = candles[-1]
    closes = [c["close"] for c in candles]
    price = curr["close"]

    # 1. Coil / Base (20 pts): >= 3 prior candles with higher lows OR tight range <= BREAKOUT_MAX_COIL_RANGE_PCT (10%)
    coil_bars = candles[-5:-1] if len(candles) >= 5 else candles[-4:-1]
    coil_low = min(c["low"] for c in coil_bars)
    coil_high = max(c["high"] for c in coil_bars)
    coil_range_pct = ((coil_high - coil_low) / coil_low) if coil_low > 0 else 1.0
    last3_lows = [c["low"] for c in candles[-4:-1]]
    higher_lows = len(last3_lows) >= 3 and (last3_lows[0] <= last3_lows[1] <= last3_lows[2]) and (last3_lows[2] > last3_lows[0])
    coil_ok = (coil_range_pct <= coil_max_frac) or higher_lows

    # 2. Resistance Break & Body Conviction (25 pts): close > 15m-30m swing high + top 35% body
    swing_lookback = 6 if bar_minutes == 5 else min(30, len(candles) - 1)
    swing_window = candles[-(swing_lookback + 1) : -1]
    swing_high = max(c["high"] for c in swing_window)
    c_range = curr["high"] - curr["low"]
    body_pos_pct = ((curr["close"] - curr["low"]) / c_range) if c_range > 0 else 0.0
    breakout_close_ok = curr["close"] > swing_high and curr["close"] > curr["open"]
    conviction_ok = c_range > 0 and body_pos_pct >= 0.65
    res_passed = breakout_close_ok and conviction_ok

    # 3. Volume & Flow Spike (25 pts): vol >= BREAKOUT_MIN_VOL_RATIO (1.5x), buy_ratio >= 62%, m5_buys >= 1.5x 1h avg
    prev_5 = candles[-6:-1]
    avg_vol_5 = sum(c.get("volume", 0.0) for c in prev_5) / len(prev_5) if prev_5 else 0.0
    vol_ratio = (curr.get("volume", 0.0) / avg_vol_5) if avg_vol_5 > 0 else 0.0
    vol_passed = vol_ratio >= vol_thresh
    _b5, _s5, buy_ratio_5m, buys_accel = _extract_flow_and_accel(pair_data)

    # 4. MA / VWAP / RSI Alignment (15 pts)
    ema9_series = compute_ema_series(closes, period=9)
    ema21_series = compute_ema_series(closes, period=21)
    ema9 = ema9_series[-1]
    ema21 = ema21_series[-1]
    ema9_sloping_up = len(ema9_series) >= 2 and ema9_series[-1] > ema9_series[-2]
    ema_ok = ema9 > ema21 and ema9_sloping_up
    vwap = compute_vwap(candles[-min(len(candles), 30) :])
    vwap_ok = price > vwap
    rsi_ok = True if rsi_val is None else (rsi_val <= 78.0)

    # 5. HTF 15m Context & Anti-Chase Extension (15 pts)
    candles_15m = _aggregate_to_15m(candles, bar_minutes=bar_minutes)
    htf_bullish = _is_htf_15m_bullish(candles_15m, price)
    ext_ema9 = ((price - ema9) / ema9) if ema9 > 0 else 0.0
    ext_vwap = ((price - vwap) / vwap) if vwap > 0 else 0.0
    over_extended = ext_ema9 > max_extension_pct or ext_vwap > max_extension_pct

    # Weighted 0-100 score across the 5 pillars (20 + 25 + 25 + 15 + 15 = 100)
    score = 0
    passed_gates: list[str] = []
    failed_gates: list[str] = []

    if coil_ok:
        score += 20
        passed_gates.append("COIL")
    else:
        if coil_range_pct <= coil_max_frac + 0.04:
            score += 10
        failed_gates.append(f"COIL({coil_range_pct * 100:.1f}%>{coil_max_frac * 100:.1f}%)")

    if res_passed:
        score += 25
        passed_gates.append("RES")
    elif breakout_close_ok:
        score += 12
        failed_gates.append(f"RES_BODY({body_pos_pct * 100:.0f}%<65%)")
    else:
        failed_gates.append("RES(no_break)")

    if vol_passed:
        score += 10
        passed_gates.append("VOL")
    else:
        if vol_ratio >= max(1.0, vol_thresh - 0.3):
            score += 5
        failed_gates.append(f"VOL_SPIKE({vol_ratio:.1f}x<{vol_thresh:.1f}x)")

    if buy_ratio_5m >= 0.62:
        score += 8
        passed_gates.append("FLOW")
    else:
        if buy_ratio_5m >= 0.55:
            score += 4
        failed_gates.append(f"BUY_RATIO({buy_ratio_5m * 100:.0f}%<62%)")

    if buys_accel >= 1.5:
        score += 7
        passed_gates.append("ACCEL")
    else:
        if buys_accel >= 1.2:
            score += 3
        failed_gates.append(f"BUY_ACCEL({buys_accel:.1f}x<1.5x)")

    if ema_ok:
        score += 5
        passed_gates.append("EMA")
    else:
        failed_gates.append("EMA(not_aligned)")

    if vwap_ok:
        score += 5
        passed_gates.append("VWAP")
    else:
        failed_gates.append("VWAP(below)")

    if rsi_ok:
        score += 5
        passed_gates.append("RSI")
    else:
        failed_gates.append(f"RSI({rsi_val:.1f}>78)")

    if htf_bullish:
        score += 7
        passed_gates.append("HTF")
    else:
        failed_gates.append("HTF_15M(bearish)")

    if not over_extended:
        score += 8
        passed_gates.append("EXT")
    else:
        max_ext_pct = max(ext_ema9, ext_vwap) * 100.0
        failed_gates.append(f"EXT(+{max_ext_pct:.1f}%>+{max_extension_pct * 100:.0f}%)")

    final_score = int(min(100, max(0, score)))
    if over_extended:
        state = "EXTENDED"
    elif final_score >= score_thresh and res_passed and vol_passed:
        state = "IGNITION"
    elif final_score >= 50:
        state = "COILING"
    else:
        state = "FORMING"

    telemetry = BreakoutTelemetry(
        score=final_score,
        state=state,
        timeframe=tf_label,
        vol_ratio=round(vol_ratio, 2),
        ema_ext_pct=round(ext_ema9 * 100.0, 2),
        passed_gates=passed_gates,
        failed_gates=failed_gates,
    )

    metrics = dict(
        timeframe=tf,
        ema9=round(ema9, 8),
        ema21=round(ema21, 8),
        vwap=round(vwap, 8),
        ext_above_ema9_pct=round(ext_ema9, 4),
        ext_above_vwap_pct=round(ext_vwap, 4),
        vol_ratio=round(vol_ratio, 2),
        buy_ratio_5m=round(buy_ratio_5m, 4),
        buys_accel_ratio=round(buys_accel, 2),
        body_pos_pct=round(body_pos_pct, 3),
        coil_range_pct=round(coil_range_pct, 4),
        htf_bullish=htf_bullish,
        telemetry=telemetry,
    )

    # Entry trigger requires: score >= 75, RES passed, VOL_SPIKE passed, state != EXTENDED, and RSI <= 78
    reasons: list[str] = []
    if not breakout_close_ok:
        reasons.append("no close above 15m-30m swing high")
    elif not conviction_ok:
        reasons.append(f"upper-wick fakeout (body_pos={body_pos_pct * 100:.0f}% < 65%)")
    if not vol_passed:
        reasons.append(f"vol_spike {vol_ratio:.1f}x < {vol_thresh:.1f}x")
    if over_extended:
        reasons.append(
            f"over-extended (ema9={ext_ema9 * 100:+.1f}%, vwap={ext_vwap * 100:+.1f}% > +{max_extension_pct * 100:.0f}%)"
        )
    if not rsi_ok:
        reasons.append(f"RSI={rsi_val:.1f} > 78")
    if final_score < score_thresh:
        if not coil_ok:
            reasons.append(f"no coil/base (range={coil_range_pct * 100:.1f}% > {coil_max_frac * 100:.0f}%)")
        reasons.append(f"breakout_score {final_score} < {score_thresh}")

    detected = (
        final_score >= score_thresh
        and res_passed
        and vol_passed
        and state != "EXTENDED"
        and rsi_ok
    )
    return BreakoutIgnitionResult(
        detected=detected,
        rejection_reason=" / ".join(reasons) if not detected and reasons else None,
        **metrics,
    )
