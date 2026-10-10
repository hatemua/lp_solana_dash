"""Paper LP bot: every minute, step the open virtual positions on real pool data and open new ones.

No wallet, no keys, no transactions: positions exist only in the bot_positions table. Prices, fees and the other
LPs' liquidity come from the indexer tables and the executor's read-only bins endpoint.
"""

import asyncio
import bisect
import json
import logging
import math
import time
from pathlib import Path
from typing import Any

import httpx
from redis.asyncio import Redis

from . import jupiter, store
from .config import Settings, get_settings
from .engine import TX_SOL, Position
from .strategies import BY_NAME, Strategy

log = logging.getLogger("lp_bot")
HEARTBEAT = Path("/tmp/lp_bot_heartbeat")


class Others:
    """Other LPs' liquidity (USD) per bin, from a live read (executor) or the latest stored snapshot."""

    def __init__(self, bins: list[dict[str, Any]] | None, sol_usd: float, step: float, fallback_usd: float) -> None:
        self.step, self.fallback = step, fallback_usd
        self.known = bins is not None
        pts = sorted(((float(b["price"]), (float(b["x"]) * float(b["price"]) + float(b["y"])) * sol_usd)
                      for b in (bins or []) if b.get("price")), key=lambda x: x[0])
        self.prices = [p for p, _ in pts]
        self.usd = [u for _, u in pts]

    def __call__(self, price: float) -> float:
        if not self.known or not self.prices:
            return self.fallback
        if price < self.prices[0] * (1 - self.step) or price > self.prices[-1] * (1 + self.step):
            return 0.0                                     # outside the read range: no other LPs this far out
        i = bisect.bisect_left(self.prices, price)
        cands = [j for j in (i - 1, i) if 0 <= j < len(self.prices)]
        j = min(cands, key=lambda k: abs(self.prices[k] - price))
        return self.usd[j] if abs(self.prices[j] / price - 1) < self.step else 0.0


_BINS: dict[str, tuple[float, list[dict[str, Any]]]] = {}


async def live_bins(client: httpx.AsyncClient, cfg: Settings, pool: str) -> list[dict[str, Any]] | None:
    hit = _BINS.get(pool)
    if hit and time.time() - hit[0] < 110:          # executor reads the chain via a public RPC: 2 min cache
        return hit[1]
    try:
        n = cfg.live_bins_each_side
        r = await client.get(f"{cfg.executor_url}/v1/pools/{pool}/bins", params={"left": n, "right": n})
        if r.status_code == 200:
            bins = r.json().get("bins")
            if bins:
                _BINS[pool] = (time.time(), bins)
            return bins
    except httpx.HTTPError as e:
        log.debug("live bins %s: %s", pool, e)
    return None


async def step_open(db: Any, client: httpx.AsyncClient, cfg: Settings, sol_usd: float) -> int:
    positions = await store.open_positions(db)
    if not positions:
        return 0
    since = min(p.last_ts for p in positions)
    mins = await store.minutes(db, sorted({p.pool for p in positions}), since)
    bins_cache: dict[str, list[dict[str, Any]] | None] = {}
    closed = 0
    for p in positions:
        rows = mins.get(p.pool, [])
        new = [r for r in rows if r["t"] > p.last_ts]
        if not new:
            continue
        if p.pool not in bins_cache:
            bins_cache[p.pool] = await live_bins(client, cfg, p.pool) or await store.snapshot_bins(db, p.pool)
        tvl_now = new[-1]["tvl"] or 0
        others = Others(bins_cache[p.pool], sol_usd, p.step, fallback_usd=tvl_now / 50)
        rules = BY_NAME[p.strategy].rules
        reason = None
        for r in new:
            prev = next((x for x in reversed(rows) if x["t"] < r["t"]), None)
            fees = max(0.0, (r["cum_fees"] or 0) - (prev["cum_fees"] or 0)) if prev and prev["cum_fees"] else 0.0
            old = next((x for x in rows if x["t"] >= r["t"] - 15 * 60), None)
            drop = (r["tvl"] / old["tvl"] - 1) if old and old["tvl"] and r["tvl"] else None
            if not r["price"]:
                continue
            reason = p.step_minute(r["t"], r["price"], fees, sol_usd, others, rules, drop)
            if reason:
                break
        if reason:
            toks = p.tokens(p.last_price)
            if toks > 0 and p.info.get("mint"):          # the real cost of selling what we hold back to SOL
                p.sell_cost = await jupiter.sell_cost(client, p.info["mint"], int(p.info.get("decimals", 6)), toks,
                                                      p.last_price)
                p.info["exit_swap_cost_pct"] = round(p.sell_cost * 100, 3)
            net = p.close(p.last_price)
            await store.close(db, p, reason, net, sol_usd)
            log.info("close %s %s %s net %+.2f%% fees $%.2f", p.strategy, p.name, reason, net, p.fees_sol * sol_usd)
            closed += 1
        else:
            await store.update(db, p, sol_usd)
    return closed


