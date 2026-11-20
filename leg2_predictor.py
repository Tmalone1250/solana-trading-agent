"""Deterministic Leg 2 Predictor (leg2_predictor.py): Pure-Python 0-100 scoring."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class Leg2Result(BaseModel):
    """0-100 Leg 2 breakout readiness score and deterministic component breakdown."""

    score: float
    buy_absorption: float  # 0-30 pts
    volume_acceleration: float  # 0-25 pts
    bounce_structure: float  # 0-25 pts
    liquidity_stability: float  # 0-20 pts
    buy_ratio_5m: float = 0.0
    txns_5m: int = 0
    vol_accel_ratio: float = 0.0
    liq_mc_ratio: float = 0.0
    liq_drop_pct: float = 0.0
    summary: str = ""


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _extract_metrics(pair_data: dict[str, Any]) -> dict[str, float]:
    """Extract normalized metrics from either a raw DexScreener pair or CandidateToken dict."""
    txns_m5 = (pair_data.get("txns") or {}).get("m5") if isinstance(pair_data.get("txns"), dict) else None
    if isinstance(txns_m5, dict):
        buys_5m = int(txns_m5.get("buys") or 0)
        sells_5m = int(txns_m5.get("sells") or 0)
    else:
        buys_5m = int(pair_data.get("buys_5m") or 0)
        sells_5m = int(pair_data.get("sells_5m") or 0)

    vol = pair_data.get("volume") if isinstance(pair_data.get("volume"), dict) else None
    if isinstance(vol, dict):
        vol_m5 = float(vol.get("m5") or 0.0)
        vol_h1 = float(vol.get("h1") or 0.0)
    else:
        vol_m5 = float(pair_data.get("volume_5m_usd") or 0.0)
        vol_h1 = float(pair_data.get("volume_1h_usd") or 0.0)

    pc = pair_data.get("priceChange") if isinstance(pair_data.get("priceChange"), dict) else None
    if isinstance(pc, dict):
        pc_m5 = float(pc.get("m5") or 0.0)
        pc_h1 = float(pc.get("h1") or 0.0)
    else:
        pc_m5 = float(pair_data.get("price_change_5m_pct") or 0.0)
        pc_h1 = float(pair_data.get("price_change_1h_pct") or 0.0)

    liq_obj = pair_data.get("liquidity")
    if isinstance(liq_obj, dict):
        liquidity_usd = float(liq_obj.get("usd") or 0.0)
    else:
        liquidity_usd = float(pair_data.get("liquidity_usd") or 0.0)

    mc_usd = float(
        pair_data.get("marketCap")
        or pair_data.get("fdv")
        or pair_data.get("market_cap_usd")
        or 0.0
    )

    return {
        "buys_5m": float(buys_5m),
        "sells_5m": float(sells_5m),
        "vol_m5": vol_m5,
        "vol_h1": vol_h1,
        "pc_m5": pc_m5,
        "pc_h1": pc_h1,
        "liquidity_usd": liquidity_usd,
        "mc_usd": mc_usd,
    }


def compute_leg2_score(
    pair_data: dict[str, Any],
    tracked_peak_mc: float = 0.0,
    tracked_low_mc: float = 0.0,
    *,
    entry_liquidity_usd: Optional[float] = None,
) -> Leg2Result:
    """Compute a deterministic 0-100 Leg 2 predictor score from live pair metrics."""
    m = _extract_metrics(pair_data)
    buys_5m = int(m["buys_5m"])
    sells_5m = int(m["sells_5m"])
    txns_5m = buys_5m + sells_5m
    buy_ratio_5m = (buys_5m / txns_5m) if txns_5m > 0 else 0.0

    # 1. Buy Absorption (30 pts): scales 0 at 50% -> 30 at >= 62%, requires txns_5m >= 15
    if txns_5m >= 15:
        buy_absorption = 30.0 * _clamp((buy_ratio_5m - 0.50) / (0.62 - 0.50), 0.0, 1.0)
    else:
        buy_absorption = 0.0

    # 2. Volume Acceleration (25 pts): (volume.m5 * 12) / max(volume.h1, 1) -> 25 at >= 1.3
    vol_accel_ratio = (m["vol_m5"] * 12.0) / max(m["vol_h1"], 1.0)
    if m["vol_m5"] > 0:
        volume_acceleration = 25.0 * _clamp(vol_accel_ratio / 1.3, 0.0, 1.0)
    else:
        volume_acceleration = 0.0

    # 3. Bounce Structure (25 pts):
    # Full 25 pts when priceChange.h1 >= 15% and priceChange.m5 in [+2%, +25%].
    pc_h1 = m["pc_h1"]
    pc_m5 = m["pc_m5"]
    if pc_h1 >= 15.0 and 2.0 <= pc_m5 <= 25.0:
        bounce_structure = 25.0
    elif pc_m5 <= 0.0:
        bounce_structure = 0.0
    else:
        h1_pts = 12.5 * _clamp(pc_h1 / 15.0, 0.0, 1.0)
        if 2.0 <= pc_m5 <= 25.0:
            m5_pts = 12.5
        elif 0.0 < pc_m5 < 2.0:
            m5_pts = 12.5 * (pc_m5 / 2.0)
        else:
            # Overextended > +25% in 5m: taper off
            m5_pts = 12.5 * _clamp(1.0 - (pc_m5 - 25.0) / 25.0, 0.0, 1.0)
        # Small structural bonus if curling above tracked_low_mc after a pullback from tracked_peak_mc
        curl_bonus = 0.0
        if tracked_peak_mc > tracked_low_mc > 0 and m["mc_usd"] > tracked_low_mc * 1.02:
            curl_bonus = 2.5
        bounce_structure = min(25.0, h1_pts + m5_pts + curl_bonus)

    # 4. Liquidity Depth & Stability (20 pts):
    # Full 20 pts when liquidity/MC >= 0.10 and liquidity drop <= 5% since watchlist entry.
    liq_mc_ratio = m["liquidity_usd"] / max(m["mc_usd"], 1.0) if m["mc_usd"] > 0 else 0.0
    ref_liq = entry_liquidity_usd if entry_liquidity_usd is not None else float(pair_data.get("entry_liquidity_usd") or 0.0)
    liq_drop_pct = (
        max(0.0, (ref_liq - m["liquidity_usd"]) / ref_liq)
        if ref_liq > 0 and m["liquidity_usd"] > 0
        else 0.0
    )

    depth_pts = 10.0 * _clamp(liq_mc_ratio / 0.10, 0.0, 1.0)
    if liq_drop_pct <= 0.05:
        stability_pts = 10.0
    else:
        # Lose stability points rapidly once liquidity drops > 5%
        stability_pts = 10.0 * _clamp(1.0 - (liq_drop_pct - 0.05) / 0.10, 0.0, 1.0)
    liquidity_stability = depth_pts + stability_pts

    total = round(
        _clamp(
            buy_absorption + volume_acceleration + bounce_structure + liquidity_stability,
            0.0,
            100.0,
        ),
        1,
    )
    summary = (
        f"leg2={total:.1f}/100 "
        f"(buy={buy_absorption:.1f}/30 [ratio={buy_ratio_5m*100:.0f}%, n={txns_5m}], "
        f"vol={volume_acceleration:.1f}/25 [x{vol_accel_ratio:.2f}], "
        f"bounce={bounce_structure:.1f}/25 [h1={pc_h1:+.1f}%, m5={pc_m5:+.1f}%], "
        f"liq={liquidity_stability:.1f}/20 [liq/mc={liq_mc_ratio:.2f}, drop={liq_drop_pct*100:.1f}%])"
    )
    return Leg2Result(
        score=total,
        buy_absorption=round(buy_absorption, 2),
        volume_acceleration=round(volume_acceleration, 2),
        bounce_structure=round(bounce_structure, 2),
        liquidity_stability=round(liquidity_stability, 2),
        buy_ratio_5m=round(buy_ratio_5m, 4),
        txns_5m=txns_5m,
        vol_accel_ratio=round(vol_accel_ratio, 3),
        liq_mc_ratio=round(liq_mc_ratio, 4),
        liq_drop_pct=round(liq_drop_pct, 4),
        summary=summary,
    )
