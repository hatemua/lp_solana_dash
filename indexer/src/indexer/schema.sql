-- M1 schema. Idempotent: safe to run at every start.

CREATE TABLE IF NOT EXISTS tokens (
    mint                       TEXT PRIMARY KEY,
    symbol                     TEXT,
    name                       TEXT,
    decimals                   INTEGER,
    created_at                 TIMESTAMPTZ,
    launchpad                  TEXT,
    holders                    BIGINT,
    mcap                       DOUBLE PRECISION,
    price_usd                  DOUBLE PRECISION,
    organic_score              DOUBLE PRECISION,
    audit                      JSONB,
    stats                      JSONB,
    freeze_authority_disabled  BOOLEAN,
    mint_authority_disabled    BOOLEAN,
    is_verified                BOOLEAN,
    enriched_at                TIMESTAMPTZ,          -- last Jupiter update
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pools (
    address           TEXT PRIMARY KEY,
    name              TEXT,
    token_x           TEXT NOT NULL,
    token_y           TEXT NOT NULL,
    bin_step          INTEGER,
    base_fee_pct      DOUBLE PRECISION,
    max_fee_pct       DOUBLE PRECISION,
    protocol_fee_pct  DOUBLE PRECISION,
    created_at        TIMESTAMPTZ,
    launchpad         TEXT,
    tags              TEXT[] NOT NULL DEFAULT '{}',
    is_blacklisted    BOOLEAN NOT NULL DEFAULT false,
    tvl               DOUBLE PRECISION,                -- latest, for filters and coverage
    volume_24h        DOUBLE PRECISION,
    fees_24h          DOUBLE PRECISION,
    is_hot            BOOLEAN NOT NULL DEFAULT false,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pools_token_x ON pools (token_x);
CREATE INDEX IF NOT EXISTS pools_token_y ON pools (token_y);
CREATE INDEX IF NOT EXISTS pools_tvl ON pools (tvl DESC);
CREATE INDEX IF NOT EXISTS pools_created ON pools (created_at DESC);

CREATE TABLE IF NOT EXISTS pool_stats (
    pool               TEXT NOT NULL,
    ts                 TIMESTAMPTZ NOT NULL,
    price              DOUBLE PRECISION,
    tvl                DOUBLE PRECISION,
    volume_5m          DOUBLE PRECISION,
    volume_30m         DOUBLE PRECISION,
    volume_1h          DOUBLE PRECISION,
    volume_24h         DOUBLE PRECISION,
    fees_5m            DOUBLE PRECISION,
    fees_30m           DOUBLE PRECISION,
    fees_1h            DOUBLE PRECISION,
    fees_24h           DOUBLE PRECISION,
    protocol_fees_1h   DOUBLE PRECISION,
    protocol_fees_24h  DOUBLE PRECISION,
    fee_tvl_1h         DOUBLE PRECISION,
    fee_tvl_24h        DOUBLE PRECISION,
    dynamic_fee_pct    DOUBLE PRECISION,
    token_x_amount     DOUBLE PRECISION,
    token_y_amount     DOUBLE PRECISION,
    price_x_usd        DOUBLE PRECISION,
    price_y_usd        DOUBLE PRECISION,
    cum_volume         DOUBLE PRECISION,
    cum_fees           DOUBLE PRECISION,
    PRIMARY KEY (pool, ts)
);

CREATE TABLE IF NOT EXISTS pool_ohlcv_5m (
    pool           TEXT NOT NULL,
    ts             TIMESTAMPTZ NOT NULL,
    open           DOUBLE PRECISION,
    high           DOUBLE PRECISION,
    low            DOUBLE PRECISION,
    close          DOUBLE PRECISION,
    volume         DOUBLE PRECISION,
    fees           DOUBLE PRECISION,
    protocol_fees  DOUBLE PRECISION,
    PRIMARY KEY (pool, ts)
);

CREATE TABLE IF NOT EXISTS token_ohlcv_1m (
    mint    TEXT NOT NULL,
    ts      TIMESTAMPTZ NOT NULL,
    open    DOUBLE PRECISION,
    high    DOUBLE PRECISION,
    low     DOUBLE PRECISION,
    close   DOUBLE PRECISION,
    volume  DOUBLE PRECISION,
    PRIMARY KEY (mint, ts)
);

CREATE TABLE IF NOT EXISTS bins_snapshot (
    pool           TEXT NOT NULL,
    ts             TIMESTAMPTZ NOT NULL,
    active_bin_id  INTEGER NOT NULL,
    bin_step       INTEGER,
    active_price   DOUBLE PRECISION,                   -- price of the active bin (token Y per token X)
    bins           JSONB NOT NULL,                     -- [{bin_id, price, x, y, supply}] within +/- N bins
    PRIMARY KEY (pool, ts)
);

CREATE TABLE IF NOT EXISTS pool_tvl_flow (
    pool              TEXT NOT NULL,
    ts                TIMESTAMPTZ NOT NULL,            -- end of the 5-min bucket
    tvl_start         DOUBLE PRECISION,
    tvl_end           DOUBLE PRECISION,
    price_effect_usd  DOUBLE PRECISION,
    net_flow_usd      DOUBLE PRECISION,                -- > 0 liquidity added, < 0 removed
    PRIMARY KEY (pool, ts)
);

CREATE TABLE IF NOT EXISTS pool_metrics_1m (                -- filled by the M4 signal engine
    pool      TEXT NOT NULL,
    ts        TIMESTAMPTZ NOT NULL,
    lp_score  DOUBLE PRECISION,
    metrics   JSONB NOT NULL,
    PRIMARY KEY (pool, ts)
);

CREATE TABLE IF NOT EXISTS ohlcv_cursor (                   -- last 5-min bar stored per pool (backfill/incremental)
    pool        TEXT PRIMARY KEY,
    last_ts     TIMESTAMPTZ,
    backfilled  BOOLEAN NOT NULL DEFAULT false,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- TimescaleDB, when available: time-partitioned history tables
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb') THEN
        CREATE EXTENSION IF NOT EXISTS timescaledb;
        PERFORM create_hypertable('pool_stats', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
        PERFORM create_hypertable('pool_ohlcv_5m', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
        PERFORM create_hypertable('token_ohlcv_1m', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
        PERFORM create_hypertable('bins_snapshot', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
        PERFORM create_hypertable('pool_tvl_flow', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
        PERFORM create_hypertable('pool_metrics_1m', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
    END IF;
END $$;
