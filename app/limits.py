"""Per-client rate limiting for a publicly reachable demo."""

from __future__ import annotations

from collections import OrderedDict
import threading
import time


class RateLimiter:
    """Token bucket per client: ``per_minute`` requests, refilled continuously.

    A client may burst up to ``per_minute`` requests, then gets one more every
    60 / per_minute seconds. Buckets live in process memory; with one API
    process that is exact, and the oldest idle clients are dropped past
    ``max_clients`` so the table cannot grow without bound.
    """

    def __init__(self, per_minute: int, max_clients: int = 10_000, clock=time.monotonic):
        if per_minute < 1:
            raise ValueError("per_minute must be at least 1")
        self.capacity = float(per_minute)
        self.refill_per_second = per_minute / 60.0
        self.max_clients = max_clients
        self.clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def acquire(self, client: str) -> float:
        """Take one token. Returns 0 when allowed, else seconds until the next token."""
        now = self.clock()
        with self._lock:
            tokens, updated = self._buckets.pop(client, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - updated) * self.refill_per_second)
            if tokens >= 1:
                tokens -= 1
                wait = 0.0
            else:
                wait = (1 - tokens) / self.refill_per_second
            self._buckets[client] = (tokens, now)
            while len(self._buckets) > self.max_clients:
                self._buckets.popitem(last=False)
            return wait
