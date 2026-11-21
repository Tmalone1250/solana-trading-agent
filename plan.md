# Full-Board CSV Export & Telemetry Ledger Plan

## Objective
Upgrade `solana_actions.csv` from a 14-column `BUY`/`SELL`/`EVICT` log to a complete, structured full-board audit trail that captures every dashboard signal, evaluation, whale event, and roster sync, while ensuring `SELL` rows always inherit their entry `strategy`.

## 1. Enriched Action Log Schema (`action_export.py` & `db.py`)
Extend `ACTION_LOG_COLUMNS` from 14 to 30 columns (with automatic `ALTER TABLE action_log ADD COLUMN` migration in `db.py` for existing DuckDB databases):
- **Existing 14 columns:** `timestamp_utc`, `bot`, `mode`, `action`, `symbol`, `asset_id`, `qty`, `notional`, `unit`, `price_or_value`, `pnl_amount`, `pnl_pct`, `strategy`, `reason`
- **New 16 columns:** `sol_equity`, `daily_realized_sol`, `circuit_breaker`, `mc_usd`, `liq_usd`, `soak_minutes`, `rsi`, `leg2_score`, `narrative_score`, `momentum_score`, `vamp_score`, `buy_confidence`, `peak_pnl_pct`, `hold_seconds`, `triggering_whales`, `whale_sol_flow`

`db.log_action()` auto-fills `sol_equity`, `daily_realized_sol`, and `circuit_breaker` from `agent_state` when not explicitly supplied so every row carries live portfolio context.

## 2. Full-Board Event Logging
Log all major dashboard events into DuckDB `action_log`:
- `BUY`, `SELL`, `REENTRY_BUY` (`executor_node.py`): include equity, daily realized SOL, liquidity, `peak_pnl_pct`, `hold_seconds`, and `triggering_whales`.
- `DECISION_1` (`alpha_node.py`): log every Decision 1 evaluation (`WATCH` / `APPROVE` / `REJECT` / `BLOCKED`) with `mc_usd`, `liq_usd`, `soak_minutes`, `rsi`, `leg2_score`, `narrative_score`, `momentum_score`, `vamp_score`, and `buy_confidence`.
- `EVICT` (`safety_gate_node.py`, `alpha_node.py`): include candidate metrics alongside eviction reason.
- `WHALE_WEBHOOK` (`helius_webhook.py`): log each recorded smart-money BUY/SELL swap with wallet label in `triggering_whales` and signed `whale_sol_flow`.
- `WHALE_CONFLUENCE` (`safety_gate_node.py`): log `WHALE_SCALP` fast-track triggers with triggering wallets, total SOL flow, MCAP, and liquidity.
- `WHALE_DISCOVERY` (`telemetry_api.py`): log Helius roster sync events (`POST /api/webhooks/helius/sync`) with synced wallet count.
- `CIRCUIT_BREAKER` (`scanner_node.py`): daily max-loss halt events.

## 3. Guaranteed `strategy` Inheritance on `SELL` Rows
- `position_monitor_node.py`: explicitly pass `strategy=pos.strategy or "SCALPER"` on all standard exit `PendingOrder`s (Hard Stop, Exodus, Trailing TP, Breakeven Shield).
- `executor_node.py`: resolve `SELL` strategy via `order.strategy` (if non-default) → `active_position.strategy` → latest `BUY` strategy for that mint in DuckDB → `"SCALPER"`, guaranteeing `strategy` is never null/empty.

## 4. Export Endpoints & Query Filtering (`telemetry_api.py`)
- Expose `GET /api/export/csv` (and preserve `GET /api/export/actions.csv` as an alias) returning the full board by default.
- Support `?type=trades|whales|watchlist|all` (default `all`):
  - `trades`: `BUY`, `SELL`, `REENTRY_BUY`
  - `whales`: `WHALE_WEBHOOK`, `WHALE_CONFLUENCE`, `WHALE_DISCOVERY`
  - `watchlist`: `DECISION_1`, `EVICT`, `CIRCUIT_BREAKER`
  - `all`: all action types
- Preserve backward-compatible `?actions=...` and `?limit=...` parameters.

## 5. Verification
- Expand `tests/test_action_ledger.py` to test all 30 columns, all board event types, `SELL` strategy inheritance, and `?type=trades|whales|watchlist|all` on `/api/export/csv` and `/api/export/actions.csv`.
- Run `./venv/bin/pytest -v`.
