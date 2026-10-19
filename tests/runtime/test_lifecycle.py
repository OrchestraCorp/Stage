from collections.abc import AsyncIterator
from typing import Annotated
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import app.main as application
from app.core.config import Settings
from app.core.db import create_engine, create_session_factory, get_session


@pytest.mark.parametrize("interrupt", (False, True))
async def test_pool_is_disposed_on_normal_and_exceptional_shutdown(
    monkeypatch: pytest.MonkeyPatch, interrupt: bool
) -> None:
    engine = MagicMock(spec=AsyncEngine)
    disposed = AsyncMock()
    engine.dispose = disposed
    monkeypatch.setattr(application, "create_engine", lambda _url: engine)
    app = application.create_app(Settings(env="test"))

    async def run() -> None:
        async with app.router.lifespan_context(app):
            assert app.state.engine is engine
            if interrupt:
                raise RuntimeError("synthetic shutdown")

    if interrupt:
        with pytest.raises(RuntimeError, match="synthetic shutdown"):
            await run()
    else:
        await run()
    disposed.assert_awaited_once()
    assert not hasattr(app.state, "engine")
    assert not hasattr(app.state, "session_factory")


async def test_request_sessions_do_not_commit_or_keep_failed_transactions() -> None:
    engine: AsyncEngine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE transaction_probe (id INTEGER PRIMARY KEY)"))
    app = application.create_app()
    app.state.session_factory = create_session_factory(engine)

    async def session_dependency() -> AsyncIterator[AsyncSession]:
        # Exercise the real dependency and its cleanup without starting a second engine.
        from starlette.requests import Request

        request = Request({"type": "http", "app": app})
        async for session in get_session(request):
            yield session

    @app.post("/_test/uncommitted")
    async def uncommitted(session: Annotated[AsyncSession, Depends(session_dependency)]) -> dict[str, bool]:
        await session.execute(text("INSERT INTO transaction_probe (id) VALUES (1)"))
        return {"ok": True}

    @app.post("/_test/failed")
    async def failed(session: Annotated[AsyncSession, Depends(session_dependency)]) -> None:
        async with session.begin():
            await session.execute(text("INSERT INTO transaction_probe (id) VALUES (2)"))
            raise RuntimeError("synthetic transaction error")

    @app.post("/_test/committed")
    async def committed(session: Annotated[AsyncSession, Depends(session_dependency)]) -> dict[str, bool]:
        async with session.begin():
            await session.execute(text("INSERT INTO transaction_probe (id) VALUES (3)"))
        return {"ok": True}

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            assert (await client.post("/_test/uncommitted")).status_code == 200
            assert (await client.post("/_test/failed")).status_code == 500
            assert (await client.post("/_test/committed")).status_code == 200
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT id FROM transaction_probe"))).scalars().all() == [3]
    finally:
        await engine.dispose()


async def test_startup_failure_still_disposes_allocated_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = MagicMock(spec=AsyncEngine)
    engine.dispose = AsyncMock()
    monkeypatch.setattr(application, "create_engine", lambda _url: engine)

    def broken_factory(_engine: AsyncEngine) -> None:
        raise RuntimeError("synthetic startup error")

    monkeypatch.setattr(application, "create_session_factory", broken_factory)
    app = application.create_app(Settings(env="test"))
    with pytest.raises(RuntimeError, match="synthetic startup"):
        async with app.router.lifespan_context(app):
            pytest.fail("Startup must fail before serving requests")
    engine.dispose.assert_awaited_once()
    assert not hasattr(app.state, "engine")
