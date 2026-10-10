"""Settings from the environment. The bot is paper-only: it has no wallet, no keys and sends no transactions."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

SOL_MINT = "So11111111111111111111111111111111111111112"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://lp:lp@localhost:5432/lp"
    redis_url: str = "redis://localhost:6379/0"
    executor_url: str = "http://localhost:8200"

    bot_mode: str = "paper"              # only "paper" is accepted
    strategies: str = "topped_bid,meridian,chop_spot,topped_bid_v2,evil_panda,grid,fee_leader"
    position_usd: float = 100.0
    max_open_per_strategy: int = 3
    tick_s: int = 60
    min_tvl: float = 10_000
    min_fees_1h: float = 300
    live_bins_each_side: int = 70
    log_level: str = "INFO"

    @property
    def strategy_names(self) -> list[str]:
        return [s.strip() for s in self.strategies.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
