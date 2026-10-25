import asyncio
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.runtime.effects import EffectClosed, EffectScope, EffectWaitCycle


async def test_registration_precedes_setup_and_cleanup_is_reverse_order() -> None:
    scope = EffectScope("owner:a", 3)
    trace: list[str] = []

    def acquire() -> Generator[Any, None, None]:
        assert len(scope.snapshot()) == 1
        assert scope.snapshot()[0].state == "setting_up"
        trace.append("open:a")
        yield lambda: trace.append("close:a")
        trace.append("open:b")
        yield lambda: trace.append("close:b")

    handle = scope.effect(acquire, "pair")
    await handle.ready()
    report = await scope.close()
    assert trace == ["open:a", "open:b", "close:b", "close:a"]
    assert report.successful
    assert report.records[0].owner_id == "owner:a" and report.records[0].generation == 3


async def test_setup_failure_preserves_partial_acquisitions_and_safe_cleanup() -> None:
    scope = EffectScope("owner")
    released: list[str] = []

    def acquire() -> Generator[Any, None, None]:
        yield lambda: released.append("a")
        yield lambda: released.append("b")
        raise ValueError("setup failed")

    with pytest.raises(ValueError, match="setup failed"):
        scope.effect(acquire)
    report = await scope.close()
    assert released == ["b", "a"]
    assert not report.successful
    assert isinstance(report.errors[0], ValueError)
    assert report.cleanup_complete and not report.cleanup_errors


async def test_close_waits_for_setup_and_reclaims_late_disposer() -> None:
    scope = EffectScope("owner")
    entered, release = asyncio.Event(), asyncio.Event()
    trace: list[str] = []

    async def acquire() -> Any:
        entered.set()
        await release.wait()
        trace.append("acquired")
        return lambda: trace.append("released")

    handle = scope.effect(acquire)
    await entered.wait()
    scope.stop_admission()
    close = asyncio.create_task(scope.close())
    with pytest.raises(EffectClosed):
        scope.effect(lambda: None)
    assert not close.done()
    release.set()
    report = await close
    assert report.successful
    assert handle.state == "closed"
    assert trace == ["acquired", "released"]


async def test_async_generator_failure_and_cleanup_failures_are_all_recorded() -> None:
    scope = EffectScope("owner")
    trace: list[str] = []

    def failed_cleanup() -> None:
        trace.append("fail")
        raise RuntimeError("cleanup failed")

    async def acquire() -> AsyncGenerator[Any, None]:
        yield lambda: trace.append("safe")
        yield failed_cleanup
        raise ValueError("setup failed")

    handle = scope.effect(acquire)
    with pytest.raises(ValueError, match="setup failed"):
        await handle.ready()
    report = await scope.close()
    assert trace == ["fail", "safe"]
    assert [str(error) for error in report.errors] == ["setup failed", "cleanup failed"]
    assert not report.cleanup_complete and [str(error) for error in report.cleanup_errors] == ["cleanup failed"]


async def test_explicit_dependency_is_retained_after_dependent_cleanup_fails() -> None:
    scope = EffectScope("owner")
    trace: list[str] = []
    pool = scope.effect(lambda: lambda: trace.append("pool.close"), "pool")
    scope.effect(lambda: lambda: trace.append("independent.close"), "independent")

    def failed_cleanup() -> None:
        trace.append("worker.close")
        raise RuntimeError("worker still owns pool")

    scope.effect(lambda: failed_cleanup, "worker", depends_on=(pool,))
    report = await scope.close()
    assert trace == ["worker.close", "independent.close"]
    assert {record.label: record.state for record in report.records} == {
        "pool": "blocked",
        "independent": "closed",
        "worker": "failed",
    }
    assert not report.successful
    assert await scope.close() is report


async def test_manual_close_cannot_release_live_dependent_resources() -> None:
    scope = EffectScope("owner")
    trace: list[str] = []
    pool = scope.effect(lambda: lambda: trace.append("pool"))
    worker = scope.effect(lambda: lambda: trace.append("worker"), depends_on=(pool,))
    with pytest.raises(EffectClosed, match="Dependent"):
        await pool.close()
    assert trace == []
    await worker.close()
    await pool.close()
    assert (await scope.close()).successful
    assert trace == ["worker", "pool"]


async def test_shared_close_survives_cancelled_waiter_and_disposes_once() -> None:
    scope = EffectScope("owner")
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def cleanup() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    handle = scope.effect(lambda: cleanup)
    first = asyncio.create_task(handle.close())
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert handle.state == "closing"
    second = asyncio.create_task(handle.close())
    release.set()
    assert (await second).successful
    assert (await scope.close()).successful
    assert calls == 1


async def test_setup_waiter_cancel_does_not_cancel_owned_acquisition() -> None:
    scope = EffectScope("owner")
    started, release = asyncio.Event(), asyncio.Event()
    released: list[bool] = []

    async def acquire() -> Any:
        started.set()
        await release.wait()
        return lambda: released.append(True)

    handle = scope.effect(acquire)
    await started.wait()
    waiter = asyncio.create_task(handle.ready())
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    assert (await scope.close()).successful
    assert released == [True]


async def test_self_wait_is_rejected_and_other_resources_still_close() -> None:
    scope = EffectScope("owner")

    async def cleanup() -> None:
        await scope.close()

    scope.effect(lambda: cleanup)
    report = await scope.close()
    assert not report.successful
    assert isinstance(report.errors[0], EffectWaitCycle)


async def test_async_context_manager_and_resource_free_setup() -> None:
    scope = EffectScope("owner")
    trace: list[str] = []

    @asynccontextmanager
    async def resource() -> AsyncGenerator[None, None]:
        trace.append("enter")
        try:
            yield
        finally:
            trace.append("exit")

    async def no_resource() -> None:
        trace.append("empty")

    scope.effect(resource)
    scope.effect(no_resource)
    await scope.ready()
    assert (await scope.close()).successful
    assert trace == ["enter", "empty", "exit"]


async def test_old_owner_and_invalid_disposer_cannot_escape_tracking() -> None:
    active = True
    scope = EffectScope("owner", is_open=lambda: active)
    active = False
    with pytest.raises(EffectClosed):
        scope.effect(lambda: None)
    active = True
    with pytest.raises(TypeError):
        scope.effect(lambda: 3)
    assert len(scope.snapshot()) == 1
    assert not (await scope.close()).successful
