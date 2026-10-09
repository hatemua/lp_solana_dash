"""FastAPI app: /health, /v1/pools, /v1/pools/{addr}/..., /v1/tokens/{mint}, /v1/presets, /v1/stream, wallet LP."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .config import Settings, get_settings
from .filters import PRESETS, FilterError, build_query
from .ratelimit import RateLimiter

log = logging.getLogger("lp_api")
ADDR = r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"


def jsonable(row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in row.items():
        out[k] = v.isoformat() if hasattr(v, "isoformat") else v
    return out


class AddLiquidityBody(BaseModel):
    user: str = Field(pattern=ADDR)
    amount_x: float = Field(ge=0)           # human units of token X
    amount_y: float = Field(ge=0)           # human units of token Y
    min_bin_id: int
    max_bin_id: int
    shape: str = Field(pattern="^(spot|curve|bidask)$")


class RemoveLiquidityBody(BaseModel):
    user: str = Field(pattern=ADDR)
    position: str = Field(pattern=ADDR)
    bps: int = Field(ge=1, le=10_000)       # share of the position to remove (10000 = all)
    claim_and_close: bool = False


class SendBody(BaseModel):
    transaction: str = Field(max_length=3000)   # base64, already signed by the user's wallet


class ClaimBody(BaseModel):
    user: str = Field(pattern=ADDR)
    position: str = Field(pattern=ADDR)


def create_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or get_settings()
    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_async_engine(cfg.database_url, pool_size=8, max_overflow=4, pool_pre_ping=True)
        redis = Redis.from_url(cfg.redis_url, decode_responses=True)
        client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        state.update(engine=engine, redis=redis, client=client,
                     limiter=RateLimiter(redis, cfg.rate_limit_per_min),
                     tx_limiter=RateLimiter(redis, cfg.tx_rate_limit_per_min, prefix="rl:tx"))
        yield
        await client.aclose()
        await redis.aclose()
        await engine.dispose()

    app = FastAPI(title="LP Solana Dash API", version="0.1.0", lifespan=lifespan,
                  docs_url="/v1/docs", openapi_url="/v1/openapi.json", redoc_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=cfg.cors_origins, allow_methods=["GET", "POST"],
                       allow_headers=["*"], allow_credentials=False)

    def client_ip(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for")
        return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")

    @app.middleware("http")
    async def rate_limit(request: Request, call_next: Any) -> Any:
        path = request.url.path
        if path.startswith("/v1/") and path != "/v1/stream" and "limiter" in state:
            limiter = state["tx_limiter"] if path.startswith("/v1/tx/") else state["limiter"]
            ok, retry = await limiter.hit(client_ip(request))
            if not ok:
                return JSONResponse({"error": "rate limit exceeded"}, status_code=429,
                                    headers={"Retry-After": str(retry)})
        return await call_next(request)

    async def rows(sql: str, **params: Any) -> list[dict[str, Any]]:
        engine: AsyncEngine = state["engine"]
        async with engine.connect() as conn:
            res = await conn.execute(text(sql), params)
            return [jsonable(dict(r._mapping)) for r in res]

    async def executor(method: str, path: str, body: Any = None) -> Any:
        try:
            r = await state["client"].request(method, cfg.executor_url + path, json=body)
        except httpx.HTTPError as e:
            raise HTTPException(503, f"executor unavailable: {e}") from e
        if r.status_code >= 400:
            raise HTTPException(r.status_code if r.status_code < 500 else 502, r.json().get("error", "executor error"))
        return r.json()

    # ------------------------------------------------------------------ health
    @app.get("/health")
    async def health() -> JSONResponse:
        db_ok, redis_ok, indexer = False, False, None
        try:
            await rows("SELECT 1 AS ok")
            db_ok = True
        except Exception as e:  # pragma: no cover - reported, not raised
            log.warning("db health: %s", e)
        try:
            redis_ok = bool(await state["redis"].ping())
        except Exception:
            pass
        try:
            r = await state["client"].get(cfg.indexer_health_url, timeout=5)
            indexer = r.json()
        except Exception:
            indexer = None
        ok = db_ok and redis_ok and (indexer or {}).get("status") == "ok"
        body = {"status": "ok" if ok else "degraded", "db": db_ok, "redis": redis_ok,
                "indexer": {k: (indexer or {}).get(k) for k in ("status", "hot_pools", "hot_stats_age_s",
                                                                "coverage_tvl10k", "counts")} if indexer else None}
        return JSONResponse(body, status_code=200 if ok else 503)

    # ------------------------------------------------------------------ pools
    @app.get("/v1/presets")
    async def presets() -> dict[str, Any]:
        return {"presets": [{"id": k, **v} for k, v in PRESETS.items()]}

    @app.get("/v1/pools")
    async def pools(request: Request) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            q = build_query(dict(request.query_params), cfg.stats_max_age_min)
        except (FilterError, ValueError) as e:
            raise HTTPException(400, str(e)) from e
        data = await rows(q.sql, **q.params)
        total = data[0]["total"] if data else 0
        for d in data:
            d.pop("total", None)
        return {"total": total, "limit": q.params["limit"], "offset": q.params["offset"],
                "took_ms": round((time.perf_counter() - t0) * 1000, 1), "data": data}

    @app.get("/v1/pools/{address}")
    async def pool(address: str = Path(..., pattern=ADDR)) -> dict[str, Any]:
        q = build_query({"q": address, "limit": 1}, 24 * 60)
        data = await rows(q.sql, **q.params)
        if not data:
            base = await rows("SELECT * FROM pools WHERE address = :a", a=address)
            if not base:
                raise HTTPException(404, "pool not found")
            data = base
        out = data[0]
        out.pop("total", None)
        live = await state["redis"].hgetall(f"pool:{address}")
        out["live"] = live or None
        out["tokens"] = await rows("SELECT * FROM tokens WHERE mint IN (:x, :y)",
                                   x=out.get("token_x"), y=out.get("token_y"))
        metrics = await rows("SELECT ts, lp_score, metrics FROM pool_metrics_1m WHERE pool = :a "
                             "ORDER BY ts DESC LIMIT 1", a=address)
        out["signal"] = metrics[0] if metrics else None
        return out

    @app.get("/v1/pools/{address}/ohlcv")
    async def ohlcv(address: str = Path(..., pattern=ADDR), timeframe: str = Query("5m", pattern="^(5m|1h)$"),
                    hours: int = Query(24, ge=1, le=72)) -> dict[str, Any]:
        if timeframe == "5m":
            sql = ("SELECT ts, open, high, low, close, volume, fees FROM pool_ohlcv_5m "
                   "WHERE pool = :a AND ts > now() - make_interval(hours => :h) ORDER BY ts")
        else:
            sql = ("SELECT date_trunc('hour', ts) AS ts, (array_agg(open ORDER BY ts))[1] AS open, max(high) AS high, "
                   "min(low) AS low, (array_agg(close ORDER BY ts DESC))[1] AS close, sum(volume) AS volume, "
                   "sum(fees) AS fees FROM pool_ohlcv_5m WHERE pool = :a AND ts > now() - make_interval(hours => :h) "
                   "GROUP BY 1 ORDER BY 1")
        return {"pool": address, "timeframe": timeframe, "data": await rows(sql, a=address, h=hours)}

    @app.get("/v1/pools/{address}/fee-tvl")
    async def fee_tvl(address: str = Path(..., pattern=ADDR), hours: int = Query(24, ge=1, le=168)) -> dict[str, Any]:
        sql = ("SELECT ts, tvl, fee_tvl_1h, fee_tvl_24h, fees_1h, volume_1h, dynamic_fee_pct FROM pool_stats "
               "WHERE pool = :a AND ts > now() - make_interval(hours => :h) ORDER BY ts")
        return {"pool": address, "data": await rows(sql, a=address, h=hours)}

    @app.get("/v1/pools/{address}/flow")
    async def flow(address: str = Path(..., pattern=ADDR), hours: int = Query(24, ge=1, le=168)) -> dict[str, Any]:
        sql = ("SELECT ts, tvl_start, tvl_end, price_effect_usd, net_flow_usd FROM pool_tvl_flow "
               "WHERE pool = :a AND ts > now() - make_interval(hours => :h) ORDER BY ts")
        return {"pool": address, "data": await rows(sql, a=address, h=hours)}

    @app.get("/v1/pools/{address}/bins")
    async def bins(address: str = Path(..., pattern=ADDR), live: bool = False,
                   each_side: int = Query(70, ge=5, le=150)) -> dict[str, Any]:
        """Latest stored snapshot (watched pools), or a live read through the executor."""
        if not live:
            snap = await rows("SELECT ts, active_bin_id, bin_step, active_price, bins FROM bins_snapshot "
                              "WHERE pool = :a AND ts > now() - interval '10 minutes' ORDER BY ts DESC LIMIT 1",
                              a=address)
            if snap:
                return {"pool": address, "source": "snapshot", **snap[0]}
        d = await executor("GET", f"/v1/pools/{address}/bins?left={each_side}&right={each_side}")
        return {"pool": address, "source": "live", "ts": time.time(), "active_bin_id": d["activeBinId"],
                "bin_step": d["binStep"], "active_price": d["activePrice"], "bins": d["bins"]}

    @app.get("/v1/tokens/{mint}")
    async def token(mint: str = Path(..., pattern=ADDR)) -> dict[str, Any]:
        t = await rows("SELECT * FROM tokens WHERE mint = :m", m=mint)
        if not t:
            raise HTTPException(404, "token not found")
        pools_ = await rows("SELECT address, name, bin_step, base_fee_pct, tvl, volume_24h, fees_24h, is_hot "
                            "FROM pools WHERE token_x = :m OR token_y = :m ORDER BY tvl DESC NULLS LAST LIMIT 50",
                            m=mint)
        return {**t[0], "pools": pools_}

    @app.get("/v1/tokens/{mint}/ohlcv")
    async def token_ohlcv(mint: str = Path(..., pattern=ADDR),
                          minutes: int = Query(120, ge=5, le=1440)) -> dict[str, Any]:
        sql = ("SELECT ts, open, high, low, close, volume FROM token_ohlcv_1m WHERE mint = :m "
               "AND ts > now() - make_interval(mins => :n) ORDER BY ts")
        return {"mint": mint, "data": await rows(sql, m=mint, n=minutes)}

    # ------------------------------------------------------------------ live stream (SSE)
    @app.get("/v1/stream")
    async def stream(request: Request) -> StreamingResponse:
        """Server-sent events: a `hot` event with the top pools every time the indexer refreshes (and every 30 s)."""
        redis: Redis = state["redis"]

        async def gen() -> AsyncIterator[str]:
            pubsub = redis.pubsub()
            await pubsub.subscribe("pools:hot")
            try:
                last = 0.0
                yield "retry: 5000\n\n"
                while not await request.is_disconnected():
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if msg or time.time() - last >= cfg.stream_every_s:
                        last = time.time()
                        top = await redis.zrevrange("rank:fees_1h", 0, 49)
                        pipe = redis.pipeline(transaction=False)
                        for a in top:
                            pipe.hgetall("pool:" + (a.decode() if isinstance(a, bytes) else str(a)))
                        payload = {"ts": last, "pools": [h for h in await pipe.execute() if h]}
                        yield f"event: hot\ndata: {json.dumps(payload)}\n\n"
                    await asyncio.sleep(0.2)
            finally:
                await pubsub.unsubscribe("pools:hot")
                await pubsub.aclose()

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ wallet LP (non-custodial)
    @app.get("/v1/wallet/{owner}/positions")
    async def positions(owner: str = Path(..., pattern=ADDR)) -> Any:
        """The wallet's DLMM positions (read on-chain by the executor), valued with our token prices."""
        d = await executor("GET", f"/v1/users/{owner}/positions")
        pos = d.get("positions") or []
        mints = sorted({m for p in pos for m in (p.get("tokenX"), p.get("tokenY")) if m})
        pools_ = sorted({p["pool"] for p in pos})
        prices = {r["mint"]: r["price_usd"] for r in await rows(
            "SELECT mint, price_usd FROM tokens WHERE mint = ANY(:m)", m=mints)} if mints else {}
        names = {r["address"]: r["name"] for r in await rows(
            "SELECT address, name FROM pools WHERE address = ANY(:a)", a=pools_)} if pools_ else {}
        for p in pos:
            px, py = prices.get(p.get("tokenX")), prices.get(p.get("tokenY"))
            known = px is not None and py is not None
            p["name"] = names.get(p["pool"])
            p["valueUsd"] = p["amountX"] * px + p["amountY"] * py if known else None
            p["feesUsd"] = p["feeX"] * px + p["feeY"] * py if known else None
        return {"owner": owner, "positions": pos}

    @app.post("/v1/tx/{pool}/add-liquidity")
    async def add_liquidity(body: AddLiquidityBody, pool: str = Path(..., pattern=ADDR)) -> Any:
        """Unsigned transaction for the user's wallet (plus the cost preview). The server never sees the user's key."""
        if body.max_bin_id < body.min_bin_id or body.max_bin_id - body.min_bin_id > 300:
            raise HTTPException(400, "invalid bin range")
        return await executor("POST", f"/v1/pools/{pool}/tx/add-liquidity", body.model_dump())

    @app.post("/v1/tx/{pool}/remove-liquidity")
    async def remove_liquidity(body: RemoveLiquidityBody, pool: str = Path(..., pattern=ADDR)) -> Any:
        return await executor("POST", f"/v1/pools/{pool}/tx/remove-liquidity", body.model_dump())

    @app.post("/v1/tx/{pool}/claim-fees")
    async def claim(body: ClaimBody, pool: str = Path(..., pattern=ADDR)) -> Any:
        return await executor("POST", f"/v1/pools/{pool}/tx/claim-fees", body.model_dump())

    @app.post("/v1/tx/send")
    async def send_signed(body: SendBody) -> Any:
        """Relay a transaction the user's wallet signed (keeps the RPC key off the browser)."""
        return await executor("POST", "/v1/tx/send", body.model_dump())

    @app.get("/v1/tx/{pool}/quote")
    async def quote(pool: str = Path(..., pattern=ADDR), user: str = Query(..., pattern=ADDR),
                    min_bin_id: int = Query(...), max_bin_id: int = Query(...)) -> Any:
        """Cost preview only: position rent, new bin arrays, transaction fees."""
        return await executor("GET", f"/v1/pools/{pool}/quote?user={user}&min_bin_id={min_bin_id}"
                                     f"&max_bin_id={max_bin_id}")

    return app


def run() -> None:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=8000, proxy_headers=True, forwarded_allow_ips="*",
                log_level="info")


if __name__ == "__main__":
    run()
