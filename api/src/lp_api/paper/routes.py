"""Paper trading API: demo accounts (up to $10k virtual), strategies (built-in templates or your own), bots.

Reads of your own account work with the web session or an OAuth token; every change needs the web session and a
same-site Origin. Virtual money only: nothing here can touch a wallet, sign or send a transaction.
"""

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from ..auth import request_user
from ..config import Settings
from . import store
from .categories import NARRATIVE_LABELS, TIER_LABELS
from .strategies import TEMPLATES, Params, template_list


class AccountBody(BaseModel):
    start_usd: float = Field(ge=100, le=store.MAX_ACCOUNT_USD)
    reset: bool = False                       # True: delete every bot and position, start again


class StrategyBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field("", max_length=400)
    base_template: str | None = Field(None, max_length=40)
    params: Params


class BotBody(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    strategy: str = Field(pattern=r"^(tpl:[a-z_]{1,40}|my:\d{1,12})$")   # tpl:<template id> or my:<strategy id>
    budget_usd: float = Field(ge=50, le=store.MAX_ACCOUNT_USD)
    position_usd: float = Field(ge=10, le=5_000)
    max_open: int = Field(3, ge=1, le=10)


class BotAction(BaseModel):
    action: str = Field(pattern="^(pause|resume|stop)$")


def router(cfg: Settings, state: dict[str, Any]) -> APIRouter:
    r = APIRouter(prefix="/v1/paper")

    def db() -> AsyncEngine:
        return state["engine"]

    async def user_id(request: Request, write: bool = False) -> int:
        try:
            return await request_user(db(), cfg, request, write)
        except HTTPException as e:
            if e.status_code == 401:
                raise HTTPException(401, "sign in to use paper trading") from None
            raise

    async def own_bot(uid: int, bot_id: int) -> dict[str, Any]:
        found = await store.bots(db(), bot_id=bot_id)
        if not found or found[0]["user_id"] != uid:
            raise HTTPException(404, "bot not found")
        return found[0]

    async def account_view(uid: int) -> dict[str, Any]:
        acc = await store.q(db(), "SELECT * FROM paper_accounts WHERE user_id = :u", u=uid)
        bots = await store.bots(db(), user_id=uid)
        if not acc:
            return {"account": None, "bots": bots, "max_usd": store.MAX_ACCOUNT_USD}
        a = acc[0]
        live = [b for b in bots if b["status"] != "stopped"]
        equity = a["cash_usd"] + sum(b["equity_usd"] for b in live)
        return {"account": {"start_usd": a["start_usd"], "cash_usd": round(a["cash_usd"], 2),
                            "in_bots_usd": round(sum(b["equity_usd"] for b in live), 2), "equity_usd": round(equity, 2),
                            "pnl_usd": round(equity - a["start_usd"], 2),
                            "pnl_pct": round((equity / a["start_usd"] - 1) * 100, 2),
                            "created_at": a["created_at"].isoformat(), "reset_at": a["reset_at"].isoformat()},
                "bots": bots, "max_usd": store.MAX_ACCOUNT_USD}

    # ------------------------------------------------------------------ public
    @r.get("/templates")
    async def templates() -> dict[str, Any]:
        return {"templates": template_list(), "narratives": NARRATIVE_LABELS, "tiers": TIER_LABELS}

    @r.get("/house")
    async def house() -> dict[str, Any]:
        """The house bots: one per built-in strategy, $1,000 each, running since the service started."""
        hb = await state["redis"].get("paper:heartbeat")
        return {"bots": await store.bots(db(), house=True), "heartbeat": json.loads(hb) if hb else None}

    @r.get("/bots/{bot_id}/positions")
    async def bot_positions(request: Request, bot_id: int = Path(ge=1), status: str = Query("all",
                            pattern="^(open|closed|all)$"), limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
        found = await store.bots(db(), bot_id=bot_id)
        if not found:
            raise HTTPException(404, "bot not found")
        if found[0]["user_id"] is not None and found[0]["user_id"] != await user_id(request):
            raise HTTPException(404, "bot not found")
        return {"bot": found[0], "positions": await store.positions(db(), bot_id, status, limit)}

    @r.get("/bots/{bot_id}/events")
    async def bot_events(request: Request, bot_id: int = Path(ge=1),
                         limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        found = await store.bots(db(), bot_id=bot_id)
        if not found or (found[0]["user_id"] is not None and found[0]["user_id"] != await user_id(request)):
            raise HTTPException(404, "bot not found")
        ev = await store.q(db(), "SELECT id, position_id, ts, kind, detail FROM paper_events WHERE bot_id = :b "
                                 "ORDER BY ts DESC LIMIT :n", b=bot_id, n=limit)
        return {"events": [{**e, "ts": e["ts"].isoformat()} for e in ev]}

    # ------------------------------------------------------------------ account
    @r.get("/account")
    async def account(request: Request) -> dict[str, Any]:
        return await account_view(await user_id(request))

    @r.post("/account")
    async def create_account(body: AccountBody, request: Request) -> dict[str, Any]:
        uid = await user_id(request, write=True)
        exists = await store.q(db(), "SELECT 1 FROM paper_accounts WHERE user_id = :u", u=uid)
        if exists and not body.reset:
            raise HTTPException(409, "you already have a demo account; send reset=true to start again")
        if exists:
            await store.q(db(), "DELETE FROM paper_bots WHERE user_id = :u", u=uid)
            await store.q(db(), "UPDATE paper_accounts SET start_usd = :s, cash_usd = :s, reset_at = now() "
                                "WHERE user_id = :u", s=body.start_usd, u=uid)
        else:
            await store.q(db(), "INSERT INTO paper_accounts (user_id, start_usd, cash_usd) VALUES (:u, :s, :s)",
                          u=uid, s=body.start_usd)
        return await account_view(uid)

    # ------------------------------------------------------------------ strategies
    @r.get("/strategies")
    async def my_strategies(request: Request) -> dict[str, Any]:
        uid = await user_id(request)
        mine = await store.q(db(), "SELECT id, name, description, base_template, params, created_at "
                                   "FROM paper_strategies WHERE user_id = :u ORDER BY created_at DESC", u=uid)
        return {"templates": template_list(),
                "mine": [{**s, "created_at": s["created_at"].isoformat()} for s in mine]}

    @r.post("/strategies")
    async def create_strategy(body: StrategyBody, request: Request) -> dict[str, Any]:
        uid = await user_id(request, write=True)
        n = await store.q(db(), "SELECT count(*) AS n FROM paper_strategies WHERE user_id = :u", u=uid)
        if n[0]["n"] >= store.MAX_STRATEGIES_PER_USER:
            raise HTTPException(400, f"at most {store.MAX_STRATEGIES_PER_USER} strategies; delete one first")
        made = await store.q(db(), """INSERT INTO paper_strategies (user_id, name, description, base_template, params)
            VALUES (:u, :n, :d, :b, CAST(:p AS jsonb)) RETURNING id""", u=uid, n=body.name, d=body.description,
                             b=body.base_template, p=store.js(body.params.model_dump()))
        return {"id": made[0]["id"]}

    @r.post("/strategies/{sid}/delete")
    async def delete_strategy(request: Request, sid: int = Path(ge=1)) -> dict[str, Any]:
        uid = await user_id(request, write=True)
        gone = await store.q(db(), "DELETE FROM paper_strategies WHERE id = :i AND user_id = :u RETURNING id",
                             i=sid, u=uid)
        if not gone:
            raise HTTPException(404, "strategy not found")
        return {"ok": True}

    # ------------------------------------------------------------------ bots
    @r.post("/bots")
    async def create_bot(body: BotBody, request: Request) -> dict[str, Any]:
        uid = await user_id(request, write=True)
        if body.position_usd > body.budget_usd:
            raise HTTPException(400, "the position size cannot be larger than the budget")
        kind, ref = body.strategy.split(":", 1)
        if kind == "tpl":
            if ref not in TEMPLATES:
                raise HTTPException(400, "unknown template")
            params, sname = TEMPLATES[ref]["params"], TEMPLATES[ref]["name"]
        else:
            s = await store.q(db(), "SELECT name, params FROM paper_strategies WHERE id = :i AND user_id = :u",
                              i=int(ref), u=uid)
            if not s:
                raise HTTPException(400, "strategy not found")
            try:
                params = Params.model_validate(s[0]["params"])
            except ValidationError as e:
                raise HTTPException(400, f"strategy is invalid: {e.errors()[0]['msg']}") from e
            sname = s[0]["name"]
        n = await store.q(db(), "SELECT count(*) AS n FROM paper_bots WHERE user_id = :u AND status <> 'stopped'",
                          u=uid)
        if n[0]["n"] >= store.MAX_BOTS_PER_USER:
            raise HTTPException(400, f"at most {store.MAX_BOTS_PER_USER} active bots")
        paid = await store.q(db(), "UPDATE paper_accounts SET cash_usd = cash_usd - :b WHERE user_id = :u "
                                   "AND cash_usd >= :b RETURNING cash_usd", b=body.budget_usd, u=uid)
        if not paid:
            raise HTTPException(400, "not enough free cash in your demo account (create one first if needed)")
        made = await store.q(db(), """INSERT INTO paper_bots (user_id, name, strategy_key, strategy_name, params,
            budget_usd, cash_usd, position_usd, max_open) VALUES (:u, :n, :k, :sn, CAST(:p AS jsonb), :b, :b, :ps, :mo)
            RETURNING id""", u=uid, n=body.name, k=body.strategy, sn=sname, p=store.js(params.model_dump()),
                             b=body.budget_usd, ps=body.position_usd, mo=body.max_open)
        await store.event(db(), made[0]["id"], "created", {"strategy": sname, "budget_usd": body.budget_usd})
        return (await store.bots(db(), bot_id=made[0]["id"]))[0]

    @r.post("/bots/{bot_id}/action")
    async def bot_action(body: BotAction, request: Request, bot_id: int = Path(ge=1)) -> dict[str, Any]:
        uid = await user_id(request, write=True)
        b = await own_bot(uid, bot_id)
        allowed = {"pause": ("running",), "resume": ("paused",), "stop": ("running", "paused")}[body.action]
        if b["status"] not in allowed:
            raise HTTPException(400, f"cannot {body.action} a {b['status']} bot")
        new = {"pause": "paused", "resume": "running", "stop": "stopping"}[body.action]
        await store.q(db(), "UPDATE paper_bots SET status = :s, updated_at = now() WHERE id = :i", s=new, i=bot_id)
        await store.event(db(), bot_id, body.action, {})
        return (await store.bots(db(), bot_id=bot_id))[0]

    @r.post("/bots/{bot_id}/delete")
    async def delete_bot(request: Request, bot_id: int = Path(ge=1)) -> dict[str, Any]:
        uid = await user_id(request, write=True)
        b = await own_bot(uid, bot_id)
        if b["status"] != "stopped":
            raise HTTPException(400, "stop the bot first; its cash returns to your account when its positions close")
        await store.q(db(), "DELETE FROM paper_bots WHERE id = :i", i=bot_id)
        return {"ok": True}

    return r
