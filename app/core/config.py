from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import Field, ModelWrapValidatorHandler, ValidationError, field_validator, model_validator
from pydantic_core import InitErrorDetails
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

SUPPORTED_DATABASE_DRIVERS = frozenset({"postgresql+asyncpg", "sqlite+aiosqlite"})


def validate_database_url(value: str) -> str:
    """Allow only async drivers supported by the Stage base layer."""

    try:
        url = make_url(value)
    except (ArgumentError, ValueError):
        raise ValueError("STAGE_DATABASE_URL is not a valid SQLAlchemy URL") from None
    if url.drivername not in SUPPORTED_DATABASE_DRIVERS:
        supported = ", ".join(sorted(SUPPORTED_DATABASE_DRIVERS))
        raise ValueError(f"unsupported database driver; supported: {supported}")
    if url.get_backend_name() != "sqlite":
        if not url.database or not url.database.strip():
            raise ValueError("STAGE_DATABASE_URL requires a database name")
        if not url.host and not url.query.get("host"):
            raise ValueError("STAGE_DATABASE_URL requires a host or socket")
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError("STAGE_DATABASE_URL port must be between 1 and 65535")
    return value


def validate_redis_url(value: str) -> str:
    """Accept explicit TCP Redis URLs without query overrides of client limits."""
    try:
        if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            raise ValueError
        url = urlsplit(value)
        if url.scheme not in ("redis", "rediss") or not url.hostname:
            raise ValueError
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError
        if url.query or url.fragment:
            raise ValueError
        if url.path not in ("", "/") and (
            not url.path.startswith("/") or not url.path[1:].isascii() or not url.path[1:].isdigit()
        ):
            raise ValueError
    except ValueError:
        raise ValueError(
            "STAGE_REDIS_URL requires redis:// or rediss://, a valid host/port and optional database number"
        ) from None
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STAGE_", env_file=".env", extra="ignore", hide_input_in_errors=True)

    env: Literal["dev", "test", "prod"] = "dev"
    app_name: str = "orchestra-stage"
    database_url: str = Field(
        default="postgresql+asyncpg://stage:stage-dev-postgres@127.0.0.1:15432/stage", repr=False, exclude=True
    )
    redis_url: str | None = Field(default=None, repr=False, exclude=True)
    redis_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=30, allow_inf_nan=False)
    redis_socket_timeout_seconds: float = Field(default=2.0, gt=0, le=30, allow_inf_nan=False)
    redis_max_connections: int = Field(default=20, ge=1, le=1000)
    redis_required: bool = False
    database_pool_size: int = Field(default=5, ge=1, le=100)
    database_max_overflow: int = Field(default=5, ge=0, le=100)
    database_pool_timeout_seconds: float = Field(default=2.0, gt=0, le=30, allow_inf_nan=False)
    dependency_probe_timeout_seconds: float = Field(default=2.0, gt=0, le=30, allow_inf_nan=False)
    dependency_probe_interval_seconds: float = Field(default=2.0, gt=0, le=60, allow_inf_nan=False)
    runtime_startup_timeout_seconds: float = Field(default=10.0, gt=0, le=120, allow_inf_nan=False)
    runtime_shutdown_timeout_seconds: float = Field(default=10.0, gt=0, le=120, allow_inf_nan=False)
    identity_request_timeout_seconds: float = Field(default=10.0, gt=0, le=60, allow_inf_nan=False)
    command_request_timeout_seconds: float = Field(default=10.0, gt=0, le=60, allow_inf_nan=False)
    native_auth_enabled: bool = False
    native_auth_issuer: str | None = None
    native_auth_organization_ref: str | None = None
    native_auth_key_file: str | None = Field(default=None, repr=False, exclude=True)
    native_auth_encryption_key_file: str | None = Field(default=None, repr=False, exclude=True)
    native_auth_request_timeout_seconds: float = Field(default=15.0, gt=0, le=60, allow_inf_nan=False)
    native_auth_peer_attempt_limit: int = Field(default=300, ge=1, le=10000)
    native_auth_authorization_attempt_limit: int = Field(default=300, ge=1, le=10000)
    native_auth_rate_window_seconds: int = Field(default=600, ge=60, le=3600)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        return validate_database_url(value)

    @field_validator("redis_url")
    @classmethod
    def _validate_redis_url(cls, value: str | None) -> str | None:
        return validate_redis_url(value) if value is not None else None

    @model_validator(mode="after")
    def _production_database(self) -> "Settings":
        if self.native_auth_enabled:
            from app.native_auth.config import NativeAuthConfig

            if not all(
                (
                    self.native_auth_issuer,
                    self.native_auth_organization_ref,
                    self.native_auth_key_file,
                    self.native_auth_encryption_key_file,
                )
            ):
                raise ValueError("native_auth_requires_explicit_issuer_organization_and_key_files")
            NativeAuthConfig(
                issuer=self.native_auth_issuer or "",
                organization_ref=self.native_auth_organization_ref or "",
                environment=self.env,
                request_timeout_seconds=self.native_auth_request_timeout_seconds,
            )
        if self.redis_required and self.redis_url is None:
            raise ValueError("required Redis needs an explicit STAGE_REDIS_URL")
        if self.env == "prod":
            if "database_url" not in self.model_fields_set:
                raise ValueError("production requires an explicit STAGE_DATABASE_URL")
            if make_url(self.database_url).get_backend_name() == "sqlite":
                raise ValueError("SQLite is supported only for development and tests")
        return self

    @model_validator(mode="wrap")
    @classmethod
    def _redact_validation_inputs(cls, value: Any, handler: ModelWrapValidatorHandler["Settings"]) -> "Settings":
        # hide_input_in_errors only hides text output; JSON/errors() also need protection.
        try:
            return handler(value)
        except ValidationError as exc:
            errors = cast(list[InitErrorDetails], exc.errors(include_input=False))
            raise ValidationError.from_exception_data(cls.__name__, errors, hide_input=True) from None


settings = Settings()
