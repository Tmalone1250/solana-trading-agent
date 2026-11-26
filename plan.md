# Phase 1 Plan — BREAKOUT_IGNITION-Only Chart Isolation (Directive 02)

1. `config.py` + `.env.example`: Add `VALID_CHART_STRATEGIES`, `CHART_ENABLED_STRATEGIES` (default all 4 Chart strategies for backward compatibility), validation rejecting unknown names, `chart_enabled_strategies()`, and `is_chart_strategy_enabled()`.
2. `scalper_entry_engine.py`: Gate `SWEEP_RECLAIM`, `DISPLACEMENT_RETEST`, and `BREAKOUT_IGNITION` in `evaluate_scalper_entry` on `config.chart_enabled_strategies()` so disabled setups cannot fire or shadow `BREAKOUT_IGNITION`.
3. `alpha_helpers.py` + `alpha_node.py`: Gate `leg2_fast_track`, cached momentum `BUY` promotion, and final `approved`/order strategy on `config.chart_enabled_strategies()`.
4. `chart_exit_engine.py` + `position_monitor_node.py`: Prevent `TRAILING_TP` from entering `WATCHING_REENTRY` and abandon any existing `WATCHING_REENTRY` without emitting `LEG_2_REENTRY` when `LEG2_ENTRY` is disabled.
5. `executor_node.py`: Add final Chart `BUY` authorization check against `config.is_chart_strategy_enabled(order.strategy)` and Whale `BUY` check against `config.WHALE_STRATEGY_ENABLED` (never blocking `SELL` / `PARTIAL_SELL`).
6. `telemetry_api.py`, `scanner_node.py`, `alpha_node.py`: Block Helius webhook ingestion and skip `active_whales` discovery/prompt injection when both `WHALE_STRATEGY_ENABLED` and `ENABLE_HELIUS_WHALE_WEBHOOK` are `False`.
7. `tests/test_breakout_isolation.py`: Cover all categories A–F and run full `./venv/bin/pytest -v`.
