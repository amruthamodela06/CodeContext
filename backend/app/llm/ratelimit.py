"""Per-process request-rate limiter for the Gemini free tier (ADR 0010).

A monotonic sliding window: `acquire()` blocks until fewer than `rpm` requests
were started in the trailing 60s, then records the new request. Single-process
only (matches the dev / portfolio deployment) — its job is to stop a dev hot-loop
from exhausting the free-tier quota, not to coordinate across replicas.
"""

import asyncio
import time
from collections import deque


class RateLimiter:
    def __init__(self, rpm: int, *, window: float = 60.0) -> None:
        self._rpm = rpm
        self._window = window
        self._events: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self._rpm <= 0:  # 0 / negative disables the limiter
            return
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._events and now - self._events[0] >= self._window:
                    self._events.popleft()
                if len(self._events) < self._rpm:
                    self._events.append(now)
                    return
                await asyncio.sleep(self._window - (now - self._events[0]))
