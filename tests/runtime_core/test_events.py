import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import pytest

from app.runtime.effects import EffectScope, EffectWaitCycle
from app.runtime.events import Continue, EventBus, EventKey, Stop


@dataclass
class Pin:
    owner: "Owner"
    released: bool = False

    def release(self) -> None:
        assert not self.released
        self.released = True
        self.owner.pins -= 1
        if not self.owner.pins:
            self.owner.drained.set()


selected: ContextVar[tuple[str, ...]] = ContextVar("test_selected", default=())


class Owner:
    """An admission/drain adapter exercising EventBus's owner protocol."""

    def __init__(self, owner_id: str, *, active: bool = True) -> None:
        self.owner_id, self.generation = owner_id, 1
        self.active, self.accepting = active, True
        self.scope = EffectScope(owner_id, 1, is_open=lambda: self.accepting)
        self.pins = 0
        self.drained = asyncio.Event()
        self.drained.set()
        self.disposed = False

    def can_dispatch(self) -> bool:
        return self.active and self.accepting

    def acquire_event_pin(self, registration_id: str) -> Pin:
        assert self.can_dispatch() and registration_id
        self.pins += 1
        self.drained.clear()
        return Pin(self)

    @contextmanager
    def dispatch_context(self, selected_owners: tuple[str, ...]) -> Iterator[None]:
        token = selected.set(selected.get() + selected_owners)
        try:
            yield
        finally:
            selected.reset(token)

    async def close(self) -> None:
        if self.owner_id in selected.get():
            raise EffectWaitCycle("dispatch owns this owner")
        self.accepting = False
        await self.drained.wait()
        assert (await self.scope.close()).successful
        self.disposed = True


async def test_listener_admission_owner_generation_and_unregister() -> None:
    bus, owner = EventBus(), Owner("owner", active=False)
    event = EventKey[object]("changed", "emit", payload_types=(str,))
    trace: list[str] = []
    listener = bus.on(owner, event, trace.append)
    bus.emit(event, "loading")
    assert trace == []
    owner.active = True
    bus.emit(event, "active")
    assert trace == ["active"]
    owner.generation += 1
    bus.emit(event, "old registration")
    assert trace == ["active"]
    owner.generation -= 1
    await listener.close()
    assert owner.scope.snapshot()[0].state == "closed"
    bus.emit(event, "removed")
    new = bus.on(owner, event, trace.append)
    assert new.registration_id != listener.registration_id
    await listener.close()
    bus.emit(event, "new registration")
    assert trace == ["active", "new registration"]
    await owner.close()
    bus.emit(event, "disposed")
    assert owner.pins == 0


async def test_serial_snapshot_pins_not_yet_started_listener_until_owner_drains() -> None:
    bus, a, b = EventBus(), Owner("a"), Owner("b")
    event = EventKey[object]("review", "serial")
    entered, release = asyncio.Event(), asyncio.Event()
    trace: list[str] = []

    async def first() -> Continue:
        entered.set()
        await release.wait()
        return Continue()

    def second() -> Continue:
        assert not b.disposed
        trace.append("b")
        return Continue()

    bus.on(a, event, first)
    listener = bus.on(b, event, second)
    dispatch = asyncio.create_task(bus.serial(event))
    await entered.wait()
    assert a.pins == b.pins == listener.in_flight == 1
    b.accepting = False
    close = asyncio.create_task(b.close())
    assert not close.done()
    release.set()
    assert isinstance(await dispatch, Continue)
    await close
    assert trace == ["b"] and b.disposed
    assert a.pins == b.pins == 0
    await a.close()


@pytest.mark.parametrize("payload", (None, False, 0, ""))
async def test_explicit_bail_results_do_not_use_python_truthiness(payload: Any) -> None:
    bus, owner = EventBus(), Owner("owner")
    event = EventKey[object]("bail", "bail")
    trace: list[str] = []
    bus.on(owner, event, lambda: Continue())
    bus.on(owner, event, lambda: Stop(payload))
    bus.on(owner, event, lambda: trace.append("unreachable"))
    assert bus.bail(event) == Stop(payload)
    assert trace == [] and owner.pins == 0
    await owner.close()


async def test_serial_short_circuit_releases_unused_pins_and_rejects_implicit_results() -> None:
    bus, a, b = EventBus(), Owner("a"), Owner("b")
    event = EventKey[object]("serial", "serial")
    first = bus.on(a, event, lambda: Stop(0))
    second = bus.on(b, event, lambda: pytest.fail("unreachable"))
    assert await bus.serial(event) == Stop(0)
    assert first.in_flight == second.in_flight == a.pins == b.pins == 0
    await first.close()
    await second.close()
    bus.on(a, event, lambda: None)
    with pytest.raises(TypeError, match="Continue"):
        await bus.serial(event)
    await a.close()
    await b.close()


