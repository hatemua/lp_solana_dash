"""Users' LP bots: create a bot that follows one house strategy, on paper or with real money, with its own take profit,
stop loss and daily limits. The bot service (follow.py) runs them; the executor derives each real bot's wallet.

Who can do what:
- the signed-in user (web session): create, change settings, pause / resume, stop and delete a paper bot;
- an OAuth connection with the bots:suggest scope (Claude through the MCP): suggest take profit / stop loss. A paper bot
  takes it at once; a real-money bot keeps it as pending until the user clicks Apply;
- the bot's owner wallet, by signing a fresh message (no transaction): create a real-money bot, close all, withdraw
  (only to that wallet), delete a real-money bot. Real money is for allowlisted wallets only (LIVE_WALLETS).
"""

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from .auth import BOTS_SCOPE, request_user
from .config import Settings

ADDR = r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"
MAX_AGE_S = 120
MAX_BOTS = 10
LIVE_STRATEGIES = ("auto",)            # real money: strategies that open spot positions (the executor's shape)
ACTIVE = ("opening", "open", "unwinding", "closing", "close_retry")


class Signed(BaseModel):
    pubkey: str = Field(pattern=ADDR)
    ts: int
    signature: str = Field(max_length=200)     # base64 ed25519 signature of the action's message


class Limits(BaseModel):
    take_profit_pct: float | None = Field(None, ge=1, le=100)
    stop_loss_pct: float | None = Field(None, ge=1, le=50)
    position_sol: float = Field(0.1, gt=0, le=50)
    max_open: int = Field(1, ge=1, le=5)
    day_loss_sol: float | None = Field(None, gt=0, le=50)
    day_target_sol: float | None = Field(None, gt=0, le=100)


class CreateBody(Limits):
    name: str = Field(min_length=1, max_length=40)
    strategy: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    mode: str = Field("paper", pattern="^(paper|live)$")
    wallet: Signed | None = None                # live: the owner wallet's signature of create_message(name, ts)


class SettingsBody(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=40)
    take_profit_pct: float | None = Field(None, ge=1, le=100)
    stop_loss_pct: float | None = Field(None, ge=1, le=50)
    position_sol: float | None = Field(None, gt=0, le=50)
    max_open: int | None = Field(None, ge=1, le=5)
    day_loss_sol: float | None = Field(None, gt=0, le=50)
    day_target_sol: float | None = Field(None, gt=0, le=100)
    apply_suggestion: bool = False


class ActionBody(BaseModel):
    action: str = Field(pattern="^(pause|resume|stop|delete)$")


class OwnerBody(Signed):
    action: str = Field(pattern="^(close_all|withdraw|delete)$")


class SuggestBody(BaseModel):
    take_profit_pct: float | None = Field(None, ge=1, le=100)
    stop_loss_pct: float | None = Field(None, ge=1, le=50)
    reason: str = Field("", max_length=400)


def create_message(name: str, ts: int) -> str:
    return f'LP Dash: create real-money bot "{name}" at {ts}'


def owner_message(bot_id: int, action: str, ts: int) -> str:
    return f"LP Dash bot {bot_id}: {action} at {ts}"     # the executor checks the same text for withdraw


