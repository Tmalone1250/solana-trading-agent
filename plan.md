# On-Demand Insider Wallet Scanner Plan

## 1. Backend: `insider_scanner.py` & `POST /api/insider-scan` (`insider_scanner.py`, `telemetry_api.py`)
- Create standalone on-demand module `insider_scanner.py` (zero background tasks or memory overhead on the trading loop):
  - **Step 1 — Pool & Symbol Resolution**: Query DexScreener (`GET /tokens/v1/solana/{mint}`) to resolve the primary Solana liquidity pool (`pairAddress`), `baseToken.address`, and `baseToken.symbol` (with fallback if a pool address was pasted directly).
  - **Step 2 — Breakout Candle Auto-Detection**: When `breakout_ts` is omitted, fetch 5m OHLCV bars from GeckoTerminal (`/networks/solana/pools/{pool_address}/ohlcv/minute?aggregate=5&limit=48`) and select the timestamp of the highest-volume green candle.
  - **Step 3 — Helius Enhanced Transactions Pagination (Capped at 5 Pages)**: Query `GET https://api.helius.xyz/v0/addresses/{pool_address}/transactions?api-key={HELIUS_API_KEY}&type=SWAP&gte-time={start_ts}&lte-time={end_ts}&sort-order=asc` across `[breakout_ts - lookback_mins*60, breakout_ts + 120]`, paginating up to a hard cap of `5` pages.
  - **Step 4 & 5 — BUY Swap Extraction & Wallet Aggregation**:
    - Extract all `BUY` swaps acquiring `mint` where the wallet spent `>= min_sol` SOL.
    - Aggregate by `feePayer` wallet: `wallet`, `total_sol_spent`, `buy_count`, `first_buy_utc`, `mins_before_breakout`, `already_in_roster` (cross-checked against `fetch_helius_tracked_wallets`), `gmgn_url`, and `solscan_url`.
  - **Step 6 — Ranking**: Sort by earliest `first_buy_utc` (tie-breaker: highest `total_sol_spent`) and return top 25 wallets with `symbol`, `pool_address`, and `breakout_ts`.
- Expose `POST /api/insider-scan` in `telemetry_api.py`.

## 2. Frontend: "Scan Insiders" Button & Modal (`InsiderScannerModal.tsx`, `WhaleListCard.tsx`, `types.ts`)
- Add a compact **"Scan Insiders"** button in the Whale Radar header next to `Sync Helius ↻`.
- Create `InsiderScannerModal.tsx` with:
  - **Token CA** input (required), **Breakout Time** input (optional, placeholder `Auto-detect volume spike`), **Lookback (mins)** (`30`), **Min SOL** (`1.0`), and **Run Scan** button.
  - **"Copy All New Addresses"** button (newline-separated wallets where `!already_in_roster`).
  - Results table showing truncated wallet + 1-click Copy, `SOL Spent`, `Timing` (`12.4m before`), `Buys`, `IN ROSTER` badge, and quick links to **GMGN** and **Solscan**.

## 3. Verification
- Add unit tests in `tests/test_insider_scanner.py` covering:
  1. Auto-detecting `breakout_ts` from GeckoTerminal 5m OHLCV.
  2. Aggregating multiple buys per wallet and filtering `< min_sol`.
  3. Flagging `already_in_roster` wallets.
  4. Enforcing the 5-page Helius pagination cap.
- Run `./venv/bin/pytest -v` and `cd dashboard && npm run build`.
