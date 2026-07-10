from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    db_path: Path = Path("data/options.duckdb")

    option_chain_window: int = 20
    default_cp: str = "C"
    risk_free_rate_fallback: float = 0.05

    page_size: int = 50

    fetch_chains_on_refresh: bool = True
    large_cap_chain_threshold: float = 200_000_000_000.0

    scheduler_enabled: bool = False
    scheduler_cron: str = "0 */1 * * 1-5"
    scheduler_watchlist_days_to_earnings: int = 14

    large_cap_scheduler_enabled: bool = True
    large_cap_scheduler_cron: str = "0 22 * * 1-5"

    iv_monitor_enabled: bool = True
    iv_monitor_cron: str = "*/10 * * * 1-5"
    iv_monitor_timezone: str = "America/New_York"
    iv_monitor_batch_size: int = 15

    daily_candles_enabled: bool = True
    daily_candles_cron: str = "*/10 * * * *"
    daily_candles_batch_size: int = 10
    daily_candles_lookback_days: int = 90

    web_host: str = "127.0.0.1"
    web_port: int = 8000


def get_settings() -> Settings:
    return Settings()
