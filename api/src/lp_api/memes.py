"""Memecoin screener: Jupiter's live token lists (trending, most traded, top organic, recent), normalized, flagged
for risk, scored, filtered. Pure functions; main.py fetches the lists, caches them and joins our Meteora pools.

Data only: the scores rank what the market is doing now, they are not buy signals.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SOL_MINT = "So11111111111111111111111111111111111111112"
JUP_TOKENS = "https://lite-api.jup.ag/tokens/v2"
LISTS = ("toptrending/5m", "toptrending/1h", "toptrending/6h", "toptrending/24h", "toptraded/1h", "toptraded/24h",
         "toporganicscore/1h", "toporganicscore/24h", "recent")

# Not memecoins: SOL, stables, liquid staking, majors. Tokens tagged like these are dropped too.
NOT_MEME_MINTS = {
    "So11111111111111111111111111111111111111112",   # SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",   # JUP
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # JitoSOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",   # mSOL
    "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v",   # JupSOL
    "27G8MtK7VtTcCHkpASjSDdkWWYfoqT6ggEuKidVJidD4",  # JLP
}
NOT_MEME_TAGS = {"stablecoin", "lst", "lrt", "xstocks", "stocks", "equities", "tokenized-stock", "rwa", "defi", "infra"}
NOT_MEME_SYMBOLS = {"SOL", "WSOL", "USDC", "USDT", "PYUSD", "USDS", "USD1", "EURC", "WBTC", "CBBTC", "WETH", "JLP",
                    "BTC", "ETH", "ZEC", "XMR", "QNT", "WNEAR", "HYPE", "ENA", "XRP", "BNB", "SUI", "LINK"}


@dataclass(frozen=True)
class Preset:
    label: str
    description: str
    filters: dict[str, float | bool]
    sort: str


PRESETS: dict[str, Preset] = {
    "safe_runners": Preset(
        "Safe runners", "Older than a day, real liquidity and holders, no red flags, buyers in control, rising",
        {"age_h_min": 24, "mcap_min": 1_000_000, "liquidity_min": 100_000, "holders_min": 2_000, "top10_max": 30,
         "organic_min": 50, "buy_ratio_1h_min": 1.1, "chg_1h_min": 0, "chg_1h_max": 40, "safe_only": True},
        "momentum"),
    "dip_recovery": Preset(
        "Dip recovery", "Down 20%+ in 24 h but turning: up this hour with buyers ahead, at least 3 days old",
        {"age_h_min": 72, "liquidity_min": 100_000, "chg_24h_max": -20, "chg_1h_min": 1, "buy_ratio_1h_min": 1.2,
         "holders_min": 2_000, "safe_only": True},
        "chg_1h"),
    "established": Preset(
        "Established memes", "Mcap $50M+, 30+ days old, deep liquidity: lower risk, lower upside",
        {"age_h_min": 720, "mcap_min": 50_000_000, "liquidity_min": 1_000_000, "safe_only": True},
        "volume_24h"),
    "new_momentum": Preset(
        "New momentum (high risk)", "Under 24 h old with real liquidity and organic buyers. Most of these go to zero",
        {"age_h_max": 24, "liquidity_min": 30_000, "organic_min": 40, "holders_min": 300, "buy_ratio_1h_min": 1.0,
         "top10_max": 35},
        "momentum"),
    "lp_candidates": Preset(
        "LP candidates", "Has a Meteora SOL pool, busy and safe: tokens worth providing liquidity for",
        {"meteora_only": True, "age_h_min": 24, "volume_1h_min": 50_000, "safe_only": True},
        "volume_1h"),
}

FILTERS = {  # param -> (field, op)
    "mcap_min": ("mcap", ">="), "mcap_max": ("mcap", "<="),
    "liquidity_min": ("liquidity", ">="),
    "age_h_min": ("age_h", ">="), "age_h_max": ("age_h", "<="),
    "holders_min": ("holders", ">="),
    "top10_max": ("top10_pct", "<="),
    "organic_min": ("organic_score", ">="),
    "volume_1h_min": ("volume_1h", ">="), "volume_24h_min": ("volume_24h", ">="),
    "buy_ratio_1h_min": ("buy_ratio_1h", ">="),
    "chg_5m_min": ("chg_5m", ">="), "chg_5m_max": ("chg_5m", "<="),
    "chg_1h_min": ("chg_1h", ">="), "chg_1h_max": ("chg_1h", "<="),
    "chg_24h_min": ("chg_24h", ">="), "chg_24h_max": ("chg_24h", "<="),
    "holder_chg_1h_min": ("holder_chg_1h", ">="),
    "momentum_min": ("momentum", ">="), "safety_min": ("safety", ">="),
}
SORTS = {"momentum", "safety", "mcap", "liquidity", "volume_1h", "volume_24h", "chg_5m", "chg_1h", "chg_6h",
         "chg_24h", "holders", "holder_chg_1h", "age_h", "organic_score", "buy_ratio_1h", "traders_1h"}


class ScreenError(ValueError):
    pass


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None                     # NaN -> None


def _age_h(t: dict[str, Any], now: datetime) -> float | None:
    raw = (t.get("firstPool") or {}).get("createdAt") or t.get("createdAt")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (now - ts).total_seconds() / 3600)


def is_meme(t: dict[str, Any]) -> bool:
    tags = {str(x).lower() for x in t.get("tags") or []}
    return (t.get("id") not in NOT_MEME_MINTS and not tags & NOT_MEME_TAGS
            and str(t.get("symbol") or "").upper() not in NOT_MEME_SYMBOLS
            and not str(t.get("name") or "").lower().startswith("wrapped "))


def flags(r: dict[str, Any]) -> list[dict[str, str]]:
    """Risk flags: level 'red' (avoid) or 'warn' (be careful)."""
    out: list[dict[str, str]] = []

    def add(level: str, code: str, text: str) -> None:
        out.append({"level": level, "code": code, "text": text})

    if r["mint_disabled"] is False:
        add("red", "mint_authority", "dev can mint new tokens")
    if r["freeze_disabled"] is False:
        add("red", "freeze_authority", "dev can freeze your tokens")
    top10 = r["top10_pct"]
    if top10 is not None and top10 > 50:
        add("red", "concentrated", f"top 10 holders own {top10:.0f}%")
    elif top10 is not None and top10 > 30:
        add("warn", "concentrated", f"top 10 holders own {top10:.0f}%")
    liq, mcap = r["liquidity"], r["mcap"]
    if liq is not None and liq < 20_000:
        add("red", "thin_liquidity", f"only ${liq:,.0f} liquidity")
    elif liq is not None and mcap and liq / mcap < 0.02:
        add("warn", "thin_liquidity", f"liquidity is {liq / mcap * 100:.1f}% of mcap")
    if r["age_h"] is not None and r["age_h"] < 24:
        add("warn", "young", "under 24 h old: crashes are ~50x more frequent")
    if r["holders"] is not None and r["holders"] < 500:
        add("warn", "few_holders", f"{r['holders']:.0f} holders")
    if r["organic_score"] is not None and r["organic_score"] < 30:
        add("warn", "low_organic", "mostly bot / wash volume")
    if (r["sell_1h"] + r["buy_1h"] >= 10_000) and r["sell_1h"] > 2 * max(r["buy_1h"], 1):
        add("red", "sell_pressure", "sellers 2x buyers in the last hour")
    if r["chg_1h"] is not None and r["chg_1h"] <= -25:
        add("red", "dumping", f"{r['chg_1h']:.0f}% in 1 h")
    if r["holder_chg_1h"] is not None and r["holder_chg_1h"] <= -2:
        add("warn", "holders_leaving", f"holders {r['holder_chg_1h']:.1f}% in 1 h")
    if (r["dev_mints"] or 0) >= 100 and (r["age_h"] or 0) < 30 * 24:      # old tokens: launchpad deployer
        add("warn", "serial_dev", f"dev launched {r['dev_mints']:.0f} tokens")
    if r["chg_1h"] is not None and r["chg_1h"] >= 100:
        add("warn", "parabolic", f"+{r['chg_1h']:.0f}% in 1 h: late entries often top-buy")
    return out


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def safety_score(r: dict[str, Any]) -> float:
    """0-100: authorities off, spread holders, deep liquidity, age, organic volume, no red flags."""
    s = 0.0
    s += 15 if r["mint_disabled"] else 0
    s += 10 if r["freeze_disabled"] else 0
    s += 15 * _clip((60 - (r["top10_pct"] if r["top10_pct"] is not None else 60)) / 45)
    s += 15 * _clip(((r["liquidity"] or 0) - 20_000) / 480_000)
    s += 15 * _clip((r["age_h"] or 0) / 168)
    s += 10 * _clip((r["organic_score"] or 0) / 80)
    s += 10 * _clip((r["holders"] or 0) / 10_000)
    s += 10 * _clip(((r["liquidity"] or 0) / r["mcap"] if r["mcap"] else 0) / 0.10)
    reds = sum(1 for f in r["flags"] if f["level"] == "red")
    return round(max(0.0, s - 25 * reds), 1)


def momentum_score(r: dict[str, Any]) -> float:
    """0-100: volume vs size, buyers ahead of sellers, net new buyers and holders, price rising (not parabolic)."""
    s = 0.0
    turnover = (r["volume_1h"] or 0) / max(r["liquidity"] or 0, 1)
    s += 25 * _clip(turnover / 0.5)                                       # 50% of liquidity traded per hour = max
    s += 20 * _clip(((r["buy_ratio_1h"] or 1) - 0.9) / 0.6)               # 1.5x buys = max
    s += 15 * _clip((r["net_buyers_1h"] or 0) / max((r["traders_1h"] or 0) * 0.3, 10))
    s += 15 * _clip((r["holder_chg_1h"] or 0) / 2)
    chg = r["chg_1h"] or 0
    s += 15 * (_clip(chg / 15) if chg <= 60 else _clip((150 - chg) / 90))  # rising, but parabolic fades
    s += 10 * _clip((r["organic_score"] or 0) / 80)
    return round(s, 1)


def normalize(t: dict[str, Any], lists: list[str], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    s5, s1, s6, s24 = (t.get(k) or {} for k in ("stats5m", "stats1h", "stats6h", "stats24h"))
    audit = t.get("audit") or {}
    buy1, sell1 = _f(s1.get("buyVolume")) or 0.0, _f(s1.get("sellVolume")) or 0.0
    r: dict[str, Any] = {
        "mint": t.get("id"), "symbol": t.get("symbol"), "name": t.get("name"), "icon": t.get("icon"),
        "launchpad": t.get("launchpad"), "verified": bool(t.get("isVerified")), "tags": t.get("tags") or [],
        "twitter": t.get("twitter"), "website": t.get("website"),
        "price": _f(t.get("usdPrice")), "mcap": _f(t.get("mcap")), "fdv": _f(t.get("fdv")),
        "liquidity": _f(t.get("liquidity")), "holders": _f(t.get("holderCount")), "age_h": _age_h(t, now),
        "organic_score": _f(t.get("organicScore")), "organic_label": t.get("organicScoreLabel"),
        "mint_disabled": audit.get("mintAuthorityDisabled"), "freeze_disabled": audit.get("freezeAuthorityDisabled"),
        "top10_pct": _f(audit.get("topHoldersPercentage")), "dev_mints": _f(audit.get("devMints")),
        "chg_5m": _f(s5.get("priceChange")), "chg_1h": _f(s1.get("priceChange")),
        "chg_6h": _f(s6.get("priceChange")), "chg_24h": _f(s24.get("priceChange")),
        "volume_5m": (_f(s5.get("buyVolume")) or 0) + (_f(s5.get("sellVolume")) or 0),
        "volume_1h": buy1 + sell1,
        "volume_24h": (_f(s24.get("buyVolume")) or 0) + (_f(s24.get("sellVolume")) or 0),
        "buy_1h": buy1, "sell_1h": sell1, "buy_ratio_1h": round(buy1 / sell1, 3) if sell1 > 0 else None,
        "traders_1h": _f(s1.get("numTraders")), "net_buyers_1h": _f(s1.get("numNetBuyers")),
        "holder_chg_1h": _f(s1.get("holderChange")), "holder_chg_24h": _f(s24.get("holderChange")),
        "lists": sorted(set(lists)),
    }
    r["flags"] = flags(r)
    r["safety"] = safety_score(r)
    r["momentum"] = momentum_score(r)
    return r


def merge_lists(lists: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Union of Jupiter lists by mint (memes only), each token tagged with the lists it appears in."""
    seen: dict[str, dict[str, Any]] = {}
    where: dict[str, list[str]] = {}
    for name, tokens in lists.items():
        for t in tokens or []:
            m = t.get("id")
            if not m or not is_meme(t):
                continue
            prev = seen.get(m)
            if prev is None or str(t.get("updatedAt") or "") > str(prev.get("updatedAt") or ""):
                seen[m] = t
            where.setdefault(m, []).append(name)
    now = datetime.now(UTC)
    return [normalize(t, where[m], now) for m, t in seen.items()]


