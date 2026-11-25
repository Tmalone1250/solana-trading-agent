# Stage 4 Plan — Soak-Test Readiness Verification Suite

## Target File (< 500 lines)
- `tests/test_soak_readiness.py`:
  1. `test_concurrent_balance_pnl_and_slot_reservation_gather`: Interleaved Chart BUY, Whale BUY, and Chart/Whale HOLD monitor ticks under `asyncio.gather`, verifying exact SOL balance, `daily_pnl_sol`, lane/global slot reservation caps, and no double-spend.
  2. `test_whale_multi_slot_preservation_mid_tick_shadow_exit_and_webhook_1_of_3`: 3 open Whale slots where Slot 0 exits while `flag_whale_shadow_exit` fires mid-tick on Slot 1; verifies Slots 1 & 2 survive, Slot 1 retains `whale_shadow_exit_flag=True` monotonically, and `helius_webhook.py` does not mark `SKIPPED / POSITION_OPEN` when 1/3 Whale slots are open.
  3. `test_chart_slots_2_and_3_full_exit_engine_and_sequential_priority`: 3 open Chart slots where Slot 2 hits Partial TP (+18%) and Slot 3 hits Hard Stop (-12%) on the same tick; verifies Hard Stop executes before Partial TP sequentially by `(mint, opened_at)` while Slot 1 remains open.
  4. `test_sell_and_partial_sell_idempotency_under_concurrency`: Concurrent `PARTIAL_SELL` calls and concurrent `SELL` calls under `asyncio.gather` execute and settle exactly once.
  5. `test_watching_reentry_abandon_preserves_open_chart_positions`: Abandoning `WATCHING_REENTRY` with another open Chart position preserves the open position in DuckDB and stays `IN_POSITION`.
  6. `test_signal_and_telemetry_completeness_mc_usd_exact_quote_and_live_momentum_cache`: Verifies non-null `mc_usd` on Chart `BUY` and `LEG_2_REENTRY`, `price_source="jupiter_exact_quote"` when `used_exact_quote=True`, and cached `WATCH` narrative transitioning to live `BUY` breakout on Tick 2 with zero extra Gemini calls.
