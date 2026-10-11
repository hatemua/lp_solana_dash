"""DexScreener public API (read-only, no key): discovery lists and per-token pair data, merged into screener rows.

Limits: 60 req/min on the boost/profile lists, 300 req/min on /tokens. We call each list once per refresh and
/tokens in batches of 30 mints.
"""

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("lp_api.dexscreener")
BASE = "https://api.dexscreener.com"
DISCOVERY = {"boosts_top": "token-boosts/top/v1", "boosts_latest": "token-boosts/latest/v1",
             "profiles_latest": "token-profiles/latest/v1", "cto": "community-takeovers/latest/v1"}
BATCH = 30
WINDOWS = ("m5", "h1", "h6", "h24")


async def _get(client: httpx.AsyncClient, path: str) -> Any:
    try:
        r = await client.get(f"{BASE}/{path}", timeout=20, headers={"accept": "application/json"})
        if r.status_code == 200:
            return r.json()
        log.info("dexscreener %s: HTTP %s", path, r.status_code)
    except (httpx.HTTPError, ValueError) as e:
        log.info("dexscreener %s: %s", path, e)
    return None


async def discovery(client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    """Solana mints on DexScreener's boost, new-profile and community-takeover lists -> what we know about them."""
    got = await asyncio.gather(*(_get(client, p) for p in DISCOVERY.values()))
    out: dict[str, dict[str, Any]] = {}
    for name, data in zip(DISCOVERY, got, strict=True):
        for x in data if isinstance(data, list) else []:
            m = x.get("tokenAddress")
            if x.get("chainId") != "solana" or not m:
                continue
            d = out.setdefault(m, {"lists": [], "boost": 0.0})
            d["lists"].append(f"dexscreener/{name}")
            d["boost"] = max(d["boost"], float(x.get("totalAmount") or x.get("amount") or 0))
    return out


def aggregate(pairs: list[dict[str, Any]], mint: str) -> dict[str, Any] | None:
    """One token's DexScreener pairs (where it is the base token) summed into one row."""
    ps = [p for p in pairs if (p.get("baseToken") or {}).get("address") == mint and p.get("chainId") == "solana"]
    if not ps:
        return None
    main = max(ps, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    vol = {w: sum(float((p.get("volume") or {}).get(w) or 0) for p in ps) for w in WINDOWS}
    buys = {w: sum(int(((p.get("txns") or {}).get(w) or {}).get("buys") or 0) for p in ps) for w in WINDOWS}
    sells = {w: sum(int(((p.get("txns") or {}).get(w) or {}).get("sells") or 0) for p in ps) for w in WINDOWS}
    info = main.get("info") or {}
    socials = {str(s.get("type") or "").lower() for s in info.get("socials") or []}
    try:
        raw = main.get("priceUsd")
        price = float(raw) if raw else None
    except ValueError:
        price = None
    return {
        "ds_price": price,
        "ds_liquidity": sum(float((p.get("liquidity") or {}).get("usd") or 0) for p in ps),
        "ds_volume_5m": vol["m5"], "ds_volume_1h": vol["h1"], "ds_volume_6h": vol["h6"], "ds_volume_24h": vol["h24"],
        "ds_buys_5m": buys["m5"], "ds_sells_5m": sells["m5"], "ds_buys_1h": buys["h1"], "ds_sells_1h": sells["h1"],
        "ds_txn_ratio_1h": round(buys["h1"] / sells["h1"], 3) if sells["h1"] else None,
        "ds_pairs": len(ps), "ds_dexes": sorted({str(p.get("dexId")) for p in ps}), "ds_main_dex": main.get("dexId"),
        "ds_url": main.get("url"),
        "has_website": bool(info.get("websites")), "has_twitter": "twitter" in socials,
        "has_telegram": "telegram" in socials,
    }


async def pairs_for(client: httpx.AsyncClient, mints: list[str]) -> dict[str, dict[str, Any]]:
    """DexScreener aggregate per mint (missing mints are simply absent). Batches of 30, 4 at a time."""
    sem = asyncio.Semaphore(4)
    out: dict[str, dict[str, Any]] = {}

    async def one(batch: list[str]) -> None:
        async with sem:
            data = await _get(client, "tokens/v1/solana/" + ",".join(batch))
        for m in batch:
            if isinstance(data, list) and (agg := aggregate(data, m)):
                out[m] = agg

    await asyncio.gather(*(one(mints[i:i + BATCH]) for i in range(0, len(mints), BATCH)))
    return out
