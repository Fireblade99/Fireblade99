"""Per-client request rate limiter (sliding window, per process).

Starts per hour and concurrency limits are checked against the database in executions.py,
so they hold across processes; this one only protects the gateway itself from request floods.
"""

import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    def __init__(self, window_seconds: float = 60.0):
        self.window = window_seconds
        self._hits: dict[int, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: int, limit: int) -> bool:
        """Register a request; return False if the limit is exceeded."""
        if limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = SlidingWindowLimiter()
