"""Bot tables (created on start) and the data the bot reads: stats, bars, bins. Read-only on indexer tables."""

import json
from typing import Any

import asyncpg

from .config import SOL_MINT
from .engine import Position
from .signals import Bar
from .strategies import Candidate

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS bot_positions (
        id bigserial PRIMARY KEY, strategy text NOT NULL, pool text NOT NULL, name text,
        status text NOT NULL CHECK (status IN ('open', 'closed')), mode text NOT NULL DEFAULT 'paper',
        opened_at timestamptz NOT NULL, closed_at timestamptz, entry_price double precision,
        capital_usd double precision, sol_usd_entry double precision, exit_reason text, net_pct double precision,
        pnl_usd double precision, fees_usd double precision, costs_usd double precision, flipped boolean DEFAULT false,
        last_price double precision, updated_at timestamptz NOT NULL DEFAULT now(), state jsonb NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS bot_positions_status ON bot_positions (status, strategy)",
    "CREATE INDEX IF NOT EXISTS bot_positions_pool ON bot_positions (pool, strategy, closed_at DESC)",
    """CREATE TABLE IF NOT EXISTS bot_events (
        id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), strategy text, position_id bigint,
        kind text NOT NULL, detail jsonb)""",
    "CREATE INDEX IF NOT EXISTS bot_events_ts ON bot_events (ts DESC)",
]


async def connect(url: str) -> asyncpg.Pool:
    async def init(conn: asyncpg.Connection) -> None:
        for typ in ("jsonb", "json"):
            await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

    pool = await asyncpg.create_pool(url.replace("postgresql+asyncpg://", "postgresql://"), min_size=1, max_size=4,
                                     init=init)
    async with pool.acquire() as c:
        for stmt in SCHEMA:
            await c.execute(stmt)
    return pool


async def sol_usd(db: asyncpg.Pool) -> float:
    return float(await db.fetchval("SELECT price_usd FROM tokens WHERE mint = $1", SOL_MINT) or 0)


async def candidates(db: asyncpg.Pool, min_tvl: float, min_fees_1h: float) -> list[Candidate]:
    rows = await db.fetch(
        """WITH s AS (
             SELECT DISTINCT ON (pool) pool, ts, price, tvl, fees_1h, fee_tvl_1h, fees_24h FROM pool_stats
             WHERE ts > now() - interval '3 minutes' ORDER BY pool, ts DESC)
           SELECT s.*, p.name, p.bin_step, p.base_fee_pct, t.mcap, t.holders, t.organic_score, t.audit,
                  extract(epoch FROM now() - t.created_at) / 3600 AS token_age_h, t.mint, t.decimals, t.stats
           FROM s JOIN pools p ON p.address = s.pool JOIN tokens t ON t.mint = p.token_x
           WHERE p.token_y = $1 AND s.tvl >= $2 AND s.fees_1h >= $3 AND s.price > 0""",
        SOL_MINT, min_tvl, min_fees_1h)
    out = []
    for r in rows:
        a = r["audit"] if isinstance(r["audit"], dict) else {}
        st = r["stats"] if isinstance(r["stats"], dict) else {}
        h1 = st.get("stats1h") or {}
        out.append(Candidate(pool=r["pool"], name=r["name"] or r["pool"][:6], price=r["price"], tvl=r["tvl"] or 0,
                             fees_1h=r["fees_1h"] or 0, fee_tvl_1h=r["fee_tvl_1h"] or 0, bin_step=r["bin_step"] or 100,
                             base_fee_pct=r["base_fee_pct"] or 1, mcap=r["mcap"] or 0, holders=r["holders"] or 0,
                             organic=r["organic_score"] or 0, top10=a.get("topHoldersPercentage") or 100,
                             mint_off=bool(a.get("mintAuthorityDisabled")),
                             freeze_off=bool(a.get("freezeAuthorityDisabled")),
                             token_age_h=float(r["token_age_h"]) if r["token_age_h"] is not None else None,
                             mint=r["mint"], decimals=r["decimals"] if r["decimals"] is not None else 6,
                             fees_24h=r["fees_24h"] or 0, buy_vol_1h=float(h1.get("buyVolume") or 0),
                             sell_vol_1h=float(h1.get("sellVolume") or 0), traders_1h=int(h1.get("numTraders") or 0),
                             bot_holders_pct=float(a.get("botHoldersPercentage") or 0),
                             price_change_1h_pct=h1.get("priceChange")))
    if out:
        bars: dict[str, list[Bar]] = {}
        for r in await db.fetch("SELECT pool, extract(epoch FROM ts)::float t, high, low, close, fees "
                                "FROM pool_ohlcv_5m WHERE pool = ANY($1) AND ts > now() - interval '5 hours' "
                                "ORDER BY pool, ts",
                                [c.pool for c in out]):
            if r["close"]:
                bars.setdefault(r["pool"], []).append(Bar(r["t"], r["high"] or r["close"], r["low"] or r["close"],
                                                          r["close"], r["fees"] or 0))
        for c in out:
            c.bars = bars.get(c.pool, [])
    return out


