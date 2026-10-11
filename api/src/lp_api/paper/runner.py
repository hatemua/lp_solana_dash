"""Paper meme-bot runner (the `memebot` service). Every 30 s: price every open position from Jupiter, apply its
bot's exit rules, and simulate sells with real Jupiter quotes. On each new universe (~2 min): buy for running bots
whose strategy finds a token, again priced with real quotes (buy, then sell back to measure the round-trip cost).

Virtual money only. Quotes are read-only; nothing is ever signed or sent, and the service has no wallet or key.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .. import auth
from .. import memes as M
from ..config import Settings, get_settings
from . import store, universe
from .engine import CRASH_EXITS, Pos, decide
from .strategies import TEMPLATES, Params, candidates

log = logging.getLogger("lp_api.paper")
HEARTBEAT = Path("/tmp/paper_heartbeat")
QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
PRICE_URL = "https://lite-api.jup.ag/price/v3"
TX_SOL = 0.0002                       # network + priority fee per swap
TICK_S = 30
MAX_QUOTES_PER_TICK = 40
HOUSE_BUDGET, HOUSE_POSITION, HOUSE_MAX_OPEN = 1_000.0, 100.0, 3


class Quotes:
    """Jupiter swap quotes, ~1/s, with a per-tick budget and a short cache shared by all bots."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client, self.last, self.used = client, 0.0, 0
        self.cache: dict[tuple[str, str, int], tuple[float, int | None]] = {}
        self.skip: dict[tuple[str, int], float] = {}           # (mint, size) -> until: round trip too expensive

    async def out(self, in_mint: str, out_mint: str, amount: int) -> int | None:
        key = (in_mint, out_mint, amount)
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        if self.used >= MAX_QUOTES_PER_TICK:
            return None
        self.used += 1
        wait = 1.0 - (time.time() - self.last)
        if wait > 0:
            await asyncio.sleep(wait)
        self.last = time.time()
        res = None
        try:
            r = await self.client.get(QUOTE_URL, params={"inputMint": in_mint, "outputMint": out_mint,
                                                         "amount": amount, "slippageBps": 300}, timeout=10)
            if r.status_code == 200:
                res = int(r.json()["outAmount"])
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.debug("quote failed: %s", e)
        self.cache[key] = (time.time(), res)
        return res


