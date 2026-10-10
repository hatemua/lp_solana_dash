"""Server-side pool filters, sorting and presets. Only allow-listed fields; every value is a bound parameter."""

from dataclasses import dataclass
from typing import Any

from .config import SOL_MINT

# numeric columns of the pools view that can be filtered with <field>_min / <field>_max and sorted on
NUMERIC = {
    "tvl", "price", "volume_5m", "volume_1h", "volume_24h", "fees_5m", "fees_1h", "fees_24h", "fee_tvl_1h",
    "fee_tvl_24h", "dynamic_fee_pct", "bin_step", "base_fee_pct", "pool_age_h", "token_age_h", "mcap", "holders",
    "organic_score", "top10_pct", "dev_pct", "token_volume_5m", "token_volume_1h", "price_change_5m",
    "price_change_1h", "price_change_24h", "volume_burst", "lp_score",
}
BOOLEAN = {"sol_pair", "is_hot", "mint_disabled", "freeze_disabled"}
SORTABLE = NUMERIC | {"pool_created_at", "token_symbol"}
MAX_LIMIT = 500

# Preset filters (also listed by GET /v1/presets). Thresholds from docs/research.
PRESETS: dict[str, dict[str, Any]] = {
    "rabbit500": {
        "label": "Rabbit 500k",
        "description": "Token volume on all venues >= $500k in 5 min (Rabbit Strat best setups); SOL pairs.",
        "filters": {"sol_pair": True, "token_volume_5m_min": 500_000, "mint_disabled": True},
        "sort": "token_volume_5m",
    },
    "rabbit300": {
        "label": "Rabbit 300k",
        "description": "Token volume on all venues >= $300k in 5 min; SOL pairs.",
        "filters": {"sol_pair": True, "token_volume_5m_min": 300_000, "mint_disabled": True},
        "sort": "token_volume_5m",
    },
    "fee_burst": {
        "label": "Fee burst",
        "description": "Last-hour pool volume >= 3x its 24 h hourly average, LP fees >= $200 in 1 h, token >= 3 days "
                       "old, price 1 h change between -15% and +30%.",
        "filters": {"sol_pair": True, "volume_burst_min": 3, "fees_1h_min": 200, "token_age_h_min": 72,
                    "price_change_1h_min": -15, "price_change_1h_max": 30},
        "sort": "volume_burst",
    },
    "evil_panda": {
        "label": "Evil Panda",
        "description": "Established, liquid tokens with wide bin steps, for one-sided SOL placed far below the price.",
        "filters": {"sol_pair": True, "token_age_h_min": 24, "volume_24h_min": 1_000_000, "bin_step_min": 80,
                    "tvl_min": 50_000},
        "sort": "fee_tvl_24h",
    },
    "safe_established": {
        "label": "Safe established",
        "description": "Token >= 7 days, >= 1,000 holders, organic score >= 70, mint and freeze authority off, "
                       "top-10 holders <= 30%, TVL >= $50k.",
        "filters": {"sol_pair": True, "token_age_h_min": 168, "holders_min": 1000, "organic_score_min": 70,
                    "mint_disabled": True, "freeze_disabled": True, "top10_pct_max": 30, "tvl_min": 50_000},
        "sort": "fee_tvl_24h",
    },
    # from open-source DLMM bots (docs/research/strategies-web.md)
    "high_volume": {
        "label": "High volume (Scout)",
        "description": "TVL >= $10k, 24 h volume >= $100k, fee/TVL >= 0.5% in 1 h and >= 10% in 24 h, 1 h price "
                       "change between -30% and +50% (LP-Meteora-Scout-Bot stage A/B).",
        "filters": {"sol_pair": True, "tvl_min": 10_000, "volume_24h_min": 100_000, "fee_tvl_1h_min": 0.5,
                    "fee_tvl_24h_min": 10, "price_change_1h_min": -30, "price_change_1h_max": 50},
        "sort": "fee_tvl_1h",
    },
    "meridian": {
        "label": "Meridian",
        "description": "TVL $10k-150k, mcap $150k-10M, >= 500 holders, organic >= 60, top-10 <= 60%, bin step 80-125 "
                       "(Meridian agent defaults; deploys one-sided SOL bid-ask, stop -15%, out of range 30 min).",
        "filters": {"sol_pair": True, "tvl_min": 10_000, "tvl_max": 150_000, "mcap_min": 150_000,
                    "mcap_max": 10_000_000, "holders_min": 500, "organic_score_min": 60, "top10_pct_max": 60,
                    "bin_step_min": 80, "bin_step_max": 125},
        "sort": "fee_tvl_1h",
    },
}


