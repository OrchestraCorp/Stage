import pytest

from app.core.config import validate_database_url


@pytest.mark.parametrize(
    "url",
    (
        "postgresql+asyncpg://stage:stage@localhost/stage",
        "postgresql+asyncpg:///stage?host=/var/run/postgresql",
        "sqlite+aiosqlite:///./stage.db",
    ),
)
def test_supported_async_database_urls(url: str) -> None:
    assert validate_database_url(url) == url


@pytest.mark.parametrize(
    "url",
    (
        "postgresql+psycopg://stage:stage@localhost/stage",
        "mysql+aiomysql://stage:stage@localhost/stage",
        "mysql+pymysql://stage:stage@localhost/stage",
    ),
)
def test_unsupported_database_driver_fails_closed(url: str) -> None:
    with pytest.raises(ValueError, match="unsupported database driver"):
        validate_database_url(url)
