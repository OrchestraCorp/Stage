import pytest

from app.core.config import validate_database_url


@pytest.mark.parametrize(
    "url",
    (
        "mysql+aiomysql://stage:stage@localhost/stage",
        "postgresql+asyncpg://stage:stage@localhost/stage",
        "sqlite+aiosqlite:///./stage.db",
    ),
)
def test_supported_async_database_urls(url: str) -> None:
    assert validate_database_url(url) == url


def test_unsupported_database_driver_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported database driver"):
        validate_database_url("postgresql+psycopg://stage:stage@localhost/stage")