def view_sql(max_age_min: int) -> str:
    """One row per pool with recent stats: pool, latest stats, token (the non-SOL side), M4 score when present."""
    return f"""
WITH latest AS (
    SELECT DISTINCT ON (pool) * FROM pool_stats
    WHERE ts > now() - interval '{int(max_age_min)} minutes' ORDER BY pool, ts DESC
), score AS (
    SELECT DISTINCT ON (pool) pool, lp_score FROM pool_metrics_1m
    WHERE ts > now() - interval '10 minutes' ORDER BY pool, ts DESC
), v AS (
    SELECT p.address, p.name, p.token_x, p.token_y, p.bin_step, p.base_fee_pct, p.created_at AS pool_created_at,
           p.launchpad, p.is_hot, l.ts AS stats_ts, l.price, l.tvl, l.volume_5m, l.volume_1h, l.volume_24h,
           l.fees_5m, l.fees_1h, l.fees_24h, l.fee_tvl_1h, l.fee_tvl_24h, l.dynamic_fee_pct,
           (p.token_x = '{SOL_MINT}' OR p.token_y = '{SOL_MINT}') AS sol_pair,
           t.mint AS token_mint, t.symbol AS token_symbol, t.name AS token_name, t.mcap, t.holders,
           t.organic_score, t.launchpad AS token_launchpad,
           COALESCE((t.audit->>'mintAuthorityDisabled')::boolean, t.mint_authority_disabled) AS mint_disabled,
           COALESCE((t.audit->>'freezeAuthorityDisabled')::boolean, t.freeze_authority_disabled) AS freeze_disabled,
           (t.audit->>'topHoldersPercentage')::float AS top10_pct,
           (t.audit->>'devBalancePercentage')::float AS dev_pct,
           COALESCE((t.stats->'stats5m'->>'buyVolume')::float, 0)
             + COALESCE((t.stats->'stats5m'->>'sellVolume')::float, 0) AS token_volume_5m,
           COALESCE((t.stats->'stats1h'->>'buyVolume')::float, 0)
             + COALESCE((t.stats->'stats1h'->>'sellVolume')::float, 0) AS token_volume_1h,
           (t.stats->'stats5m'->>'priceChange')::float AS price_change_5m,
           (t.stats->'stats1h'->>'priceChange')::float AS price_change_1h,
           (t.stats->'stats24h'->>'priceChange')::float AS price_change_24h,
           (EXTRACT(EPOCH FROM now() - p.created_at) / 3600)::float AS pool_age_h,
           (EXTRACT(EPOCH FROM now() - t.created_at) / 3600)::float AS token_age_h,
           l.volume_1h / NULLIF(l.volume_24h / 24, 0) AS volume_burst,
           s.lp_score
    FROM latest l
    JOIN pools p ON p.address = l.pool
    LEFT JOIN tokens t ON t.mint = CASE WHEN p.token_x = '{SOL_MINT}' THEN p.token_y ELSE p.token_x END
    LEFT JOIN score s ON s.pool = l.pool
)"""


@dataclass
class Query:
    sql: str
    params: dict[str, Any]


class FilterError(ValueError):
    pass


def build_query(args: dict[str, Any], max_age_min: int = 20) -> Query:
    """args: request query params (strings or values). Unknown keys are rejected."""
    args = {k: v for k, v in args.items() if v not in (None, "")}
    preset = args.pop("preset", None)
    merged: dict[str, Any] = {}
    sort = None
    if preset:
        if preset not in PRESETS:
            raise FilterError(f"unknown preset {preset!r}")
        merged.update(PRESETS[preset]["filters"])
        sort = PRESETS[preset]["sort"]
    merged.update(args)                         # explicit params override the preset

    where: list[str] = []
    params: dict[str, Any] = {}
    q = merged.pop("q", None)
    sort = merged.pop("sort", None) or sort or "fee_tvl_1h"
    order = str(merged.pop("order", "desc")).lower()
    limit = int(merged.pop("limit", 100))
    offset = int(merged.pop("offset", 0))
    bin_steps = merged.pop("bin_steps", None)

    for key, raw in merged.items():
        if key.endswith("_min") or key.endswith("_max"):
            field, op = key[:-4], (">=" if key.endswith("_min") else "<=")
            if field not in NUMERIC:
                raise FilterError(f"unknown filter {key!r}")
            try:
                val = float(raw)
            except (TypeError, ValueError) as e:
                raise FilterError(f"{key} must be a number") from e
            where.append(f"{field} {op} :{key}")
            params[key] = val
        elif key in BOOLEAN:
            val = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes")
            where.append(f"{key} = :{key}")
            params[key] = val
        else:
            raise FilterError(f"unknown filter {key!r}")
    if q:
        where.append("(token_symbol ILIKE :q OR token_name ILIKE :q OR name ILIKE :q OR address = :q_exact "
                     "OR token_mint = :q_exact)")
        params["q"], params["q_exact"] = f"%{str(q)[:64]}%", str(q)[:64]
    if bin_steps:
        steps = [int(x) for x in str(bin_steps).split(",") if x.strip()]
        where.append("bin_step = ANY(:bin_steps)")
        params["bin_steps"] = steps
    if sort not in SORTABLE:
        raise FilterError(f"cannot sort by {sort!r}")
    if order not in ("asc", "desc"):
        raise FilterError("order must be asc or desc")
    params["limit"] = max(1, min(limit, MAX_LIMIT))
    params["offset"] = max(0, offset)
    sql = (view_sql(max_age_min) + "\nSELECT *, count(*) OVER () AS total FROM v"
           + (" WHERE " + " AND ".join(where) if where else "")
           + f" ORDER BY {sort} {order.upper()} NULLS LAST, address LIMIT :limit OFFSET :offset")
    return Query(sql, params)
