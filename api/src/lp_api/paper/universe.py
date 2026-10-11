"""The memecoin universe: Jupiter's live token lists + DexScreener's discovery lists, enriched with DexScreener pair
data, classified (narrative, tier, stage), scored, and joined with our Meteora pools. Built at most every ~2 min and
shared through Redis by the API (screener) and the paper bot runner.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from redis.asyncio import Redis

from .. import memes as M
from . import dexscreener as DS
from .categories import classify, heat

log = logging.getLogger("lp_api.universe")
KEY, LOCK = "memes:universe", "memes:universe:lock"
PoolsLookup = Callable[[list[str]], Awaitable[dict[str, dict[str, Any]]]]


async def jupiter_list(client: httpx.AsyncClient, name: str) -> list[dict[str, Any]]:
    try:
        r = await client.get(f"{M.JUP_TOKENS}/{name}", params={"limit": 100}, timeout=20)
        data = r.json() if r.status_code == 200 else None
        return data if isinstance(data, list) else []
    except (httpx.HTTPError, ValueError):
        return []


async def jupiter_search(client: httpx.AsyncClient, mints: list[str]) -> list[dict[str, Any]]:
    """Full Jupiter token records (price, stats, audit) for up to 100 mints per call."""
    out: list[dict[str, Any]] = []
    for i in range(0, len(mints), 100):
        try:
            r = await client.get(f"{M.JUP_TOKENS}/search", params={"query": ",".join(mints[i:i + 100])}, timeout=20)
            data = r.json() if r.status_code == 200 else None
            out += data if isinstance(data, list) else []
        except (httpx.HTTPError, ValueError):
            pass
    return out


async def build(client: httpx.AsyncClient, pools: PoolsLookup | None = None) -> dict[str, Any]:
    t0 = time.time()
    lists_task = asyncio.gather(*(jupiter_list(client, n) for n in M.LISTS))
    disc_task = DS.discovery(client)
    got, disc = await asyncio.gather(lists_task, disc_task)
    rows = M.merge_lists(dict(zip(M.LISTS, got, strict=True)))
    have = {r["mint"] for r in rows}
    extra = [m for m in disc if m not in have]
    if extra:                                          # DexScreener-only tokens: get their Jupiter record
        for t in await jupiter_search(client, extra):
            if t.get("id") in disc and M.is_meme(t):
                rows.append(M.normalize(t, []))
    ds = await DS.pairs_for(client, [r["mint"] for r in rows])
    for r in rows:
        M.enrich(r, ds.get(r["mint"]), disc.get(r["mint"]))
        classify(r)
    if pools and rows:
        by = await pools([r["mint"] for r in rows])
        for r in rows:
            r["meteora_pool"] = by.get(r["mint"])
    sources = {"jupiter_lists": sum(1 for g in got if g), "dexscreener_discovery": len(disc),
               "dexscreener_pairs": len(ds)}
    log.info("universe: %d tokens in %.1fs %s", len(rows), time.time() - t0, sources)
    return {"at": time.time(), "tokens": rows, "heat": heat(rows), "sources": sources}


async def get(redis: Redis, client: httpx.AsyncClient, pools: PoolsLookup | None = None,
              max_age: float = 150) -> dict[str, Any]:
    """The cached universe if fresh, else rebuild it (one builder at a time across processes)."""
    raw = await redis.get(KEY)
    cached = json.loads(raw) if raw else None
    if cached and time.time() - cached["at"] < max_age:
        return cached
    if await redis.set(LOCK, "1", nx=True, ex=90):
        try:
            u = await build(client, pools)
            if u["tokens"]:
                await redis.set(KEY, json.dumps(u, default=str), ex=3600)
                return u
        finally:
            await redis.delete(LOCK)
    elif not cached:                                   # someone else is building: wait for it
        for _ in range(30):
            await asyncio.sleep(2)
            if raw := await redis.get(KEY):
                return json.loads(raw)
    if cached:
        return cached                                  # sources down or busy: serve the last good universe
    raise RuntimeError("memecoin universe unavailable (Jupiter and DexScreener)")
