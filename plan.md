# Directive 03 Plan — Whale Control Separation & Early Re-entry Abandonment

1. `telemetry_api.py`: Gate `POST /api/webhooks/helius` and `POST /api/webhooks/helius/sync` strictly on `ENABLE_HELIUS_WHALE_WEBHOOK` (never `WHALE_STRATEGY_ENABLED`), preserving auth when enabled.
2. `scanner_node.py` & `alpha_node.py`: Gate active Whale signal consumption strictly on `WHALE_STRATEGY_ENABLED` (never `ENABLE_HELIUS_WHALE_WEBHOOK`).
3. `position_monitor_node.py`: Abandon `WATCHING_REENTRY` early when `LEG2_ENTRY` is disabled—before market-data/Jupiter quote calls for the watcher—while preserving normal monitoring/exits for any open token-holding Chart positions.
4. `tests/test_whale_control_separation.py`: Add regression tests for all 4 `(WHALE_STRATEGY_ENABLED, ENABLE_HELIUS_WHALE_WEBHOOK)` combinations plus zero-HTTP early re-entry abandonment and coexisting open-position exit preservation.
