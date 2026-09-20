from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "AliExpress Competitor Monitor"
    database_url: str = "postgresql+psycopg://monitor:monitor@localhost:5432/monitor"
    timezone: str = "Asia/Shanghai"
    scheduler_enabled: bool = True
    scheduler_hour: int = Field(default=2, ge=0, le=23)
    scheduler_minute: int = Field(default=0, ge=0, le=59)
    collector_timeout_seconds: float = Field(default=20.0, gt=0)
    collector_user_agent: str = "AliExpressMonitor/0.1 (+public-data-research)"
    firecrawl_enabled: bool = True
    firecrawl_api_key: str | None = None
    firecrawl_api_url: str = "https://api.firecrawl.dev"
    firecrawl_wait_for_ms: int = Field(default=2000, ge=0, le=15000)
    extension_allowed_origin_regex: str = r"chrome-extension://[a-z]{32}"
    frontend_allowed_origins: str = (
        "https://galoremw.github.io,http://127.0.0.1:3000,http://localhost:3000"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
