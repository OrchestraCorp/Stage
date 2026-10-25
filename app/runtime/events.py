"""Typed process-local events with owned registrations and in-flight pins."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from contextlib import AbstractContextManager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import uuid4

from app.runtime.effects import EffectHandle, EffectScope, EffectWaitCycle

DispatchMode = Literal["emit", "parallel", "serial", "bail", "waterfall"]


@dataclass(frozen=True)
class EventKey[T]:
    name: str
    mode: DispatchMode
    asynchronous: bool = False
    payload_types: tuple[type[Any], ...] | None = None

    def __post_init__(self) -> None:
        if not self.name or self.mode not in ("emit", "parallel", "serial", "bail", "waterfall"):
            raise ValueError("An event requires a stable name and a supported dispatch mode")
        if self.asynchronous and self.mode != "waterfall":
            raise ValueError("Only waterfall needs an explicit synchronous/asynchronous variant")


@dataclass(frozen=True)
class Continue:
    """Continue a serial/bail dispatch; distinct from any returned payload."""


@dataclass(frozen=True)
class Stop[T]:
    value: T


class EventPin(Protocol):
    def release(self) -> None: ...


class EventOwner(Protocol):
    @property
    def owner_id(self) -> str: ...

    @property
    def generation(self) -> int: ...

    @property
    def scope(self) -> EffectScope: ...

    def can_dispatch(self) -> bool: ...

    def acquire_event_pin(self, registration_id: str) -> EventPin: ...

    def dispatch_context(self, selected_owners: tuple[str, ...]) -> AbstractContextManager[Any]: ...


_selected: ContextVar[frozenset[str]] = ContextVar("runtime_event_registrations", default=frozenset())


class ListenerHandle:
    def __init__(self, bus: EventBus, owner: EventOwner, key: EventKey[Any], callback: Callable[..., Any]) -> None:
        self.bus = bus
        self.owner = owner
        self.generation = owner.generation
        self.key = key
        self.callback = callback
        self.registration_id = uuid4().hex
        self.registered = True
        self._in_flight = 0
        self._drained = asyncio.Event()
        self._drained.set()
        self._closing: asyncio.Task[bool] | None = None
        self._effect: EffectHandle | None = None

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def unregister(self) -> None:
        self.registered = False
        hooks = self.bus._hooks.get(self.key.name, [])
        if self in hooks:
            hooks.remove(self)

    async def close(self) -> None:
        if self.registration_id in _selected.get():
            raise EffectWaitCycle("A dispatch cannot await closure of one of its selected listeners")
        self.unregister()
        if self._effect is not None:
            report = await self._effect.close()
            if report.cleanup_errors:
                raise BaseExceptionGroup("Listener cleanup failed", list(report.cleanup_errors))
        else:
            await self._dispose_registration()

    async def _dispose_registration(self) -> None:
        if self.registration_id in _selected.get():
            raise EffectWaitCycle("A listener cannot wait for its own dispatch during owner cleanup")
        self.unregister()
        if self._closing is None:
            self._closing = asyncio.create_task(self._drained.wait(), name=f"listener-close:{self.key.name}")
        await asyncio.shield(self._closing)


class _Snapshot:
    def __init__(self, listeners: tuple[ListenerHandle, ...], pins: list[EventPin]) -> None:
        self.listeners = listeners
        self.pins = pins

    def release(self) -> None:
        for listener, pin in zip(self.listeners, self.pins, strict=True):
            listener._in_flight -= 1
            if not listener._in_flight:
                listener._drained.set()
            pin.release()
        self.pins = []


class EventBus:
    def __init__(self) -> None:
        self._keys: dict[str, EventKey[Any]] = {}
        self._hooks: dict[str, list[ListenerHandle]] = {}

    def _check_key(self, key: EventKey[Any]) -> None:
        previous = self._keys.setdefault(key.name, key)
        if previous != key:
            raise ValueError("An event name cannot change its mode or payload contract")

    def on(
        self,
        owner: EventOwner,
        key: EventKey[Any],
        callback: Callable[..., Any],
        *,
        prepend: bool = False,
    ) -> ListenerHandle:
        self._check_key(key)
        synchronous = key.mode in ("emit", "bail") or (key.mode == "waterfall" and not key.asynchronous)
        if synchronous and (
            inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(type(callback).__call__)
        ):
            raise TypeError("Synchronous events cannot register asynchronous listeners")
        listener = ListenerHandle(self, owner, key, callback)

        def register() -> Callable[[], Any]:
            hooks = self._hooks.setdefault(key.name, [])
            hooks.insert(0, listener) if prepend else hooks.append(listener)
            return listener._dispose_registration

        listener._effect = owner.scope.effect(register, label=f"listener:{key.name}")
        return listener

    def _snapshot(self, key: EventKey[Any], args: tuple[Any, ...]) -> _Snapshot:
        self._check_key(key)
        if key.payload_types is not None and (
            len(args) != len(key.payload_types)
            or any(not isinstance(value, expected) for value, expected in zip(args, key.payload_types, strict=True))
        ):
            raise TypeError("Event payload does not match its declared types")
        listeners: list[ListenerHandle] = []
        pins: list[EventPin] = []
        try:
            # No await between admission, snapshot selection and pin acquisition.
            for listener in tuple(self._hooks.get(key.name, ())):
                owner = listener.owner
                if not listener.registered or owner.generation != listener.generation or not owner.can_dispatch():
                    continue
                pin = owner.acquire_event_pin(listener.registration_id)
                listener._in_flight += 1
                listener._drained.clear()
                listeners.append(listener)
                pins.append(pin)
        except BaseException:
            _Snapshot(tuple(listeners), pins).release()
            raise
        return _Snapshot(tuple(listeners), pins)

    @staticmethod
    def _sync_result(value: Any) -> Any:
        if inspect.isawaitable(value):
            if inspect.iscoroutine(value):
                value.close()
            raise TypeError("A synchronous listener returned an awaitable")
        return value

    @staticmethod
    def _decision(value: Any) -> Continue | Stop[Any]:
        if not isinstance(value, (Continue, Stop)):
            raise TypeError("Bail and serial listeners must return Continue() or Stop(value)")
        return value

    @staticmethod
    async def _settle(task: asyncio.Task[Any]) -> Any:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            # Repeated caller cancellation must not abandon owned callbacks.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if task.done() and not task.cancelled():
                task.exception()
            raise

    def _sync_dispatch(self, key: EventKey[Any], args: tuple[Any, ...], terminal: Callable[..., Any] | None) -> Any:
        snapshot = self._snapshot(key, args)
        token = _selected.set(_selected.get() | {listener.registration_id for listener in snapshot.listeners})
        owners = tuple(listener.owner.owner_id for listener in snapshot.listeners)
        try:

            def invoke(listener: ListenerHandle, *values: Any) -> Any:
                with listener.owner.dispatch_context(owners):
                    return self._sync_result(listener.callback(*values))

            if key.mode == "waterfall":
                index = 0

                def chain() -> Any:
                    nonlocal index
                    if index == len(snapshot.listeners):
                        return self._sync_result(terminal(*args)) if terminal else None
                    listener = snapshot.listeners[index]
                    index += 1
                    return invoke(listener, *args, chain)

                return chain()
            for listener in snapshot.listeners:
                result = invoke(listener, *args)
                if key.mode == "bail" and isinstance(decision := self._decision(result), Stop):
                    return decision
            return Continue() if key.mode == "bail" else None
        finally:
            _selected.reset(token)
            snapshot.release()

    async def _async_dispatch(
        self, key: EventKey[Any], args: tuple[Any, ...], terminal: Callable[..., Any] | None = None
    ) -> Any:
        snapshot = self._snapshot(key, args)
        token = _selected.set(_selected.get() | {listener.registration_id for listener in snapshot.listeners})
        owners = tuple(listener.owner.owner_id for listener in snapshot.listeners)

        async def invoke(listener: ListenerHandle, *values: Any) -> Any:
            with listener.owner.dispatch_context(owners):
                result = listener.callback(*values)
                return await result if inspect.isawaitable(result) else result

        async def run() -> Any:
            if key.mode == "parallel":

                async def captured(listener: ListenerHandle) -> BaseException | None:
                    try:
                        await invoke(listener, *args)
                        return None
                    except BaseException as exc:
                        return exc

                tasks = [asyncio.create_task(captured(listener)) for listener in snapshot.listeners]
                try:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                except asyncio.CancelledError:
                    for child in tasks:
                        child.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
                errors = [result for result in results if isinstance(result, BaseException)]
                if errors:
                    raise BaseExceptionGroup("Event listeners failed", errors)
                return None
            if key.mode == "waterfall":
                index = 0

                async def chain() -> Any:
                    nonlocal index
                    if index == len(snapshot.listeners):
                        result = terminal(*args) if terminal else None
                        return await result if inspect.isawaitable(result) else result
                    listener = snapshot.listeners[index]
                    index += 1
                    return await invoke(listener, *args, chain)

                return await chain()
            for listener in snapshot.listeners:
                decision = self._decision(await invoke(listener, *args))
                if isinstance(decision, Stop):
                    return decision
            return Continue()

        try:
            operation = asyncio.create_task(run(), name=f"event:{key.name}")
            return await self._settle(operation)
        finally:
            _selected.reset(token)
            snapshot.release()

    def emit(self, key: EventKey[Any], *args: Any) -> None:
        self._mode(key, "emit")
        self._sync_dispatch(key, args, None)

    def bail(self, key: EventKey[Any], *args: Any) -> Continue | Stop[Any]:
        self._mode(key, "bail")
        return self._decision(self._sync_dispatch(key, args, None))

    async def parallel(self, key: EventKey[Any], *args: Any) -> None:
        self._mode(key, "parallel")
        await self._async_dispatch(key, args)

    async def serial(self, key: EventKey[Any], *args: Any) -> Continue | Stop[Any]:
        self._mode(key, "serial")
        return self._decision(await self._async_dispatch(key, args))

    def waterfall(self, key: EventKey[Any], *args: Any, next: Callable[..., Any]) -> Any:
        self._mode(key, "waterfall")
        if key.asynchronous:
            return self._async_dispatch(key, args, next)
        return self._sync_dispatch(key, args, next)

    @staticmethod
    def _mode(key: EventKey[Any], expected: DispatchMode) -> None:
        if key.mode != expected:
            raise ValueError("Dispatch mode does not match the event contract")
