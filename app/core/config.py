from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

SUPPORTED_DATABASE_DRIVERS = frozenset({"mysql+aiomysql", "postgresql+asyncpg", "sqlite+aiosqlite"})


def validate_database_url(value: str) -> str:
    """Allow only async drivers supported by the Stage base layer."""

    try:
        driver = make_url(value).drivername
    except Exception as exc:  # noqa: BLE001 - configuration errors become a clear ValueError
        raise ValueError("STAGE_DATABASE_URL is not a valid SQLAlchemy URL") from exc
    if driver not in SUPPORTED_DATABASE_DRIVERS:
        supported = ", ".join(sorted(SUPPORTED_DATABASE_DRIVERS))
        raise ValueError(f"unsupported database driver {driver!r}; supported: {supported}")
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STAGE_", env_file=".env", extra="ignore")

    env: str = "dev"
    app_name: str = "orchestra-stage"
    database_url: str = "mysql+aiomysql://stage:stage@127.0.0.1:3306/stage"
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        return validate_database_url(value)


settings = Settings()
