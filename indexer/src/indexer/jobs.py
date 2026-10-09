"""Indexer jobs: full pool list, hot-pool stats, new pools, token info, OHLCV backfill, token candles, bins."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import models as M
from .config import Settings
from .db import Database
from .redis_store import RedisStore
from .sources import OHLCV_MAX_WINDOW_S, Executor, Jupiter, Meteora

log = logging.getLogger(__name__)


@dataclass
class JobState:
    every_s: int
    runs: int = 0
    failures: int = 0
    last_start: float | None = None
    last_ok: float | None = None
    last_error: str | None = None
    last_duration_s: float | None = None
    info: dict[str, Any] = field(default_factory=dict)


def floor_ts(t: datetime, seconds: int) -> datetime:
    return datetime.fromtimestamp(int(t.timestamp()) // seconds * seconds, UTC)


class Indexer:
    def __init__(self, cfg: Settings, db: Database, redis: RedisStore, meteora: Meteora, jupiter: Jupiter,
                 executor: Executor) -> None:
        self.cfg, self.db, self.redis = cfg, db, redis
        self.meteora, self.jupiter, self.executor = meteora, jupiter, executor
        self.jobs: dict[str, JobState] = {}
        self.hot: dict[str, dict[str, Any]] = {}          # latest hot pools (raw API objects)
        self.hot_stats: dict[str, dict[str, Any]] = {}    # latest pool_stats row per hot pool
        self.hot_at: float | None = None
        self.tvl10k_truth: list[str] = []                 # pools with TVL >= $10k according to the API
        self._flow_prev: dict[str, tuple[datetime, dict[str, Any]]] = {}
        self._tokens_seen_1m: set[str] = set()
        self.stop = asyncio.Event()

    # ------------------------------------------------------------------ scheduler
    def schedule(self) -> list[asyncio.Task[None]]:
        c = self.cfg
        specs: list[tuple[str, Callable[[], Awaitable[None]], int, float]] = [
            ("hot_stats", self.hot_stats_job, c.hot_stats_every_s, 0),
            ("new_pools", self.new_pools_job, c.new_pools_every_s, 5),
            ("full_pool_list", self.full_pool_list_job, c.full_list_every_s, 10),
            ("token_info", self.token_info_job, c.token_info_every_s, 20),
            ("ohlcv", self.ohlcv_job, c.ohlcv_every_s, 30),
            ("token_ohlcv_1m", self.token_ohlcv_job, c.token_ohlcv_every_s, 40),
            ("bins", self.bins_job, c.bins_every_s, 45),
        ]
        return [asyncio.create_task(self._loop(n, fn, every, delay), name=n) for n, fn, every, delay in specs]

    async def _loop(self, name: str, fn: Callable[[], Awaitable[None]], every: int, first_delay: float) -> None:
        st = self.jobs.setdefault(name, JobState(every_s=every))
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=first_delay)
            return
        except TimeoutError:
            pass
        while not self.stop.is_set():
            t0 = time.time()
            st.last_start, st.runs = t0, st.runs + 1
            try:
                await fn()
                st.last_ok, st.last_error = time.time(), None
            except Exception as e:  # a job never kills the indexer
                st.failures += 1
                st.last_error = f"{type(e).__name__}: {e}"[:300]
                log.exception("job %s failed", name)
            st.last_duration_s = round(time.time() - t0, 2)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=max(1.0, every - (time.time() - t0)))
            except TimeoutError:
                pass

    def _info(self, name: str, **kw: Any) -> None:
        self.jobs.setdefault(name, JobState(every_s=0)).info.update(kw)

    # ------------------------------------------------------------------ jobs
    async def full_pool_list_job(self) -> None:
        """Every pool (~137k), paged; pools + basic token info; stats for pools with TVL >= tracked_min_tvl."""
        c, now = self.cfg, datetime.now(UTC)
        ts = floor_ts(now, 60)
        page, pages, n_pools, n_stats, failed, bad_pages = 1, None, 0, 0, 0, 0
        while pages is None or page <= pages:
            if self.stop.is_set():
                return
            d = await self.meteora.pools(page, c.full_list_page_size, "pool_created_at:desc")
            if d is None:
                failed += 1
                if failed > 5:
                    raise RuntimeError(f"pool list stopped at page {page} after repeated errors")
                await asyncio.sleep(5)
                continue
            pages = int(d.get("pages") or 1)
            data = d["data"]
            tokens: dict[str, dict[str, Any]] = {}
            for p in data:
                for t in M.token_rows(p, now):
                    tokens[t["mint"]] = t
            try:
                await self.db.upsert("tokens_basic", tokens.values())
                n_pools += await self.db.upsert("pools", [M.pool_row(p, now) for p in data if p.get("address")])
                tracked = [M.stat_row(p, ts) for p in data if (M.fnum(p.get("tvl")) or 0) >= c.tracked_min_tvl]
                n_stats += await self.db.upsert("pool_stats", tracked)
            except Exception:                    # one bad page must not stop the whole list
                bad_pages += 1
                log.exception("full pool list: page %d failed, continuing", page)
            page += 1
        self._info("full_pool_list", pools=n_pools, pages=pages, stats_rows=n_stats, bad_pages=bad_pages)
        if bad_pages:
            raise RuntimeError(f"{bad_pages} page(s) of the pool list could not be stored")
        log.info("full pool list: %d pools in %s pages, %d tracked stats", n_pools, pages, n_stats)

    async def hot_stats_job(self) -> None:
        """Hot pools (SOL pairs, TVL >= $10k or 1 h volume >= $50k): stats, Redis, TVL flow."""
        c, now = self.cfg, datetime.now(UTC)
        ts = floor_ts(now, 60)
        by_tvl = await self.meteora.pools(1, 1000, "tvl:desc")
        by_vol = await self.meteora.pools(1, 1000, "volume_1h:desc")
        if by_tvl is None and by_vol is None:
            raise RuntimeError("pool list unavailable")
        merged: dict[str, dict[str, Any]] = {}
        for d in (by_tvl, by_vol):
            for p in (d or {}).get("data") or []:
                if p.get("address"):
                    merged[p["address"]] = p
        if by_tvl:
            self.tvl10k_truth = [p["address"] for p in by_tvl["data"] if (M.fnum(p.get("tvl")) or 0) >= 10_000]
        hot = {a: p for a, p in merged.items() if M.is_hot(p, c.hot_min_tvl, c.hot_min_volume_1h)}
        addrs = list(hot)

        last5 = {r["pool"]: r for r in await self.db.fetch(
            "SELECT DISTINCT ON (pool) pool, volume, fees FROM pool_ohlcv_5m "
            "WHERE pool = ANY(:addrs) AND ts >= now() - interval '15 minutes' ORDER BY pool, ts DESC", addrs=addrs)}
        pools = [dict(M.pool_row(p, now), is_hot=True) for p in hot.values()]
        stats = [M.stat_row(p, ts, (last5.get(a) or {}).get("volume"), (last5.get(a) or {}).get("fees"))
                 for a, p in hot.items()]
        tokens: dict[str, dict[str, Any]] = {}
        for p in hot.values():
            for t in M.token_rows(p, now):
                tokens[t["mint"]] = t
        await self.db.upsert("tokens_basic", tokens.values())
        await self.db.upsert("pools", pools)
        await self.db.upsert("pool_stats", stats)
        await self.db.execute("UPDATE pools SET is_hot = (address = ANY(:addrs)) WHERE is_hot OR address = ANY(:addrs)",
                              addrs=addrs)
        await self._tvl_flow(ts, stats)

        pool_by = {r["address"]: r for r in pools}
        await self.redis.put_hot([(pool_by[s["pool"]], s, ((hot[s["pool"]].get("token_x") or {}).get("symbol"),
                                                            (hot[s["pool"]].get("token_y") or {}).get("symbol")))
                                  for s in stats])
        self.hot, self.hot_stats, self.hot_at = hot, {s["pool"]: s for s in stats}, time.time()
        self._info("hot_stats", hot_pools=len(hot), sol_pools_tvl10k=sum(
            1 for p in hot.values() if (M.fnum(p.get("tvl")) or 0) >= 10_000), api_pools_tvl10k=len(self.tvl10k_truth))

    async def _tvl_flow(self, ts: datetime, stats: list[dict[str, Any]]) -> None:
        bucket = floor_ts(ts, 300)
        rows = []
        for s in stats:
            prev = self._flow_prev.get(s["pool"])
            if prev is None:
                self._flow_prev[s["pool"]] = (bucket, s)
                continue
            pb, ps = prev
            if bucket > pb:                       # a new 5-min bucket: liquidity flow since the last one
                f = M.tvl_flow(ps, s)
                rows.append({"pool": s["pool"], "ts": bucket, "tvl_start": ps.get("tvl"), "tvl_end": s.get("tvl"), **f})
                self._flow_prev[s["pool"]] = (bucket, s)
        await self.db.upsert("pool_tvl_flow", rows)

    async def new_pools_job(self) -> None:
        now = datetime.now(UTC)
        d = await self.meteora.pools(1, 100, "pool_created_at:desc")
        if d is None:
            raise RuntimeError("new pools unavailable")
        tokens: dict[str, dict[str, Any]] = {}
        for p in d["data"]:
            for t in M.token_rows(p, now):
                tokens[t["mint"]] = t
        await self.db.upsert("tokens_basic", tokens.values())
        n = await self.db.upsert("pools", [M.pool_row(p, now) for p in d["data"] if p.get("address")])
        newest = M.ts_from_ms(max((M.fnum(p.get("created_at")) or 0 for p in d["data"]), default=0))
        self._info("new_pools", upserted=n, newest_created_at=newest.isoformat() if newest else None)

    async def token_info_job(self) -> None:
        """Jupiter token info (holders, mcap, organic score, audit, stats) for the tokens of hot pools."""
        now = datetime.now(UTC)
        mints = sorted({m for p in self.hot.values() if (m := M.other_mint(p))})
        n = 0
        for i in range(0, len(mints), Jupiter.MAX_MINTS):
            assets = await self.jupiter.assets(mints[i:i + Jupiter.MAX_MINTS])
            n += await self.db.upsert("tokens_jupiter", [dict(M.jupiter_token_row(a, now), enriched_at=now)
                                                         for a in assets])
        self._info("token_info", tokens=n, requested=len(mints))

    async def ohlcv_job(self) -> None:
        """5-min OHLCV + fees for hot pools: 72 h backfill the first time, then incremental."""
        c = self.cfg
        if not self.hot:
            return
        order = sorted(self.hot, key=lambda a: -(self.hot_stats.get(a, {}).get("fees_1h") or 0))
        cursors = {r["pool"]: r for r in await self.db.fetch(
            "SELECT pool, last_ts, backfilled FROM ohlcv_cursor WHERE pool = ANY(:addrs)", addrs=order)}
        sem = asyncio.Semaphore(c.ohlcv_concurrency)
        done = {"bars": 0, "backfilled": 0, "pools": 0}
        end = int(time.time()) // 300 * 300

        async def one(addr: str) -> None:
            async with sem:
                if self.stop.is_set():
                    return
                cur = cursors.get(addr)
                backfill = not cur or not cur["backfilled"]
                if backfill:
                    start = end - c.ohlcv_backfill_hours * 3600
                elif cur and cur["last_ts"] is not None:
                    start = int(cur["last_ts"].timestamp()) - 600        # overlap: the last bar may have changed
                else:
                    start = end - OHLCV_MAX_WINDOW_S["5m"]
                last = None
                step = OHLCV_MAX_WINDOW_S["5m"]
                for w0 in range(start, end, step):
                    w1 = min(w0 + step, end)
                    ohlcv = await self.meteora.ohlcv(addr, "5m", w0, w1)
                    vol = await self.meteora.volume_history(addr, "5m", w0, w1)
                    rows = M.ohlcv_rows(addr, ohlcv, vol)
                    done["bars"] += await self.db.upsert("pool_ohlcv_5m", rows)
                    if rows:
                        last = max(r["ts"] for r in rows)
                if last is not None or backfill:
                    await self.db.upsert("ohlcv_cursor", [{"pool": addr, "last_ts": last or (cur or {}).get("last_ts"),
                                                           "backfilled": True, "updated_at": datetime.now(UTC)}])
                done["pools"] += 1
                done["backfilled"] += int(backfill)

        await asyncio.gather(*(one(a) for a in order))
        self._info("ohlcv", **done)

    async def token_ohlcv_job(self) -> None:
        """1-min candles (all venues, Jupiter) for the tokens of the top N hot pools by 1 h fees."""
        c = self.cfg
        top = sorted(self.hot, key=lambda a: -(self.hot_stats.get(a, {}).get("fees_1h") or 0))[: c.track_tokens_n]
        mints = list(dict.fromkeys(m for a in top if (m := M.other_mint(self.hot[a]))))
        n = 0
        to_ms = int(time.time() * 1000)
        for m in mints:
            candles = 10 if m in self._tokens_seen_1m else 120     # first time: last 2 h
            chart = await self.jupiter.chart(m, "1_MINUTE", to_ms, candles)
            rows = M.candle_rows(m, chart)
            n += await self.db.upsert("token_ohlcv_1m", rows)
            if rows:
                self._tokens_seen_1m.add(m)
        self._info("token_ohlcv_1m", tokens=len(mints), candles=n)

    def watched_pools(self) -> list[str]:
        c = self.cfg
        fixed = [a.strip() for a in c.watch_pools.split(",") if a.strip()]
        top = sorted(self.hot, key=lambda a: -(self.hot_stats.get(a, {}).get("fee_tvl_1h") or 0))
        return list(dict.fromkeys(fixed + top[: c.watch_pools_n]))

    async def bins_job(self) -> None:
        """Active bin + liquidity per bin within +/- N bins for watched pools (executor, DLMM SDK, read-only)."""
        pools = self.watched_pools()
        rows, failed = [], 0
        ts = datetime.now(UTC).replace(microsecond=0)
        for addr in pools:
            d = await self.executor.bins(addr, self.cfg.bins_each_side)
            if d is None:
                failed += 1
                continue
            rows.append({"pool": addr, "ts": ts, "active_bin_id": int(d["activeBinId"]), "bin_step": d.get("binStep"),
                         "active_price": M.fnum(d.get("activePrice")), "bins": d.get("bins") or []})
        await self.db.upsert("bins_snapshot", rows)
        self._info("bins", watched=len(pools), stored=len(rows), failed=failed)
        if pools and not rows:
            raise RuntimeError("executor returned no bins (is it running and does it have an RPC?)")

    # ------------------------------------------------------------------ health
    async def health(self) -> dict[str, Any]:
        now = time.time()
        hot_age = now - self.hot_at if self.hot_at else None
        coverage = None
        if self.tvl10k_truth:
            r = await self.db.fetch("SELECT count(*) AS n FROM pools WHERE address = ANY(:a)", a=self.tvl10k_truth)
            coverage = round(r[0]["n"] / len(self.tvl10k_truth), 4)
        counts = (await self.db.fetch(
            "SELECT (SELECT count(*) FROM pools) AS pools, (SELECT count(*) FROM tokens) AS tokens, "
            "(SELECT count(*) FROM ohlcv_cursor WHERE backfilled) AS pools_backfilled, "
            "(SELECT max(ts) FROM pool_stats) AS last_stats, (SELECT max(ts) FROM bins_snapshot) AS last_bins"))[0]
        oldest_ohlcv = None
        if self.hot:
            r = await self.db.fetch("SELECT min(last_ts) AS t FROM ohlcv_cursor WHERE pool = ANY(:a)", a=list(self.hot))
            oldest_ohlcv = r[0]["t"]
        ok = hot_age is not None and hot_age <= self.cfg.hot_stats_max_age_s
        return {
            "status": "ok" if ok else "degraded",
            "hot_pools": len(self.hot),
            "hot_stats_age_s": round(hot_age, 1) if hot_age is not None else None,
            "coverage_tvl10k": coverage,
            "counts": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in counts.items()},
            "oldest_hot_ohlcv_bar": oldest_ohlcv.isoformat() if oldest_ohlcv else None,
            "redis": await self.redis.ping(),
            "jobs": {n: {"every_s": s.every_s, "runs": s.runs, "failures": s.failures,
                         "last_ok_age_s": round(now - s.last_ok, 1) if s.last_ok else None,
                         "last_duration_s": s.last_duration_s, "last_error": s.last_error, **s.info}
                     for n, s in self.jobs.items()},
        }

