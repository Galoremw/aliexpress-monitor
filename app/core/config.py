from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(value: str) -> str:
    """Make provider-style PostgreSQL URLs explicit for SQLAlchemy + psycopg."""
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value[len("postgres://") :]
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value[len("postgresql://") :]
    return value


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
    auth_required: bool = True
    auth_session_secret: str = "change-this-session-secret"
    auth_session_ttl_hours: int = Field(default=168, ge=1, le=8760)
    auth_cookie_name: str = "aliexpress_monitor_session"
    auth_cookie_secure: bool = False
    auth_cookie_samesite: str = Field(default="lax", pattern="^(lax|strict|none)$")
    auth_admin_username: str | None = None
    auth_admin_password: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
