#!/usr/bin/env python3
"""Automated daily whale discovery and single-batch Helius webhook sync job."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import AgentConfig, load_config
from helius_webhook import _extract_account_addresses, _is_active_webhook
from whale_discovery_scoring import (
    WalletScore,
    extract_buyer_candidates,
    fetch_runner_pools,
    fetch_wallet_sol_balance,
    fetch_wallet_transactions_7d,
    is_wallet_stale,
    score_wallet_history,
)

logger = logging.getLogger(__name__)

DEFAULT_PINNED_PATH = ROOT_DIR / "data" / "pinned_whales.json"
DEFAULT_SYNC_URL = "http://127.0.0.1:8000/api/webhooks/helius/sync"
DEFAULT_RPC_URL = "https://mainnet.helius-rpc.com"


async def resolve_active_webhook(
    client: httpx.AsyncClient,
    config: AgentConfig,
) -> dict[str, Any]:
    """Fetch the full active Helius webhook config (GET /v0/webhooks/{webhookID})."""
    api_key = config.HELIUS_API_KEY.strip()
    if not api_key:
        raise RuntimeError("HELIUS_API_KEY is required for whale discovery and webhook sync.")

    base = config.HELIUS_API_BASE_URL.rstrip("/")
    webhook_id = config.HELIUS_WEBHOOK_ID.strip()

    if not webhook_id:
        list_resp = await client.get(
            f"{base}/webhooks", params={"api-key": api_key}, timeout=12.0
        )
        list_resp.raise_for_status()
        hooks = list_resp.json()
        if not isinstance(hooks, list) or not hooks:
            raise RuntimeError("No Helius webhooks found for this API key.")
        active_hooks = [h for h in hooks if isinstance(h, dict) and _is_active_webhook(h)]
        url_needle = (config.HELIUS_WEBHOOK_URL.strip().lower() or "solana-api.projectkami.net")
        selected = next(
            (h for h in active_hooks if url_needle in str(h.get("webhookURL") or "").lower()),
            active_hooks[0] if active_hooks else hooks[0],
        )
        webhook_id = str(selected.get("webhookID") or selected.get("id") or "").strip()
        if not webhook_id:
            raise RuntimeError("Matched Helius webhook is missing webhookID.")

    detail_resp = await client.get(
        f"{base}/webhooks/{webhook_id}", params={"api-key": api_key}, timeout=12.0
    )
    detail_resp.raise_for_status()
    detail = detail_resp.json()
    if not isinstance(detail, dict):
        raise RuntimeError(f"Unexpected Helius webhook detail payload for {webhook_id}.")
    detail.setdefault("webhookID", webhook_id)
    return detail


def load_pinned_whales(path: Path) -> list[str]:
    """Load pinned wallet addresses from data/pinned_whales.json."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else (raw.get("wallets") if isinstance(raw, dict) else [])
    result: list[str] = []
    seen: set[str] = set()
    for w in items or []:
        addr = str(w or "").strip()
        if addr and addr not in seen:
            seen.add(addr)
            result.append(addr)
    return result


def save_pinned_whales(path: Path, wallets: list[str], webhook_id: str) -> list[str]:
    """Snapshot wallet addresses into data/pinned_whales.json."""
    deduped: list[str] = []
    seen: set[str] = set()
    for w in wallets:
        addr = str(w or "").strip()
        if addr and addr not in seen:
            seen.add(addr)
            deduped.append(addr)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pinned_at_utc": datetime.now(timezone.utc).isoformat(),
        "webhook_id": webhook_id,
        "wallets": deduped,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return deduped


