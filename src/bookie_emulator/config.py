"""Runtime configuration via environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    port: int = 8005
    log_level: str = "info"
    database_url: str = "postgres://emulator_svc:localdev@localhost:5432/bookiebreaker?search_path=emulator,public"
    redis_url: str = "redis://localhost:6379"
    lines_service_url: str = "http://localhost:8001"
    statistics_service_url: str = "http://localhost:8002"

    starting_bankroll_units: float = 100.0
    unit_size_dollars: float = 100.0
    max_bet_units: float = 3.0
    max_daily_exposure_units: float = 10.0
    kelly_fraction: float = 0.25
    kelly_enabled: bool = True

    grading_poll_seconds: int = 1_800  # fallback poller for missed game.completed events
    grading_grace_seconds: int = 10_800  # only poll games that started >3h ago
    game_map_ttl_seconds: int = 86_400  # statistics<->lines game id mapping cache

    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "bookie-emulator"


@lru_cache
def get_settings() -> Settings:
    return Settings()
