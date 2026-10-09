import os
from datetime import UTC, datetime
from importlib import resources
from typing import Any

import pytest

from indexer import models as M
from indexer.db import Database, split_sql, upsert_sql
from indexer.redis_store import pool_hash


def test_upsert_sql_coalesce_and_pk() -> None:
    sql = upsert_sql("pools", ["address", "name", "tvl"])
    assert "ON CONFLICT (address)" in sql
    assert "name = COALESCE(EXCLUDED.name, pools.name)" in sql
    assert "address = COALESCE" not in sql


def test_upsert_sql_tokens_basic_does_not_touch_jupiter_fields() -> None:
    sql = upsert_sql("tokens_basic", ["mint", "symbol", "organic_score"])
    assert "INSERT INTO tokens" in sql
    assert "symbol = COALESCE" in sql and "organic_score =" not in sql


def test_upsert_sql_json_cast() -> None:
    assert "CAST(:audit AS JSONB)" in upsert_sql("tokens_jupiter", ["mint", "audit"])


def test_split_sql_keeps_do_block() -> None:
    sql = resources.files("indexer").joinpath("schema.sql").read_text(encoding="utf-8")
    stmts = split_sql(sql)
    assert any(s.startswith("DO $$") and s.rstrip().endswith("END $$") for s in stmts)
    assert all(not s.startswith("--") for s in stmts)
    assert sum(s.startswith("CREATE TABLE") for s in stmts) >= 9


def test_pool_hash_strings(pools_page: dict[str, Any]) -> None:
    now = datetime(2026, 10, 9, tzinfo=UTC)
    p = pools_page["data"][1]
    h = pool_hash(M.pool_row(p, now), M.stat_row(p, now), ("A", "SOL"))
    assert h["address"] == p["address"] and h["symbol_y"] == "SOL"
    assert all(isinstance(v, str) for v in h.values())
    assert "pool" not in h


@pytest.mark.db
@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs DATABASE_URL")
async def test_migrate_and_idempotent_upsert(pools_page: dict[str, Any]) -> None:
    db = Database(os.environ["DATABASE_URL"])
    try:
        await db.migrate()
        await db.migrate()                                   # safe to run twice
        now = datetime.now(UTC)
        rows = [M.pool_row(p, now) for p in pools_page["data"]]
        assert await db.upsert("pools", rows) == len(rows)
        assert await db.upsert("pools", rows) == len(rows)   # idempotent
        r = await db.fetch("SELECT count(*) AS n FROM pools WHERE address = ANY(:a)", a=[x["address"] for x in rows])
        assert r[0]["n"] == len(rows)
        tok = M.token_rows(pools_page["data"][1], now)
        await db.upsert("tokens_basic", tok)
        await db.upsert("tokens_jupiter", [{"mint": tok[0]["mint"], "organic_score": 77.0, "audit": {"a": 1},
                                             "enriched_at": now, "updated_at": now}])
        await db.upsert("tokens_basic", tok)                 # must not erase the Jupiter fields
        r = await db.fetch("SELECT organic_score, audit FROM tokens WHERE mint = :m", m=tok[0]["mint"])
        assert r[0]["organic_score"] == 77.0 and r[0]["audit"] == {"a": 1}
    finally:
        await db.close()
