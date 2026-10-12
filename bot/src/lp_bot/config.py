"""Settings from the environment. The paper bot has no wallet. The live part (live.py) is off unless BOT_LIVE=true;
its wallet key is held by the executor only, never by this service."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

SOL_MINT = "So11111111111111111111111111111111111111112"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://lp:lp@localhost:5432/lp"
    redis_url: str = "redis://localhost:6379/0"
    executor_url: str = "http://localhost:8200"

    bot_mode: str = "paper"              # only "paper" is accepted
    strategies: str = "v2_wide,v2_calm,v2_bid_flip,auto"
    position_usd: float = 100.0
    max_open_per_strategy: int = 3
    tick_s: int = 60
    min_tvl: float = 10_000
    min_fees_1h: float = 300
    daily_loss_limit_usd: float = 25.0    # no new entries for the rest of the UTC day below this
    min_token_age_h: float = 24          # crash_study: tokens < 24 h crash ~240x more often and 6x faster
    live_bins_each_side: int = 70
    # live trading: mirrors one paper strategy with real, small positions (executor holds the key)
    bot_live: bool = False
    live_strategy: str = "auto"
    live_position_sol: float = 0.1
    live_max_open: int = 1
    live_daily_loss_sol: float = 0.05    # pause live entries for the UTC day below this
    live_reserve_sol: float = 0.05       # always kept for fees and position rent
    live_shapes: str = "spot_10,spot_25,spot_40"
    log_level: str = "INFO"

    @property
    def strategy_names(self) -> list[str]:
        return [s.strip() for s in self.strategies.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