def _parse(params: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in params.items():
        if v in ("", None):
            continue
        if k in FILTERS:
            try:
                out[k] = float(v)
            except ValueError as e:
                raise ScreenError(f"{k} must be a number") from e
        elif k in ("safe_only", "meteora_only"):
            out[k] = str(v).lower() in ("1", "true", "yes")
    return out


def screen(tokens: list[dict[str, Any]], params: dict[str, str]) -> dict[str, Any]:
    preset = params.get("preset") or ""
    if preset and preset not in PRESETS:
        raise ScreenError(f"unknown preset {preset!r}")
    p = PRESETS.get(preset)
    f: dict[str, Any] = dict(p.filters) if p else {}
    f.update(_parse(params))                         # explicit params override the preset
    sort = params.get("sort") or (p.sort if p else "momentum")
    if sort not in SORTS:
        raise ScreenError(f"sort must be one of {sorted(SORTS)}")
    order = params.get("order") or "desc"
    try:
        limit = max(1, min(int(params.get("limit") or 100), 500))
    except ValueError as e:
        raise ScreenError("limit must be an integer") from e
    q = (params.get("q") or "").strip().lower()

    def keep(r: dict[str, Any]) -> bool:
        if q and q not in str(r["symbol"] or "").lower() and q not in str(r["name"] or "").lower() and q != r["mint"]:
            return False
        if f.get("safe_only") and any(x["level"] == "red" for x in r["flags"]):
            return False
        if f.get("meteora_only") and not r.get("meteora_pool"):
            return False
        for k, (field, op) in FILTERS.items():
            if k not in f:
                continue
            v = r.get(field)
            if v is None or (op == ">=" and v < f[k]) or (op == "<=" and v > f[k]):
                return False
        return True

    rows = [r for r in tokens if keep(r)]
    rows.sort(key=lambda r: (r.get(sort) is None, -(r.get(sort) or 0) if order == "desc" else (r.get(sort) or 0)))
    return {"total": len(rows), "universe": len(tokens), "preset": preset or None, "filters": f, "sort": sort,
            "order": order, "data": rows[:limit]}
