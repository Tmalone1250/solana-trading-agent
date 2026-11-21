"""Stage 1 & Stage 2 scoring helpers for automated daily whale discovery."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from config import LAMPORTS_PER_SOL, SOL_MINT, AgentConfig
from helius_pacer import HeliusEnhancedPacer
from helius_webhook import IGNORED_BASE_MINTS

logger = logging.getLogger(__name__)


# Known Solana DEX, AMM, router, and system program addresses excluded from buyer candidates
KNOWN_PROGRAM_ADDRESSES: set[str] = {
    "11111111111111111111111111111111", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "ComputeBudget111111111111111111111111111111", "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB", "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK", "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
    "routeUGWgWzqBWFcrCfv8tritsqukccJPu3q5GPP3xS", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA", "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo", "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
}

TOKEN_CREATION_TX_TYPES: set[str] = {
    "TOKEN_MINT", "CREATE", "INITIALIZE_MINT", "COMPRESSED_NFT_MINT", "NFT_MINT",
}


@dataclass
class RunnerPool:
    mint: str
    symbol: str
    pair_address: str
    liquidity_usd: float
    market_cap_usd: float
    price_change_24h: float
    volume_24h: float


@dataclass
class WalletScore:
    wallet: str
    passed: bool
    reject_reasons: list[str] = field(default_factory=list)
    sol_balance: float = 0.0
    tx_count_7d: int = 0
    buy_count: int = 0
    sell_count: int = 0
    mint_create_count: int = 0
    sleep_gap_hrs: int = 0
    closed_mints: int = 0
    winning_mints: int = 0
    win_rate: float = 0.0
    realized_sol_pnl: float = 0.0
    max_single_mint_share: float = 0.0
    rank_score: float = 0.0


def max_consecutive_zero_hours(timestamps: list[int]) -> int:
    """Compute the longest consecutive run of UTC hours (0-23, circular) with 0 trades."""
    if not timestamps:
        return 24
    active_hours = {int((ts // 3600) % 24) for ts in timestamps}
    if len(active_hours) == 24:
        return 0
    # Double the 24-hour ring to measure wrap-around gaps across midnight UTC
    best = 0
    run = 0
    for h in range(48):
        if (h % 24) not in active_hours:
            run += 1
            if run > best:
                best = run
        else:
            run = 0
    return min(best, 24)


def _extract_sol_delta_for_wallet(tx: dict[str, Any], wallet: str) -> float:
    """Return signed net SOL flow (+ received, - spent) via nativeTransfers, WSOL, and events.swap."""
    native_out = 0.0
    native_in = 0.0
    for nf in tx.get("nativeTransfers") or []:
        if not isinstance(nf, dict):
            continue
        amt = float(nf.get("amount") or 0.0) / LAMPORTS_PER_SOL
        if amt <= 0:
            continue
        if nf.get("fromUserAccount") == wallet:
            native_out += amt
        if nf.get("toUserAccount") == wallet:
            native_in += amt

    wsol_out = 0.0
    wsol_in = 0.0
    for tf in tx.get("tokenTransfers") or []:
        if not isinstance(tf, dict) or tf.get("mint") != SOL_MINT:
            continue
        amt = float(tf.get("tokenAmount") or 0.0)
        if amt <= 0:
            continue
        if tf.get("fromUserAccount") == wallet:
            wsol_out += amt
        if tf.get("toUserAccount") == wallet:
            wsol_in += amt

    event_out = 0.0
    event_in = 0.0
    swap_ev = (tx.get("events") or {}).get("swap") if isinstance(tx.get("events"), dict) else None
    if isinstance(swap_ev, dict):
        nat_in = swap_ev.get("nativeInput")
        if isinstance(nat_in, dict) and (not nat_in.get("account") or nat_in.get("account") == wallet):
            event_out = max(event_out, float(nat_in.get("amount") or 0.0) / LAMPORTS_PER_SOL)
        nat_out = swap_ev.get("nativeOutput")
        if isinstance(nat_out, dict) and (not nat_out.get("account") or nat_out.get("account") == wallet):
            event_in = max(event_in, float(nat_out.get("amount") or 0.0) / LAMPORTS_PER_SOL)

    sol_out = max(native_out, wsol_out, event_out)
    sol_in = max(native_in, wsol_in, event_in)
    return sol_in - sol_out


def _extract_primary_swap_for_wallet(
    tx: dict[str, Any], wallet: str
) -> Optional[tuple[str, str, float, float]]:
    """Return (side, mint, sol_amount, token_amount) for a wallet's SWAP tx, or None."""
    if str(tx.get("type") or "").upper() != "SWAP":
        return None
    net_by_mint: dict[str, float] = {}
    for tf in tx.get("tokenTransfers") or []:
        if not isinstance(tf, dict):
            continue
        mint = str(tf.get("mint") or "").strip()
        if not mint or mint in IGNORED_BASE_MINTS:
            continue
        amt = float(tf.get("tokenAmount") or 0.0)
        if amt <= 0:
            continue
        if tf.get("toUserAccount") == wallet:
            net_by_mint[mint] = net_by_mint.get(mint, 0.0) + amt
        if tf.get("fromUserAccount") == wallet:
            net_by_mint[mint] = net_by_mint.get(mint, 0.0) - amt

    if not net_by_mint:
        return None

    primary_mint, net_tokens = max(net_by_mint.items(), key=lambda kv: abs(kv[1]))
    if net_tokens == 0.0:
        return None

    side = "BUY" if net_tokens > 0 else "SELL"
    sol_delta = _extract_sol_delta_for_wallet(tx, wallet)
    sol_amount = max(0.0, -sol_delta) if side == "BUY" else max(0.0, sol_delta)
    return side, primary_mint, sol_amount, abs(net_tokens)


