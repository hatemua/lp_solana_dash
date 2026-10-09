"""Indexer entry point: run the jobs and serve /health."""

import asyncio
import logging
import signal
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .config import Settings, get_settings
from .db import Database
from .http import Upstream
from .jobs import Indexer
from .redis_store import RedisStore
from .sources import Executor, Jupiter, Meteora

log = logging.getLogger("indexer")


def build_app(indexer: Indexer, upstreams: list[Upstream]) -> FastAPI:
    app = FastAPI(title="lp-indexer", docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> JSONResponse:
        body: dict[str, Any] = await indexer.health()
        body["upstreams"] = {u.name: dict(u.stats) for u in upstreams}
        return JSONResponse(body, status_code=200 if body["status"] == "ok" else 503)

    return app


async def amain(cfg: Settings) -> None:
    db = Database(cfg.database_url)
    await db.migrate()
    redis = RedisStore(cfg.redis_url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), limits=httpx.Limits(max_connections=20)) as client:
        met = Upstream("meteora", cfg.meteora_api, cfg.meteora_rpm, client)
        jup = Upstream("jupiter", cfg.jupiter_data_api, cfg.jupiter_rpm, client)
        exe = Upstream("executor", cfg.executor_url, cfg.executor_rpm, client, retries=1)
        indexer = Indexer(cfg, db, redis, Meteora(met), Jupiter(jup), Executor(exe))
        tasks = indexer.schedule()
        server = uvicorn.Server(uvicorn.Config(build_app(indexer, [met, jup, exe]), host=cfg.health_host,
                                               port=cfg.health_port, log_level="warning"))
        server_task = asyncio.create_task(server.serve())

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, indexer.stop.set)
            except NotImplementedError:       # Windows
                pass
        log.info("indexer started: health on :%s", cfg.health_port)
        await indexer.stop.wait()
        log.info("stopping")
        server.should_exit = True
        await asyncio.gather(*tasks, server_task, return_exceptions=True)
    await redis.close()
    await db.close()


def run() -> None:
    cfg = get_settings()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(amain(cfg))


if __name__ == "__main__":
    run()
