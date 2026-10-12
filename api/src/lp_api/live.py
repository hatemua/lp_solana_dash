"""Live bot routes: status, a transfer for the user's wallet to fund the bot, and owner-signed controls.

The bot wallet key is in the executor only. Control actions (pause, resume, close all, withdraw) need a fresh message
signed by the owner wallet (BOT_OWNER_WALLET); a withdrawal can only go to that wallet.
"""

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

ADDR = r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"
ACTIONS = ("pause", "resume", "close_all", "withdraw")
MAX_AGE_S = 120


class FundBody(BaseModel):
    user: str = Field(pattern=ADDR)
    sol: float = Field(gt=0, le=5)


class ControlBody(BaseModel):
    action: str = Field(pattern="^(" + "|".join(ACTIONS) + ")$")
    ts: int
    pubkey: str = Field(pattern=ADDR)
    signature: str = Field(max_length=200)        # base64 ed25519 signature of message(action, ts)


def message(action: str, ts: int) -> str:
    return f"LP Dash bot: {action} at {ts}"


def router(state: dict[str, Any], rows: Callable[..., Awaitable[list[dict[str, Any]]]],
           executor: Callable[..., Awaitable[Any]]) -> APIRouter:
    r = APIRouter()

    async def safe_rows(sql: str, **params: Any) -> list[dict[str, Any]]:
        try:
            return await rows(sql, **params)
        except Exception as e:                      # tables not created yet (bot never ran live)
            if "does not exist" in str(e):
                return []
            raise

    @r.get("/v1/live/status")
    async def status() -> dict[str, Any]:
        try:
            wallet = await executor("GET", "/v1/bot/wallet")
        except HTTPException as e:
            wallet = {"enabled": False, "error": e.detail}
        ctl = await safe_rows("SELECT value, at FROM bot_control WHERE key = 'live'")
        pos = await safe_rows(
            "SELECT l.id, l.paper_id, l.pool, l.name, l.status, l.opened_at, l.closed_at, l.sol_size, l.position, "
            "l.unwind_position, l.sol_flow, l.pnl_sol, l.exit_reason, l.txs, l.error, "
            "b.net_pct AS paper_net_pct, b.status AS paper_status, b.state->'info'->>'shape' AS shape "
            "FROM live_positions l LEFT JOIN bot_positions b ON b.id = l.paper_id ORDER BY l.id DESC LIMIT 100")
        closed = [p for p in pos if p["status"] == "closed"]
        return {
            "wallet": wallet,
            "control": ctl[0]["value"] if ctl else {},
            "positions": pos,
            "totals": {"closed": len(closed), "wins": sum(1 for p in closed if (p["pnl_sol"] or 0) > 0),
                       "pnl_sol": sum(p["pnl_sol"] or 0 for p in closed),
                       "open": sum(1 for p in pos if p["status"] not in ("closed", "failed", "check"))},
        }

    @r.post("/v1/live/fund-tx")
    async def fund_tx(body: FundBody) -> Any:
        """Unsigned SOL transfer from the user's wallet to the bot wallet; the user signs it in their wallet."""
        return await executor("POST", "/v1/bot/fund-tx", body.model_dump())

    @r.post("/v1/live/control")
    async def control(body: ControlBody) -> dict[str, Any]:
        if abs(time.time() - body.ts) > MAX_AGE_S:
            raise HTTPException(400, "request expired: sign again")
        v = await executor("POST", "/v1/bot/verify", {"pubkey": body.pubkey, "message": message(body.action, body.ts),
                                                      "signature": body.signature})
        if not v.get("ok"):
            raise HTTPException(403, v.get("error") or "not allowed")
        if not await state["redis"].set(f"live:sig:{body.signature}", "1", nx=True, ex=MAX_AGE_S * 2):
            raise HTTPException(409, "this signature was already used")
        if body.action == "withdraw":
            busy = await safe_rows("SELECT count(*) AS n FROM live_positions WHERE status NOT IN "
                                   "('closed', 'failed', 'check')")
            if busy and busy[0]["n"]:
                raise HTTPException(409, "close the live positions first (close all), then withdraw")
            return await executor("POST", "/v1/bot/withdraw", {})
        value = {"pause": {"paused": True}, "resume": {"paused": False},
                 "close_all": {"close_all": True, "paused": True}}[body.action]
        engine: AsyncEngine = state["engine"]
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE IF NOT EXISTS bot_control (key text PRIMARY KEY, value jsonb "
                                    "NOT NULL, at timestamptz DEFAULT now())"))
            await conn.execute(text(
                "INSERT INTO bot_control (key, value) VALUES ('live', CAST(:v AS jsonb)) ON CONFLICT (key) DO UPDATE "
                "SET value = bot_control.value || EXCLUDED.value, at = now()"),
                {"v": json.dumps({**value, "by": body.pubkey, "action": body.action})})
        return {"ok": True, "action": body.action}

    return r