def score_wallet_history(
    wallet: str,
    txs: list[dict[str, Any]],
    *,
    liquid_sol_balance: float,
    now_ts: int,
    min_sleep_gap_hrs: int = 3,
    min_tx_7d: int = 25,
    max_tx_7d: int = 1500,
    min_sol_balance: float = 5.0,
    min_win_rate: float = 0.45,
    min_realized_sol_pnl: float = 3.0,
    max_single_mint_pnl_share: float = 0.85,
    min_winning_mints: int = 2,
    max_mint_creations: int = 10,
) -> WalletScore:
    """Apply Stage 2 human & edge filters to a candidate wallet's transaction history."""
    cutoff_7d = now_ts - (7 * 86400)
    timestamps_7d: list[int] = []
    buy_count = 0
    sell_count = 0
    mint_create_count = 0

    # Per-mint realized PnL tracking: mint -> {sol_in, tokens_bought, sol_out, tokens_sold}
    mint_ledger: dict[str, dict[str, float]] = {}

    for tx in txs:
        if not isinstance(tx, dict):
            continue
        tx_type = str(tx.get("type") or "").upper()
        if tx_type in TOKEN_CREATION_TX_TYPES:
            mint_create_count += 1

        raw_ts = tx.get("timestamp")
        ts = int(raw_ts) if isinstance(raw_ts, (int, float)) and raw_ts > 0 else 0
        if ts < cutoff_7d or ts > now_ts + 300:
            continue

        timestamps_7d.append(ts)
        swap = _extract_primary_swap_for_wallet(tx, wallet)
        if swap is None:
            continue
        side, mint, sol_amt, tok_amt = swap
        entry = mint_ledger.setdefault(
            mint,
            {"sol_in": 0.0, "tokens_bought": 0.0, "sol_out": 0.0, "tokens_sold": 0.0},
        )
        if side == "BUY":
            buy_count += 1
            entry["sol_in"] += sol_amt
            entry["tokens_bought"] += tok_amt
        else:
            sell_count += 1
            entry["sol_out"] += sol_amt
            entry["tokens_sold"] += tok_amt

    tx_count_7d = len(timestamps_7d)
    sleep_gap_hrs = max_consecutive_zero_hours(timestamps_7d)

    # Realized SOL PnL across closed / partially closed positions
    mint_pnls: dict[str, float] = {}
    for mint, rec in mint_ledger.items():
        if rec["tokens_bought"] > 0 and rec["tokens_sold"] > 0:
            sold_frac = min(1.0, rec["tokens_sold"] / rec["tokens_bought"])
            cost_basis = rec["sol_in"] * sold_frac
            mint_pnls[mint] = rec["sol_out"] - cost_basis

    closed_mints = len(mint_pnls)
    wins = sum(1 for pnl in mint_pnls.values() if pnl > 0)
    win_rate = (wins / closed_mints) if closed_mints > 0 else 0.0
    realized_sol_pnl = sum(mint_pnls.values())
    max_pos_mint_pnl = max((p for p in mint_pnls.values() if p > 0), default=0.0)
    total_pos_profit = sum(p for p in mint_pnls.values() if p > 0)
    max_single_share = 0.0
    if realized_sol_pnl > 0:
        share_vs_net = max_pos_mint_pnl / realized_sol_pnl
        share_vs_gross = (max_pos_mint_pnl / total_pos_profit) if total_pos_profit > 0 else 0.0
        max_single_share = max(share_vs_net, share_vs_gross)

    reasons: list[str] = []
    if sleep_gap_hrs < min_sleep_gap_hrs:
        reasons.append(f"NO_SLEEP_GAP ({sleep_gap_hrs}h < {min_sleep_gap_hrs}h)")
    if tx_count_7d < min_tx_7d:
        reasons.append(f"GHOST_WALLET ({tx_count_7d} < {min_tx_7d} txs/7d)")
    elif tx_count_7d > max_tx_7d:
        reasons.append(f"SPAM_BOT ({tx_count_7d} > {max_tx_7d} txs/7d)")
    if sell_count > 3 * buy_count:
        reasons.append(f"DEPLOYER_DUMPER (sells={sell_count} > 3*buys={3 * buy_count})")
    if mint_create_count > max_mint_creations:
        reasons.append(f"SERIAL_MINTER ({mint_create_count} > {max_mint_creations} creations)")
    if liquid_sol_balance < min_sol_balance:
        reasons.append(f"LOW_AMMO ({liquid_sol_balance:.2f} < {min_sol_balance:.1f} SOL)")
    if win_rate < min_win_rate:
        reasons.append(f"LOW_WIN_RATE ({win_rate:.2%} < {min_win_rate:.0%})")
    if realized_sol_pnl < min_realized_sol_pnl:
        reasons.append(f"LOW_REALIZED_PNL ({realized_sol_pnl:.2f} < {min_realized_sol_pnl:.1f} SOL)")
    if wins < min_winning_mints:
        reasons.append(f"ONE_HIT_WONDER (winning_mints={wins} < {min_winning_mints})")
    elif realized_sol_pnl > 0 and max_single_share > max_single_mint_pnl_share:
        reasons.append(f"ONE_HIT_WONDER (top_mint={max_single_share:.1%} > {max_single_mint_pnl_share:.0%})")

    passed = len(reasons) == 0
    rank_score = (win_rate * math.log1p(max(0.0, realized_sol_pnl))) if passed else 0.0

    return WalletScore(
        wallet=wallet,
        passed=passed,
        reject_reasons=reasons,
        sol_balance=round(liquid_sol_balance, 4),
        tx_count_7d=tx_count_7d,
        buy_count=buy_count,
        sell_count=sell_count,
        mint_create_count=mint_create_count,
        sleep_gap_hrs=sleep_gap_hrs,
        closed_mints=closed_mints,
        winning_mints=wins,
        win_rate=round(win_rate, 4),
        realized_sol_pnl=round(realized_sol_pnl, 4),
        max_single_mint_share=round(max_single_share, 4),
        rank_score=round(rank_score, 6),
    )


