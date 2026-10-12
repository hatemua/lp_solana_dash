"""Users' bots (lp_bots, created from the dashboard): each follows one house strategy, on paper or with real money.

When a house position of the strategy opens, every running bot that follows it opens one too (if it has a free slot and
is not stopped for the day). The house position's net % is the signal: a bot closes on its own take profit or stop
loss, or when the house position closes, whichever comes first. A paper bot's P&L is its size x that net %.

A real-money bot ('live') opens a real Meteora spot position from its own wallet; when the house position starts
unwinding, the real one unwinds too. Its P&L is measured from the wallet's SOL balance around each step. The executor
derives each bot's wallet key from a master seed (never stored) and alone signs; this service never sees a key.
"""

import asyncio
import logging
from typing import Any

import httpx

from .config import Settings
from .engine import Position

log = logging.getLogger("lp_bot.follow")
ACTIVE = ("opening", "open", "unwinding", "closing", "close_retry")
MAX_SIDE = 34                                         # one position account holds at most 69 bins


def decide(net: float, take_profit: float | None, stop_loss: float | None) -> str | None:
    """The bot's own exits on the house position's net %: take profit at +tp %, stop loss at -sl %."""
    if take_profit is not None and net >= take_profit:
        return "take_profit"
    if stop_loss is not None and net <= -stop_loss:
        return "stop_loss"
    return None


def day_stop(b: dict[str, Any], day_sol: float) -> str | None:
    if b["day_loss_sol"] and day_sol <= -b["day_loss_sol"]:
        return f"daily loss limit ({day_sol:+.4f} SOL today)"
    if b["day_target_sol"] and day_sol >= b["day_target_sol"]:
        return f"daily target reached ({day_sol:+.4f} SOL today)"
    return None