async def test_parallel_waits_for_other_listener_before_aggregating_failure() -> None:
    bus, a, b = EventBus(), Owner("a"), Owner("b")
    event = EventKey[object]("parallel", "parallel")
    failed, release = asyncio.Event(), asyncio.Event()

    def failure() -> None:
        failed.set()
        raise ValueError("one listener failed")

    async def slow() -> None:
        await release.wait()

    bus.on(a, event, failure)
    bus.on(b, event, slow)
    dispatch = asyncio.create_task(bus.parallel(event))
    await failed.wait()
    assert not dispatch.done() and b.pins == 1
    release.set()
    with pytest.raises(ExceptionGroup) as error:
        await dispatch
    assert isinstance(error.value.exceptions[0], ValueError)
    assert a.pins == b.pins == 0
    await a.close()
    await b.close()


async def test_cancel_waits_for_noncooperative_listener_before_releasing_pin() -> None:
    bus, owner = EventBus(), Owner("owner")
    event = EventKey[object]("slow", "parallel")
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    listener = bus.on(owner, event, slow)
    dispatch = asyncio.create_task(bus.parallel(event))
    await started.wait()
    dispatch.cancel()
    await cancelled.wait()
    assert owner.pins == listener.in_flight == 1 and not dispatch.done()
    owner.accepting = False
    close = asyncio.create_task(owner.close())
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await dispatch
    await close
    assert owner.disposed and owner.pins == 0


@pytest.mark.parametrize("asynchronous", (False, True))
async def test_waterfall_around_order_and_short_circuit(asynchronous: bool) -> None:
    bus, owner = EventBus(), Owner("owner")
    event = EventKey[object]("around", "waterfall", asynchronous=asynchronous)
    trace: list[str] = []

    def sync_outer(value: int, next: Callable[[], Any]) -> int:
        trace.append("before")
        result = next()
        trace.append("after")
        return int(result) + value

    async def async_outer(value: int, next: Callable[[], Any]) -> int:
        trace.append("before")
        result = await next()
        trace.append("after")
        return int(result) + value

    outer = bus.on(owner, event, async_outer if asynchronous else sync_outer)
    bus.on(owner, event, lambda value, next: 5)
    result = bus.waterfall(event, 2, next=lambda _: pytest.fail("short circuited"))
    assert (await result if asynchronous else result) == 7
    assert trace == ["before", "after"] and owner.pins == 0
    await outer.close()
    await owner.close()


async def test_cross_listener_wait_is_rejected_and_new_listener_not_in_existing_snapshot() -> None:
    bus, a, b = EventBus(), Owner("a"), Owner("b")
    event = EventKey[object]("cross", "serial")
    trace: list[str] = []

    def late() -> Continue:
        trace.append("late")
        return Continue()

    def second_callback() -> Continue:
        trace.append("second")
        return Continue()

    async def first() -> Continue:
        with pytest.raises(EffectWaitCycle):
            await b.close()
        with pytest.raises(EffectWaitCycle):
            await second.close()
        bus.on(a, event, late)
        return Continue()

    first_handle = bus.on(a, event, first)
    second = bus.on(b, event, second_callback)
    await bus.serial(event)
    assert trace == ["second"]
    await first_handle.close()
    await bus.serial(event)
    assert trace == ["second", "second", "late"]
    await a.close()
    await b.close()


async def test_synchronous_modes_reject_async_registration_and_awaitable_results() -> None:
    bus, owner = EventBus(), Owner("owner")
    event = EventKey[object]("sync", "emit")

    async def listener() -> None:
        pass

    with pytest.raises(TypeError, match="asynchronous"):
        bus.on(owner, event, listener)
    bus.on(owner, event, lambda: listener())
    with pytest.raises(TypeError, match="awaitable"):
        bus.emit(event)
    assert owner.pins == 0
    with pytest.raises(ValueError, match="mode"):
        bus.bail(event)
    with pytest.raises(ValueError, match="contract"):
        bus.on(owner, EventKey[object]("sync", "bail"), lambda: Continue())
    await owner.close()


async def test_payload_contract_and_parallel_returned_exception_value() -> None:
    bus, owner = EventBus(), Owner("owner")
    event = EventKey[object]("payload", "parallel", payload_types=(str,))
    bus.on(owner, event, lambda value: ValueError(value))
    with pytest.raises(TypeError, match="payload"):
        await bus.parallel(event, 4)
    assert owner.pins == 0
    await bus.parallel(event, "a returned exception is not a rejected call")
    await owner.close()
