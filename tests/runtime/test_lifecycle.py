import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.bootstrap.application import ApplicationRuntime
from app.core.config import Settings
from app.core.db import create_engine, get_session
from app.main import create_app
from app.providers import storage as providers
from app.providers.storage import STORAGE, schema_heads
from app.runtime import ComponentContext, PluginSpec


@pytest.fixture
async def config(tmp_path: Path) -> Settings:
    url = f"sqlite+aiosqlite:///{tmp_path / 'lifecycle.db'}"
    engine = create_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
            for revision in schema_heads():
                await connection.execute(text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": revision})
            await connection.execute(text("CREATE TABLE transaction_probe (id INTEGER PRIMARY KEY)"))
    finally:
        await engine.dispose()
    return Settings(
        env="test",
        database_url=url,
        dependency_probe_interval_seconds=0.02,
        dependency_probe_timeout_seconds=0.2,
        runtime_startup_timeout_seconds=1,
        runtime_shutdown_timeout_seconds=1,
    )


async def eventually(check: Callable[[], bool], timeout: float = 2) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize("interrupt", (False, True))
async def test_pool_is_disposed_on_normal_and_exceptional_shutdown(
    config: Settings,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: bool,
) -> None:
    disposed = []
    original = AsyncEngine.dispose

    async def dispose(engine: AsyncEngine, close: bool = True) -> None:
        disposed.append(engine)
        await original(engine, close=close)

    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    app = create_app(config)

    async def run() -> None:
        async with app.router.lifespan_context(app):
            assert app.state.runtime.ready
            if interrupt:
                raise RuntimeError("synthetic shutdown")

    if interrupt:
        with pytest.raises(RuntimeError, match="synthetic shutdown"):
            await run()
    else:
        await run()
    assert len(disposed) == 1
    assert not hasattr(app.state, "runtime")


async def test_request_sessions_do_not_commit_or_keep_failed_transactions(config: Settings) -> None:
    app = create_app(config)

    @app.post("/_test/uncommitted")
    async def uncommitted(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, bool]:
        await session.execute(text("INSERT INTO transaction_probe (id) VALUES (1)"))
        return {"ok": True}

    @app.post("/_test/failed")
    async def failed(session: Annotated[AsyncSession, Depends(get_session)]) -> None:
        async with session.begin():
            await session.execute(text("INSERT INTO transaction_probe (id) VALUES (2)"))
            raise RuntimeError("synthetic transaction error")

    @app.post("/_test/committed")
    async def committed(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, bool]:
        async with session.begin():
            await session.execute(text("INSERT INTO transaction_probe (id) VALUES (3)"))
        return {"ok": True}

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.post("/_test/uncommitted")).status_code == 200
            assert (await client.post("/_test/failed")).status_code == 500
            assert (await client.post("/_test/committed")).status_code == 200
        async with app.state.runtime.host.call(STORAGE) as call:
            async with call.require(STORAGE).engine.connect() as connection:
                assert (await connection.execute(text("SELECT id FROM transaction_probe"))).scalars().all() == [3]


