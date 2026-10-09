"""Upstream APIs: Meteora DLMM data API, Jupiter data API, and our executor (bins via the DLMM SDK)."""

from typing import Any

from .http import Upstream

OHLCV_MAX_WINDOW_S = {"5m": 6 * 3600, "1h": 72 * 3600}   # API limit per call


class Meteora:
    def __init__(self, up: Upstream) -> None:
        self.up = up

    async def pools(self, page: int, page_size: int, sort_by: str) -> dict[str, Any] | None:
        d = await self.up.get_json("/pools", {"page": page, "page_size": page_size, "sort_by": sort_by})
        return d if isinstance(d, dict) and isinstance(d.get("data"), list) else None

    async def pool(self, address: str) -> dict[str, Any] | None:
        d = await self.up.get_json(f"/pools/{address}")
        return d if isinstance(d, dict) and d.get("address") else None

    async def ohlcv(self, address: str, timeframe: str, start: int, end: int) -> dict[str, Any] | None:
        d = await self.up.get_json(f"/pools/{address}/ohlcv",
                                   {"timeframe": timeframe, "start_time": start, "end_time": end})
        return d if isinstance(d, dict) else None

    async def volume_history(self, address: str, timeframe: str, start: int, end: int) -> dict[str, Any] | None:
        d = await self.up.get_json(f"/pools/{address}/volume/history",
                                   {"timeframe": timeframe, "start_time": start, "end_time": end})
        return d if isinstance(d, dict) else None


class Jupiter:
    MAX_MINTS = 50

    def __init__(self, up: Upstream) -> None:
        self.up = up

    async def assets(self, mints: list[str]) -> list[dict[str, Any]]:
        d = await self.up.get_json("/v1/assets/search", {"query": ",".join(mints[: self.MAX_MINTS])})
        return [a for a in d if isinstance(a, dict) and a.get("id")] if isinstance(d, list) else []

    async def chart(self, mint: str, interval: str, to_ms: int, candles: int) -> dict[str, Any] | None:
        d = await self.up.get_json(f"/v2/charts/{mint}", {"interval": interval, "to": to_ms, "candles": candles})
        return d if isinstance(d, dict) else None


class Executor:
    """Our Node service around the Meteora DLMM SDK (read-only calls in M1)."""

    def __init__(self, up: Upstream) -> None:
        self.up = up

    async def bins(self, pool: str, each_side: int) -> dict[str, Any] | None:
        d = await self.up.get_json(f"/v1/pools/{pool}/bins", {"left": each_side, "right": each_side})
        return d if isinstance(d, dict) and "activeBinId" in d else None
