"""Async persistence primitives; domain commands own transaction commits."""

from collections.abc import AsyncGenerator
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import validate_database_url


def create_engine(
    database_url: str, *, pool_size: int = 5, max_overflow: int = 5, pool_timeout: float = 2
) -> AsyncEngine:
    """Create an unconnected engine with explicit per-dialect session semantics."""

    validate_database_url(database_url)
    backend = make_url(database_url).get_backend_name()
    options: dict[str, Any] = {"pool_pre_ping": True, "hide_parameters": True}
    if backend == "postgresql":
        options.update(pool_size=pool_size, max_overflow=max_overflow, pool_timeout=pool_timeout)
        options["isolation_level"] = "READ COMMITTED"
        options["connect_args"] = {"server_settings": {"timezone": "UTC"}}
    engine = create_async_engine(database_url, **options)
    if backend == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _foreign_keys(connection: Any, _record: Any) -> None:
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Pin storage through session cleanup; the request never auto-commits."""
    # Provider definitions depend on these factory primitives, not vice versa.
    from app.providers.storage import STORAGE

    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None or not runtime.ready:
        raise HTTPException(503)
    async with runtime.host.call(STORAGE) as call:
        async with call.require(STORAGE).sessions() as session:
            try:
                yield session
            except BaseException:
                await session.rollback()
                raise
