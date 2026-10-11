"""Paper trading tables: demo accounts (max $10k each), user strategies, bots, positions, events, and per-refresh
memecoin snapshots (for later backtests). Virtual money only: no wallet, no key, nothing is ever signed or sent.
"""

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

MAX_ACCOUNT_USD = 10_000
MAX_BOTS_PER_USER = 10
MAX_STRATEGIES_PER_USER = 20
BOT_STATUSES = ("running", "paused", "stopping", "stopped")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS paper_accounts (
        user_id bigint PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        start_usd double precision NOT NULL, cash_usd double precision NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(), reset_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS paper_strategies (
        id bigserial PRIMARY KEY, user_id bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name text NOT NULL, description text NOT NULL DEFAULT '', base_template text, params jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS paper_bots (
        id bigserial PRIMARY KEY, user_id bigint REFERENCES users(id) ON DELETE CASCADE,
        house_key text UNIQUE,                                   -- house bots (no user) run every built-in template
        name text NOT NULL, strategy_key text NOT NULL, strategy_name text NOT NULL, params jsonb NOT NULL,
        budget_usd double precision NOT NULL, cash_usd double precision NOT NULL,
        position_usd double precision NOT NULL, max_open integer NOT NULL,
        status text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'paused', 'stopping', 'stopped')),
        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
        stopped_at timestamptz)""",
    "CREATE INDEX IF NOT EXISTS paper_bots_user ON paper_bots (user_id)",
    """CREATE TABLE IF NOT EXISTS paper_positions (
        id bigserial PRIMARY KEY, bot_id bigint NOT NULL REFERENCES paper_bots(id) ON DELETE CASCADE,
        mint text NOT NULL, symbol text, name text, narrative text, tier text, decimals integer NOT NULL,
        status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
        opened_at timestamptz NOT NULL DEFAULT now(), entry_price double precision NOT NULL,
        entry_liquidity double precision NOT NULL, usd_in double precision NOT NULL, sol_in double precision,
        tokens double precision NOT NULL, tokens_left double precision NOT NULL,
        peak_price double precision NOT NULL, last_price double precision NOT NULL,
        last_ts timestamptz NOT NULL DEFAULT now(), usd_out double precision NOT NULL DEFAULT 0,
        costs_usd double precision NOT NULL DEFAULT 0, entry_cost_pct double precision,
        closed_at timestamptz, exit_reason text, pnl_usd double precision, pnl_pct double precision,
        state jsonb NOT NULL DEFAULT '{}', entry_row jsonb)""",
    "CREATE INDEX IF NOT EXISTS paper_positions_bot ON paper_positions (bot_id, status)",
    "CREATE INDEX IF NOT EXISTS paper_positions_open ON paper_positions (status) WHERE status = 'open'",
    """CREATE TABLE IF NOT EXISTS paper_events (
        id bigserial PRIMARY KEY, bot_id bigint REFERENCES paper_bots(id) ON DELETE CASCADE, position_id bigint,
        ts timestamptz NOT NULL DEFAULT now(), kind text NOT NULL, detail jsonb NOT NULL DEFAULT '{}')""",
    "CREATE INDEX IF NOT EXISTS paper_events_bot ON paper_events (bot_id, ts DESC)",
    """CREATE TABLE IF NOT EXISTS meme_snapshots (
        ts timestamptz NOT NULL, mint text NOT NULL, symbol text, narrative text, price double precision,
        mcap double precision, liquidity double precision, volume_1h double precision, chg_5m double precision,
        chg_1h double precision, chg_24h double precision, buy_ratio_1h double precision,
        ds_txn_ratio_1h double precision, holders double precision, momentum double precision,
        safety double precision, PRIMARY KEY (mint, ts))""",
    """DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
            PERFORM create_hypertable('meme_snapshots', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
        END IF;
    END $$""",
]


async def ensure_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for stmt in SCHEMA:
            await conn.execute(text(stmt))


async def q(engine: AsyncEngine, sql: str, **params: Any) -> list[dict[str, Any]]:
    async with engine.begin() as conn:
        res = await conn.execute(text(sql), params)
        return [dict(r._mapping) for r in res] if res.returns_rows else []


def js(v: Any) -> str:
    return json.dumps(v, default=str)


async def event(engine: AsyncEngine, bot_id: int | None, kind: str, detail: dict[str, Any],
                position_id: int | None = None) -> None:
    await q(engine, "INSERT INTO paper_events (bot_id, position_id, kind, detail) "
            "VALUES (:b, :p, :k, CAST(:d AS jsonb))", b=bot_id, p=position_id, k=kind, d=js(detail))


BOT_STATS_SQL = """
SELECT b.*,
  count(p.id) FILTER (WHERE p.status = 'open') AS open_positions,
  count(p.id) FILTER (WHERE p.status = 'closed') AS closed_positions,
  count(p.id) FILTER (WHERE p.status = 'closed' AND p.pnl_usd > 0) AS wins,
  coalesce(sum(p.pnl_usd) FILTER (WHERE p.status = 'closed'), 0) AS realized_usd,
  coalesce(sum(p.tokens_left * p.last_price) FILTER (WHERE p.status = 'open'), 0) AS open_value_usd,
  coalesce(sum(p.tokens_left * p.last_price + p.usd_out - p.usd_in - p.costs_usd)
           FILTER (WHERE p.status = 'open'), 0) AS unrealized_usd
FROM paper_bots b LEFT JOIN paper_positions p ON p.bot_id = b.id
WHERE {where}
GROUP BY b.id ORDER BY b.created_at
"""


def bot_view(b: dict[str, Any]) -> dict[str, Any]:
    out = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in b.items()}
    out["equity_usd"] = round(b["cash_usd"] + b["open_value_usd"], 2)
    out["pnl_usd"] = round(out["equity_usd"] - b["budget_usd"], 2)
    out["pnl_pct"] = round(out["pnl_usd"] / b["budget_usd"] * 100, 2) if b["budget_usd"] else 0.0
    closed = b["closed_positions"] or 0
    out["win_rate"] = round(b["wins"] / closed * 100, 1) if closed else None
    return out


async def bots(engine: AsyncEngine, user_id: int | None = None, house: bool = False,
               bot_id: int | None = None) -> list[dict[str, Any]]:
    if bot_id is not None:
        params: dict[str, Any] = {"id": bot_id}
        where = "b.id = :id"
    elif house:
        where, params = "b.house_key IS NOT NULL", {}
    else:
        where, params = "b.user_id = :u", {"u": user_id}
    return [bot_view(b) for b in await q(engine, BOT_STATS_SQL.format(where=where), **params)]


async def positions(engine: AsyncEngine, bot_id: int, status: str = "all", limit: int = 100) -> list[dict[str, Any]]:
    cond = "" if status == "all" else "AND status = :s"
    rows = await q(engine, f"""SELECT id, mint, symbol, name, narrative, tier, status, opened_at, entry_price,
        usd_in, tokens, tokens_left, peak_price, last_price, last_ts, usd_out, costs_usd, entry_cost_pct, closed_at,
        exit_reason, pnl_usd, pnl_pct, state->>'tp_done' AS tp_done FROM paper_positions
        WHERE bot_id = :b {cond} ORDER BY status = 'open' DESC, opened_at DESC LIMIT :n""",
                   b=bot_id, s=status, n=limit)
    out = []
    for r in rows:
        r = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
        if r["status"] == "open":
            value = r["tokens_left"] * r["last_price"]
            r["value_usd"] = round(value, 2)
            r["pnl_usd"] = round(value + r["usd_out"] - r["usd_in"] - r["costs_usd"], 2)
            r["pnl_pct"] = round(r["pnl_usd"] / r["usd_in"] * 100, 2) if r["usd_in"] else None
        r["price_chg_pct"] = round((r["last_price"] / r["entry_price"] - 1) * 100, 2) if r["entry_price"] else None
        out.append(r)
    return out


async def save_snapshots(engine: AsyncEngine, tokens: list[dict[str, Any]]) -> int:
    rows = [{"mint": t["mint"], "symbol": t.get("symbol"), "narrative": t.get("narrative"), "price": t.get("price"),
             "mcap": t.get("mcap"), "liquidity": t.get("liquidity"), "volume_1h": t.get("volume_1h"),
             "chg_5m": t.get("chg_5m"), "chg_1h": t.get("chg_1h"), "chg_24h": t.get("chg_24h"),
             "buy_ratio_1h": t.get("buy_ratio_1h"), "ds_txn_ratio_1h": t.get("ds_txn_ratio_1h"),
             "holders": t.get("holders"), "momentum": t.get("momentum"), "safety": t.get("safety")}
            for t in tokens if t.get("mint") and t.get("price")]
    if not rows:
        return 0
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO meme_snapshots (ts, mint, symbol, narrative, price, mcap, liquidity, volume_1h, chg_5m, "
            "chg_1h, chg_24h, buy_ratio_1h, ds_txn_ratio_1h, holders, momentum, safety) VALUES (date_trunc('minute', "
            "now()), :mint, :symbol, :narrative, :price, :mcap, :liquidity, :volume_1h, :chg_5m, :chg_1h, :chg_24h, "
            ":buy_ratio_1h, :ds_txn_ratio_1h, :holders, :momentum, :safety) ON CONFLICT DO NOTHING"), rows)
    return len(rows)