def strategy_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Closed house positions of one strategy (net %) -> the numbers that help pick it and set TP / SL."""
    nets = sorted(float(r["net_pct"] or 0) for r in rows)
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]

    def median(xs: list[float]) -> float | None:
        return round(xs[len(xs) // 2], 2) if xs else None

    return {"closed": len(nets), "win_rate": round(len(wins) / len(nets) * 100, 1) if nets else None,
            "avg_net_pct": round(sum(nets) / len(nets), 2) if nets else None,
            "median_win_pct": median(wins), "median_loss_pct": median(losses),
            "best_pct": round(nets[-1], 2) if nets else None, "worst_pct": round(nets[0], 2) if nets else None,
            "pnl_usd": round(sum(float(r["pnl_usd"] or 0) for r in rows), 2)}


def router(cfg: Settings, state: dict[str, Any], rows: Callable[..., Awaitable[list[dict[str, Any]]]],
           executor: Callable[..., Awaitable[Any]]) -> APIRouter:
    r = APIRouter(prefix="/v1/lpbots")

    def engine() -> AsyncEngine:
        return state["engine"]

    async def write(sql: str, **params: Any) -> list[dict[str, Any]]:
        async with engine().begin() as conn:
            res = await conn.execute(text(sql), params)
            return [dict(x._mapping) for x in res] if res.returns_rows else []

    async def safe_rows(sql: str, **params: Any) -> list[dict[str, Any]]:
        try:
            return await rows(sql, **params)
        except Exception as e:                      # tables are created by the bot service on its start
            if "does not exist" in str(e):
                return []
            raise

    async def own(uid: int, bot_id: int) -> dict[str, Any]:
        found = await safe_rows("SELECT * FROM lp_bots WHERE id = :i AND user_id = :u", i=bot_id, u=uid)
        if not found:
            raise HTTPException(404, "bot not found")
        return found[0]

    async def house() -> tuple[list[str], dict[str, str]]:
        hb = await state["redis"].get("bot:heartbeat")
        d = json.loads(hb) if hb else {}
        return list(d.get("strategies") or []), dict(d.get("labels") or {})

    async def signed_by(s: Signed, message: str) -> None:
        if abs(time.time() - s.ts) > MAX_AGE_S:
            raise HTTPException(400, "request expired: sign again")
        v = await executor("POST", "/v1/bot/verify", {"pubkey": s.pubkey, "message": message,
                                                      "signature": s.signature})
        if not v.get("ok"):
            raise HTTPException(403, v.get("error") or "bad signature")
        if not await state["redis"].set(f"lpbots:sig:{s.signature}", "1", nx=True, ex=MAX_AGE_S * 2):
            raise HTTPException(409, "this signature was already used")

    async def n_active(bot_id: int) -> int:
        n = await safe_rows("SELECT count(*) AS n FROM lp_bot_positions WHERE bot_id = :b AND status = ANY(:s)",
                            b=bot_id, s=list(ACTIVE))
        return int(n[0]["n"]) if n else 0

    async def wallet_of(b: dict[str, Any]) -> dict[str, Any] | None:
        if b["mode"] != "live":
            return None
        try:
            return dict(await executor("GET", f"/v1/bot/wallet?bot_id={b['id']}&owner={b['owner_wallet']}"))
        except HTTPException as e:
            return {"error": e.detail}

    async def view(b: dict[str, Any]) -> dict[str, Any]:
        pos = await safe_rows(
            "SELECT f.id, f.paper_id, f.pool, f.name, f.shape, f.status, f.opened_at, f.closed_at, f.sol_size, "
            "f.net_pct, f.peak_pct, f.exit_reason, f.pnl_sol, f.txs, f.error, h.exit_reason AS house_exit "
            "FROM lp_bot_positions f LEFT JOIN bot_positions h ON h.id = f.paper_id WHERE f.bot_id = :b "
            "ORDER BY f.id DESC LIMIT 30", b=b["id"])
        tot = await safe_rows(
            "SELECT count(*) FILTER (WHERE status = 'closed') AS closed, "
            "count(*) FILTER (WHERE status = 'closed' AND pnl_sol > 0) AS wins, "
            "COALESCE(sum(pnl_sol) FILTER (WHERE status = 'closed'), 0) AS pnl_sol, "
            "count(*) FILTER (WHERE status = ANY(:s)) AS open, "
            "COALESCE(sum(CASE WHEN status = 'closed' THEN pnl_sol ELSE sol_size * COALESCE(net_pct, 0) / 100 END) "
            "FILTER (WHERE status = ANY(:s) OR (status = 'closed' AND closed_at >= date_trunc('day', now()))), 0) "
            "AS day_sol FROM lp_bot_positions WHERE bot_id = :b", b=b["id"], s=list(ACTIVE))
        skip = await safe_rows(
            "SELECT ts, detail->>'why' AS why FROM bot_events WHERE kind = 'follow_skip' "
            "AND (detail->>'bot_id')::bigint = :b AND ts > now() - interval '1 day' ORDER BY ts DESC LIMIT 1",
            b=b["id"])
        return {**{k: v for k, v in b.items() if k != "user_id"}, "positions": pos,
                "totals": tot[0] if tot else {}, "last_skip": skip[0] if skip else None,
                "wallet": await wallet_of(b)}

    # ------------------------------------------------------------------ read
    @r.get("/strategies")
    async def strategies() -> dict[str, Any]:
        """The house strategies a bot can follow, with their last 7 days (closed positions)."""
        names, labels = await house()
        closed = await safe_rows("SELECT strategy, net_pct, pnl_usd FROM bot_positions WHERE status = 'closed' "
                                 "AND closed_at > now() - interval '7 days' AND strategy = ANY(:n)", n=names)
        return {"strategies": [{"name": n, "label": labels.get(n, n), "live_ok": n in LIVE_STRATEGIES,
                                "last_7d": strategy_stats([c for c in closed if c["strategy"] == n])} for n in names]}

    @r.get("")
    async def my_bots(request: Request) -> dict[str, Any]:
        uid = await request_user(engine(), cfg, request)
        bots = await safe_rows("SELECT * FROM lp_bots WHERE user_id = :u ORDER BY id DESC", u=uid)
        return {"bots": [await view(b) for b in bots]}

    @r.get("/access")
    async def access(owner: str = "") -> dict[str, Any]:
        """May this wallet create real-money bots?"""
        if not owner:
            return {"configured": False, "live_enabled": False, "allowed": False}
        try:
            return dict(await executor("GET", f"/v1/bot/access?owner={owner[:60]}"))
        except HTTPException as e:
            return {"configured": False, "live_enabled": False, "allowed": False, "error": e.detail}

    # ------------------------------------------------------------------ create and change
    @r.post("")
    async def create(body: CreateBody, request: Request) -> dict[str, Any]:
        uid = await request_user(engine(), cfg, request, write=True)
        names, _ = await house()
        if body.strategy not in names:
            raise HTTPException(400, f"unknown strategy; choose one of {', '.join(names) or '(bot service starting)'}")
        owner = None
        if body.mode == "live":
            if body.strategy not in LIVE_STRATEGIES:
                raise HTTPException(400, f"real money follows only {', '.join(LIVE_STRATEGIES)} for now")
            if not body.wallet:
                raise HTTPException(400, "sign the request with your wallet to create a real-money bot")
            if body.position_sol > 2:
                raise HTTPException(400, "real-money positions are at most 2 SOL")
            await signed_by(body.wallet, create_message(body.name, body.wallet.ts))
            acc = await executor("GET", f"/v1/bot/access?owner={body.wallet.pubkey}")
            if not acc.get("configured") or not acc.get("allowed"):
                raise HTTPException(403, "real money is invite-only for now: this wallet is not on the list")
            owner = body.wallet.pubkey
        n = await safe_rows("SELECT count(*) AS n FROM lp_bots WHERE user_id = :u AND status <> 'stopped'", u=uid)
        if n and n[0]["n"] >= MAX_BOTS:
            raise HTTPException(400, f"at most {MAX_BOTS} active bots")
        try:
            made = await write(
                "INSERT INTO lp_bots (user_id, name, strategy, mode, owner_wallet, position_sol, max_open, "
                "take_profit_pct, stop_loss_pct, day_loss_sol, day_target_sol) VALUES (:u, :n, :s, :m, :o, :ps, :mo, "
                ":tp, :sl, :dl, :dt) RETURNING *", u=uid, n=body.name, s=body.strategy, m=body.mode, o=owner,
                ps=body.position_sol, mo=body.max_open, tp=body.take_profit_pct, sl=body.stop_loss_pct,
                dl=body.day_loss_sol, dt=body.day_target_sol)
        except Exception as e:
            if "does not exist" in str(e):
                raise HTTPException(503, "the bot service is starting: try again in a minute") from e
            raise
        return await view(made[0])

    @r.post("/{bot_id}/settings")
    async def settings(body: SettingsBody, request: Request, bot_id: int = Path(ge=1)) -> dict[str, Any]:
        uid = await request_user(engine(), cfg, request, write=True)
        b = await own(uid, bot_id)
        ch = body.model_dump(exclude_unset=True)
        ch.pop("apply_suggestion", None)
        if body.apply_suggestion and b["pending"]:
            p = b["pending"]
            ch.update({k: p.get(k) for k in ("take_profit_pct", "stop_loss_pct") if k in p})
        if b["mode"] == "live" and (ch.get("position_sol") or 0) > 2:
            raise HTTPException(400, "real-money positions are at most 2 SOL")
        for k in ("position_sol", "max_open", "name"):
            if k in ch and ch[k] is None:
                ch.pop(k)
        if not ch and not body.apply_suggestion:
            raise HTTPException(400, "nothing to change")
        sets = ", ".join(f"{k} = :{k}" for k in ch)
        clear = ", pending = NULL" if body.apply_suggestion else ""
        await write(f"UPDATE lp_bots SET {sets + ', ' if sets else ''}updated_at = now(){clear} WHERE id = :id",
                    id=bot_id, **ch)
        return await view(await own(uid, bot_id))

    @r.post("/{bot_id}/action")
    async def action(body: ActionBody, request: Request, bot_id: int = Path(ge=1)) -> dict[str, Any]:
        uid = await request_user(engine(), cfg, request, write=True)
        b = await own(uid, bot_id)
        if b["mode"] == "live" and body.action in ("stop", "delete"):
            raise HTTPException(400, "a real-money bot is closed or deleted with your wallet's signature")
        if body.action == "delete":
            if b["status"] != "stopped":
                raise HTTPException(400, "stop the bot first")
            await write("DELETE FROM lp_bots WHERE id = :i", i=bot_id)
            return {"ok": True, "deleted": bot_id}
        allowed = {"pause": ("running",), "resume": ("paused",), "stop": ("running", "paused")}[body.action]
        if b["status"] not in allowed:
            raise HTTPException(400, f"cannot {body.action} a {b['status']} bot")
        new = {"pause": "paused", "resume": "running", "stop": "stopping"}[body.action]
        await write("UPDATE lp_bots SET status = :s, updated_at = now() WHERE id = :i", s=new, i=bot_id)
        return await view(await own(uid, bot_id))

    # ------------------------------------------------------------------ owner wallet (real money)
    @r.post("/{bot_id}/fund-tx")
    async def fund_tx(request: Request, bot_id: int = Path(ge=1)) -> Any:
        """Unsigned SOL transfer from the owner wallet to the bot wallet; the owner signs it in their wallet."""
        uid = await request_user(engine(), cfg, request, write=True)
        b = await own(uid, bot_id)
        if b["mode"] != "live":
            raise HTTPException(400, "a paper bot needs no money")
        body = await request.json()
        return await executor("POST", "/v1/bot/fund-tx", {"bot_id": bot_id, "owner": b["owner_wallet"],
                                                          "sol": float(body.get("sol") or 0)})

    @r.post("/{bot_id}/owner")
    async def owner(body: OwnerBody, request: Request, bot_id: int = Path(ge=1)) -> dict[str, Any]:
        uid = await request_user(engine(), cfg, request, write=True)
        b = await own(uid, bot_id)
        if b["mode"] != "live" or body.pubkey != b["owner_wallet"]:
            raise HTTPException(403, "only the bot's owner wallet can do this")
        message = owner_message(bot_id, body.action, body.ts)
        if body.action == "withdraw":               # the executor verifies the signature too; we make it single-use
            if await n_active(bot_id):
                raise HTTPException(409, "close the positions first (Close all), then withdraw")
            await signed_by(body, message)
            return dict(await executor("POST", "/v1/bot/withdraw", {"bot_id": bot_id, "owner": b["owner_wallet"],
                                                                    "ts": body.ts, "signature": body.signature}))
        await signed_by(body, message)
        if body.action == "close_all":
            await write("UPDATE lp_bots SET close_all = true, updated_at = now() WHERE id = :i", i=bot_id)
            return {"ok": True, "action": "close_all"}
        if await n_active(bot_id):
            raise HTTPException(409, "close the positions first (Close all)")
        w = await wallet_of(b) or {}
        if (w.get("sol") or 0) > 0.001:
            raise HTTPException(409, "withdraw the bot's SOL first")
        await write("DELETE FROM lp_bots WHERE id = :i", i=bot_id)
        return {"ok": True, "deleted": bot_id}

    # ------------------------------------------------------------------ Claude (MCP) suggestions
    @r.post("/{bot_id}/suggest")
    async def suggest(body: SuggestBody, request: Request, bot_id: int = Path(ge=1)) -> dict[str, Any]:
        uid = await request_user(engine(), cfg, request, write=True, scope=BOTS_SCOPE)
        b = await own(uid, bot_id)
        if body.take_profit_pct is None and body.stop_loss_pct is None:
            raise HTTPException(400, "suggest a take profit, a stop loss or both")
        s = {k: v for k, v in body.model_dump().items() if v is not None and v != ""}
        s.update(at=time.time(), applied=b["mode"] == "paper")
        if b["mode"] == "paper":
            ch = {k: s[k] for k in ("take_profit_pct", "stop_loss_pct") if k in s}
            sets = ", ".join(f"{k} = :{k}" for k in ch)
            await write(f"UPDATE lp_bots SET {sets}, pending = CAST(:p AS jsonb), updated_at = now() WHERE id = :i",
                        i=bot_id, p=json.dumps(s), **ch)
            note = "applied: this is a paper bot"
        else:
            await write("UPDATE lp_bots SET pending = CAST(:p AS jsonb), updated_at = now() WHERE id = :i",
                        i=bot_id, p=json.dumps(s))
            note = "saved as a suggestion: the owner applies it on the dashboard (real-money bot)"
        return {"ok": True, "bot_id": bot_id, "mode": b["mode"], "suggestion": s, "note": note}

    return r