async def fetch_runner_pools(
    client: httpx.AsyncClient,
    config: AgentConfig,
    *,
    max_runners: int = 18,
    min_liquidity_usd: float = 25_000.0,
    min_volume_24h_usd: float = 100_000.0,
    min_mcap_usd: float = 100_000.0,
    max_mcap_usd: float = 50_000_000.0,
) -> list[RunnerPool]:
    """Stage 1a: Fetch Solana boosts, profiles, and SOL/pump search pools from DexScreener."""
    base = config.DEXSCREENER_BASE_URL.rstrip("/")
    mints: list[str] = []
    seen: set[str] = set()
    best_by_mint: dict[str, RunnerPool] = {}

    def _consider_pair(pair: dict[str, Any], *, min_liq: float, min_vol_24h: float) -> None:
        if str(pair.get("chainId") or "").lower() != "solana":
            return
        base_tok = pair.get("baseToken") or {}
        mint = str(base_tok.get("address") or "").strip()
        if not mint or mint in IGNORED_BASE_MINTS:
            return
        liq = float((pair.get("liquidity") or {}).get("usd") or 0.0)
        mcap = float(pair.get("marketCap") or pair.get("fdv") or 0.0)
        chg_24h = float((pair.get("priceChange") or {}).get("h24") or 0.0)
        vol_24h = float((pair.get("volume") or {}).get("h24") or 0.0)
        if liq < min_liq or vol_24h < min_vol_24h:
            return
        if not (min_mcap_usd <= mcap <= max_mcap_usd):
            return
        existing = best_by_mint.get(mint)
        if existing is None or liq > existing.liquidity_usd:
            best_by_mint[mint] = RunnerPool(
                mint=mint,
                symbol=str(base_tok.get("symbol") or "UNKNOWN").strip(),
                pair_address=str(pair.get("pairAddress") or "").strip(),
                liquidity_usd=liq,
                market_cap_usd=mcap,
                price_change_24h=chg_24h,
                volume_24h=vol_24h,
            )

    for path in ("/token-boosts/top/v1", "/token-boosts/latest/v1", "/token-profiles/latest/v1"):
        try:
            resp = await client.get(f"{base}{path}", timeout=12.0)
            resp.raise_for_status()
            for item in resp.json() if isinstance(resp.json(), list) else []:
                if isinstance(item, dict) and str(item.get("chainId") or "").lower() == "solana":
                    addr = str(item.get("tokenAddress") or "").strip()
                    if addr and addr not in seen and addr not in IGNORED_BASE_MINTS:
                        seen.add(addr)
                        mints.append(addr)
        except Exception as exc:
            logger.warning("[Discovery] DexScreener %s failed: %s", path, exc)

    for i in range(0, len(mints), 30):
        batch = mints[i : i + 30]
        try:
            resp = await client.get(f"{base}/tokens/v1/solana/{','.join(batch)}", timeout=15.0)
            resp.raise_for_status()
            pairs = resp.json()
            if isinstance(pairs, list):
                for p in pairs:
                    if isinstance(p, dict):
                        _consider_pair(p, min_liq=min_liquidity_usd, min_vol_24h=min_volume_24h_usd)
        except Exception as exc:
            logger.warning("[Discovery] DexScreener batch enrich failed: %s", exc)

    # Supplement beyond paid boosts with high-volume organic Solana & pump pools (green or red dips)
    for query in ("SOL", "pump"):
        try:
            s_resp = await client.get(f"{base}/latest/dex/search", params={"q": query}, timeout=12.0)
            s_resp.raise_for_status()
            s_pairs = (s_resp.json() or {}).get("pairs") if isinstance(s_resp.json(), dict) else []
            for p in s_pairs or []:
                if isinstance(p, dict):
                    _consider_pair(p, min_liq=min_liquidity_usd, min_vol_24h=min_volume_24h_usd)
        except Exception as exc:
            logger.warning("[Discovery] DexScreener search?q=%s failed: %s", query, exc)

    runners = sorted(
        best_by_mint.values(),
        key=lambda r: (r.volume_24h, r.liquidity_usd),
        reverse=True,
    )
    return runners[:max_runners]


