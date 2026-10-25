import pytest
from pydantic import ValidationError

from app.core.config import Settings, validate_database_url


@pytest.mark.parametrize(
    "url",
    (
        "mysql+aiomysql://localhost",
        "postgresql+asyncpg:///stage",
        "postgresql+asyncpg://localhost:99999/stage",
        "not a url",
    ),
)
def test_incomplete_database_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        validate_database_url(url)


def test_database_credentials_are_not_printed_or_serialized() -> None:
    secret = "synthetic-private-password"
    config = Settings(env="test", database_url=f"postgresql+asyncpg://stage:{secret}@localhost/stage")
    assert secret not in repr(config)
    assert secret not in config.model_dump_json()
    assert "database_url" not in config.model_dump()
    with pytest.raises(ValidationError) as error:
        Settings(database_url=f"unsupported://stage:{secret}@localhost/stage")
    assert secret not in str(error.value)
    assert secret not in repr(error.value)
    assert secret not in error.value.json()
    assert secret not in repr(error.value.errors())


def test_database_default_is_postgresql(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STAGE_DATABASE_URL", raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    config = Settings()
    assert config.database_url.startswith("postgresql+asyncpg://")
    assert "127.0.0.1:15432/stage" in config.database_url


def test_malformed_database_url_error_does_not_expose_credentials() -> None:
    secret = "synthetic-port-secret"
    with pytest.raises(ValueError) as error:
        validate_database_url(f"postgresql+asyncpg://stage:password@localhost:{secret}/stage")
    assert secret not in str(error.value)
    assert error.value.__suppress_context__


def test_production_requires_explicit_database_and_rejects_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STAGE_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="explicit"):
        Settings(env="prod")
    with pytest.raises(ValidationError, match="SQLite"):
        Settings(env="prod", database_url="sqlite+aiosqlite:///:memory:")
    config = Settings(env="prod", database_url="postgresql+asyncpg://localhost/stage")
    assert config.env == "prod"
