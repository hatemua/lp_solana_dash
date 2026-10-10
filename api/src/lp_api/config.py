from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

SOL_MINT = "So11111111111111111111111111111111111111112"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://lp:lp@localhost:5432/lp"
    redis_url: str = "redis://localhost:6379/0"
    executor_url: str = "http://localhost:8200"
    indexer_health_url: str = "http://localhost:8001/health"

    # comma-separated; https://lp.joulity.com in production, localhost:3000 in dev
    api_cors_origins: str = "https://lp.joulity.com,http://localhost:3000"
    rate_limit_per_min: int = 120        # public read endpoints, per client IP
    tx_rate_limit_per_min: int = 20      # transaction building endpoints, per client IP
    stats_max_age_min: int = 20          # pools with no stats newer than this are left out of the table
    stream_every_s: int = 30

    # accounts + OAuth for the MCP
    public_api_url: str = "https://lp.api.joulity.com"   # OAuth issuer; the MCP resource is <this>/mcp
    web_url: str = "https://lp.joulity.com"              # login and sign-up pages
    cookie_secure: bool = True                           # False only for http://localhost development
    signup_open: bool = True
    auth_rate_limit_per_min: int = 10                    # login, sign-up, token, registration; per client IP

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