async def extract_buyer_candidates(
    client: httpx.AsyncClient,
    runners: list[RunnerPool],
    *,
    api_key: str,
    helius_base_url: str,
    exclude_wallets: set[str],
    pacer: Optional[HeliusEnhancedPacer] = None,
    min_buy_sol: float = 1.0,
    pages_per_pool: int = 2,
    tx_limit_per_page: int = 100,
    max_candidates: int = 40,
) -> list[str]:
    """Stage 1b: Query Helius SWAPs (up to 2 pages/pool) and extract >=1.0 SOL buyers (native+WSOL)."""
    candidates: list[str] = []
    seen = set(exclude_wallets) | KNOWN_PROGRAM_ADDRESSES
    base = helius_base_url.rstrip("/")
    req_pacer = pacer or HeliusEnhancedPacer()

    for runner in runners:
        if len(candidates) >= max_candidates:
            break
        target_addr = runner.pair_address or runner.mint
        url = f"{base}/addresses/{target_addr}/transactions"
        before_sig: Optional[str] = None
        for _ in range(pages_per_pool):
            if len(candidates) >= max_candidates:
                break
            params: dict[str, Any] = {"api-key": api_key, "type": "SWAP", "limit": tx_limit_per_page}
            if before_sig:
                params["before"] = before_sig
            try:
                resp = await req_pacer.request(client, "GET", url, params=params, timeout=15.0)
                txs = resp.json()
                if not isinstance(txs, list) or not txs:
                    break
            except Exception as exc:
                logger.warning("[Discovery] Helius tx lookup failed for %s: %s", target_addr[:8], exc)
                break

            for tx in txs:
                if not isinstance(tx, dict):
                    continue
                wallet = str(tx.get("feePayer") or "").strip()
                if not wallet or wallet in seen:
                    continue
                swap = _extract_primary_swap_for_wallet(tx, wallet)
                if swap is not None and swap[0] == "BUY" and swap[2] >= min_buy_sol:
                    seen.add(wallet)
                    candidates.append(wallet)
                    if len(candidates) >= max_candidates:
                        break

            last_sig = str(txs[-1].get("signature") or "").strip() if isinstance(txs[-1], dict) else ""
            if len(txs) < tx_limit_per_page or not last_sig:
                break
            before_sig = last_sig

    return candidates


