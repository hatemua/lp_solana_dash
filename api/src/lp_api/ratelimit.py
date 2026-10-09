"""Fixed-window rate limit per client IP, stored in Redis (shared by all API workers)."""

import time
from typing import Any


class RateLimiter:
    def __init__(self, redis: Any, per_minute: int, prefix: str = "rl") -> None:
        self.redis = redis
        self.per_minute = per_minute
        self.prefix = prefix

    async def hit(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """Count one request; returns (allowed, seconds until the window resets)."""
        now = time.time() if now is None else now
        window = int(now // 60)
        rkey = f"{self.prefix}:{key}:{window}"
        try:
            pipe = self.redis.pipeline(transaction=True)
            pipe.incr(rkey)
            pipe.expire(rkey, 70)
            count, _ = await pipe.execute()
        except Exception:
            return True, 0                      # Redis down: do not block reads
        retry = 60 - int(now % 60)
        return int(count) <= self.per_minute, retry
