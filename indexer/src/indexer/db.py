"""PostgreSQL access: schema migration and idempotent upserts (INSERT ... ON CONFLICT)."""

import json
import logging
from collections.abc import Iterable, Sequence
from importlib import resources
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

log = logging.getLogger(__name__)

JSON_COLUMNS = {"audit", "stats", "bins", "metrics"}

# primary keys and the columns an upsert may overwrite (others are left as they are)
TABLES: dict[str, tuple[tuple[str, ...], tuple[str, ...] | None]] = {
    "pools": (("address",), None),
    "pool_stats": (("pool", "ts"), None),
    "pool_ohlcv_5m": (("pool", "ts"), None),
    "token_ohlcv_1m": (("mint", "ts"), None),
    "bins_snapshot": (("pool", "ts"), None),
    "pool_tvl_flow": (("pool", "ts"), None),
    "ohlcv_cursor": (("pool",), None),
    # from the pool list: basic fields only, never overwrite Jupiter's richer data with nulls
    "tokens_basic": (("mint",), ("symbol", "name", "decimals", "holders", "mcap", "price_usd",
                                  "freeze_authority_disabled", "is_verified", "updated_at")),
    "tokens_jupiter": (("mint",), ("symbol", "name", "decimals", "created_at", "launchpad", "holders", "mcap",
                                    "price_usd", "organic_score", "audit", "stats", "freeze_authority_disabled",
                                    "mint_authority_disabled", "enriched_at", "updated_at")),
}
TABLE_NAME = {"tokens_basic": "tokens", "tokens_jupiter": "tokens"}


def upsert_sql(kind: str, columns: Sequence[str]) -> str:
    """INSERT ... ON CONFLICT (pk) DO UPDATE for the given columns; COALESCE keeps old values over new NULLs."""
    pk, updatable = TABLES[kind]
    table = TABLE_NAME.get(kind, kind)
    cols = ", ".join(columns)
    vals = ", ".join(f"CAST(:{c} AS JSONB)" if c in JSON_COLUMNS else f":{c}" for c in columns)
    upd = [c for c in columns if c not in pk and (updatable is None or c in updatable)]
    sets = ", ".join(f"{c} = COALESCE(EXCLUDED.{c}, {table}.{c})" for c in upd)
    action = f"DO UPDATE SET {sets}" if sets else "DO NOTHING"
    return f"INSERT INTO {table} ({cols}) VALUES ({vals}) ON CONFLICT ({', '.join(pk)}) {action}"


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True)

        @event.listens_for(self.engine.sync_engine, "connect")
        def _json_codec(dbapi_conn: Any, _rec: Any) -> None:
            # JSONB in and out as Python objects (asyncpg returns text by default)
            dbapi_conn.run_async(lambda c: c.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads,
                                                            schema="pg_catalog"))

    async def migrate(self) -> None:
        sql = resources.files("indexer").joinpath("schema.sql").read_text(encoding="utf-8")
        async with self.engine.begin() as conn:
            # asyncpg runs one statement per call; the DO block is kept whole
            for stmt in split_sql(sql):
                await conn.execute(text(stmt))
        log.info("database schema ready")

    async def upsert(self, kind: str, rows: Iterable[dict[str, Any]], chunk: int = 500) -> int:
        rows = ordered_rows(kind, rows)
        if not rows:
            return 0
        n = 0
        columns = list(rows[0].keys())
        stmt = text(upsert_sql(kind, columns))
        async with self.engine.begin() as conn:
            for i in range(0, len(rows), chunk):
                part = rows[i:i + chunk]
                await conn.execute(stmt, part)
                n += len(part)
        return n

    async def fetch(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            res = await conn.execute(text(sql), params)
            return [dict(r._mapping) for r in res]

    async def execute(self, sql: str, **params: Any) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text(sql), params)

    async def close(self) -> None:
        await self.engine.dispose()


def ordered_rows(kind: str, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate by primary key (last wins) and sort by it.

    Writers that upsert overlapping rows (e.g. hot_stats and full_pool_list both touch `pools`) then lock them in
    the same order, which prevents deadlocks; and one INSERT never updates the same row twice (Postgres rejects it).
    """
    pk = TABLES[kind][0]
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in rows:
        if r:
            by_key[tuple(r.get(k) for k in pk)] = r
    return [by_key[k] for k in sorted(by_key, key=lambda t: tuple(str(x) for x in t))]


def split_sql(sql: str) -> list[str]:
    """Split a SQL script into statements, keeping $$ ... $$ blocks intact and dropping comments."""
    out, buf, in_dollar = [], [], False
    for line in sql.splitlines():
        stripped = line.strip()
        if not in_dollar and (not stripped or stripped.startswith("--")):
            continue
        buf.append(line)
        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar
        if not in_dollar and stripped.endswith(";"):
            out.append("\n".join(buf).strip().rstrip(";"))
            buf = []
    if buf and "".join(buf).strip():
        out.append("\n".join(buf).strip().rstrip(";"))
    return out
