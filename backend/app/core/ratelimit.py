"""Rate limiting foundation (Brief §25).

Phase 2 ships a process-local sliding-window limiter used to protect the login
endpoint. It is honest about its limits: with multiple API workers a distributed
limiter (Redis-backed, per architecture doc 23) will replace this — the interface
below is designed so that swap is drop-in.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowRateLimiter:
    def __init__(self, max_events: int, per_seconds: float = 60.0) -> None:
        self.max_events = max_events
        self.per_seconds = per_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        """Record an event for `key`; return True if allowed, False if limited."""
        now = time.monotonic()
        cutoff = now - self.per_seconds
        with self._lock:
            window = self._events[key]
            while window and window[0] < cutoff:
                window.popleft()
            if len(window) >= self.max_events:
                return False
            window.append(now)
            return True

    def retry_after_seconds(self, key: str) -> float:
        with self._lock:
            window = self._events.get(key)
            if not window:
                return 0.0
            return max(0.0, self.per_seconds - (time.monotonic() - window[0]))

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