async def minutes(db: asyncpg.Pool, pools: list[str], since: float) -> dict[str, list[dict[str, Any]]]:
    """Per-minute stats (price, cumulative fees, TVL) from 16 min before `since`, for fee deltas and the rug check."""
    out: dict[str, list[dict[str, Any]]] = {}
    for r in await db.fetch("SELECT pool, extract(epoch FROM ts)::float t, price, cum_fees, tvl FROM pool_stats "
                            "WHERE pool = ANY($1) AND ts > to_timestamp($2) - interval '16 minutes' ORDER BY pool, ts",
                            pools, since):
        out.setdefault(r["pool"], []).append(dict(r))
    return out


async def snapshot_bins(db: asyncpg.Pool, pool: str) -> list[dict[str, Any]] | None:
    row = await db.fetchrow("SELECT bins FROM bins_snapshot WHERE pool = $1 AND ts > now() - interval '10 minutes' "
                            "ORDER BY ts DESC LIMIT 1", pool)
    return row["bins"] if row else None


async def open_positions(db: asyncpg.Pool) -> list[Position]:
    return [Position.from_row(dict(r)) for r in await db.fetch(
        "SELECT * FROM bot_positions WHERE status = 'open' ORDER BY id")]


async def recently_closed(db: asyncpg.Pool, strategy: str, minutes_: int) -> tuple[set[str], set[str]]:
    """Pools and token mints this strategy closed within the cooldown (a token can have several pools)."""
    rows = await db.fetch(
        "SELECT pool, state->'info'->>'mint' AS mint FROM bot_positions WHERE strategy = $1 AND status = 'closed' "
        "AND closed_at > now() - make_interval(mins => $2)", strategy, minutes_)
    return {r["pool"] for r in rows}, {r["mint"] for r in rows if r["mint"]}


async def insert(db: asyncpg.Pool, p: Position, capital_usd: float, sol_usd_: float) -> int:
    pid = await db.fetchval(
        "INSERT INTO bot_positions (strategy, pool, name, status, opened_at, entry_price, capital_usd, sol_usd_entry, "
        "fees_usd, costs_usd, last_price, net_pct, state) VALUES ($1, $2, $3, 'open', to_timestamp($4), $5, $6, $7, "
        "0, $8, $5, $9, $10) RETURNING id",
        p.strategy, p.pool, p.name, p.opened_at, p.entry_price, capital_usd, sol_usd_, p.costs_sol * sol_usd_,
        p.net_pct(p.entry_price), p.state())
    await event(db, p.strategy, pid, "open", {"pool": p.pool, "name": p.name, "price": p.entry_price, **p.info})
    return int(pid)


async def update(db: asyncpg.Pool, p: Position, sol_usd_: float) -> None:
    net = p.net_pct(p.last_price)
    await db.execute(
        "UPDATE bot_positions SET state = $2, fees_usd = $3, costs_usd = $4, last_price = $5, net_pct = $6, "
        "pnl_usd = capital_usd * $6 / 100, flipped = $7, updated_at = now() WHERE id = $1",
        p.id, p.state(), p.fees_sol * sol_usd_, p.costs_sol * sol_usd_, p.last_price, net, p.flipped)


async def close(db: asyncpg.Pool, p: Position, reason: str, net: float, sol_usd_: float) -> None:
    await db.execute(
        "UPDATE bot_positions SET status = 'closed', closed_at = to_timestamp($2), exit_reason = $3, net_pct = $4, "
        "pnl_usd = capital_usd * $4 / 100, state = $5, fees_usd = $6, costs_usd = $7, last_price = $8, flipped = $9, "
        "updated_at = now() WHERE id = $1",
        p.id, p.last_ts, reason, net, p.state(), p.fees_sol * sol_usd_, p.costs_sol * sol_usd_, p.last_price,
        p.flipped)
    await event(db, p.strategy, p.id, "close", {"reason": reason, "net_pct": round(net, 3),
                                                "fees_usd": round(p.fees_sol * sol_usd_, 4)})


async def event(db: asyncpg.Pool, strategy: str | None, pid: int | None, kind: str, detail: dict[str, Any]) -> None:
    await db.execute("INSERT INTO bot_events (strategy, position_id, kind, detail) VALUES ($1, $2, $3, $4)",
                     strategy, pid, kind, detail)