class Runner:
    def __init__(self, cfg: Settings, engine: AsyncEngine, redis: Redis, client: httpx.AsyncClient) -> None:
        self.cfg, self.db, self.redis, self.client = cfg, engine, redis, client
        self.quotes = Quotes(client)
        self.sol_usd = 0.0
        self.sol_hist: list[tuple[float, float]] = []
        self.universe_at = 0.0
        self.ranks: dict[str, list[int]] = {}                 # narrative -> heat ranks of the last refreshes
        self.paused: str | None = None

    # ------------------------------------------------------------------ market data
    async def refresh_sol(self) -> None:
        try:
            r = await self.client.get(PRICE_URL, params={"ids": M.SOL_MINT}, timeout=10)
            px = float(r.json()[M.SOL_MINT]["usdPrice"])
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            return
        now = time.time()
        self.sol_usd = px
        self.sol_hist = [(t, v) for t, v in self.sol_hist if now - t <= 3700] + [(now, px)]
        old = [v for t, v in self.sol_hist if now - t >= 3000]
        chg = (px / old[0] - 1) * 100 if old else 0.0
        self.paused = f"SOL dumping ({chg:.1f}% in 1 h)" if chg <= -4 else None

    async def live_rows(self, mints: list[str]) -> dict[str, dict[str, Any]]:
        recs = await universe.jupiter_search(self.client, mints)
        return {t["id"]: M.normalize(t, []) for t in recs if t.get("id") in mints}

    async def pools(self, mints: list[str]) -> dict[str, dict[str, Any]]:
        found = await store.q(self.db, "SELECT DISTINCT ON (token_x) token_x AS mint, address, name, bin_step, tvl "
                              "FROM pools WHERE token_x = ANY(:m) AND token_y = :sol AND tvl > 0 "
                              "ORDER BY token_x, tvl DESC NULLS LAST", m=mints, sol=M.SOL_MINT)
        return {p.pop("mint"): p for p in found}

    def narrative_cold(self, narrative: str | None, cool_rank: int | None) -> bool:
        if not cool_rank or not narrative:
            return False
        last = self.ranks.get(narrative, [99])[-2:]
        return len(last) == 2 and all(r > cool_rank for r in last)     # two refreshes in a row: not a blip

    # ------------------------------------------------------------------ bots
    async def ensure_house_bots(self) -> None:
        for key, t in TEMPLATES.items():
            await store.q(self.db, """INSERT INTO paper_bots (house_key, name, strategy_key, strategy_name, params,
                budget_usd, cash_usd, position_usd, max_open) VALUES (:k, :n, :k, :n, CAST(:p AS jsonb), :b, :b,
                :ps, :mo) ON CONFLICT (house_key) DO NOTHING""",
                          k=key, n=t["name"], p=store.js(t["params"].model_dump()), b=HOUSE_BUDGET,
                          ps=HOUSE_POSITION, mo=HOUSE_MAX_OPEN)

    async def sell(self, p: Pos, part: float, reason: str, row: dict[str, Any] | None) -> bool:
        """Simulate selling `part` of the remaining tokens with a real quote. False = no quote yet (retry)."""
        tokens = p.tokens_left * part if part < 1 else p.tokens_left
        raw = int(tokens * 10 ** p.decimals)
        out = await self.quotes.out(p.mint, M.SOL_MINT, raw) if raw > 0 else 0
        if out is None:
            p.quote_fails += 1
            if p.quote_fails < 3 or not self.sol_usd:
                return False
            usd_out, how = tokens * p.last_price * 0.9, "no_quote_mid_minus_10pct"    # no route: assume a bad fill
        else:
            usd_out, how = out / 1e9 * self.sol_usd, "quote"
        fee = TX_SOL * self.sol_usd
        closing = part >= 1 or tokens >= p.tokens_left
        p.tokens_left = 0.0 if closing else p.tokens_left - tokens
        if reason == "take_profit" and not closing:
            p.tp_done = True
        p.quote_fails = 0
        st = p.state()
        r = (await store.q(self.db, """UPDATE paper_positions SET tokens_left = :tl, usd_out = usd_out + :uo,
            costs_usd = costs_usd + :fee, last_price = :lp, peak_price = :pk, last_ts = now(),
            state = CAST(:st AS jsonb),
            status = CASE WHEN :closing THEN 'closed' ELSE status END,
            closed_at = CASE WHEN :closing THEN now() ELSE closed_at END,
            exit_reason = CASE WHEN :closing THEN :reason ELSE exit_reason END
            WHERE id = :id RETURNING usd_in, usd_out, costs_usd""",
                             tl=p.tokens_left, uo=usd_out, fee=fee, lp=p.last_price, pk=p.peak_price,
                             st=store.js(st), closing=closing, reason=reason, id=p.id))[0]
        if closing:
            pnl = r["usd_out"] - r["usd_in"] - r["costs_usd"]
            await store.q(self.db, "UPDATE paper_positions SET pnl_usd = :pnl, pnl_pct = :pct WHERE id = :id",
                          pnl=pnl, pct=pnl / r["usd_in"] * 100 if r["usd_in"] else None, id=p.id)
        await store.q(self.db, "UPDATE paper_bots SET cash_usd = cash_usd + :c, updated_at = now() WHERE id = :b",
                      c=usd_out - fee, b=p.bot_id)
        await store.event(self.db, p.bot_id, "close" if closing else "sell_part",
                          {"symbol": p.symbol, "reason": reason, "part": part, "usd_out": round(usd_out, 2),
                           "price": p.last_price, "gain_pct": round(p.gain_pct(p.last_price), 2), "fill": how,
                           "liquidity": (row or {}).get("liquidity")}, p.id)
        log.info("bot %s %s %s %s: $%.2f (%s)", p.bot_id, "close" if closing else "sell part", p.symbol, reason,
                 usd_out, how)
        return True

    async def step_open(self) -> int:
        rows = await store.q(self.db, """SELECT p.*, extract(epoch FROM p.opened_at) AS opened_s, b.params,
            b.status AS bot_status FROM paper_positions p JOIN paper_bots b ON b.id = p.bot_id
            WHERE p.status = 'open'""")
        if not rows:
            return 0
        live = await self.live_rows(sorted({r["mint"] for r in rows}))
        now = time.time()
        for r in rows:
            st = r["state"] if isinstance(r["state"], dict) else json.loads(r["state"] or "{}")
            p = Pos(id=r["id"], bot_id=r["bot_id"], mint=r["mint"], symbol=r["symbol"], narrative=r["narrative"],
                    decimals=r["decimals"], opened_at=r["opened_s"], entry_price=r["entry_price"],
                    entry_liquidity=r["entry_liquidity"], usd_in=r["usd_in"], tokens=r["tokens"],
                    tokens_left=r["tokens_left"], peak_price=r["peak_price"], last_price=r["last_price"],
                    tp_done=bool(st.get("tp_done")), misses=int(st.get("misses", 0)),
                    quote_fails=int(st.get("quote_fails", 0)),
                    px_log=[tuple(x) for x in st.get("px_log", [])])
            params = Params.model_validate(r["params"] if isinstance(r["params"], dict) else json.loads(r["params"]))
            row = live.get(p.mint)
            act = decide(p, row, now, params.exit, self.narrative_cold(p.narrative, params.exit.narrative_cool_rank),
                         stopping=r["bot_status"] == "stopping")
            if act and await self.sell(p, act[0], act[1], row):
                continue
            await store.q(self.db, """UPDATE paper_positions SET last_price = :lp, peak_price = :pk, last_ts = now(),
                state = CAST(:st AS jsonb) WHERE id = :id""", lp=p.last_price, pk=p.peak_price,
                          st=store.js(p.state()), id=p.id)
        return len(rows)

    async def finish_stopping(self) -> None:
        """Bots asked to stop whose positions are all sold: return their cash to the owner's account."""
        done = await store.q(self.db, """UPDATE paper_bots b SET status = 'stopped', stopped_at = now(),
            updated_at = now() WHERE status = 'stopping' AND NOT EXISTS (SELECT 1 FROM paper_positions p
            WHERE p.bot_id = b.id AND p.status = 'open') RETURNING id, user_id, cash_usd""")
        for b in done:
            if b["user_id"] is not None:
                await store.q(self.db, "UPDATE paper_accounts SET cash_usd = cash_usd + :c WHERE user_id = :u",
                              c=b["cash_usd"], u=b["user_id"])
            await store.event(self.db, b["id"], "stopped", {"cash_returned": round(b["cash_usd"], 2)})

    async def buy(self, bot: dict[str, Any], r: dict[str, Any], params: Params) -> bool:
        size = float(bot["position_usd"])
        key = (r["mint"], int(size))
        if self.quotes.skip.get(key, 0) > time.time():
            return False
        sol_in = size / self.sol_usd
        got = await self.quotes.out(M.SOL_MINT, r["mint"], int(sol_in * 1e9))
        if not got:
            return False
        back = await self.quotes.out(r["mint"], M.SOL_MINT, got)
        if back is None:
            return False
        rt_cost = (1 - back / 1e9 / sol_in) * 100                 # round trip: what an instant exit would lose
        if rt_cost > params.max_cost_pct:
            self.quotes.skip[key] = time.time() + 1800
            await store.event(self.db, bot["id"], "skip_cost",
                              {"symbol": r["symbol"], "round_trip_pct": round(rt_cost, 2)})
            return False
        tokens = got / 10 ** int(r["decimals"])
        fee = TX_SOL * self.sol_usd
        entry_row = {k: r.get(k) for k in ("mcap", "liquidity", "age_h", "holders", "chg_5m", "chg_1h", "chg_6h",
                                           "chg_24h", "buy_ratio_1h", "ds_txn_ratio_1h", "momentum", "safety",
                                           "organic_score", "top10_pct", "lists")}
        entry_row["flags"] = [f["code"] for f in r.get("flags") or []]
        pos = await store.q(self.db, """INSERT INTO paper_positions (bot_id, mint, symbol, name, narrative, tier,
            decimals, entry_price, entry_liquidity, usd_in, sol_in, tokens, tokens_left, peak_price, last_price,
            costs_usd, entry_cost_pct, entry_row) VALUES (:b, :m, :s, :n, :nar, :tier, :d, :px, :liq, :usd, :sol,
            :tok, :tok, :px, :px, :fee, :rt, CAST(:er AS jsonb)) RETURNING id""",
                            b=bot["id"], m=r["mint"], s=r["symbol"], n=r["name"], nar=r.get("narrative"),
                            tier=r.get("tier"), d=int(r["decimals"]), px=float(r["price"]),
                            liq=float(r.get("liquidity") or 0), usd=size, sol=sol_in, tok=tokens, fee=fee, rt=rt_cost,
                            er=store.js(entry_row))
        await store.q(self.db, "UPDATE paper_bots SET cash_usd = cash_usd - :c, updated_at = now() WHERE id = :b",
                      c=size + fee, b=bot["id"])
        await store.event(self.db, bot["id"], "open", {"symbol": r["symbol"], "narrative": r.get("narrative"),
                                                       "price": r["price"], "usd": size,
                                                       "round_trip_pct": round(rt_cost, 2)}, pos[0]["id"])
        log.info("bot %s open %s $%.0f (round trip %.2f%%)", bot["id"], r["symbol"], size, rt_cost)
        return True

    async def open_new(self, u: dict[str, Any]) -> int:
        if self.paused or not self.sol_usd:
            return 0
        bots = await store.q(self.db, """SELECT b.*, (SELECT count(*) FROM paper_positions p WHERE p.bot_id = b.id
            AND p.status = 'open') AS n_open FROM paper_bots b WHERE b.status = 'running' ORDER BY random()""")
        opened = 0
        for bot in bots:
            if bot["n_open"] >= bot["max_open"] or bot["cash_usd"] < bot["position_usd"] + 1:
                continue
            params = Params.model_validate(bot["params"] if isinstance(bot["params"], dict)
                                           else json.loads(bot["params"]))
            recent = {x["mint"] for x in await store.q(self.db, """SELECT mint FROM paper_positions WHERE bot_id = :b
                AND (status = 'open' OR closed_at > now() - make_interval(secs => :cd)
                     OR (exit_reason = ANY(:crash) AND closed_at > now() - interval '24 hours'))""",
                                                      b=bot["id"], cd=params.cooldown_h * 3600,
                                                      crash=list(CRASH_EXITS))}
            for r in candidates(u["tokens"], params, u["heat"])[:5]:
                if r["mint"] in recent:
                    continue
                if await self.buy(bot, r, params):
                    opened += 1
                    break                                   # one new position per bot per refresh
        return opened

    # ------------------------------------------------------------------ loop
    async def tick(self) -> dict[str, Any]:
        self.quotes.used = 0
        await self.refresh_sol()
        u = await universe.get(self.redis, self.client, self.pools, max_age=110)
        fresh = u["at"] != self.universe_at
        if fresh:
            self.universe_at = u["at"]
            hot = {h["narrative"]: h["rank"] for h in u["heat"]}
            for n in set(self.ranks) | set(hot):
                self.ranks[n] = ([*self.ranks.get(n, []), hot.get(n, 99)])[-3:]
            try:
                await store.save_snapshots(self.db, u["tokens"])
            except Exception:
                log.exception("snapshots")
        n_open = await self.step_open()
        await self.finish_stopping()
        opened = await self.open_new(u) if fresh else 0
        return {"ts": time.time(), "universe_at": u["at"], "tokens": len(u["tokens"]), "open_positions": n_open,
                "opened": opened, "sol_usd": self.sol_usd, "paused": self.paused, "quotes": self.quotes.used}

    async def run(self) -> None:
        await auth.ensure_schema(self.db)
        await store.ensure_schema(self.db)
        await self.ensure_house_bots()
        log.info("paper meme bot started (virtual money only)")
        while True:
            t0 = time.time()
            try:
                hb = await self.tick()
                await self.redis.set("paper:heartbeat", json.dumps(hb), ex=600)
                await asyncio.to_thread(HEARTBEAT.touch)
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(max(5.0, TICK_S - (time.time() - t0)))


async def main() -> None:
    cfg = get_settings()
    engine = create_async_engine(cfg.database_url, pool_size=4, max_overflow=2, pool_pre_ping=True)
    redis = Redis.from_url(cfg.redis_url, decode_responses=True)
    async with httpx.AsyncClient(headers={"User-Agent": "lp-dash-paper/0.1"}) as client:
        await Runner(cfg, engine, redis, client).run()


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())


if __name__ == "__main__":
    run()
