"""Anti-Cabal Safety Gate: Deterministic RugCheck + Jupiter Shield verification."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Optional

import httpx

from config import AgentConfig
from db import AgentDatabase
from state import AgentGraphState, FocusState, SafetyReport

logger = logging.getLogger(__name__)

SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
CRITICAL_SHIELD_TYPES = {
    "HAS_MINT_AUTHORITY",
    "HAS_FREEZE_AUTHORITY",
    "NOT_SELLABLE",
    "HONEYPOT",
    "MUTABLE_TRANSFER_FEES",
    "HIGH_PRICE_IMPACT",
    "CRITICAL",
}


def _parse_pct(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"(\d+(?:\.\d+)?)", str(value))
    return float(match.group(1)) if match else None


def evaluate_rugcheck_summary(
    summary: dict[str, Any],
    config: AgentConfig,
) -> tuple[list[str], Optional[int], Optional[float], Optional[float]]:
    """Deterministically evaluate RugCheck /v1/tokens/{mint}/report/summary."""
    reasons: list[str] = []
    score = summary.get("riskScore")
    if score is None:
        score = summary.get("score")
    rugcheck_score = int(score) if score is not None else None

    if summary.get("rugged") is True:
        reasons.append("RugCheck: token flagged as rugged")

    # Direct authority fields if present
    mint_auth = summary.get("mintAuthority")
    if mint_auth and str(mint_auth) not in ("", "null", "None", SYSTEM_PROGRAM_ID):
        reasons.append("RugCheck: active mint authority")

    freeze_auth = summary.get("freezeAuthority")
    if freeze_auth and str(freeze_auth) not in ("", "null", "None", SYSTEM_PROGRAM_ID):
        reasons.append("RugCheck: active freeze authority")

    # Top-10 holders check
    top10_pct: Optional[float] = None
    if summary.get("topHolders") and isinstance(summary["topHolders"], list):
        top10_pct = sum(
            float(h.get("pct") or 0.0) for h in summary["topHolders"][:10] if isinstance(h, dict)
        )
        if top10_pct > config.MAX_TOP10_HOLDERS_PCT:
            reasons.append(
                f"RugCheck: Top-10 holders {top10_pct:.1f}% > {config.MAX_TOP10_HOLDERS_PCT:.0f}%"
            )

    # LP lock / burn check across markets
    lp_locked_pct: Optional[float] = None
    markets = summary.get("markets")
    if isinstance(markets, list) and markets:
        locked_vals = []
        for m in markets:
            lp = m.get("lp") if isinstance(m, dict) else None
            if isinstance(lp, dict) and lp.get("lpLockedPct") is not None:
                locked_vals.append(float(lp["lpLockedPct"]))
        if locked_vals:
            lp_locked_pct = max(locked_vals)
            if lp_locked_pct < config.MIN_LP_LOCKED_PCT:
                reasons.append(
                    f"RugCheck: unburned/unlocked LP ({lp_locked_pct:.1f}% < {config.MIN_LP_LOCKED_PCT:.0f}%)"
                )

    # Risk items
    for risk in summary.get("risks") or []:
        if not isinstance(risk, dict):
            continue
        name = str(risk.get("name") or "")
        desc = str(risk.get("description") or "")
        level = str(risk.get("level") or "").lower()
        combined = f"{name} {desc}".lower()

        if "mint authority" in combined:
            reasons.append(f"RugCheck: {name or 'active mint authority'}")
        elif "freeze authority" in combined:
            reasons.append(f"RugCheck: {name or 'active freeze authority'}")
        elif "lp" in combined and ("unlocked" in combined or "unburned" in combined or "not locked" in combined):
            reasons.append(f"RugCheck: {name or 'unburned/unlocked LP'}")
        elif "top 10" in combined or "top holders" in combined:
            pct = _parse_pct(risk.get("value"))
            if pct is not None:
                top10_pct = pct if top10_pct is None else max(top10_pct, pct)
                if pct > config.MAX_TOP10_HOLDERS_PCT:
                    reasons.append(
                        f"RugCheck: Top-10 holders {pct:.1f}% > {config.MAX_TOP10_HOLDERS_PCT:.0f}%"
                    )
            elif level in ("danger", "critical"):
                reasons.append(f"RugCheck: critical holder concentration ({name})")
        elif level in ("danger", "critical"):
            reasons.append(f"RugCheck critical risk: {name or desc}")

    return reasons, rugcheck_score, top10_pct, lp_locked_pct


def evaluate_jupiter_shield(shield_payload: dict[str, Any], mint: str) -> list[str]:
    """Deterministically evaluate Jupiter /ultra/v1/shield response for a mint."""
    reasons: list[str] = []

    # Handle both {"warnings": {mint: [...]}} and {"mints": {mint: {"shield": {...}}}} formats
    warnings_map = shield_payload.get("warnings")
    if isinstance(warnings_map, dict) and mint in warnings_map:
        for w in warnings_map.get(mint) or []:
            if isinstance(w, dict):
                w_type = str(w.get("type") or "").upper()
                severity = str(w.get("severity") or "").lower()
                msg = str(w.get("message") or w_type or "warning")
                if w_type in CRITICAL_SHIELD_TYPES or severity in ("critical", "danger"):
                    reasons.append(f"Jupiter Shield: {msg}")
            elif isinstance(w, str) and w:
                low = w.lower()
                if any(k in low for k in ("mint", "freeze", "honeypot", "critical", "not sellable")):
                    reasons.append(f"Jupiter Shield: {w}")

    mints_map = shield_payload.get("mints")
    token_obj: Optional[dict[str, Any]] = None
    if isinstance(mints_map, dict):
        token_obj = mints_map.get(mint)
    elif isinstance(shield_payload, list):
        for item in shield_payload:
            if isinstance(item, dict) and item.get("mint") == mint:
                token_obj = item
                break

    if isinstance(token_obj, dict):
        shield = token_obj.get("shield") if isinstance(token_obj.get("shield"), dict) else token_obj
        if shield.get("isFrozen") is True:
            reasons.append("Jupiter Shield: token is frozen")
        fa = shield.get("freezeAuthority")
        if fa and str(fa) not in ("", "null", "None", SYSTEM_PROGRAM_ID):
            reasons.append("Jupiter Shield: active freeze authority")
        ma = shield.get("mintAuthority")
        if ma and str(ma) not in ("", "null", "None", SYSTEM_PROGRAM_ID):
            reasons.append("Jupiter Shield: active mint authority")
        for w in shield.get("warnings") or []:
            if isinstance(w, dict):
                w_type = str(w.get("type") or "").upper()
                severity = str(w.get("severity") or "").lower()
                msg = str(w.get("message") or w_type)
                if w_type in CRITICAL_SHIELD_TYPES or severity in ("critical", "danger"):
                    reasons.append(f"Jupiter Shield: {msg}")
            elif isinstance(w, str) and w:
                low = w.lower()
                if any(k in low for k in ("mint", "freeze", "honeypot", "critical", "not sellable")):
                    reasons.append(f"Jupiter Shield: {w}")

    return reasons


async def check_token_safety(
    mint: str,
    config: AgentConfig,
    client: httpx.AsyncClient,
) -> SafetyReport:
    """Run RugCheck summary + Jupiter Shield checks for a single mint."""
    reasons: list[str] = []
    rugcheck_score: Optional[int] = None
    top10_pct: Optional[float] = None
    lp_locked_pct: Optional[float] = None

    # 1. RugCheck /v1/tokens/{mint}/report/summary
    rug_url = f"{config.RUGCHECK_BASE_URL}/v1/tokens/{mint}/report/summary"
    try:
        rug_resp = await client.get(rug_url, timeout=10.0)
        rug_resp.raise_for_status()
        rug_data = rug_resp.json()
        r_reasons, rugcheck_score, top10_pct, lp_locked_pct = evaluate_rugcheck_summary(
            rug_data, config
        )
        reasons.extend(r_reasons)
    except Exception as exc:
        reasons.append(f"RugCheck unavailable/failed: {exc}")

    # 2. Jupiter Shield /ultra/v1/shield
    shield_url = f"{config.JUPITER_BASE_URL}/ultra/v1/shield"
    headers = {"x-api-key": config.JUPITER_API_KEY} if config.JUPITER_API_KEY else {}
    try:
        sh_resp = await client.get(
            shield_url,
            params={"mints": mint},
            headers=headers,
            timeout=10.0,
        )
        sh_resp.raise_for_status()
        sh_data = sh_resp.json()
        reasons.extend(evaluate_jupiter_shield(sh_data, mint))
    except Exception as exc:
        reasons.append(f"Jupiter Shield unavailable/failed: {exc}")

    # Deduplicate reasons while preserving order
    deduped = list(dict.fromkeys(reasons))
    return SafetyReport(
        mint=mint,
        passed=len(deduped) == 0,
        reasons=deduped,
        rugcheck_score=rugcheck_score,
        top10_holders_pct=top10_pct,
        lp_locked_pct=lp_locked_pct,
    )


async def safety_gate_node(
    state: AgentGraphState,
    *,
    config: AgentConfig,
    db: Optional[AgentDatabase] = None,
    http_client: Optional[httpx.AsyncClient] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """LangGraph node: deterministically filters candidates before calling Gemini."""
    if state.get("focus_state") != FocusState.SCANNING.value:
        return {"safe_candidates": [], "rejected_candidates": []}

    raw_candidates = state.get("raw_candidates") or []
    if not raw_candidates:
        return {"safe_candidates": [], "rejected_candidates": []}

    watchlist_safety = (
        {w["mint"]: bool(w.get("safety_passed", False)) for w in db.get_watchlist()}
        if db is not None
        else {}
    )

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient()
    safe: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    try:
        for cand in raw_candidates:
            mint = cand["mint"]
            symbol = cand.get("symbol", "UNKNOWN")

            # Cache hit: skip RugCheck + Jupiter Shield on retained active_watchlist slots
            if cand.get("safety_passed") is True or watchlist_safety.get(mint) is True:
                enriched = dict(cand)
                enriched["safety_passed"] = True
                enriched["safety_report"] = {"mint": mint, "passed": True, "cached": True, "reasons": []}
                safe.append(enriched)
                logger.info("[SafetyGate] $%s | CACHED PASS (skipped redundant API calls)", symbol)
                continue

            report = await check_token_safety(mint, config, client)
            if report.passed:
                if db is not None:
                    db.mark_watchlist_safety_passed(mint, True)
                enriched = dict(cand)
                enriched["safety_passed"] = True
                enriched["safety_report"] = report.model_dump()
                safe.append(enriched)
            else:
                reason_str = "; ".join(report.reasons)
                if db is not None:
                    db.remove_from_watchlist(mint)
                    db.set_cooldown_minutes(
                        mint,
                        minutes=config.SAFETY_REJECT_COOLDOWN_MINUTES,
                        reason=f"SAFETY_REJECT: {reason_str}",
                        now=now,
                    )
                    db.log_action(
                        mode=config.EXECUTION_MODE,
                        action="EVICT",
                        symbol=symbol,
                        asset_id=mint,
                        strategy="SAFETY_GATE",
                        reason=f"SAFETY_REJECT: {reason_str}",
                        timestamp_utc=now.isoformat() if now else None,
                    )
                logger.info(
                    "[SafetyGate] $%s | REJECTED (%s) -> EVICTED (%dm cooldown)",
                    symbol,
                    reason_str,
                    config.SAFETY_REJECT_COOLDOWN_MINUTES,
                )
                rejected.append(
                    {
                        "mint": mint,
                        "symbol": symbol,
                        "reasons": report.reasons,
                    }
                )
    finally:
        if owns_client:
            await client.aclose()

    logger.info(
        "Anti-Cabal Gate: %d passed, %d rejected",
        len(safe),
        len(rejected),
    )
    out: dict[str, Any] = {
        "safe_candidates": safe,
        "rejected_candidates": rejected,
    }
    if db is not None:
        out["active_watchlist"] = db.get_watchlist()
    return out
