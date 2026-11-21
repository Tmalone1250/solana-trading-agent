# Recent Closed Trades Pagination & Win/Loss Summary Plan

## 1. Backend (`db.py`, `telemetry_api.py`)
- Add `AgentDatabase.get_closed_trades_summary()` computing directly from DuckDB `trades` (`side = 'SELL'`):
  - `total_trades` (`int`)
  - `wins` (`int`, `pnl_sol > 0`)
  - `losses` (`int`, `pnl_sol <= 0`)
  - `win_rate_pct` (`float` rounded to 1 decimal, `0.0` when `total_trades == 0`)
  - `net_pnl_sol` (`float`)
- Include `closed_trades_summary` in `GET /api/telemetry` and raise `recent_trades` limit from `15` to `100` (newest-first) for zero-latency client-side pagination over Cloudflare Tunnel.

## 2. Frontend (`dashboard/`)
- Add `ClosedTradesSummary` interface to `dashboard/src/lib/types.ts` and pass `telemetry?.closed_trades_summary` from `DashboardClient.tsx` into `RecentTradesTable.tsx`.
- In `RecentTradesTable.tsx`:
  - Show live Win/Loss + Win % badge (`9W / 12L • 42.9% WR`) in the card header next to the trade count pill, color-coded emerald (`>= 50%`), amber (`40%–49.9%`), or rose (`< 40%`).
  - Paginate at `PAGE_SIZE = 7` with compact `Prev` / `Next` buttons and `Page X of Y` footer, preserving the current page across live polling updates and safely clamping if total pages shrink.

## 3. Verification
- Add unit tests covering `closed_trades_summary` (empty 0 trades, all wins, breakeven `0.0` PnL, mixed wins/losses, and 100-trade limit on `/api/telemetry`).
- Run `./venv/bin/pytest -v` and `cd dashboard && npm run build`.
