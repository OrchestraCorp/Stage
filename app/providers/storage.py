"""Bounded PostgreSQL and Redis resources, health probes, and runtime ownership."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from alembic.script import ScriptDirectory
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.db import create_engine, create_session_factory
from app.core.redis import create_redis_client
from app.runtime import ComponentContext, PluginSpec, ServiceKey


@dataclass(frozen=True)
class Storage:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


@dataclass
class DependencyHealth:
    available: bool = False
    error_code: str | None = None


STORAGE: ServiceKey[Storage] = ServiceKey("stage", "storage", "1")
REDIS: ServiceKey[Redis] = ServiceKey("stage", "redis", "1")


def schema_heads() -> frozenset[str]:
    return frozenset(ScriptDirectory(str(Path(__file__).resolve().parents[2] / "migrations")).get_heads())


async def probe_database(engine: AsyncEngine, expected: frozenset[str], timeout: float) -> None:
    async with asyncio.timeout(timeout), engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
        actual = frozenset((await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars())
        if actual != expected:
            raise RuntimeError("database schema revision mismatch")


def database_plugin(config: Settings, health: DependencyHealth) -> PluginSpec:
    async def setup(ctx: ComponentContext) -> None:
        engine = create_engine(
            config.database_url,
            pool_size=config.database_pool_size,
            max_overflow=config.database_max_overflow,
            pool_timeout=config.database_pool_timeout_seconds,
        )
        ctx.effect(lambda: engine.dispose, label="database.engine")
        storage = Storage(engine, create_session_factory(engine))
        expected = schema_heads()
        try:
            await probe_database(engine, expected, config.dependency_probe_timeout_seconds)
        except Exception:
            health.available = False
            health.error_code = "database.unavailable_or_schema_mismatch"
            raise RuntimeError(health.error_code) from None
        health.available = True
        health.error_code = None
        binding = ctx.provide(STORAGE, storage, check=lambda: health.available)

        async def monitor() -> None:
            try:
                while True:
                    await asyncio.sleep(config.dependency_probe_interval_seconds)
                    previous = health.available
                    try:
                        await probe_database(engine, expected, config.dependency_probe_timeout_seconds)
                    except Exception:
                        health.available = False
                        health.error_code = "database.unavailable_or_schema_mismatch"
                    else:
                        health.available = True
                        health.error_code = None
                    if health.available != previous:
                        binding.notify()
            finally:
                health.available = False

        ctx.spawn(monitor(), name="database.probe")

    return PluginSpec("database", setup, provides=(STORAGE,))


def redis_plugin(config: Settings, health: DependencyHealth) -> PluginSpec:
    async def setup(ctx: ComponentContext) -> None:
        client = create_redis_client(config)
        ctx.effect(lambda: client.aclose, label="redis.client")

        async def probe() -> None:
            async with asyncio.timeout(config.dependency_probe_timeout_seconds):
                if not await client.ping():
                    raise RuntimeError("redis.unavailable")

        try:
            await probe()
        except Exception:
            health.available = False
            health.error_code = "redis.unavailable"
            if config.redis_required:
                raise RuntimeError(health.error_code) from None
        else:
            health.available = True
            health.error_code = None
        binding = ctx.provide(REDIS, client, check=lambda: health.available)

        async def monitor() -> None:
            try:
                while True:
                    await asyncio.sleep(config.dependency_probe_interval_seconds)
                    previous = health.available
                    try:
                        await probe()
                    except Exception:
                        health.available = False
                        health.error_code = "redis.unavailable"
                    else:
                        health.available = True
                        health.error_code = None
                    if health.available != previous:
                        binding.notify()
            finally:
                health.available = False

        ctx.spawn(monitor(), name="redis.probe")

    return PluginSpec("redis", setup, provides=(REDIS,))
