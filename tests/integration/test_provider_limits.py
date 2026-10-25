"""Real provider budget checks; enabled by explicit isolated service URLs."""

import os

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app.core.config import Settings
from app.core.db import create_engine
from app.core.redis import create_redis_client


async def test_postgresql_pool_acquisition_is_bounded() -> None:
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url.startswith("postgresql+asyncpg://"):
        pytest.skip("requires explicit isolated PostgreSQL TEST_DATABASE_URL")
    engine = create_engine(url, pool_size=1, max_overflow=0, pool_timeout=0.05)
    try:
        async with engine.connect():
            with pytest.raises(PoolTimeout):
                async with engine.connect():
                    pytest.fail("second connection cannot exceed the pool budget")
        async with engine.connect():
            pass
    finally:
        await engine.dispose()


async def test_redis_pool_exhaustion_and_recovery_are_explicit() -> None:
    url = os.environ.get("TEST_REDIS_URL")
    if url is None:
        pytest.skip("requires explicit isolated TEST_REDIS_URL")
    client = create_redis_client(Settings(env="test", redis_url=url, redis_max_connections=1))
    held = await client.connection_pool.get_connection()
    try:
        with pytest.raises(RedisConnectionError):
            await client.ping()
    finally:
        await client.connection_pool.release(held)
    try:
        assert await client.ping()
    finally:
        await client.aclose()
