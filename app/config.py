"""Process configuration. Validated at startup, never mid-run. TR-705."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]

#: Hosting providers hand out a bare ``postgres://`` URL. SQLAlchemy 2.0 removed
#: that alias, and picks a default driver we do not install for the longer
#: ``postgresql://`` form. Both are rewritten to name the driver explicitly.
#:
#: This is not a violation of TR-704, which forbids code that *behaves*
#: differently depending on the backend. Nothing downstream branches: the same
#: models, the same migrations and the same queries run either way. This only
#: accepts a URL somebody else wrote and hands it to the engine in the form it
#: requires - a spelling correction, made once, at the edge.
_DRIVER = "postgresql+psycopg://"
_REWRITE = ("postgres://", "postgresql://")


class Settings(BaseSettings):
    """The database is chosen by URL alone; no code path branches on backend. TR-704."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = f"sqlite:///{ROOT / 'reconcile.db'}"
    log_level: str = "INFO"
    data_dir: Path = ROOT / "data"

    @field_validator("database_url")
    @classmethod
    def _name_the_driver(cls, value: str) -> str:
        for prefix in _REWRITE:
            if value.startswith(prefix):
                return _DRIVER + value[len(prefix) :]
        return value


settings = Settings()


def refuse_ephemeral_sqlite(settings: Settings, environ: Mapping[str, str]) -> None:
    """On a managed host, a missing DATABASE_URL must not fall back to SQLite.

    The SQLite default is what makes `git clone && make run` work with nothing
    installed, so it has to stay. But a platform's disk is usually ephemeral,
    and there the same default is the worst kind of failure: the application
    starts, serves pages, accepts uploads, and loses all of it on the next
    restart. Nothing looks broken until someone asks where their work went.

    Detected by the host's own marker rather than by parsing the URL, so this
    says nothing about which database is in use (TR-704).
    """
    on_a_managed_host = any(key in environ for key in ("RENDER", "DYNO", "FLY_APP_NAME"))
    if on_a_managed_host and settings.database_url.startswith("sqlite"):
        raise RuntimeError(
            "DATABASE_URL is not set, and the SQLite fallback would be lost on "
            "the next restart. Set DATABASE_URL to a managed database."
        )
