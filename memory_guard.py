"""Memory bounding, DuckDB resource capping, cache eviction, and RSS telemetry helpers."""

from __future__ import annotations

import logging
import os
import resource
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("solana-meme-agent")

DUCKDB_MEMORY_LIMIT = "256MB"
DUCKDB_THREADS = 2
MEMORY_LOG_INTERVAL_SECONDS = 600.0

_last_memory_log_ts: float = 0.0


def reset_memory_log_timer() -> None:
    """Reset the periodic RSS memory log timer (useful for tests)."""
    global _last_memory_log_ts
    _last_memory_log_ts = 0.0


def configure_duckdb_connection(conn: Any) -> None:
    """Apply strict Raspberry Pi 4 (2GB RAM) resource limits to a DuckDB connection."""
    conn.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}';")
    conn.execute(f"SET threads={DUCKDB_THREADS};")
    conn.execute("SET preserve_insertion_order=false;")


def prune_timestamped_cache(
    cache: dict[str, dict[str, Any]],
    *,
    now_ts: Optional[float] = None,
    max_entries: int = 128,
    ttl_seconds: float = 300.0,
    ts_key: str = "fetched_at",
) -> int:
    """Evict expired entries (> ttl_seconds) and oldest entries exceeding max_entries."""
    if not cache:
        return 0
    current_ts = now_ts if now_ts is not None else time.time()
    removed = 0

    if ttl_seconds > 0:
        expired_keys = [
            k
            for k, v in list(cache.items())
            if isinstance(v, dict)
            and v.get(ts_key) is not None
            and (current_ts - float(v.get(ts_key) or 0.0)) > ttl_seconds
            and current_ts >= float(v.get("backoff_until") or 0.0)
        ]
        for k in expired_keys:
            cache.pop(k, None)
            removed += 1

    if max_entries > 0 and len(cache) > max_entries:
        excess = len(cache) - max_entries
        ordered_keys = sorted(
            cache.keys(),
            key=lambda k: float((cache.get(k) or {}).get(ts_key) or 0.0)
            if isinstance(cache.get(k), dict)
            else 0.0,
        )
        for k in ordered_keys[:excess]:
            cache.pop(k, None)
            removed += 1

    return removed


def prune_duckdb_tables(
    conn: Any,
    lock: threading.RLock,
    *,
    now_dt: Optional[datetime] = None,
    max_whale_events: int = 2000,
    max_decisions_log: int = 1000,
    max_action_log: int = 20000,
    max_seen_tweets: int = 2000,
) -> None:
    """Prune expired cooldowns and bound high-frequency DuckDB tables so storage/RAM stay flat."""
    dt = now_dt or datetime.now(timezone.utc)
    now_iso = dt.isoformat()
    cutoff_48h = (dt - timedelta(hours=48)).isoformat()
    with lock:
        conn.execute("DELETE FROM cooldowns WHERE expires_at <= ?", [now_iso])
        conn.execute("DELETE FROM whale_events WHERE timestamp_utc < ?", [cutoff_48h])
        conn.execute(
            """
            DELETE FROM whale_events
            WHERE signature NOT IN (
                SELECT signature FROM whale_events ORDER BY timestamp_utc DESC LIMIT ?
            )
            """,
            [int(max_whale_events)],
        )
        conn.execute("DELETE FROM whale_signals WHERE last_seen_utc < ?", [cutoff_48h])
        conn.execute("DELETE FROM whale_decisions WHERE updated_at < ?", [cutoff_48h])
        conn.execute("DELETE FROM social_signals WHERE last_seen_utc < ?", [cutoff_48h])
        conn.execute(
            """
            DELETE FROM x_seen_tweets
            WHERE tweet_id NOT IN (
                SELECT tweet_id FROM x_seen_tweets ORDER BY fetched_at_utc DESC LIMIT ?
            )
            """,
            [int(max_seen_tweets)],
        )
        conn.execute(
            """
            DELETE FROM decisions_log
            WHERE id NOT IN (
                SELECT id FROM decisions_log ORDER BY id DESC LIMIT ?
            )
            """,
            [int(max_decisions_log)],
        )
        conn.execute(
            """
            DELETE FROM action_log
            WHERE id NOT IN (
                SELECT id FROM action_log ORDER BY id DESC LIMIT ?
            )
            """,
            [int(max_action_log)],
        )


def get_process_rss_mb() -> float:
    """Return current process Resident Set Size (RSS) in megabytes."""
    try:
        if os.path.exists("/proc/self/status"):
            with open("/proc/self/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            return round(float(parts[1]) / 1024.0, 2)
    except Exception:
        pass
    usage_kb = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return round(usage_kb / 1024.0, 2)


def maybe_log_rss_memory(
    *,
    db: Any = None,
    interval_seconds: float = MEMORY_LOG_INTERVAL_SECONDS,
    force: bool = False,
    now_ts: Optional[float] = None,
) -> Optional[float]:
    """Periodically log process RSS memory and prune bounded DuckDB tables every 10 minutes."""
    global _last_memory_log_ts
    current_ts = now_ts if now_ts is not None else time.time()
    if not force and _last_memory_log_ts > 0 and (current_ts - _last_memory_log_ts) < interval_seconds:
        return None

    _last_memory_log_ts = current_ts
    if db is not None and hasattr(db, "prune_bounded_tables"):
        try:
            db.prune_bounded_tables()
        except Exception as exc:
            logger.debug("[Memory] Table prune skipped: %s", exc)

    rss_mb = get_process_rss_mb()
    logger.info(
        "[Memory] RSS=%.2f MB | duckdb_limit=%s threads=%d",
        rss_mb,
        DUCKDB_MEMORY_LIMIT,
        DUCKDB_THREADS,
    )
    return rss_mb
