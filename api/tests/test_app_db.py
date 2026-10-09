"""End-to-end API test against a real PostgreSQL (schema from the indexer) and Redis."""

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = [pytest.mark.db, pytest.mark.skipif(not (os.environ.get("DATABASE_URL") and os.environ.get("REDIS_URL")),
                                                 reason="needs DATABASE_URL and REDIS_URL")]

SOL = "So11111111111111111111111111111111111111112"
POOL = "TestPoo1111111111111111111111111111111111111"
MINT = "TestMint111111111111111111111111111111111111"
SCHEMA = Path(__file__).resolve().parents[2] / "indexer" / "src" / "indexer" / "schema.sql"


async def _seed() -> None:
    import asyncpg

    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url)
    try:
        sql = SCHEMA.read_text(encoding="utf-8")
        await conn.execute(sql)
        now = datetime.now(UTC)
        await conn.execute("DELETE FROM pool_stats WHERE pool = $1", POOL)
        await conn.execute(
            "INSERT INTO pools (address, name, token_x, token_y, bin_step, base_fee_pct, created_at) "
            "VALUES ($1, 'TEST-SOL', $2, $3, 100, 2.0, $4) ON CONFLICT (address) DO NOTHING",
            POOL, MINT, SOL, now - timedelta(days=5))
        await conn.execute(
            "INSERT INTO tokens (mint, symbol, name, created_at, holders, mcap, organic_score, audit, stats) "
            "VALUES ($1, 'TEST', 'Test token', $2, 2500, 1500000, 85, "
            "'{\"mintAuthorityDisabled\": true, \"freezeAuthorityDisabled\": true, \"topHoldersPercentage\": 20}', "
            "'{\"stats5m\": {\"buyVolume\": 400000, \"sellVolume\": 200000, \"priceChange\": 3.5}}') "
            "ON CONFLICT (mint) DO NOTHING", MINT, now - timedelta(days=10))
        for i in range(3):
            await conn.execute(
                "INSERT INTO pool_stats (pool, ts, price, tvl, volume_1h, volume_24h, fees_1h, fee_tvl_1h, "
                "fee_tvl_24h) VALUES ($1, $2, 0.001, 120000, 90000, 480000, 1800, 0.015, 0.09)",
                POOL, now - timedelta(minutes=i))
        for i in range(12):
            await conn.execute(
                "INSERT INTO pool_ohlcv_5m (pool, ts, open, high, low, close, volume, fees) VALUES "
                "($1, $2, 1, 1.1, 0.9, 1.05, 1000, 20) ON CONFLICT DO NOTHING",
                POOL, (now - timedelta(minutes=5 * i)).replace(second=0, microsecond=0))
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def client():  # type: ignore[no-untyped-def]
    import asyncio

    from fastapi.testclient import TestClient

    from lp_api.config import Settings
    from lp_api.main import create_app

    asyncio.run(_seed())
    cfg = Settings(database_url=os.environ["DATABASE_URL"], redis_url=os.environ["REDIS_URL"],
                   indexer_health_url="http://127.0.0.1:9/none", rate_limit_per_min=1000)
    with TestClient(create_app(cfg)) as c:
        yield c


def test_pools_filter_and_speed(client) -> None:  # type: ignore[no-untyped-def]
    t0 = time.perf_counter()
    r = client.get("/v1/pools", params={"tvl_min": 100000, "sol_pair": "true", "q": "TEST"})
    assert r.status_code == 200
    assert time.perf_counter() - t0 < 1.0
    body = r.json()
    assert body["total"] >= 1
    row = next(d for d in body["data"] if d["address"] == POOL)
    assert row["token_symbol"] == "TEST" and row["token_volume_5m"] == 600000
    assert row["mint_disabled"] is True and row["top10_pct"] == 20
    assert isinstance(row["token_age_h"], float) and isinstance(row["pool_age_h"], float)   # numbers, not strings
    assert 9 < row["token_age_h"] / 24 < 11


def test_preset_rabbit500_matches(client) -> None:  # type: ignore[no-untyped-def]
    r = client.get("/v1/pools", params={"preset": "rabbit500", "q": "TEST"})
    assert r.status_code == 200 and any(d["address"] == POOL for d in r.json()["data"])
    r = client.get("/v1/pools", params={"preset": "rabbit500", "token_volume_5m_min": 700000, "q": "TEST"})
    assert all(d["address"] != POOL for d in r.json()["data"])


def test_bad_filter_is_400(client) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/v1/pools", params={"nope_min": 1}).status_code == 400


def test_pool_detail_and_ohlcv(client) -> None:  # type: ignore[no-untyped-def]
    r = client.get(f"/v1/pools/{POOL}")
    assert r.status_code == 200 and r.json()["address"] == POOL
    r = client.get(f"/v1/pools/{POOL}/ohlcv", params={"timeframe": "5m", "hours": 2})
    assert r.status_code == 200 and len(r.json()["data"]) >= 10
    r = client.get(f"/v1/pools/{POOL}/ohlcv", params={"timeframe": "1h", "hours": 2})
    assert r.status_code == 200 and r.json()["data"]
    assert client.get("/v1/pools/not-an-address").status_code == 422


def test_cors_only_allowed_origin(client) -> None:  # type: ignore[no-untyped-def]
    ok = client.get("/v1/presets", headers={"Origin": "https://lp.joulity.com"})
    assert ok.headers.get("access-control-allow-origin") == "https://lp.joulity.com"
    bad = client.get("/v1/presets", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in bad.headers