async def fetch_wallet_transactions_7d(
    client: httpx.AsyncClient,
    wallet: str,
    *,
    api_key: str,
    helius_base_url: str,
    now_ts: int,
    pacer: Optional[HeliusEnhancedPacer] = None,
    max_pages: int = 5,
    page_limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch a wallet's recent 7-day SWAP history from Helius Enhanced Transactions API (capped at 500)."""
    cutoff_7d = now_ts - (7 * 86400)
    base = helius_base_url.rstrip("/")
    url = f"{base}/addresses/{wallet}/transactions"
    all_txs: list[dict[str, Any]] = []
    before_sig: Optional[str] = None
    req_pacer = pacer or HeliusEnhancedPacer()

    for _ in range(max_pages):
        params: dict[str, Any] = {"api-key": api_key, "type": "SWAP", "limit": page_limit}
        if before_sig:
            params["before"] = before_sig
        resp = await req_pacer.request(client, "GET", url, params=params, timeout=15.0)
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break

        all_txs.extend(t for t in batch if isinstance(t, dict))
        oldest_ts = min(
            (int(t["timestamp"]) for t in batch if isinstance(t, dict) and isinstance(t.get("timestamp"), (int, float))),
            default=0,
        )
        last_sig = str(batch[-1].get("signature") or "").strip() if isinstance(batch[-1], dict) else ""
        if (oldest_ts and oldest_ts < cutoff_7d) or len(batch) < page_limit or not last_sig:
            break
        before_sig = last_sig

    return all_txs


async def fetch_wallet_sol_balance(
    client: httpx.AsyncClient,
    wallet: str,
    *,
    api_key: str,
    rpc_url: str = "https://mainnet.helius-rpc.com",
) -> float:
    """Fetch liquid SOL balance via Helius JSON-RPC getBalance (10 req/sec RPC tier)."""
    resp = await client.post(
        rpc_url,
        params={"api-key": api_key},
        json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]},
        timeout=10.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    lamports = float(((payload.get("result") or {}).get("value")) or 0.0)
    return lamports / LAMPORTS_PER_SOL


async def is_wallet_stale(
    client: httpx.AsyncClient,
    wallet: str,
    *,
    api_key: str,
    helius_base_url: str,
    now_ts: int,
    pacer: Optional[HeliusEnhancedPacer] = None,
    stale_hours: float = 72.0,
) -> bool:
    """Return True if wallet has had no transactions in the last `stale_hours` hours."""
    url = f"{helius_base_url.rstrip('/')}/addresses/{wallet}/transactions"
    req_pacer = pacer or HeliusEnhancedPacer()
    resp = await req_pacer.request(
        client, "GET", url, params={"api-key": api_key, "limit": 1}, timeout=10.0
    )
    batch = resp.json()
    if not isinstance(batch, list) or not batch:
        return True
    last_ts = batch[0].get("timestamp") if isinstance(batch[0], dict) else None
    if not isinstance(last_ts, (int, float)) or last_ts <= 0:
        return True
    return (now_ts - float(last_ts)) > (stale_hours * 3600.0)