def format_report_table(
    passing: list[WalletScore],
    stale_pruned: list[str],
    pinned_count: int,
    final_count: int,
    applied: bool,
) -> str:
    """Render a clean Markdown/CLI summary table for dry-run and apply runs."""
    lines = [
        f"Mode: {'APPLY' if applied else 'DRY-RUN'} | Pinned: {pinned_count} | "
        f"Passing Candidates: {len(passing)} | Stale Pruned (>72h): {len(stale_pruned)} | "
        f"Final Roster: {final_count}",
        "",
        "| Wallet | SOL Bal | Tx 7d | Win Rate | Realized SOL | Sleep Gap | Score |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    if passing:
        for w in passing:
            lines.append(
                f"| `{w.wallet}` | {w.sol_balance:.2f} | {w.tx_count_7d} | "
                f"{w.win_rate * 100:.1f}% | {w.realized_sol_pnl:+.2f} | "
                f"{w.sleep_gap_hrs}h | {w.rank_score:.3f} |"
            )
    else:
        lines.append("| *(none)* | - | - | - | - | - | - |")

    if stale_pruned:
        lines.append("")
        lines.append("Stale non-pinned wallets flagged for pruning (>72h inactive):")
        for addr in stale_pruned:
            lines.append(f"- `{addr}`")
    return "\n".join(lines)


async def run_discovery(
    config: AgentConfig,
    *,
    pinned_path: Path = DEFAULT_PINNED_PATH,
    pin_current: bool = False,
    apply_changes: bool = False,
    max_new: int = 10,
    max_total: int = 25,
    max_runners: int = 15,
    stale_hours: float = 72.0,
    sync_url: str = DEFAULT_SYNC_URL,
    rpc_url: str = DEFAULT_RPC_URL,
    http_client: Optional[httpx.AsyncClient] = None,
    now_ts: Optional[int] = None,
) -> dict[str, Any]:
    """Execute the full 3-stage whale discovery and optional single-batch Helius sync."""
    current_ts = int(now_ts if now_ts is not None else time.time())
    api_key = config.HELIUS_API_KEY.strip()
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(follow_redirects=True)

    try:
        webhook = await resolve_active_webhook(client, config)
        webhook_id = str(webhook["webhookID"])
        current_addresses = _extract_account_addresses(webhook)

        # Pinned Roster Protection: snapshot on first run or explicit --pin-current
        if pin_current or not pinned_path.exists():
            pinned_wallets = save_pinned_whales(pinned_path, current_addresses, webhook_id)
            logger.info(
                "[WhaleDiscovery] Snapshotted %d pinned wallets to %s",
                len(pinned_wallets),
                pinned_path,
            )
            if pin_current:
                return {
                    "mode": "pin_current",
                    "webhook_id": webhook_id,
                    "pinned_wallets": pinned_wallets,
                    "passing_candidates": [],
                    "stale_pruned": [],
                    "final_addresses": current_addresses,
                    "put_called": False,
                    "report": f"Pinned {len(pinned_wallets)} wallets to {pinned_path}.",
                }
        else:
            pinned_wallets = load_pinned_whales(pinned_path)

        pinned_set = set(pinned_wallets)

        # Identify stale non-pinned wallets currently on the webhook (>72h inactive)
        existing_non_pinned = [w for w in current_addresses if w not in pinned_set]
        stale_pruned: list[str] = []
        retained_non_pinned: list[str] = []
        for w in existing_non_pinned:
            try:
                stale = await is_wallet_stale(
                    client,
                    w,
                    api_key=api_key,
                    helius_base_url=config.HELIUS_API_BASE_URL,
                    now_ts=current_ts,
                    stale_hours=stale_hours,
                )
            except Exception as exc:
                logger.warning("[WhaleDiscovery] Staleness check failed for %s: %s", w[:8], exc)
                stale = False
            if stale:
                stale_pruned.append(w)
            else:
                retained_non_pinned.append(w)

        # Stage 1: Candidate Sourcing (DexScreener runners + Helius >=1.5 SOL buyers)
        runners = await fetch_runner_pools(client, config, max_runners=max_runners)
        candidate_wallets = await extract_buyer_candidates(
            client,
            runners,
            api_key=api_key,
            helius_base_url=config.HELIUS_API_BASE_URL,
            exclude_wallets=pinned_set,
            min_buy_sol=1.5,
        )

        # Stage 2: Human & Edge Scoring Filter
        scored_all: list[WalletScore] = []
        for wallet in candidate_wallets:
            try:
                txs = await fetch_wallet_transactions_7d(
                    client,
                    wallet,
                    api_key=api_key,
                    helius_base_url=config.HELIUS_API_BASE_URL,
                    now_ts=current_ts,
                )
                sol_bal = await fetch_wallet_sol_balance(
                    client, wallet, api_key=api_key, rpc_url=rpc_url
                )
                ws = score_wallet_history(
                    wallet, txs, liquid_sol_balance=sol_bal, now_ts=current_ts
                )
                scored_all.append(ws)
            except Exception as exc:
                logger.warning("[WhaleDiscovery] Scoring failed for %s: %s", wallet[:8], exc)

        passing = sorted(
            [s for s in scored_all if s.passed],
            key=lambda s: s.rank_score,
            reverse=True,
        )

        # Build final roster: pinned (always preserved) + top vetted new wallets (capped at max_total)
        available_slots = max(0, max_total - len(pinned_wallets))
        selected_new = passing[: min(max_new, available_slots)]
        final_addresses: list[str] = []
        seen_final: set[str] = set()
        for addr in pinned_wallets + [s.wallet for s in selected_new] + retained_non_pinned:
            if addr in seen_final:
                continue
            if len(final_addresses) >= max_total and addr not in pinned_set:
                break
            seen_final.add(addr)
            final_addresses.append(addr)

        # Stage 3: Single-Batch Helius Update (--apply)
        put_called = False
        sync_ok = False
        if apply_changes:
            base = config.HELIUS_API_BASE_URL.rstrip("/")
            put_body: dict[str, Any] = {
                "webhookURL": webhook.get("webhookURL", ""),
                "transactionTypes": webhook.get("transactionTypes", ["SWAP"]),
                "accountAddresses": final_addresses,
                "webhookType": webhook.get("webhookType", "enhanced"),
            }
            if webhook.get("authHeader") is not None:
                put_body["authHeader"] = webhook["authHeader"]
            if webhook.get("txnStatus") is not None:
                put_body["txnStatus"] = webhook["txnStatus"]

            put_resp = await client.put(
                f"{base}/webhooks/{webhook_id}",
                params={"api-key": api_key},
                json=put_body,
                timeout=15.0,
            )
            put_resp.raise_for_status()
            put_called = True
            logger.info(
                "[WhaleDiscovery] Updated Helius webhook %s with %d addresses in 1 PUT call.",
                webhook_id,
                len(final_addresses),
            )

            try:
                sync_resp = await client.post(sync_url, timeout=10.0)
                sync_resp.raise_for_status()
                sync_ok = True
            except Exception as exc:
                logger.warning("[WhaleDiscovery] Local agent sync POST %s failed: %s", sync_url, exc)

        report = format_report_table(
            selected_new,
            stale_pruned,
            pinned_count=len(pinned_wallets),
            final_count=len(final_addresses),
            applied=apply_changes,
        )
        return {
            "mode": "apply" if apply_changes else "dry_run",
            "webhook_id": webhook_id,
            "pinned_wallets": pinned_wallets,
            "runners_count": len(runners),
            "candidates_evaluated": len(scored_all),
            "passing_candidates": selected_new,
            "rejected_candidates": [s for s in scored_all if not s.passed],
            "stale_pruned": stale_pruned,
            "final_addresses": final_addresses,
            "put_called": put_called,
            "sync_ok": sync_ok,
            "report": report,
        }
    finally:
        if owns_client:
            await client.aclose()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Automated daily whale discovery and Helius webhook roster sync."
    )
    parser.add_argument(
        "--pin-current",
        action="store_true",
        help="Snapshot current Helius webhook accountAddresses into data/pinned_whales.json and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview passing candidates and stale wallets without updating Helius (default).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply pinned + top vetted wallets via a single PUT /v0/webhooks/{webhookID} call.",
    )
    parser.add_argument("--max-new", type=int, default=10, help="Max new vetted wallets to add (default: 10).")
    parser.add_argument("--max-total", type=int, default=25, help="Max total webhook roster size (default: 25).")
    parser.add_argument("--pinned-file", type=Path, default=DEFAULT_PINNED_PATH, help="Path to pinned_whales.json.")
    parser.add_argument("--sync-url", type=str, default=DEFAULT_SYNC_URL, help="Live agent roster sync endpoint.")
    args = parser.parse_args(argv)

    cfg = load_config()
    result = asyncio.run(
        run_discovery(
            cfg,
            pinned_path=args.pinned_file,
            pin_current=args.pin_current,
            apply_changes=args.apply and not args.dry_run,
            max_new=args.max_new,
            max_total=args.max_total,
            sync_url=args.sync_url,
        )
    )
    print(result["report"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
