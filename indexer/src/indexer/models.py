"""Pure transforms from upstream JSON to table rows (unit-tested with real API fixtures)."""

from datetime import UTC, datetime
from typing import Any

from .config import SOL_MINT


def fnum(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def ts_from_ms(ms: Any) -> datetime | None:
    f = fnum(ms)
    return datetime.fromtimestamp(f / 1000.0, UTC) if f else None


def ts_from_s(s: Any) -> datetime | None:
    f = fnum(s)
    return datetime.fromtimestamp(f, UTC) if f else None


def iso_ts(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def is_sol_pair(p: dict[str, Any]) -> bool:
    return SOL_MINT in ((p.get("token_x") or {}).get("address"), (p.get("token_y") or {}).get("address"))


def other_mint(p: dict[str, Any]) -> str | None:
    """The non-SOL token of a SOL pair (None for other pairs)."""
    x, y = (p.get("token_x") or {}).get("address"), (p.get("token_y") or {}).get("address")
    if x == SOL_MINT:
        return y
    if y == SOL_MINT:
        return x
    return None


def window(d: Any, key: str) -> float | None:
    return fnum((d or {}).get(key))


def is_hot(p: dict[str, Any], min_tvl: float, min_volume_1h: float) -> bool:
    """Hot pool: SOL pair with TVL >= min_tvl or 1 h volume >= min_volume_1h."""
    if not is_sol_pair(p) or p.get("is_blacklisted"):
        return False
    return (fnum(p.get("tvl")) or 0) >= min_tvl or (window(p.get("volume"), "1h") or 0) >= min_volume_1h


def pool_row(p: dict[str, Any], now: datetime) -> dict[str, Any]:
    cfg = p.get("pool_config") or {}
    return {
        "address": p["address"],
        "name": p.get("name"),
        "token_x": (p.get("token_x") or {}).get("address"),
        "token_y": (p.get("token_y") or {}).get("address"),
        "bin_step": int(cfg["bin_step"]) if cfg.get("bin_step") is not None else None,
        "base_fee_pct": fnum(cfg.get("base_fee_pct")),
        "max_fee_pct": fnum(cfg.get("max_fee_pct")),
        "protocol_fee_pct": fnum(cfg.get("protocol_fee_pct")),
        "created_at": ts_from_ms(p.get("created_at")),
        "launchpad": p.get("launchpad") or None,
        "tags": list(p.get("tags") or []),
        "is_blacklisted": bool(p.get("is_blacklisted")),
        "tvl": fnum(p.get("tvl")),
        "volume_24h": window(p.get("volume"), "24h"),
        "fees_24h": window(p.get("fees"), "24h"),
        "updated_at": now,
    }


def token_rows(p: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Basic token info that the pool list carries (enriched later from Jupiter)."""
    out = []
    for side in ("token_x", "token_y"):
        t = p.get(side) or {}
        if not t.get("address"):
            continue
        out.append({
            "mint": t["address"],
            "symbol": t.get("symbol"),
            "name": t.get("name"),
            "decimals": t.get("decimals"),
            "holders": t.get("holders"),
            "mcap": fnum(t.get("market_cap")),
            "price_usd": fnum(t.get("price")),
            "freeze_authority_disabled": t.get("freeze_authority_disabled"),
            "is_verified": t.get("is_verified"),
            "updated_at": now,
        })
    return out


def stat_row(p: dict[str, Any], ts: datetime, volume_5m: float | None = None,
             fees_5m: float | None = None) -> dict[str, Any]:
    vol, fees, pfees, ftvl = p.get("volume"), p.get("fees"), p.get("protocol_fees"), p.get("fee_tvl_ratio")
    cum = p.get("cumulative_metrics") or {}
    return {
        "pool": p["address"],
        "ts": ts,
        "price": fnum(p.get("current_price")),
        "tvl": fnum(p.get("tvl")),
        "volume_5m": volume_5m,
        "volume_30m": window(vol, "30m"),
        "volume_1h": window(vol, "1h"),
        "volume_24h": window(vol, "24h"),
        "fees_5m": fees_5m,
        "fees_30m": window(fees, "30m"),
        "fees_1h": window(fees, "1h"),
        "fees_24h": window(fees, "24h"),
        "protocol_fees_1h": window(pfees, "1h"),
        "protocol_fees_24h": window(pfees, "24h"),
        "fee_tvl_1h": window(ftvl, "1h"),
        "fee_tvl_24h": window(ftvl, "24h"),
        "dynamic_fee_pct": fnum(p.get("dynamic_fee_pct")),
        "token_x_amount": fnum(p.get("token_x_amount")),
        "token_y_amount": fnum(p.get("token_y_amount")),
        "price_x_usd": fnum((p.get("token_x") or {}).get("price")),
        "price_y_usd": fnum((p.get("token_y") or {}).get("price")),
        "cum_volume": fnum(cum.get("volume")),
        "cum_fees": fnum(cum.get("fees")),
    }


def ohlcv_rows(pool: str, ohlcv: Any, volume: Any) -> list[dict[str, Any]]:
    """Join the 5-min OHLCV bars with the 5-min fee bars of the same pool, by timestamp."""
    fee_by_ts = {int(b["timestamp"]): b for b in ((volume or {}).get("data") or []) if b.get("timestamp") is not None}
    rows = []
    for b in (ohlcv or {}).get("data") or []:
        t = b.get("timestamp")
        if t is None:
            continue
        f = fee_by_ts.get(int(t), {})
        rows.append({
            "pool": pool, "ts": ts_from_s(t),
            "open": fnum(b.get("open")), "high": fnum(b.get("high")), "low": fnum(b.get("low")),
            "close": fnum(b.get("close")), "volume": fnum(b.get("volume")),
            "fees": fnum(f.get("fees")), "protocol_fees": fnum(f.get("protocol_fees")),
        })
    return rows


def jupiter_token_row(a: dict[str, Any], now: datetime) -> dict[str, Any]:
    audit = a.get("audit") or {}
    return {
        "mint": a["id"],
        "symbol": a.get("symbol"),
        "name": a.get("name"),
        "decimals": a.get("decimals"),
        "created_at": iso_ts(a.get("createdAt")) or iso_ts((a.get("firstPool") or {}).get("createdAt")),
        "launchpad": a.get("launchpad") or None,
        "holders": a.get("holderCount"),
        "mcap": fnum(a.get("mcap")),
        "price_usd": fnum(a.get("usdPrice")),
        "organic_score": fnum(a.get("organicScore")),
        "audit": audit or None,
        "stats": {k: a[k] for k in ("stats5m", "stats1h", "stats24h") if k in a} or None,
        "freeze_authority_disabled": audit.get("freezeAuthorityDisabled"),
        "mint_authority_disabled": audit.get("mintAuthorityDisabled"),
        "updated_at": now,
    }


def candle_rows(mint: str, chart: Any) -> list[dict[str, Any]]:
    return [{"mint": mint, "ts": ts_from_s(c.get("time")), "open": fnum(c.get("open")), "high": fnum(c.get("high")),
             "low": fnum(c.get("low")), "close": fnum(c.get("close")), "volume": fnum(c.get("volume"))}
            for c in (chart or {}).get("candles") or [] if c.get("time") is not None]


def tvl_flow(prev: dict[str, Any], cur: dict[str, Any]) -> dict[str, float | None]:
    """Estimate LP liquidity added/removed between two pool snapshots.

    The value change has two parts: the price move of the reserves that were already there, and liquidity that LPs
    added or removed. A swap exchanges X for Y at about the market price, so the reserve changes it causes are worth
    ~0 at the new prices (plus the fee); adds and removes are not. So:
      net_flow     = (dX * price_x + dY * price_y) at the new prices
      price_effect = old reserves * (new prices - old prices)
    """
    keys = ("token_x_amount", "token_y_amount", "price_x_usd", "price_y_usd")
    if any(prev.get(k) is None for k in keys) or any(cur.get(k) is None for k in keys):
        return {"price_effect_usd": None, "net_flow_usd": None}
    dx = cur["token_x_amount"] - prev["token_x_amount"]
    dy = cur["token_y_amount"] - prev["token_y_amount"]
    net_flow = dx * cur["price_x_usd"] + dy * cur["price_y_usd"]
    price_effect = (prev["token_x_amount"] * (cur["price_x_usd"] - prev["price_x_usd"])
                    + prev["token_y_amount"] * (cur["price_y_usd"] - prev["price_y_usd"]))
    return {"price_effect_usd": price_effect, "net_flow_usd": net_flow}
