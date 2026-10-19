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


def test_production_requires_explicit_database_and_rejects_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STAGE_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="explicit"):
        Settings(env="prod")
    with pytest.raises(ValidationError, match="SQLite"):
        Settings(env="prod", database_url="sqlite+aiosqlite:///:memory:")
    config = Settings(env="prod", database_url="postgresql+asyncpg://localhost/stage")
    assert config.env == "prod"