class Follow:
    def __init__(self, cfg: Settings, db: Any, client: httpx.AsyncClient) -> None:
        self.cfg, self.db, self.client = cfg, db, client
        self.lock = asyncio.Lock()                     # one wallet operation at a time, in order
        self.olock = asyncio.Lock()                    # paper opens: slot counts stay right
        self.tasks: set[asyncio.Task[None]] = set()
        self.shapes = {s.strip() for s in cfg.live_shapes.split(",") if s.strip()}
        self.bots: list[dict[str, Any]] = []
        self.closing: set[int] = set()                 # rows with a close queued or running

    # --- plumbing
    def spawn(self, coro: Any) -> None:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def call(self, path: str, body: dict[str, Any] | None = None,
                   params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.cfg.executor_url}{path}"
        r = await (self.client.post(url, json=body, timeout=240) if body is not None
                   else self.client.get(url, params=params, timeout=30))
        data: dict[str, Any] = r.json()
        if r.status_code != 200:
            raise RuntimeError(f"{path}: {r.status_code} {data.get('error')}")
        return data

    async def bot(self, bot_id: int) -> dict[str, Any] | None:
        r = await self.db.fetchrow("SELECT * FROM lp_bots WHERE id = $1", bot_id)
        return dict(r) if r else None

    async def save(self, rid: int, out: dict[str, Any] | None = None, **cols: Any) -> None:
        txs = [t for t in (out or {}).get("txs", []) if t]
        flow = float(out.get("sol_delta", 0)) if out else 0.0
        sets = ", ".join(f"{k} = ${i + 4}" for i, k in enumerate(cols))
        await self.db.execute(
            f"UPDATE lp_bot_positions SET txs = txs || $2::jsonb, sol_flow = sol_flow + $3"
            f"{', ' + sets if sets else ''} WHERE id = $1", rid, txs, flow, *cols.values())

    async def skip(self, b: dict[str, Any], p: Position, why: str) -> None:
        log.info("bot %s skips %s: %s", b["id"], p.name, why)
        await self.db.execute("INSERT INTO bot_events (strategy, position_id, kind, detail) VALUES ($1, $2, "
                              "'follow_skip', $3)", p.strategy, p.id, {"bot_id": b["id"], "why": why})

    async def day_sol(self, bot_id: int) -> float:
        """Today's (UTC) closed result plus the open positions' current estimate, in SOL."""
        return float(await self.db.fetchval(
            "SELECT COALESCE(sum(CASE WHEN status = 'closed' THEN pnl_sol ELSE sol_size * COALESCE(net_pct, 0) / 100 "
            "END), 0) FROM lp_bot_positions WHERE bot_id = $1 AND (status = ANY($2) "
            "OR (status = 'closed' AND closed_at >= date_trunc('day', now())))", bot_id, list(ACTIVE)) or 0)

    # --- startup and every tick
    async def recover(self) -> None:
        """After a restart: an interrupted close is retried; an interrupted open needs a look at the wallet."""
        await self.db.execute("UPDATE lp_bot_positions SET status = 'close_retry' WHERE status = 'closing'")
        await self.db.execute("UPDATE lp_bot_positions SET status = 'check', error = 'interrupted while opening: "
                              "check the bot wallet' WHERE status = 'opening'")

    async def tick(self) -> dict[str, Any]:
        """Run after the house positions were stepped: mark the followers, apply their exits, owner switches."""
        self.bots = [dict(r) for r in await self.db.fetch("SELECT * FROM lp_bots WHERE status <> 'stopped'")]
        by_id = {b["id"]: b for b in self.bots}
        rows = await self.db.fetch(
            "SELECT f.*, h.status AS house_status, h.net_pct AS house_net, h.exit_reason AS house_reason "
            "FROM lp_bot_positions f JOIN bot_positions h ON h.id = f.paper_id WHERE f.status = ANY($1)",
            ["open", "unwinding"])
        for r in map(dict, rows):
            b = by_id.get(r["bot_id"])
            if not b:
                continue
            net = float(r["house_net"] or 0)
            peak = max(float(r["peak_pct"] if r["peak_pct"] is not None else net), net)
            reason = (decide(net, b["take_profit_pct"], b["stop_loss_pct"])
                      or ((r["house_reason"] or "house_closed") if r["house_status"] == "closed" else None)
                      or ("owner_close_all" if b["close_all"] else None)
                      or ("bot_stopped" if b["status"] == "stopping" else None))
            if b["mode"] == "paper" and reason:
                await self.db.execute(
                    "UPDATE lp_bot_positions SET status = 'closed', closed_at = now(), net_pct = $2, peak_pct = $3, "
                    "pnl_sol = sol_size * $2 / 100, exit_reason = $4 WHERE id = $1", r["id"], net, peak, reason)
                log.info("bot %s closes %s %s net %+.2f%%", b["id"], r["name"], reason, net)
                continue
            await self.db.execute("UPDATE lp_bot_positions SET net_pct = $2, peak_pct = $3 WHERE id = $1",
                                  r["id"], net, peak)
            if reason:
                self.queue_close(r, reason)
        if not self.lock.locked():
            for r in await self.db.fetch("SELECT * FROM lp_bot_positions WHERE status = 'close_retry'"):
                self.queue_close(dict(r), r["exit_reason"] or "retry")
        if any(b["close_all"] for b in self.bots):
            await self.db.execute("UPDATE lp_bots SET close_all = false, updated_at = now(), status = CASE WHEN "
                                  "status = 'running' THEN 'paused' ELSE status END WHERE close_all")
        await self.db.execute(
            "UPDATE lp_bots SET status = 'stopped', updated_at = now() WHERE status = 'stopping' AND NOT EXISTS "
            "(SELECT 1 FROM lp_bot_positions f WHERE f.bot_id = lp_bots.id AND f.status = ANY($1))", list(ACTIVE))
        n_open = await self.db.fetchval("SELECT count(*) FROM lp_bot_positions WHERE status = ANY($1)", list(ACTIVE))
        return {"bots": len(self.bots), "open": int(n_open or 0)}

    # --- house position hooks
    def on_paper_open(self, p: Position) -> None:
        for b in self.bots:
            if b["strategy"] == p.strategy and b["status"] == "running" and not b["close_all"]:
                self.spawn(self.open(b, p))

    def on_paper_unwind(self, p: Position) -> None:
        self.spawn(self.unwind(p))

    async def open(self, b: dict[str, Any], p: Position) -> None:
        if b["mode"] == "paper":
            async with self.olock:
                return await self.open_paper(b, p)
        async with self.lock:
            return await self.open_live(b, p)

    async def checks(self, b: dict[str, Any], p: Position) -> bool:
        n_open = await self.db.fetchval("SELECT count(*) FROM lp_bot_positions WHERE bot_id = $1 AND status = ANY($2)",
                                        b["id"], list(ACTIVE))
        if n_open >= b["max_open"]:
            await self.skip(b, p, f"{n_open} position(s) open, the bot's maximum")
            return False
        stop = day_stop(b, await self.day_sol(b["id"]))
        if stop:
            await self.skip(b, p, stop)
            return False
        return True

    async def open_paper(self, b: dict[str, Any], p: Position) -> None:
        if not await self.checks(b, p):
            return
        net = p.net_pct(p.last_price)
        await self.db.execute(
            "INSERT INTO lp_bot_positions (bot_id, paper_id, pool, name, mint, shape, status, sol_size, net_pct, "
            "peak_pct) VALUES ($1, $2, $3, $4, $5, $6, 'open', $7, $8, $8)", b["id"], p.id, p.pool, p.name,
            p.info.get("mint"), p.info.get("shape"), b["position_sol"], net)
        log.info("bot %s opens %s (paper, %.3f SOL)", b["id"], p.name, b["position_sol"])

    async def open_live(self, b: dict[str, Any], p: Position) -> None:
        shape = str(p.info.get("shape", ""))
        if not self.cfg.live_enabled:
            return await self.skip(b, p, "real-money trading is switched off on the server")
        if shape not in self.shapes:
            return await self.skip(b, p, f"shape {shape or '?'} is not traded with real money")
        if not await self.checks(b, p):
            return None
        who = {"bot_id": b["id"], "owner": b["owner_wallet"]}
        try:
            w = await self.call("/v1/bot/wallet", params=who)
        except Exception as e:
            return await self.skip(b, p, f"wallet: {e}")
        size = float(b["position_sol"])
        need = size + self.cfg.live_reserve_sol
        if (w.get("sol") or 0) < need:
            return await self.skip(b, p, f"wallet has {w.get('sol')} SOL, needs {need:.3f}")
        each = min(MAX_SIDE, int(p.info.get("bins_each_side") or 25))
        rid = await self.db.fetchval(
            "INSERT INTO lp_bot_positions (bot_id, paper_id, pool, name, mint, shape, status, sol_size, net_pct, "
            "peak_pct) VALUES ($1, $2, $3, $4, $5, $6, 'opening', $7, $8, $8) RETURNING id", b["id"], p.id, p.pool,
            p.name, p.info.get("mint"), shape, size, p.net_pct(p.last_price))
        try:
            out = await self.call(f"/v1/bot/pools/{p.pool}/open", {**who, "sol": size, "each_side": each})
        except Exception as e:
            log.warning("bot %s live open %s failed: %s", b["id"], p.name, e)
            await self.save(rid, None, status="failed", error=str(e)[:500])
            return None
        await self.save(rid, out, status="open", position=out["position"])
        log.info("bot %s live open %s %.3f SOL +/-%d bins %s", b["id"], p.name, size, each, out["position"])
        return None

    async def unwind(self, p: Position) -> None:
        rows = await self.db.fetch("SELECT * FROM lp_bot_positions WHERE paper_id = $1 AND status = 'open' "
                                   "AND position IS NOT NULL", p.id)
        for r in map(dict, rows):
            b = await self.bot(r["bot_id"])
            if not b or b["mode"] != "live":
                continue
            async with self.lock:
                cur = await self.db.fetchval("SELECT status FROM lp_bot_positions WHERE id = $1", r["id"])
                if cur != "open":
                    continue
                try:
                    out = await self.call(f"/v1/bot/pools/{p.pool}/unwind", {
                        "bot_id": b["id"], "owner": b["owner_wallet"], "position": r["position"], "bins": 5})
                except Exception as e:                 # the house close (or TP/SL) will close it for real
                    log.warning("bot %s live unwind %s failed: %s", b["id"], p.name, e)
                    await self.save(r["id"], None, error=str(e)[:500])
                    continue
                await self.save(r["id"], out, status="unwinding", position=None,
                                unwind_position=out.get("position"))
                log.info("bot %s live unwind %s", b["id"], p.name)

    def queue_close(self, r: dict[str, Any], reason: str) -> None:
        if r["id"] not in self.closing:
            self.closing.add(r["id"])
            self.spawn(self.close_row(r, reason))

    async def close_row(self, r: dict[str, Any], reason: str) -> None:
        try:
            await self._close_row(r, reason)
        finally:
            self.closing.discard(r["id"])

    async def _close_row(self, r: dict[str, Any], reason: str) -> None:
        async with self.lock:
            cur = await self.db.fetchrow("SELECT status, sol_flow, position, unwind_position FROM lp_bot_positions "
                                         "WHERE id = $1", r["id"])
            b = await self.bot(r["bot_id"])
            if not cur or not b or cur["status"] not in ("open", "unwinding", "close_retry"):
                return
            await self.save(r["id"], None, status="closing", exit_reason=reason)
            positions = [x for x in (cur["position"], cur["unwind_position"]) if x]
            try:
                out = await self.call(f"/v1/bot/pools/{r['pool']}/close", {
                    "bot_id": b["id"], "owner": b["owner_wallet"], "positions": positions})
            except Exception as e:
                log.warning("bot %s live close %s failed (will retry): %s", b["id"], r["name"], e)
                await self.save(r["id"], None, status="close_retry", error=str(e)[:500])
                return
            pnl = float(cur["sol_flow"]) + float(out.get("sol_delta", 0))
            await self.save(r["id"], out, status="closed", pnl_sol=pnl, error=None)
            await self.db.execute("UPDATE lp_bot_positions SET closed_at = now() WHERE id = $1", r["id"])
            log.info("bot %s live close %s %s pnl %+.4f SOL", b["id"], r["name"], reason, pnl)
