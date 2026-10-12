"""Paper LP bot: every minute, step the open virtual positions on real pool data and open new ones.

Paper positions exist only in the bot_positions table. Prices, fees and the other LPs' liquidity come from the indexer
tables and the executor's read-only bins endpoint. Users' bots (follow.py) follow these house strategies, on paper or
with real money through the executor (which alone derives and uses the bots' wallet keys).
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

from . import analyze as AN
from . import jupiter, store
from .config import Settings, get_settings
from .engine import SOFT_EXITS, TX_SOL, Position
from .follow import Follow
from .guards import DayRules, calm_entry, day_guard, entry_guard, exit_warning, portfolio_pause
from .strategies import BY_NAME, SELL_COST_ESTIMATE, Strategy

log = logging.getLogger("lp_bot")
HEARTBEAT = Path("/tmp/lp_bot_heartbeat")
MAX_ENTRY_SWAP_COST = 0.02
MAX_ROUND_TRIP = 0.03            # careful entries: buy + sell back, real quotes
PRICE_GAP = (-0.03, 0.04)        # Jupiter fill price vs pool price (the buy fee alone is ~+1%)
REENTRY_BLOCK_MIN = 120
LOSS_BLOCK_H = 6                 # a token that just lost money is not re-entered for 6 h
ANALYZE_EVERY_S = 300
ALL: dict[str, Strategy] = {**BY_NAME, AN.Auto.name: AN.Auto()}
_RT: dict[str, tuple[float, tuple[float, float] | None]] = {}   # pool -> (when, round trip quote), 10 min
PAUSED: dict[str, str | None] = {"reason": None}
DAY: dict[str, tuple[str | None, bool]] = {}     # strategy -> (stop reason, close the open ones): guards.day_guard
_SKIP: dict[str, float] = {}                     # mint -> until: too expensive to buy, re-quote after 30 min


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


async def day_guards(db: Any, cfg: Settings, names: list[str]) -> None:
    rules = DayRules(cfg.day_target_usd, cfg.day_lock_min_peak_usd, cfg.day_lock_giveback, cfg.streak_losses,
                     cfg.streak_pause_h)
    res = await store.day_results(db)
    for n in names:
        r = res.get(n, {"today": [], "open_usd": 0.0, "recent": []})
        why, close = day_guard(r["today"], r["open_usd"], r["recent"], time.time(), rules)
        if why and DAY.get(n, (None, False))[0] is None:
            log.info("%s stops for now: %s", n, why)
            await store.event(db, n, None, "day_guard", {"why": why, "close_open": close})
        DAY[n] = (why, close)


async def step_open(db: Any, client: httpx.AsyncClient, cfg: Settings, sol_usd: float, follow: Follow) -> int:
    positions = await store.open_positions(db)
    if not positions:
        return 0
    since = min(p.last_ts for p in positions)
    mins = await store.minutes(db, sorted({p.pool for p in positions}), since)
    bins_cache: dict[str, list[dict[str, Any]] | None] = {}
    flow_by_mint = await store.flows(db, sorted({p.info["mint"] for p in positions if p.info.get("mint")}))
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
        rules = AN.rules_for(ALL[p.strategy], p.info)
        reason = None
        for r in new:
            prev = next((x for x in reversed(rows) if x["t"] < r["t"]), None)
            fees = max(0.0, (r["cum_fees"] or 0) - (prev["cum_fees"] or 0)) if prev and prev["cum_fees"] else 0.0
            old = next((x for x in rows if x["t"] >= r["t"] - 15 * 60), None)
            drop = (r["tvl"] / old["tvl"] - 1) if old and old["tvl"] and r["tvl"] else None
            if not r["price"]:
                continue
            danger = None
            if r is new[-1]:                                # warnings use the latest token stats and TVL
                old30 = next((x for x in rows if x["t"] >= r["t"] - 30 * 60), None)
                tvl30 = (r["tvl"] / old30["tvl"] - 1) if old30 and old30["tvl"] and r["tvl"] else None
                danger = exit_warning(flow_by_mint.get(p.info.get("mint", "")), tvl30)
                if rules.confirm_warnings:                  # one noisy 5-min stat is not a crash
                    p.info["warn_n"] = p.info.get("warn_n", 0) + 1 if danger else 0
                    if danger and (p.info["warn_n"] < 2 or r["t"] - p.opened_at < rules.warn_min_age_min * 60):
                        danger = None
            gap = r["t"] - p.last_ts
            if gap > 600:
                fees = 0.0                                  # fees earned while we were blind are not ours to count
            reason = p.step_minute(r["t"], r["price"], fees, sol_usd, others, rules, drop, danger)
            if not reason and gap > 600:
                reason = "data_gap"                         # we were blind > 10 min: close at the first price back
            if reason:
                break
        if not reason and DAY.get(p.strategy, (None, False))[1] and not p.info.get("unwind"):
            reason = "profit_lock"                          # the day is made (or protected): close what is open
        if reason in SOFT_EXITS and rules.unwind and not p.info.get("unwind") \
                and p.start_unwind(p.last_price, rules, reason):
            log.info("unwind %s %s after %s", p.strategy, p.name, reason)
            await store.event(db, p.strategy, p.id, "unwind", {"reason": reason,
                                                               "net_pct": round(p.net_pct(p.last_price), 3)})
            follow.on_paper_unwind(p)
            reason = None
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
                   sol_usd: float, follow: Follow) -> int:
    cands = [c for c in await store.candidates(db, cfg.min_tvl, cfg.min_fees_1h)
             if c.token_age_h is not None and c.token_age_h >= cfg.min_token_age_h]
    if not cands:
        return 0
    pause = portfolio_pause(await store.day_pnl_usd(db), cfg.daily_loss_limit_usd, await store.sol_change_1h(db))
    if pause:
        log.info("entries paused: %s", pause)
        PAUSED["reason"] = pause
        return 0
    PAUSED["reason"] = None
    black = await store.blacklisted_mints(db)
    guarded = []
    for c in cands:
        why = entry_guard(c.flow, c.tvl_change_30m, c.mint in black) if c.flow else []
        if why:
            log.debug("guard %s: %s", c.name, why)
        else:
            guarded.append(c)
    cands = guarded
    open_ = await store.open_positions(db)
    recent = await store.closed_mints(db, REENTRY_BLOCK_MIN) | await store.losing_mints(db, LOSS_BLOCK_H)
    opened = 0
    capital_sol = cfg.position_usd / sol_usd
    for s in strategies:
        if DAY.get(s.name, (None, False))[0]:
            continue
        mine = [p for p in open_ if p.strategy == s.name]
        slots = cfg.max_open_per_strategy - len(mine)
        if slots <= 0:
            continue
        cooled_pools, cooled_mints = await store.recently_closed(db, s.name, s.rules.cooldown_min)
        busy = {p.pool for p in mine} | cooled_pools
        taken_tokens = {p.name.split("-")[0] for p in mine}
        taken_mints = {p.info.get("mint") for p in mine} | cooled_mints
        for c in sorted(cands, key=s.rank, reverse=True):
            if slots <= 0:
                break
            cooling = c.mint in taken_mints or _SKIP.get(c.mint, 0) > time.time()
            if c.pool in busy or c.name.split("-")[0] in taken_tokens or cooling:
                continue
            if s.careful and (c.mint in recent or calm_entry(c.flow)):
                continue
            plan = s.entry(c, capital_sol)
            if not plan:
                continue
            n_positions = sum(math.ceil(len(lg.bins) / 69) for lg in plan.legs)
            swap_cost, sell_cost = 0.0, c.sell_cost
            info = {k: v for k, v in plan.info.items() if isinstance(v, int | float | str | None)}
            info.update(mint=c.mint, decimals=c.decimals)
            if s.careful:                                  # real round trip; bids fill into tokens we sell too
                rt = await jupiter.round_trip(client, c.mint, c.decimals, plan.buy_sol or capital_sol / 2, c.price)
                if rt is None or rt[0] > MAX_ROUND_TRIP or not PRICE_GAP[0] <= rt[1] <= PRICE_GAP[1]:
                    log.info("skip %s %s: round trip %s", s.name, c.name,
                             "no quote" if rt is None else f"{rt[0]:.1%}, price gap {rt[1]:+.1%}")
                    _SKIP[c.mint] = time.time() + (300 if rt is None else 1800)
                    continue
                swap_cost = plan.buy_sol * rt[0] / 2
                sell_cost = max(SELL_COST_ESTIMATE, rt[0] / 2)
                info.update(round_trip_pct=round(rt[0] * 100, 3), price_gap_pct=round(rt[1] * 100, 3))
            elif plan.buy_sol > 0:                         # the token half: real Jupiter quote
                frac = await jupiter.buy_cost(client, c.mint, c.decimals, plan.buy_sol, c.price)
                if frac > MAX_ENTRY_SWAP_COST:             # thin liquidity: the position would start deep red
                    log.info("skip %s %s: swap cost %.1f%%", s.name, c.name, frac * 100)
                    _SKIP[c.mint] = time.time() + 1800
                    continue
                swap_cost = plan.buy_sol * frac
                info["entry_swap_cost_pct"] = round(frac * 100, 3)
            p = Position(strategy=s.name, pool=c.pool, name=c.name, opened_at=time.time(), entry_price=c.price,
                         capital_sol=capital_sol, step=c.step, sell_cost=sell_cost, legs=plan.legs,
                         idle_sol=plan.idle_sol, costs_sol=swap_cost + n_positions * TX_SOL,
                         last_ts=time.time(), last_price=c.price, info=info)
            p.id = await store.insert(db, p, cfg.position_usd, sol_usd)
            log.info("open %s %s at %.3g (%s)", s.name, c.name, c.price, json.dumps(p.info, default=str)[:200])
            follow.on_paper_open(p)
            slots -= 1
            opened += 1
            taken_tokens.add(c.name.split("-")[0])
            taken_mints.add(c.mint)
    return opened


async def analyze_pools(db: Any, client: httpx.AsyncClient, cfg: Settings) -> tuple[int, int]:
    """Analyze every candidate pool (see analyze.py) and store the result for the dashboard and the auto strategy."""
    sol = await store.sol_usd(db)
    if sol <= 0:
        return 0, 0
    capital_sol = cfg.position_usd / sol
    done = chosen = 0
    black = await store.blacklisted_mints(db) | await store.losing_mints(db, LOSS_BLOCK_H)   # same blocks as entries
    for c in await store.candidates(db, cfg.min_tvl, cfg.min_fees_1h):
        banned = c.mint in black
        quoted = not AN.token_vetoes(c) and not banned  # don't spend quotes on tokens we won't enter
        rt = None
        if quoted:
            hit = _RT.get(c.pool)
            if hit and time.time() - hit[0] < 600:
                rt = hit[1]
            else:
                rt = await jupiter.round_trip(client, c.mint, c.decimals, capital_sol / 2, c.price)
                _RT[c.pool] = (time.time(), rt)
        a = AN.assess(c, rt, quoted, banned)
        if not a["vetoes"] and rt is not None:
            bins = await live_bins(client, cfg, c.pool) or await store.snapshot_bins(db, c.pool)
            others = Others(bins, sol, c.step, fallback_usd=c.tvl / 50)
            rows = await store.history(db, c.pool, AN.LOOKBACK_H)
            a = await asyncio.to_thread(AN.analyze, c, rows, others, sol, capital_sol, rt, banned)
        AN.LATEST[c.pool] = a
        await store.save_analysis(db, a)
        done += 1
        chosen += bool(a["choice"])
    return done, chosen


async def analyze_loop(db: Any, client: httpx.AsyncClient, cfg: Settings) -> None:
    while True:
        t0 = time.time()
        try:
            done, chosen = await analyze_pools(db, client, cfg)
            log.info("analyzed %d pools in %.0fs, %d worth entering", done, time.time() - t0, chosen)
        except Exception:
            log.exception("analysis failed")
        await asyncio.sleep(max(30.0, ANALYZE_EVERY_S - (time.time() - t0)))


async def run_async() -> None:
    cfg = get_settings()
    if cfg.bot_mode != "paper":
        raise SystemExit("BOT_MODE must be 'paper' (real money runs in users' bots, see follow.py)")
    strategies = [ALL[n] for n in cfg.strategy_names]
    db = await store.connect(cfg.database_url)
    redis = Redis.from_url(cfg.redis_url, decode_responses=True)
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    log.info("paper bot started: %s, $%.0f per position, max %d open each", [s.name for s in strategies],
             cfg.position_usd, cfg.max_open_per_strategy)
    await store.event(db, None, None, "start", {"strategies": [s.name for s in strategies],
                                                "position_usd": cfg.position_usd})
    analyzer = asyncio.create_task(analyze_loop(db, client, cfg))
    follow = Follow(cfg, db, client)
    await follow.recover()
    log.info("users' bots: real money %s", "on" if cfg.live_enabled else "off")
    while True:
        t0 = time.time()
        status: dict[str, Any] = {"ts": t0, "mode": "paper", "strategies": [s.name for s in strategies],
                                  "labels": {s.name: s.label for s in strategies}, "analyzer": not analyzer.done()}
        try:
            sol = await store.sol_usd(db)
            if sol <= 0:
                raise RuntimeError("no SOL price")
            await day_guards(db, cfg, [st.name for st in strategies])
            status["day_guard"] = {k: v[0] for k, v in DAY.items() if v[0]}
            status["closed"] = await step_open(db, client, cfg, sol, follow)
            status["followers"] = await follow.tick()        # after the house positions moved, before new ones
            status["opened"] = await open_new(db, client, cfg, strategies, sol, follow)
            status["ok"] = True
            status["paused"] = PAUSED["reason"]
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
