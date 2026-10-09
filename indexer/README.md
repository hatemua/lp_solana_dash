# indexer (M1)

Python 3.11 service that indexes Meteora DLMM pools and their tokens into PostgreSQL (TimescaleDB) and keeps the
latest hot-pool stats in Redis.

## Jobs

| Job | Every | Source | Writes |
|---|---|---|---|
| `full_pool_list` | 10 min | Meteora `/pools` (all ~137k, 1000 per page) | `pools`, `tokens` (basic), `pool_stats` for pools with TVL ≥ $10k |
| `hot_stats` | 60 s | Meteora `/pools` sorted by TVL and by 1 h volume | `pool_stats`, `pool_tvl_flow`, Redis `pool:{addr}` + `rank:*` |
| `new_pools` | 60 s | Meteora `/pools?sort_by=pool_created_at:desc` | `pools`, `tokens` |
| `token_info` | 5 min | Jupiter `/v1/assets/search` (50 mints per call) | `tokens` (holders, mcap, organic score, audit, stats) |
| `ohlcv` | 5 min | Meteora `/pools/{addr}/ohlcv` + `/volume/history` (5m, 6 h per call) | `pool_ohlcv_5m` (72 h backfill, then incremental) |
| `token_ohlcv_1m` | 5 min | Jupiter `/v2/charts/{mint}` (1-min, all venues) | `token_ohlcv_1m` for the tokens of the top 40 hot pools |
| `bins` | 60 s | executor → DLMM SDK `getBinsAroundActiveBin(70, 70)` | `bins_snapshot` for the top 10 hot pools by 1 h fee/TVL |

**Hot pool**: SOL pair, not blacklisted, TVL ≥ $10k or 1 h volume ≥ $50k (thresholds in env).

**Liquidity flow** (`pool_tvl_flow`, per 5 min): reserve changes valued at the new prices
(`ΔX·price_x + ΔY·price_y`). Swaps exchange X for Y at about the market price, so they net to ~0; LP adds and removes
do not. The price move of the existing reserves is stored separately (`price_effect_usd`).

## Robustness

- Every upstream has its own rate limit (Meteora 240/min of its 300, Jupiter 120/min) and retries with exponential
  backoff on 429 / 5xx / timeouts, honouring `Retry-After`. API errors are logged, never raised to the scheduler.
- Upserts are idempotent (`INSERT … ON CONFLICT … DO UPDATE` with `COALESCE`, so a later NULL never erases a value;
  the basic token info from the pool list never overwrites Jupiter's richer fields).
- NUL bytes (seen in real token names) are stripped before writing.
- A job failure is recorded and retried at the next interval; one bad page does not stop the full list.

## /health (127.0.0.1:8001)

Status 200 when the hot-pool stats are < 2 min old, otherwise 503. Shows: hot pools, stats age, coverage of the
pools with TVL ≥ $10k (`coverage_tvl10k`), table counts, oldest 5-min bar of the hot pools, Redis, per-job runs,
failures, last error and duration, and per-upstream request counters (ok / 429 / errors).

## Develop

    pip install -e ".[dev]"
    ruff check . && mypy && pytest -q            # DB test runs when DATABASE_URL is set

Tests use real API responses saved in `tests/fixtures/`.
