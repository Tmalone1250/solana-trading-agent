# Memory Bounding, DuckDB Cap & HTTP/Cache Hardening Plan (Raspberry Pi 4 2GB)

## 1. Cap DuckDB Memory & Prevent Leaked Connections (`db.py`, `memory_guard.py`, `telemetry_api.py`)
- Immediately on opening any DuckDB connection in `AgentDatabase.__init__`, execute:
  `SET memory_limit='256MB'; SET threads=2; SET preserve_insertion_order=false;`
- Remove the eager module-level `app = create_telemetry_app()` instantiation in `telemetry_api.py` (replace with lazy module `__getattr__`) so importing `create_telemetry_app` in `main.py` or tests never opens a second leaked DuckDB connection.
- Add SQL-level cutoff filtering (`WHERE timestamp_utc >= ?`) and periodic table pruning (`prune_bounded_tables`) for `cooldowns`, `whale_events`, `whale_signals`, `whale_decisions`, `decisions_log`, `social_signals`, and `x_seen_tweets`.

## 2. Bound All In-Memory Caches & Buffers (`ohlcv_client.py`, `jupiter_token_client.py`, `safety_gate_node.py`, `helius_webhook.py`, `batched_price_monitor.py`)
- **OHLCV Cache (`ohlcv_client.py`):** Evict entries older than TTL (`600s`) and cap `_ohlcv_cache` at `MAX_OHLCV_CACHE_ENTRIES = 64` (evicting oldest entries first).
- **Jupiter Token v2 Cache (`jupiter_token_client.py`):** Cap `_mint_cache` at `MAX_MINT_CACHE_ENTRIES = 256` (TTL `300s`) and `_discovery_cache["tokens"]` at `100` entries.
- **SafetyGate Cache (`safety_gate_node.py`):** Add bounded `_safety_cache` (`MAX_SAFETY_CACHE_ENTRIES = 128`, TTL `300s`) so repeated safety checks reuse recent verdicts without unbounded growth.
- **Helius Webhook & Price Backoff (`helius_webhook.py`, `batched_price_monitor.py`):** Bound `_recent_webhook_sigs` ring buffer (`MAX_RECENT_WEBHOOK_SIGS = 500`, TTL `1800s`), cap `_helius_roster_cache["wallets"]` (`100`), and cap `_price_backoff` (`32`).

## 3. Reuse HTTP Clients & 60s `[WhaleRoster]` Failure Backoff (`helius_webhook.py`, `telemetry_api.py`, `main.py`, `x_discovery_worker.py`)
- Add a 60-second failure backoff (`HELIUS_ROSTER_ERROR_BACKOFF_SECONDS = 60.0`) to `fetch_helius_tracked_wallets()` in `helius_webhook.py` so network blips do not trigger retry storms across `/api/telemetry` or webhook hits.
- Share a single bounded `httpx.AsyncClient` across `main.py`, `telemetry_api.py`, and `x_discovery_worker.py`, closing it cleanly on shutdown.

## 4. Periodic RSS Memory Telemetry (`memory_guard.py`, `main.py`)
- Log process RSS memory (`[Memory] RSS=... MB`) on startup and every 10 minutes (`600s`) in `main.py` alongside cache and DuckDB pruning so memory stability is tracked in `journalctl`.

## 5. Verification
- Add unit tests in `tests/test_memory_bounds_and_backoff.py` verifying DuckDB `memory_limit`/`threads`/`preserve_insertion_order` settings, bounded cache eviction (OHLCV, Jupiter, SafetyGate, webhook signatures), 60s `[WhaleRoster]` error backoff, and RSS memory logging.
- Run `./venv/bin/pytest -v`.
