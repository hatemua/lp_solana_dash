"""Latest hot-pool stats in Redis: pool:{addr} hashes, rank:* sorted sets, and a pub/sub update channel."""

import json
import time
from typing import Any, cast

from redis.asyncio import Redis

POOL_TTL_S = 3600
RANKS = {
    "rank:tvl": "tvl",
    "rank:volume_1h": "volume_1h",
    "rank:volume_24h": "volume_24h",
    "rank:fees_1h": "fees_1h",
    "rank:fees_24h": "fees_24h",
    "rank:fee_tvl_1h": "fee_tvl_1h",
    "rank:fee_tvl_24h": "fee_tvl_24h",
}
CHANNEL = "pools:hot"


def pool_hash(pool: dict[str, Any], stat: dict[str, Any], symbols: tuple[str | None, str | None]) -> dict[str, str]:
    """Flat string mapping for HSET (None values are dropped)."""
    fields: dict[str, Any] = {
        "address": pool["address"], "name": pool.get("name"), "token_x": pool.get("token_x"),
        "token_y": pool.get("token_y"), "symbol_x": symbols[0], "symbol_y": symbols[1],
        "bin_step": pool.get("bin_step"), "base_fee_pct": pool.get("base_fee_pct"),
        "created_at": pool["created_at"].isoformat() if pool.get("created_at") else None,
        "launchpad": pool.get("launchpad"),
    }
    for k, v in stat.items():
        if k in ("pool",):
            continue
        fields[k] = v.isoformat() if k == "ts" and v is not None else v
    return {k: str(v) for k, v in fields.items() if v is not None}


class RedisStore:
    def __init__(self, url: str) -> None:
        self.r = Redis.from_url(url, decode_responses=True)

    async def put_hot(self, items: list[tuple[dict[str, Any], dict[str, Any], tuple[str | None, str | None]]]) -> None:
        pipe = self.r.pipeline(transaction=False)
        addrs = []
        for pool, stat, symbols in items:
            key = f"pool:{pool['address']}"
            pipe.hset(key, mapping=cast(Any, pool_hash(pool, stat, symbols)))
            pipe.expire(key, POOL_TTL_S)
            addrs.append(pool["address"])
        # rebuild the rankings from this snapshot (pools that left the hot set drop out)
        for zkey, field in RANKS.items():
            pipe.delete(zkey)
            scores = {p["address"]: float(s[field]) for p, s, _ in items if s.get(field) is not None}
            if scores:
                pipe.zadd(zkey, scores)
        pipe.delete("hot:pools")
        if addrs:
            pipe.sadd("hot:pools", *addrs)
        now = time.time()
        pipe.set("hot:updated_at", str(now))
        pipe.publish(CHANNEL, json.dumps({"type": "hot_stats", "count": len(items), "ts": now}))
        await pipe.execute()

    async def set_json(self, key: str, value: Any, ttl_s: int | None = None) -> None:
        await self.r.set(key, json.dumps(value, default=str), ex=ttl_s)

    async def ping(self) -> bool:
        try:
            return bool(await self.r.ping())
        except Exception:
            return False

    async def close(self) -> None:
        await self.r.aclose()
