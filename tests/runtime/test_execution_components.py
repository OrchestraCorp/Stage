"""Real application components with finite test identities, never production IAM."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.delivery.worker import OutboxWorker
from app.providers.execution import COMMANDS, DELIVERY, command_plugin, delivery_plugin
from app.providers.storage import STORAGE, Storage
from app.runtime import ComponentContext, PluginSpec, RuntimeHost
from tests.delivery.test_worker import (
    SERVICE,
    TENANT_A,
    Clock,
    MutableAuthorizer,
    ReceiverSink,
    message_state,
    seed_message,
)
from tests.execution.test_kernel import TestFactsLoader as FixtureFactsLoader
from tests.execution.test_kernel import command, context, counts, seed, spec


def storage_component(
    sessions: async_sessionmaker[AsyncSession],
    events: list[str],
    before_cleanup: Callable[[], Awaitable[None]] | None = None,
) -> PluginSpec:
    """The fixture owns engine disposal; Core owns its consumer/pin ordering."""

    def setup(ctx: ComponentContext) -> None:
        async def cleanup() -> None:
            if before_cleanup is not None:
                await before_cleanup()
            events.append("storage.cleanup")

        ctx.provide(STORAGE, Storage(sessions.kw["bind"], sessions))
        ctx.effect(lambda: cleanup, label="fixture.storage")

    return PluginSpec("fixture.storage", setup, provides=(STORAGE,))


async def test_command_component_uses_core_lease_and_preserves_persistent_idempotency(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(persistence_sessions)
    host = RuntimeHost()
    events: list[str] = []
    storage = host.load(storage_component(persistence_sessions, events))
    commands = host.load(command_plugin(FixtureFactsLoader(), [spec()]))
    try:
        assert (await host.await_ready([storage, commands], timeout=5)).ready
        async with host.call(COMMANDS) as call:
            kernel = call.require(COMMANDS)
            first, second = await asyncio.gather(
                kernel.execute(context(), command()), kernel.execute(context(), command())
            )
            assert first.execution_id == second.execution_id
            assert sorted((first.replayed, second.replayed)) == [False, True]
            assert (await kernel.get_result(context(), first.execution_id)).result == {"version": 2}
            assert events == []
        assert await counts(persistence_sessions) == (2, 1, 1, 1)
    finally:
        report = await host.close(timeout=5)
        assert report.successful, report
    assert events == ["storage.cleanup"]


async def test_core_managed_delivery_component_starts_and_persists_receipt(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock, sink = Clock(now=datetime.now(UTC)), ReceiverSink()
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    # Fixture SERVICE role-assignment facts exercise existing authz only. They
    # do not claim the ServiceGrant-backed production adapter is implemented.
    authorizer = MutableAuthorizer(clock, {(TENANT_A, aggregate)})
    sink.authorizer = authorizer
    host = RuntimeHost()
    events: list[str] = []
    storage = host.load(storage_component(persistence_sessions, events))
    delivery = host.load(
        delivery_plugin(
            authorizer=authorizer,
            sinks={"test-sink": sink},
            service_ref=SERVICE,
            worker_ref="fixture:delivery",
            tenants=[TENANT_A],
            poll_interval=0.01,
        )
    )
    try:
        assert (await host.await_ready([storage, delivery], timeout=5)).ready
        async with host.call(DELIVERY) as call:
            worker = call.require(DELIVERY)
            assert worker.status.state == "running"
        async with asyncio.timeout(5):
            while (row := (await message_state(persistence_sessions, message_id))[0]).status != "sent":
                await asyncio.sleep(0)
        assert row.receipt_ref == "receipt:1" and row.lease_token is None
        assert sink.effects == 1 and authorizer.operations == ["send"]
    finally:
        report = await host.close(timeout=5)
        assert report.successful, report
    assert events == ["storage.cleanup"]


async def test_core_delivery_cancellation_records_unknown_before_storage_cleanup(
    persistence_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, sink = Clock(now=datetime.now(UTC)), ReceiverSink(block_first=True)
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    authorizer = MutableAuthorizer(clock, {(TENANT_A, aggregate)})
    sink.authorizer = authorizer
    host = RuntimeHost()
    events: list[str] = []
    original_close = OutboxWorker.close

    async def instrumented_close(worker: OutboxWorker) -> None:
        await original_close(worker)
        row, attempts = await message_state(persistence_sessions, message_id)
        assert row.status == "unknown" and row.lease_token is None
        assert attempts[-1].status == "unknown"
        events.append("worker.closed")

    async def storage_cleanup() -> None:
        row, _ = await message_state(persistence_sessions, message_id)
        assert row.status == "unknown"
        assert events == ["worker.closed"]
        events.append("unknown.observed")

    monkeypatch.setattr(OutboxWorker, "close", instrumented_close)
    storage = host.load(storage_component(persistence_sessions, events, storage_cleanup))
    delivery = host.load(
        delivery_plugin(
            authorizer=authorizer,
            sinks={"test-sink": sink},
            service_ref=SERVICE,
            worker_ref="fixture:delivery",
            tenants=[TENANT_A],
            poll_interval=0.01,
        )
    )
    try:
        assert (await host.await_ready([storage, delivery], timeout=5)).ready
        await asyncio.wait_for(sink.started.wait(), timeout=5)
        assert (await message_state(persistence_sessions, message_id))[0].status == "delivering"
    finally:
        report = await host.close(timeout=5)
        assert report.successful, report
    assert events == ["worker.closed", "unknown.observed", "storage.cleanup"]
    assert sink.calls == 1


async def test_delivery_poll_fault_is_observed_by_host_before_application_close(
    persistence_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, sink = Clock(now=datetime.now(UTC)), ReceiverSink()
    _, aggregate = await seed_message(persistence_sessions, clock)
    authorizer = MutableAuthorizer(clock, {(TENANT_A, aggregate)})
    fail = asyncio.Event()

    async def broken_poll(worker: OutboxWorker, tenant_ref: str) -> Any:
        await fail.wait()
        raise OSError("fixture database poll failure")

    monkeypatch.setattr(OutboxWorker, "run_once", broken_poll)
    host = RuntimeHost()
    events: list[str] = []
    storage = host.load(storage_component(persistence_sessions, events))
    delivery = host.load(
        delivery_plugin(
            authorizer=authorizer,
            sinks={"test-sink": sink},
            service_ref=SERVICE,
            worker_ref="fixture:delivery",
            tenants=[TENANT_A],
        )
    )
    try:
        assert (await host.await_ready([storage, delivery], timeout=5)).ready
        async with host.call(DELIVERY) as call:
            worker = call.require(DELIVERY)
        fail.set()
        with pytest.raises(OSError, match="fixture database poll failure"):
            await worker.wait()
        async with asyncio.timeout(5):
            while not any(error.error_type == "OSError" for error in host.observations):
                await asyncio.sleep(0)
        assert worker.status.state == "failed"
        assert not host.is_ready([delivery])
        assert (await host.await_ready([delivery], timeout=1)).outcome == "failed"
        assert sink.calls == 0
    finally:
        report = await host.close(timeout=5)
        assert report.failed or report.blocked, report
