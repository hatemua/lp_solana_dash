from typing import Any

from lp_api.ratelimit import RateLimiter


class FakePipe:
    def __init__(self, store: dict[str, int]) -> None:
        self.store, self.ops = store, []

    def incr(self, k: str) -> None:
        self.ops.append(k)

    def expire(self, k: str, s: int) -> None:
        pass

    async def execute(self) -> list[Any]:
        k = self.ops[0]
        self.store[k] = self.store.get(k, 0) + 1
        return [self.store[k], True]


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> FakePipe:
        return FakePipe(self.store)


class DownRedis:
    def pipeline(self, transaction: bool = True) -> Any:
        raise ConnectionError("down")


async def test_limit_per_window() -> None:
    lim = RateLimiter(FakeRedis(), per_minute=3)
    results = [await lim.hit("1.2.3.4", now=120.0) for _ in range(4)]
    assert [ok for ok, _ in results] == [True, True, True, False]
    assert results[-1][1] == 60                     # window resets in 60 s
    ok, _ = await lim.hit("1.2.3.4", now=181.0)     # next minute
    assert ok


async def test_ips_are_separate() -> None:
    lim = RateLimiter(FakeRedis(), per_minute=1)
    assert (await lim.hit("a", now=0))[0] and (await lim.hit("b", now=0))[0]


async def test_redis_down_does_not_block() -> None:
    assert (await RateLimiter(DownRedis(), per_minute=1).hit("a"))[0]
