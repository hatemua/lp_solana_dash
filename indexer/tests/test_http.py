import time

import httpx

from indexer.http import RateLimiter, Upstream


async def test_backoff_on_429_then_success() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        up = Upstream("t", "http://x", 6000, client, retries=4, backoff_s=0.01)
        assert await up.get_json("/a") == {"ok": True}
    assert calls["n"] == 3
    assert up.stats["429"] == 2 and up.stats["ok"] == 1


async def test_gives_up_without_raising() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client:
        up = Upstream("t", "http://x", 6000, client, retries=2, backoff_s=0.01)
        assert await up.get_json("/a") is None
    assert up.stats["gave_up"] == 1


async def test_no_retry_on_404() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        up = Upstream("t", "http://x", 6000, client, retries=3, backoff_s=0.01)
        assert await up.get_json("/a") is None
    assert calls["n"] == 1


async def test_network_error_is_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=[1])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        up = Upstream("t", "http://x", 6000, client, retries=2, backoff_s=0.01)
        assert await up.get_json("/a") == [1]


async def test_rate_limiter_spacing() -> None:
    lim = RateLimiter(per_minute=600)          # one request per 0.1 s
    t0 = time.monotonic()
    for _ in range(4):
        await lim.wait()
    assert time.monotonic() - t0 >= 0.28
