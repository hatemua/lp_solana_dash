"""HTTP client with a per-upstream rate limit and backoff on 429 / 5xx / timeouts. Never raises on API errors."""

import asyncio
import logging
import random
import time
from collections import Counter
from typing import Any

import httpx

log = logging.getLogger(__name__)

UA = "lp-solana-dash-indexer/0.1 (+https://lp.joulity.com)"
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 522, 524}


class RateLimiter:
    """Spaces requests evenly: at most `per_minute` requests per minute."""

    def __init__(self, per_minute: int) -> None:
        self.interval = 60.0 / max(per_minute, 1)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            await asyncio.sleep(delay)

    def pause(self, seconds: float) -> None:
        """Push the next slot back (e.g. after a 429)."""
        self._next = max(self._next, time.monotonic() + seconds)


class Upstream:
    def __init__(self, name: str, base_url: str, per_minute: int, client: httpx.AsyncClient,
                 retries: int = 4, backoff_s: float = 1.0) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.limiter = RateLimiter(per_minute)
        self.client = client
        self.retries = retries
        self.backoff_s = backoff_s
        self.stats: Counter[str] = Counter()

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any | None:
        """GET base_url + path. Returns parsed JSON, or None after the retries are used up."""
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        for attempt in range(self.retries + 1):
            await self.limiter.wait()
            delay = self.backoff_s * (2**attempt) + random.random() * 0.5
            try:
                r = await self.client.get(url, params=params, headers={"User-Agent": UA, "Accept": "application/json"})
                if r.status_code == 200:
                    self.stats["ok"] += 1
                    return r.json()
                self.stats[str(r.status_code)] += 1
                if r.status_code not in RETRY_STATUS:
                    log.warning("%s %s -> HTTP %s", self.name, url, r.status_code)
                    return None
                retry_after = r.headers.get("retry-after")
                if retry_after and retry_after.replace(".", "", 1).isdigit():
                    delay = max(delay, float(retry_after))
                if r.status_code == 429:
                    self.limiter.pause(delay)
                    log.warning("%s rate-limited (429), backing off %.1fs", self.name, delay)
            except (httpx.HTTPError, ValueError) as e:
                self.stats["error"] += 1
                log.debug("%s %s failed: %s", self.name, url, e)
            if attempt < self.retries:
                await asyncio.sleep(min(delay, 60))
        self.stats["gave_up"] += 1
        return None
