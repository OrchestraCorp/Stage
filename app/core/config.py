from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

SUPPORTED_DATABASE_DRIVERS = frozenset({"mysql+aiomysql", "postgresql+asyncpg", "sqlite+aiosqlite"})


def validate_database_url(value: str) -> str:
    """Allow only async drivers supported by the Stage base layer."""

    try:
        url = make_url(value)
    except (ArgumentError, ValueError) as exc:
        raise ValueError("STAGE_DATABASE_URL is not a valid SQLAlchemy URL") from exc
    if url.drivername not in SUPPORTED_DATABASE_DRIVERS:
        supported = ", ".join(sorted(SUPPORTED_DATABASE_DRIVERS))
        raise ValueError(f"unsupported database driver; supported: {supported}")
    if url.get_backend_name() != "sqlite":
        if not url.database or not url.database.strip():
            raise ValueError("STAGE_DATABASE_URL requires a database name")
        if not url.host and not url.query.get("host") and not url.query.get("unix_socket"):
            raise ValueError("STAGE_DATABASE_URL requires a host or socket")
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError("STAGE_DATABASE_URL port must be between 1 and 65535")
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STAGE_", env_file=".env", extra="ignore", hide_input_in_errors=True)

    env: Literal["dev", "test", "prod"] = "dev"
    app_name: str = "orchestra-stage"
    database_url: str = Field(default="mysql+aiomysql://stage:stage@127.0.0.1:3306/stage", repr=False, exclude=True)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        return validate_database_url(value)

    @model_validator(mode="after")
    def _production_database(self) -> "Settings":
        if self.env == "prod":
            if "database_url" not in self.model_fields_set:
                raise ValueError("production requires an explicit STAGE_DATABASE_URL")
            if make_url(self.database_url).get_backend_name() == "sqlite":
                raise ValueError("SQLite is supported only for development and tests")
        return self


settings = Settings()
