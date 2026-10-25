"""Owned resource acquisition and shared, cancellation-safe cleanup operations.

Dependencies are explicit: a failed dependent keeps its dependencies blocked,
while independent resources can still be released. No business rollback occurs.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Generator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.runtime.contracts import LifecycleWaitCycle, RuntimeClosed


class EffectClosed(RuntimeClosed):
    """The owner no longer accepts resource acquisitions."""


class EffectWaitCycle(LifecycleWaitCycle):
    """A setup or disposer tried to wait for its own completion."""


_executing: ContextVar[frozenset[str]] = ContextVar("runtime_effect_operations", default=frozenset())


@dataclass(frozen=True)
class EffectRecord:
    registration_id: str
    owner_id: str
    generation: int
    label: str
    state: str
    errors: tuple[BaseException, ...] = ()
    cleanup_errors: tuple[BaseException, ...] = ()

    @property
    def cleanup_complete(self) -> bool:
        return self.state == "closed" and not self.cleanup_errors


@dataclass(frozen=True)
class EffectReport:
    records: tuple[EffectRecord, ...]

    @property
    def errors(self) -> tuple[BaseException, ...]:
        return tuple(error for record in self.records for error in record.errors)

    @property
    def successful(self) -> bool:
        return all(record.state == "closed" and not record.errors for record in self.records)

    @property
    def cleanup_complete(self) -> bool:
        """Setup may have failed even though every acquired resource was released."""
        return all(record.cleanup_complete for record in self.records)

    @property
    def cleanup_errors(self) -> tuple[BaseException, ...]:
        return tuple(error for record in self.records for error in record.cleanup_errors)


class EffectHandle:
    def __init__(
        self,
        scope: EffectScope,
        label: str,
        depends_on: tuple[EffectHandle, ...],
    ) -> None:
        self.scope = scope
        self.registration_id = uuid4().hex
        self.label = label
        self.depends_on = depends_on
        self.state = "setting_up"
        self._errors: list[BaseException] = []
        self._cleanup_errors: list[BaseException] = []
        self._disposers: list[Callable[[], Any]] = []
        self._setup: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[EffectReport] | None = None

    def snapshot(self) -> EffectRecord:
        return EffectRecord(
            self.registration_id,
            self.scope.owner_id,
            self.scope.generation,
            self.label,
            self.state,
            tuple(self._errors),
            tuple(self._cleanup_errors),
        )

    async def ready(self) -> EffectHandle:
        if self.registration_id in _executing.get():
            raise EffectWaitCycle("An effect cannot await its own setup")
        if self._setup is not None:
            await asyncio.shield(self._setup)
        if self._errors and self.state == "setup_failed":
            raise self._errors[0]
        return self

    async def close(self) -> EffectReport:
        if self.registration_id in _executing.get():
            raise EffectWaitCycle("An effect cannot await its own cleanup")
        if any(self in other.depends_on and other.state != "closed" for other in self.scope._effects):
            raise EffectClosed("Dependent effects must finish before their resource is closed")
        return await self._close_shared()

    async def _close_shared(self) -> EffectReport:
        if self._closing is None:
            self._closing = asyncio.create_task(self._close(), name=f"effect-close:{self.label}")
        return await asyncio.shield(self._closing)

    async def _close(self) -> EffectReport:
        token = _executing.set(_executing.get() | {self.registration_id})
        try:
            if self._setup is not None:
                # Setup remains owned even if the initiating waiter was cancelled.
                await asyncio.shield(self._setup)
            self.state = "closing"
            for disposer in reversed(self._disposers):
                try:
                    result = disposer()
                    if inspect.isawaitable(result):
                        await result
                except BaseException as exc:
                    self._errors.append(exc)
                    self._cleanup_errors.append(exc)
            self._disposers.clear()
            self.state = "failed" if self._cleanup_errors else "closed"
            return EffectReport((self.snapshot(),))
        finally:
            _executing.reset(token)

    def _start(self, setup: Callable[[], Any]) -> None:
        token = _executing.set(_executing.get() | {self.registration_id})
        try:
            self._consume_sync(setup())
        except BaseException as exc:
            self._errors.append(exc)
            self.state = "setup_failed"
            raise
        finally:
            _executing.reset(token)

    def _consume_sync(self, result: Any) -> None:
        if inspect.isgenerator(result):
            self._consume_generator(result)
        elif inspect.isawaitable(result) or inspect.isasyncgen(result) or hasattr(result, "__aenter__"):
            self._setup = asyncio.create_task(self._consume_async(result), name=f"effect-setup:{self.label}")
        elif hasattr(result, "__enter__") and hasattr(result, "__exit__"):
            result.__enter__()
            self._disposers.append(lambda: result.__exit__(None, None, None))
            self.state = "ready"
        elif result is None or callable(result):
            if result is not None:
                self._disposers.append(result)
            self.state = "ready"
        else:
            raise TypeError("Effect setup must return a disposer, generator, context manager, awaitable or None")

    def _consume_generator(self, generator: Generator[Any, Any, Any]) -> None:
        try:
            for result in generator:
                if result is not None and not callable(result):
                    raise TypeError("Effect generators must yield disposers or None")
                if result is not None:
                    self._disposers.append(result)
            self.state = "ready"
        finally:
            generator.close()

    async def _consume_async(self, result: Any) -> None:
        token = _executing.set(_executing.get() | {self.registration_id})
        try:
            while inspect.isawaitable(result):
                result = await result
            if inspect.isasyncgen(result):
                try:
                    async for disposer in result:
                        if disposer is not None and not callable(disposer):
                            raise TypeError("Effect generators must yield disposers or None")
                        if disposer is not None:
                            self._disposers.append(disposer)
                finally:
                    await result.aclose()
                self.state = "ready"
            elif hasattr(result, "__aenter__") and hasattr(result, "__aexit__"):
                await result.__aenter__()
                self._disposers.append(lambda: result.__aexit__(None, None, None))
                self.state = "ready"
            else:
                self._consume_sync(result)
        except BaseException as exc:
            self._errors.append(exc)
            self.state = "setup_failed"
            # Store errors on the owned record; ready() is the propagation point.
        finally:
            _executing.reset(token)


class EffectScope:
    def __init__(
        self,
        owner_id: str,
        generation: int = 0,
        *,
        is_open: Callable[[], bool] | None = None,
    ) -> None:
        self.owner_id = owner_id
        self.generation = generation
        self._is_open = is_open or (lambda: True)
        self._accepting = True
        self._effects: list[EffectHandle] = []
        self._closing: asyncio.Task[EffectReport] | None = None

    def effect(
        self,
        setup: Callable[[], Any],
        label: str = "effect",
        *,
        depends_on: tuple[EffectHandle, ...] = (),
    ) -> EffectHandle:
        if not self._accepting or not self._is_open():
            raise EffectClosed("The resource owner is closing or inactive")
        if any(
            dependency.scope is not self or dependency.state in ("closing", "closed", "failed")
            for dependency in depends_on
        ):
            raise ValueError("Effect dependencies must be live, previously registered effects of the same owner")
        handle = EffectHandle(self, label, depends_on)
        self._effects.append(handle)
        handle._start(setup)
        return handle

    async def ready(self) -> None:
        # Setup can register nested resources; include those before returning.
        index = 0
        while index < len(self._effects):
            await self._effects[index].ready()
            index += 1

    def stop_admission(self) -> None:
        self._accepting = False

    def snapshot(self) -> tuple[EffectRecord, ...]:
        return tuple(handle.snapshot() for handle in self._effects)

    @property
    def errors(self) -> tuple[BaseException, ...]:
        return tuple(error for record in self.snapshot() for error in record.errors)

    async def close(self) -> EffectReport:
        if any(handle.registration_id in _executing.get() for handle in self._effects):
            raise EffectWaitCycle("An effect cannot await its own owner cleanup")
        self.stop_admission()
        if self._closing is None:
            self._closing = asyncio.create_task(self._close(), name=f"scope-close:{self.owner_id}")
        return await asyncio.shield(self._closing)

    async def _close(self) -> EffectReport:
        blocked: set[EffectHandle] = set()

        def block_dependencies(handle: EffectHandle) -> None:
            for dependency in handle.depends_on:
                if dependency not in blocked:
                    blocked.add(dependency)
                    block_dependencies(dependency)

        for handle in reversed(self._effects):
            if handle in blocked:
                if handle.state != "closed":
                    handle.state = "blocked"
                continue
            report = await handle._close_shared()
            if not report.cleanup_complete:
                block_dependencies(handle)
        return EffectReport(self.snapshot())
