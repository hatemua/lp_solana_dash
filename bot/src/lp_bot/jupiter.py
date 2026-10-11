"""Real swap costs from Jupiter quotes (read-only: quotes only, nothing is signed or sent)."""

import asyncio
import logging
import time

import httpx

from .config import SOL_MINT

log = logging.getLogger("lp_bot.jupiter")
QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
DEFAULT_COST = 0.015            # used when no quote is available
MAX_COST = 0.15
_last = 0.0
_lock = asyncio.Lock()


async def _quote(client: httpx.AsyncClient, in_mint: str, out_mint: str, amount_raw: int) -> int | None:
    global _last
    for attempt in range(3):                               # the keyless API answers 429 in bursts (the server's IP
        async with _lock:                                  # is shared with the meme bot): back off and retry
            wait = 1.0 - (time.time() - _last)
            if wait > 0:
                await asyncio.sleep(wait)
            _last = time.time()
        try:
            r = await client.get(QUOTE_URL, params={"inputMint": in_mint, "outputMint": out_mint,
                                                    "amount": amount_raw, "slippageBps": 100},
                                 headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            if r.status_code == 200:
                return int(r.json()["outAmount"])
            log.debug("quote %s: %s", r.status_code, r.text[:200])
            if r.status_code != 429:
                return None
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.debug("quote failed: %s", e)
            return None
        await asyncio.sleep(2.0 * (attempt + 1))
    return None


def cost_fraction(value_in_sol: float, value_out_sol: float) -> float:
    """Share of the value lost in a swap (fees + price impact), both sides valued at the pool price."""
    if value_in_sol <= 0:
        return DEFAULT_COST
    return min(MAX_COST, max(0.0, 1 - value_out_sol / value_in_sol))


async def buy_cost(client: httpx.AsyncClient, mint: str, decimals: int, sol: float, price: float) -> float:
    """Cost of buying the token with `sol` SOL; price = SOL per token (pool)."""
    out = await _quote(client, SOL_MINT, mint, int(sol * 1e9))
    if out is None:
        return DEFAULT_COST
    return cost_fraction(sol, out / 10 ** decimals * price)


async def round_trip(client: httpx.AsyncClient, mint: str, decimals: int, sol: float,
                     price: float) -> tuple[float, float] | None:
    """Buy the token with `sol` and sell it straight back: (cost share of the round trip, gap between the price
    Jupiter fills at and the pool price we mark at). None when a quote fails. Unlike buy_cost, nothing here depends
    on the pool price being right, which it was not (-4%..+10% vs the real fills on 2026-10-11)."""
    out = await _quote(client, SOL_MINT, mint, int(sol * 1e9))
    if not out:
        return None
    back = await _quote(client, mint, SOL_MINT, out)
    if not back:
        return None
    return 1 - back / 1e9 / sol, sol / (out / 10 ** decimals) / price - 1


async def sell_cost(client: httpx.AsyncClient, mint: str, decimals: int, tokens: float, price: float) -> float:
    """Cost of selling `tokens` back to SOL; price = SOL per token (pool)."""
    raw = int(tokens * 10 ** decimals)
    if raw <= 0:
        return 0.0
    out = await _quote(client, mint, SOL_MINT, raw)
    if out is None:
        return DEFAULT_COST
    return cost_fraction(tokens * price, out / 1e9)