async def open_new(db: Any, client: httpx.AsyncClient, cfg: Settings, strategies: list[Strategy],
                   sol_usd: float) -> int:
    cands = await store.candidates(db, cfg.min_tvl, cfg.min_fees_1h)
    if not cands:
        return 0
    open_ = await store.open_positions(db)
    opened = 0
    capital_sol = cfg.position_usd / sol_usd
    for s in strategies:
        mine = [p for p in open_ if p.strategy == s.name]
        slots = cfg.max_open_per_strategy - len(mine)
        if slots <= 0:
            continue
        busy = {p.pool for p in mine} | await store.recently_closed(db, s.name, s.rules.cooldown_min)
        taken_tokens = {p.name.split("-")[0] for p in mine}
        for c in sorted(cands, key=s.rank, reverse=True):
            if slots <= 0:
                break
            if c.pool in busy or c.name.split("-")[0] in taken_tokens:
                continue
            plan = s.entry(c, capital_sol)
            if not plan:
                continue
            n_positions = sum(math.ceil(len(lg.bins) / 69) for lg in plan.legs)
            swap_cost = 0.0
            info = {k: v for k, v in plan.info.items() if isinstance(v, int | float | str | None)}
            info.update(mint=c.mint, decimals=c.decimals)
            if plan.buy_sol > 0:                           # the token half: real Jupiter quote
                frac = await jupiter.buy_cost(client, c.mint, c.decimals, plan.buy_sol, c.price)
                swap_cost = plan.buy_sol * frac
                info["entry_swap_cost_pct"] = round(frac * 100, 3)
            p = Position(strategy=s.name, pool=c.pool, name=c.name, opened_at=time.time(), entry_price=c.price,
                         capital_sol=capital_sol, step=c.step, sell_cost=c.sell_cost, legs=plan.legs,
                         idle_sol=plan.idle_sol, costs_sol=swap_cost + n_positions * TX_SOL,
                         last_ts=time.time(), last_price=c.price, info=info)
            p.id = await store.insert(db, p, cfg.position_usd, sol_usd)
            log.info("open %s %s at %.3g (%s)", s.name, c.name, c.price, json.dumps(p.info, default=str)[:200])
            slots -= 1
            opened += 1
            taken_tokens.add(c.name.split("-")[0])
    return opened


async def run_async() -> None:
    cfg = get_settings()
    if cfg.bot_mode != "paper":
        raise SystemExit("BOT_MODE must be 'paper': live trading is not enabled in this build")
    strategies = [BY_NAME[n] for n in cfg.strategy_names]
    db = await store.connect(cfg.database_url)
    redis = Redis.from_url(cfg.redis_url, decode_responses=True)
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    log.info("paper bot started: %s, $%.0f per position, max %d open each", [s.name for s in strategies],
             cfg.position_usd, cfg.max_open_per_strategy)
    await store.event(db, None, None, "start", {"strategies": [s.name for s in strategies],
                                                "position_usd": cfg.position_usd})
    while True:
        t0 = time.time()
        status: dict[str, Any] = {"ts": t0, "mode": "paper", "strategies": [s.name for s in strategies]}
        try:
            sol = await store.sol_usd(db)
            if sol <= 0:
                raise RuntimeError("no SOL price")
            status["closed"] = await step_open(db, client, cfg, sol)
            status["opened"] = await open_new(db, client, cfg, strategies, sol)
            status["ok"] = True
        except Exception as e:                      # keep running; the error is visible in the heartbeat
            log.exception("tick failed")
            status.update(ok=False, error=str(e)[:300])
        status["took_s"] = round(time.time() - t0, 2)
        try:
            await redis.set("bot:heartbeat", json.dumps(status), ex=600)
        except Exception:
            log.warning("redis heartbeat failed")
        await asyncio.to_thread(HEARTBEAT.write_text, str(int(time.time())))
        # run ~20 s after each minute so the indexer's per-minute stats are in
        await asyncio.sleep(max(5.0, cfg.tick_s - (time.time() % 60) + 20) if cfg.tick_s == 60 else cfg.tick_s)


def run() -> None:
    cfg = get_settings()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run_async())


if __name__ == "__main__":
    run()