async def test_startup_failure_still_disposes_allocated_engine(
    config: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    disposed = []
    original = AsyncEngine.dispose

    async def dispose(engine: AsyncEngine, close: bool = True) -> None:
        disposed.append(engine)
        await original(engine, close=close)

    monkeypatch.setattr(AsyncEngine, "dispose", dispose)

    def broken_factory(_engine: AsyncEngine) -> None:
        raise RuntimeError("synthetic startup secret")

    monkeypatch.setattr(providers, "create_session_factory", broken_factory)
    app = create_app(config)
    with pytest.raises(RuntimeError, match="required components"):
        async with app.router.lifespan_context(app):
            pytest.fail("Startup must fail before serving requests")
    assert len(disposed) == 1
    assert not app.state.runtime.ready
    assert app.state.runtime.host.snapshot()


async def test_readiness_tracks_database_failure_and_recovery_without_leaking_details(
    config: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy = True
    original = providers.probe_database

    async def probe(engine: AsyncEngine, expected: frozenset[str], timeout: float) -> None:
        if not healthy:
            raise OSError("synthetic-private-connection-string")
        await original(engine, expected, timeout)

    monkeypatch.setattr(providers, "probe_database", probe)
    app = create_app(config)
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/ready")).status_code == 200
            healthy = False
            await eventually(lambda: not runtime.ready)
            failed = await client.get("/ready")
            assert failed.status_code == 503
            assert failed.json()["components"]["database"] == "unavailable"
            assert "synthetic-private" not in failed.text
            assert (await client.get("/health")).status_code == 200
            healthy = True
            await eventually(lambda: runtime.ready)
            assert (await client.get("/ready")).status_code == 200


async def test_schema_mismatch_is_not_ready(config: Settings) -> None:
    engine = create_engine(config.database_url)
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE alembic_version SET version_num='old-schema'"))
    await engine.dispose()
    runtime = ApplicationRuntime(config)
    try:
        with pytest.raises(RuntimeError, match="required components"):
            await runtime.start()
        assert not runtime.ready
    finally:
        report = await runtime.host.close(timeout=1)
        assert not report.pending and not report.blocked


async def test_shutdown_waits_for_transaction_cleanup_before_disposing_pool(config: Settings) -> None:
    runtime = ApplicationRuntime(config)
    await runtime.start()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def transaction() -> None:
        async with runtime.host.call(STORAGE) as call:
            async with call.require(STORAGE).sessions() as session:
                async with session.begin():
                    await session.execute(text("INSERT INTO transaction_probe (id) VALUES (4)"))
                    entered.set()
                    await finish.wait()

    task = asyncio.create_task(transaction())
    await entered.wait()
    closing = asyncio.create_task(runtime.close())
    await eventually(lambda: not runtime.ready)
    assert not closing.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await closing
    engine = create_engine(config.database_url)
    try:
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT count(*) FROM transaction_probe"))).scalar() == 0
    finally:
        await engine.dispose()


@pytest.mark.parametrize("required", (False, True))
async def test_redis_unavailability_only_blocks_declared_required_dependency(config: Settings, required: bool) -> None:
    server = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    config = Settings(
        env="test",
        database_url=config.database_url,
        redis_url=f"redis://127.0.0.1:{port}/0",
        redis_required=required,
        redis_connect_timeout_seconds=0.1,
        dependency_probe_timeout_seconds=0.1,
        runtime_startup_timeout_seconds=1,
    )
    runtime = ApplicationRuntime(config)
    try:
        if required:
            with pytest.raises(RuntimeError, match="required components"):
                await runtime.start()
            assert not runtime.ready
        else:
            await runtime.start()
            assert runtime.ready
            assert runtime.public_status()["redis"] == "unavailable"
    finally:
        report = await runtime.host.close(timeout=2)
        assert not report.pending and not report.blocked


@pytest.mark.parametrize("optional", (False, True))
async def test_composition_declares_required_and_optional_component_failures(config: Settings, optional: bool) -> None:
    def broken(_ctx: ComponentContext) -> None:
        raise RuntimeError("synthetic component startup failure")

    plugin = PluginSpec("broken", broken)
    runtime = (
        ApplicationRuntime(config, optional_plugins=(plugin,)) if optional else ApplicationRuntime(config, (plugin,))
    )
    try:
        if optional:
            await runtime.start()
            assert runtime.ready
        else:
            with pytest.raises(RuntimeError, match="required components"):
                await runtime.start()
            assert not runtime.ready
    finally:
        report = await runtime.host.close(timeout=1)
        assert not report.pending and not report.blocked


async def test_application_start_is_idempotent_and_does_not_duplicate_providers(config: Settings) -> None:
    runtime = ApplicationRuntime(config)
    try:
        await runtime.start()
        await runtime.start()
        assert runtime.ready
        snapshot = runtime.host.snapshot()
        assert [item["name"] for item in snapshot["components"]].count("database") == 1
    finally:
        report = await runtime.close(timeout=2)
        assert report.successful


async def test_failed_lifespan_close_keeps_runtime_until_drain_completes(
    config: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as application

    release = asyncio.Event()

    def setup(ctx: ComponentContext) -> None:
        ctx.effect(lambda: release.wait, label="slow-close")

    runtime = ApplicationRuntime(
        config.model_copy(update={"runtime_shutdown_timeout_seconds": 0.05}), (PluginSpec("slow", setup),)
    )
    monkeypatch.setattr(application, "ApplicationRuntime", lambda _config: runtime)
    app = application.create_app(config)
    try:
        with pytest.raises(RuntimeError, match="shutdown"):
            async with app.router.lifespan_context(app):
                assert runtime.ready
        assert app.state.runtime is runtime
        assert not runtime.ready
    finally:
        release.set()
        await runtime.close()
