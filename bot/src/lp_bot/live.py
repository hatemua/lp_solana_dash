"""Live trading: mirror one paper strategy with small real positions.

When a paper position of `live_strategy` opens, a real one opens in the same pool (spot shapes only); when the paper
one starts unwinding or closes, the real one does the same. Transactions are built and signed by the executor, which
alone holds the bot wallet key. Off unless BOT_LIVE=true.

Limits: `live_position_sol` per position, `live_max_open` at once, a daily loss stop, a SOL reserve, and the owner's
pause / close-all switch (bot_control 'live', written by the API after the owner wallet signs the request).
"""

import asyncio
import logging
from typing import Any

import httpx

from .config import Settings
from .engine import Position

log = logging.getLogger("lp_bot.live")
ACTIVE = ("opening", "open", "unwinding", "closing", "close_retry")
MAX_SIDE = 34                                         # one position account holds at most 69 bins


class Live:
    def __init__(self, cfg: Settings, db: Any, client: httpx.AsyncClient) -> None:
        self.cfg, self.db, self.client = cfg, db, client
        self.lock = asyncio.Lock()                     # one wallet operation at a time, in order
        self.tasks: set[asyncio.Task[None]] = set()
        self.shapes = {s.strip() for s in cfg.live_shapes.split(",") if s.strip()}
        self.paused: str | None = None

    @property
    def on(self) -> bool:
        return self.cfg.bot_live

    # --- plumbing
    def spawn(self, coro: Any) -> None:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def call(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.cfg.executor_url}{path}"
        r = await (self.client.post(url, json=body, timeout=240) if body is not None
                   else self.client.get(url, timeout=30))
        data: dict[str, Any] = r.json()
        if r.status_code != 200:
            raise RuntimeError(f"{path}: {r.status_code} {data.get('error')}")
        return data

    async def control(self) -> dict[str, Any]:
        v = await self.db.fetchval("SELECT value FROM bot_control WHERE key = 'live'")
        return v if isinstance(v, dict) else {}

    async def set_control(self, **kv: Any) -> None:
        await self.db.execute(
            "INSERT INTO bot_control (key, value) VALUES ('live', $1) ON CONFLICT (key) DO UPDATE "
            "SET value = bot_control.value || EXCLUDED.value, at = now()", kv)

    async def row(self, paper_id: int) -> dict[str, Any] | None:
        r = await self.db.fetchrow("SELECT * FROM live_positions WHERE paper_id = $1 AND status = ANY($2) "
                                   "ORDER BY id DESC LIMIT 1", paper_id, list(ACTIVE))
        return dict(r) if r else None

    async def save(self, rid: int, out: dict[str, Any] | None = None, **cols: Any) -> None:
        if out:
            cols.setdefault("txs_add", [t for t in out.get("txs", []) if t])
        txs = cols.pop("txs_add", [])
        flow = float(out.get("sol_delta", 0)) if out else 0.0
        sets = ", ".join(f"{k} = ${i + 4}" for i, k in enumerate(cols))
        await self.db.execute(
            f"UPDATE live_positions SET txs = txs || $2::jsonb, sol_flow = sol_flow + $3"
            f"{', ' + sets if sets else ''} WHERE id = $1", rid, txs, flow, *cols.values())

    # --- startup and every tick
    async def recover(self) -> None:
        """After a restart: an interrupted close is retried; an interrupted open needs a look at the wallet."""
        await self.db.execute("UPDATE live_positions SET status = 'close_retry' WHERE status = 'closing'")
        await self.db.execute("UPDATE live_positions SET status = 'check', error = 'interrupted while opening: "
                              "check the bot wallet' WHERE status = 'opening'")

    async def tick(self) -> dict[str, Any]:
        if not self.on:
            return {"live": False}
        ctl = await self.control()
        day = float(await self.db.fetchval(
            "SELECT COALESCE(sum(pnl_sol), 0) FROM live_positions WHERE closed_at >= date_trunc('day', now())") or 0)
        self.paused = ("paused by the owner" if ctl.get("paused") else
                       f"daily loss {day:+.3f} SOL" if day <= -self.cfg.live_daily_loss_sol else None)
        if ctl.get("close_all"):
            await self.set_control(close_all=False, paused=True)
            for r in await self.db.fetch("SELECT * FROM live_positions WHERE status = ANY($1)",
                                         ["open", "unwinding", "close_retry"]):
                self.spawn(self.close_row(dict(r), "owner_close_all"))
        elif not self.lock.locked():
            for r in await self.db.fetch("SELECT * FROM live_positions WHERE status = 'close_retry'"):
                self.spawn(self.close_row(dict(r), r["exit_reason"] or "retry"))
        return {"live": True, "paused": self.paused, "day_pnl_sol": round(day, 4)}

    # --- mirror the paper position
    def on_paper_open(self, p: Position) -> None:
        if self.on and p.strategy == self.cfg.live_strategy:
            self.spawn(self.open(p))

    def on_paper_unwind(self, p: Position) -> None:
        if self.on and p.strategy == self.cfg.live_strategy:
            self.spawn(self.unwind(p))

    def on_paper_close(self, p: Position, reason: str) -> None:
        if self.on and p.strategy == self.cfg.live_strategy:
            self.spawn(self.close_paper(p, reason))

    async def skip(self, p: Position, why: str) -> None:
        log.info("live skip %s: %s", p.name, why)
        await self.db.execute("INSERT INTO bot_events (strategy, position_id, kind, detail) VALUES ($1, $2, "
                              "'live_skip', $3)", p.strategy, p.id, {"why": why})

    async def open(self, p: Position) -> None:
        async with self.lock:
            shape = str(p.info.get("shape", ""))
            if shape not in self.shapes:
                return await self.skip(p, f"shape {shape or '?'} is not traded live")
            if self.paused:
                return await self.skip(p, f"paused: {self.paused}")
            n_open = await self.db.fetchval("SELECT count(*) FROM live_positions WHERE status = ANY($1)", list(ACTIVE))
            if n_open >= self.cfg.live_max_open:
                return await self.skip(p, f"{n_open} live position(s) open")
            try:
                w = await self.call("/v1/bot/wallet")
            except Exception as e:
                return await self.skip(p, f"wallet: {e}")
            size = self.cfg.live_position_sol
            if not w.get("enabled") or (w.get("sol") or 0) < size + self.cfg.live_reserve_sol:
                return await self.skip(p, f"wallet has {w.get('sol')} SOL, needs {size + self.cfg.live_reserve_sol}")
            each = min(MAX_SIDE, int(p.info.get("bins_each_side") or 25))
            rid = await self.db.fetchval(
                "INSERT INTO live_positions (paper_id, strategy, pool, name, mint, status, sol_size) VALUES "
                "($1, $2, $3, $4, $5, 'opening', $6) RETURNING id", p.id, p.strategy, p.pool, p.name,
                p.info.get("mint"), size)
            try:
                out = await self.call(f"/v1/bot/pools/{p.pool}/open", {"sol": size, "each_side": each})
            except Exception as e:
                log.warning("live open %s failed: %s", p.name, e)
                await self.save(rid, None, status="failed", error=str(e)[:500])
                return None
            await self.save(rid, out, status="open", position=out["position"])
            log.info("live open %s %.3f SOL +/-%d bins %s", p.name, size, each, out["position"])
        return None

    async def unwind(self, p: Position) -> None:
        async with self.lock:
            r = await self.row(p.id or 0)
            if not r or r["status"] != "open":
                return
            try:
                out = await self.call(f"/v1/bot/pools/{p.pool}/unwind", {"position": r["position"], "bins": 5})
            except Exception as e:                     # the paper twin's close will close it for real
                log.warning("live unwind %s failed: %s", p.name, e)
                await self.save(r["id"], None, error=str(e)[:500])
                return
            await self.save(r["id"], out, status="unwinding", position=None, unwind_position=out.get("position"))
            log.info("live unwind %s", p.name)

    async def close_paper(self, p: Position, reason: str) -> None:
        r = await self.row(p.id or 0)
        if r and r["status"] in ("open", "unwinding", "close_retry"):
            await self.close_row(r, reason)

    async def close_row(self, r: dict[str, Any], reason: str) -> None:
        async with self.lock:
            cur = await self.db.fetchrow("SELECT status, sol_flow FROM live_positions WHERE id = $1", r["id"])
            if not cur or cur["status"] not in ("open", "unwinding", "close_retry"):
                return
            await self.save(r["id"], None, status="closing", exit_reason=reason)
            positions = [x for x in (r["position"], r["unwind_position"]) if x]
            try:
                out = await self.call(f"/v1/bot/pools/{r['pool']}/close", {"positions": positions})
            except Exception as e:
                log.warning("live close %s failed (will retry): %s", r["name"], e)
                await self.save(r["id"], None, status="close_retry", error=str(e)[:500])
                return
            pnl = float(cur["sol_flow"]) + float(out.get("sol_delta", 0))
            await self.save(r["id"], out, status="closed", pnl_sol=pnl, error=None)
            await self.db.execute("UPDATE live_positions SET closed_at = now() WHERE id = $1", r["id"])
            log.info("live close %s %s pnl %+.4f SOL", r["name"], reason, pnl)
