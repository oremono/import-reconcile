"""Process configuration. Validated at startup, never mid-run. TR-705."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """The database is chosen by URL alone; no code path branches on backend. TR-704."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = f"sqlite:///{ROOT / 'reconcile.db'}"
    log_level: str = "INFO"
    data_dir: Path = ROOT / "data"


settings = Settings()
