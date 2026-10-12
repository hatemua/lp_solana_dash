"""Bot tables (created on start) and the data the bot reads: stats, bars, bins. Read-only on indexer tables."""

import json
from typing import Any

import asyncpg

from .config import SOL_MINT
from .engine import Position
from .guards import CRASH_EXITS, TokenFlow
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
    """CREATE TABLE IF NOT EXISTS pool_analysis (
        pool text PRIMARY KEY, at timestamptz NOT NULL, name text, choice text, expected_pct double precision,
        data jsonb NOT NULL)""",
    # users' bots: each follows one house strategy, on paper or with real money (follow.py)
    """CREATE TABLE IF NOT EXISTS lp_bots (
        id bigserial PRIMARY KEY, user_id bigint NOT NULL, name text NOT NULL, strategy text NOT NULL,
        mode text NOT NULL CHECK (mode IN ('paper', 'live')), owner_wallet text, position_sol double precision NOT NULL,
        max_open int NOT NULL DEFAULT 1, take_profit_pct double precision, stop_loss_pct double precision,
        day_loss_sol double precision, day_target_sol double precision, status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'paused', 'stopping', 'stopped')), close_all boolean NOT NULL DEFAULT false,
        pending jsonb, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS lp_bots_user ON lp_bots (user_id)",
    """CREATE TABLE IF NOT EXISTS lp_bot_positions (
        id bigserial PRIMARY KEY, bot_id bigint NOT NULL REFERENCES lp_bots(id) ON DELETE CASCADE,
        paper_id bigint NOT NULL, pool text NOT NULL, name text, mint text, shape text, status text NOT NULL,
        opened_at timestamptz NOT NULL DEFAULT now(), closed_at timestamptz, sol_size double precision NOT NULL,
        net_pct double precision, peak_pct double precision, exit_reason text, pnl_sol double precision,
        position text, unwind_position text, sol_flow double precision NOT NULL DEFAULT 0,
        txs jsonb NOT NULL DEFAULT '[]', error text)""",
    "CREATE INDEX IF NOT EXISTS lp_bot_positions_bot ON lp_bot_positions (bot_id, status)",
    # history of the first live mirror (one global wallet, before users' bots)
    """CREATE TABLE IF NOT EXISTS live_positions (
        id bigserial PRIMARY KEY, paper_id bigint NOT NULL, strategy text NOT NULL, pool text NOT NULL, name text,
        mint text, status text NOT NULL, opened_at timestamptz NOT NULL DEFAULT now(), closed_at timestamptz,
        sol_size double precision, position text, unwind_position text, sol_flow double precision NOT NULL DEFAULT 0,
        pnl_sol double precision, exit_reason text, txs jsonb NOT NULL DEFAULT '[]', error text)""",
    "CREATE INDEX IF NOT EXISTS live_positions_status ON live_positions (status)",
    "CREATE TABLE IF NOT EXISTS bot_control (key text PRIMARY KEY, value jsonb NOT NULL, at timestamptz DEFAULT now())",
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
        flow = token_flow(st)
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
                             price_change_1h_pct=h1.get("priceChange"), flow=flow))
    if out:
        bars: dict[str, list[Bar]] = {}
        for r in await db.fetch("SELECT pool, extract(epoch FROM ts)::float t, high, low, close, fees "
                                "FROM pool_ohlcv_5m WHERE pool = ANY($1) AND ts > now() - interval '5 hours' "
                                "AND ts <= now() - interval '10 minutes' ORDER BY pool, ts",   # final bars only
                                [c.pool for c in out]):
            if r["close"]:
                bars.setdefault(r["pool"], []).append(Bar(r["t"], r["high"] or r["close"], r["low"] or r["close"],
                                                          r["close"], r["fees"] or 0))
        for c in out:
            c.bars = bars.get(c.pool, [])
        # fee momentum from per-minute cumulative fees (fresh, no forming 5-min bar):
        # average fees per minute over the last 15 min / over the 60 min before
        cum: dict[str, list[tuple[float, float]]] = {}
        for r in await db.fetch("SELECT pool, extract(epoch FROM ts)::float t, cum_fees FROM pool_stats "
                                "WHERE pool = ANY($1) AND ts > now() - interval '80 minutes' AND cum_fees IS NOT NULL "
                                "ORDER BY pool, ts", [c.pool for c in out]):
            cum.setdefault(r["pool"], []).append((r["t"], r["cum_fees"]))
        for c in out:
            c.fee_momentum = fee_momentum(cum.get(c.pool, []))
        tvl30 = {r["pool"]: r["tvl"] for r in await db.fetch(
            "SELECT DISTINCT ON (pool) pool, tvl FROM pool_stats WHERE pool = ANY($1) "
            "AND ts BETWEEN now() - interval '36 minutes' AND now() - interval '30 minutes' ORDER BY pool, ts DESC",
            [c.pool for c in out])}
        for c in out:
            old = tvl30.get(c.pool)
            c.tvl_change_30m = (c.tvl / old - 1) if old and c.tvl else None
    return out


def token_flow(stats: dict[str, Any]) -> TokenFlow:
    m5, h1 = stats.get("stats5m") or {}, stats.get("stats1h") or {}
    return TokenFlow(chg5m_pct=m5.get("priceChange"), chg1h_pct=h1.get("priceChange"),
                     buy5m=float(m5.get("buyVolume") or 0), sell5m=float(m5.get("sellVolume") or 0),
                     holder_change_1h_pct=h1.get("holderChange"))


async def flows(db: asyncpg.Pool, mints: list[str]) -> dict[str, TokenFlow]:
    rows = await db.fetch("SELECT mint, stats FROM tokens WHERE mint = ANY($1)", mints)
    return {r["mint"]: token_flow(r["stats"] if isinstance(r["stats"], dict) else {}) for r in rows}


async def blacklisted_mints(db: asyncpg.Pool) -> set[str]:
    """Tokens that hit a crash-type exit in the last 24 h (any strategy)."""
    return {r["m"] for r in await db.fetch(
        "SELECT DISTINCT state->'info'->>'mint' AS m FROM bot_positions WHERE status = 'closed' "
        "AND exit_reason = ANY($1) AND closed_at > now() - interval '24 hours'", list(CRASH_EXITS)) if r["m"]}


async def day_pnl_usd(db: asyncpg.Pool) -> float:
    """Today's (UTC) closed P&L plus the open positions' current P&L."""
    return float(await db.fetchval(
        "SELECT COALESCE(sum(pnl_usd), 0) FROM bot_positions WHERE status = 'open' "
        "OR (status = 'closed' AND closed_at >= date_trunc('day', now()))") or 0)


async def sol_change_1h(db: asyncpg.Pool) -> float | None:
    v = await db.fetchval("SELECT stats->'stats1h'->>'priceChange' FROM tokens WHERE mint = $1", SOL_MINT)
    return float(v) if v is not None else None


def fee_momentum(points: list[tuple[float, float]]) -> float | None:
    """(ts, cumulative fees) oldest first -> fees/min last 15 min divided by fees/min in the 60 min before."""
    if len(points) < 40:
        return None
    t_end, c_end = points[-1]

    def at(t: float) -> float | None:
        before = [c for ts, c in points if ts <= t]
        return before[-1] if before else None

    c15, c75 = at(t_end - 15 * 60), at(t_end - 75 * 60)
    if c15 is None or c75 is None:
        c75 = points[0][1]
        if c15 is None:
            return None
    recent, prev = max(0.0, c_end - c15) / 15, max(0.0, c15 - c75) / 60
    return recent / prev if prev > 0 else None


async def minutes(db: asyncpg.Pool, pools: list[str], since: float) -> dict[str, list[dict[str, Any]]]:
    """Per-minute stats (price, cumulative fees, TVL) from 31 min before `since`: fee deltas, rug and LP exodus."""
    out: dict[str, list[dict[str, Any]]] = {}
    for r in await db.fetch("SELECT pool, extract(epoch FROM ts)::float t, price, cum_fees, tvl FROM pool_stats "
                            "WHERE pool = ANY($1) AND ts > to_timestamp($2) - interval '31 minutes' ORDER BY pool, ts",
                            pools, since):
        out.setdefault(r["pool"], []).append(dict(r))
    return out


async def history(db: asyncpg.Pool, pool: str, hours: float) -> list[dict[str, Any]]:
    """Per-minute price, cumulative fees and TVL of one pool over the last `hours` (the analyzer's replays)."""
    return [dict(r) for r in await db.fetch(
        "SELECT extract(epoch FROM ts)::float t, price, cum_fees, tvl FROM pool_stats WHERE pool = $1 "
        "AND ts > now() - make_interval(secs => $2) ORDER BY ts", pool, hours * 3600)]


async def save_analysis(db: asyncpg.Pool, a: dict[str, Any]) -> None:
    ch = a.get("choice") or {}
    await db.execute(
        "INSERT INTO pool_analysis (pool, at, name, choice, expected_pct, data) VALUES ($1, to_timestamp($2), $3, $4, "
        "$5, $6) ON CONFLICT (pool) DO UPDATE SET at = EXCLUDED.at, name = EXCLUDED.name, choice = EXCLUDED.choice, "
        "expected_pct = EXCLUDED.expected_pct, data = EXCLUDED.data",
        a["pool"], a["at"], a["name"], ch.get("shape"), ch.get("expected_pct"), a)


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


async def closed_mints(db: asyncpg.Pool, minutes_: int) -> set[str]:
    """Tokens any strategy closed within the last `minutes_` (no quick re-entry: QI 2026-10-11 00:21 lost $6.91)."""
    rows = await db.fetch("SELECT DISTINCT state->'info'->>'mint' AS mint FROM bot_positions WHERE status = 'closed' "
                          "AND closed_at > now() - make_interval(mins => $1)", minutes_)
    return {r["mint"] for r in rows if r["mint"]}


async def day_results(db: asyncpg.Pool) -> dict[str, dict[str, Any]]:
    """Per strategy: today's (UTC) closed P&L in close order, the open P&L now, the latest 5 closed (ts, P&L)."""
    out: dict[str, dict[str, Any]] = {}
    for r in await db.fetch(
            "SELECT strategy, COALESCE(array_agg(pnl_usd ORDER BY closed_at, id) FILTER (WHERE status = 'closed' AND "
            "closed_at >= date_trunc('day', now())), '{}') AS today, "
            "COALESCE(sum(pnl_usd) FILTER (WHERE status = 'open'), 0) AS open_usd FROM bot_positions "
            "WHERE status = 'open' OR closed_at >= now() - interval '2 days' GROUP BY strategy"):
        out[r["strategy"]] = {"today": [float(x or 0) for x in r["today"]], "open_usd": float(r["open_usd"]),
                              "recent": []}
    for r in await db.fetch(
            "SELECT strategy, extract(epoch FROM closed_at)::float t, pnl_usd FROM ("
            "SELECT strategy, closed_at, pnl_usd, "
            "row_number() OVER (PARTITION BY strategy ORDER BY closed_at DESC) n FROM bot_positions "
            "WHERE status = 'closed') x WHERE n <= 5 ORDER BY strategy, t DESC"):
        out.setdefault(r["strategy"], {"today": [], "open_usd": 0.0, "recent": []})["recent"].append(
            (r["t"], float(r["pnl_usd"] or 0)))
    return out


async def losing_mints(db: asyncpg.Pool, hours: float) -> set[str]:
    """Tokens a position closed at a loss within `hours` (DARK 2026-10-12 05:18 re-entered after a loss: -4.6%)."""
    rows = await db.fetch("SELECT DISTINCT state->'info'->>'mint' AS mint FROM bot_positions WHERE status = 'closed' "
                          "AND net_pct < 0 AND closed_at > now() - make_interval(secs => $1)", hours * 3600)
    return {r["mint"] for r in rows if r["mint"]}


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
