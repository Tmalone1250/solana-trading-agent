"""Rate limiter and 429 exponential-backoff helper for Helius Enhanced API (v0)."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger(__name__)


class HeliusEnhancedPacer:
    """Rate-limits api.helius.xyz/v0 calls (>=0.6s spacing) with jittered 429 exponential backoff."""

    def __init__(
        self,
        *,
        min_interval: float = 0.6,
        base_backoff: float = 1.0,
        max_retries: int = 4,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_interval = min_interval
        self.base_backoff = base_backoff
        self.max_retries = max_retries
        self._sleep = sleep_fn
        self._clock = clock_fn
        self._last_call_at: Optional[float] = None

    async def request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            if self._last_call_at is not None and self.min_interval > 0:
                wait = self.min_interval - (self._clock() - self._last_call_at)
                if wait > 0:
                    await self._sleep(wait)
            self._last_call_at = self._clock()
            resp = await client.request(method, url, **kwargs)
            if resp.status_code != 429 or attempt >= self.max_retries:
                resp.raise_for_status()
                return resp
            delay = self.base_backoff * (2**attempt) * random.uniform(0.75, 1.25)
            logger.warning(
                "[HeliusPacer] HTTP 429 on %s %s (attempt %d/%d) — retrying in %.2fs",
                method,
                url.split("?")[0],
                attempt + 1,
                self.max_retries,
                delay,
            )
            await self._sleep(delay)
        raise RuntimeError("Unreachable retry loop exit")
