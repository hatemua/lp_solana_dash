"""Settings, read from the environment (see .env.example). No secrets have defaults."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

SOL_MINT = "So11111111111111111111111111111111111111112"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://lp:lp@localhost:5432/lp"
    redis_url: str = "redis://localhost:6379/0"
    executor_url: str = "http://localhost:8200"

    meteora_api: str = "https://dlmm.datapi.meteora.ag"
    jupiter_data_api: str = "https://datapi.jup.ag"

    # rate limits (requests per minute) per upstream; Meteora allows 300 per window
    meteora_rpm: int = 240
    jupiter_rpm: int = 120
    executor_rpm: int = 120

    # job intervals (seconds)
    full_list_every_s: int = 600
    hot_stats_every_s: int = 60
    new_pools_every_s: int = 60
    token_info_every_s: int = 300
    ohlcv_every_s: int = 300
    token_ohlcv_every_s: int = 300
    bins_every_s: int = 60

    # hot pools: SOL pairs with TVL >= hot_min_tvl or 1 h volume >= hot_min_volume_1h
    hot_min_tvl: float = 10_000
    hot_min_volume_1h: float = 50_000
    # pool_stats are also kept for every pool (any pair) with TVL >= this, from the full list
    tracked_min_tvl: float = 10_000

    full_list_page_size: int = 1000
    ohlcv_backfill_hours: int = 72
    ohlcv_concurrency: int = 4
    track_tokens_n: int = 40           # token_ohlcv_1m for the tokens of the top N hot pools (by 1 h fees)
    watch_pools_n: int = 10            # bins_snapshot for the top N hot pools (by 1 h fee/TVL)
    watch_pools: str = ""              # optional comma-separated pool addresses, always watched
    bins_each_side: int = 70

    health_host: str = "0.0.0.0"
    health_port: int = 8001
    hot_stats_max_age_s: int = 120
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
