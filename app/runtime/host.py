"""Single-event-loop component coordination and resource lifetime protection.

Registry changes are synchronous: no user await occurs between admission checks
and pinning, or while withdrawing an affected dependency closure.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import Context, ContextVar
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import uuid4

from app.runtime.contracts import (
    BindingIdentity,
    CloseReport,
    ComponentState,
    DuplicateProvider,
    LifecycleWaitCycle,
    MissingService,
    PluginSpec,
    ReadyReport,
    RuntimeClosed,
    RuntimeFailure,
    ServiceKey,
)
from app.runtime.effects import EffectScope

_current_owners: ContextVar[tuple[str, ...]] = ContextVar("runtime_current_owners", default=())


@dataclass(eq=False)
class _Binding:
    identity: BindingIdentity
    owner: _Record
    value: Any
    check: Callable[[], bool] | None = None
    published: bool = False
    retired: bool = False
    invocations: int = 0
    consumers: set[_Record] = field(default_factory=set)

    def available(self) -> bool:
        if not self.published or self.retired or not self.owner.admission:
            return False
        try:
            return self.check is None or self.check() is True
        except Exception as error:
            self.owner.host._observe(self.owner, error, "availability")
            return False


@dataclass(eq=False)
class _Record:
    host: RuntimeHost
    spec: PluginSpec
    base: ComponentContext
    parent: _Record | None = None
    owner_id: str = field(default_factory=lambda: str(uuid4()))
    generation: int = 0
    revision: int = 0
    state: ComponentState = ComponentState.PENDING
    admission: bool = False
    retired: bool = False
    restart_requested: bool = False
    failure: RuntimeFailure | None = None
    blocked: bool = False
    context: ComponentContext | None = None
    scope: EffectScope | None = None
    bindings: list[_Binding] = field(default_factory=list)
    committed: dict[ServiceKey[Any], _Binding] = field(default_factory=dict)
    inherited: dict[tuple[str, ServiceKey[Any]], _Binding] = field(default_factory=dict)
    inferred_requirements: dict[ServiceKey[Any], str] = field(default_factory=dict)
    cleanup_failures: list[RuntimeFailure] = field(default_factory=list)
    children: set[_Record] = field(default_factory=set)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    stop_callbacks: list[Callable[[], Any]] = field(default_factory=list)
    activation: asyncio.Task[None] | None = None
    shutdown: asyncio.Task[None] | None = None
    calls: int = 0
    stopped: bool = False
    stop_tasks: list[asyncio.Future[Any]] = field(default_factory=list)


class _WaitingDependency(MissingService):
    """A legal inherited dependency has not published its generation yet."""


class _Pin:
    def __init__(self, host: RuntimeHost, owner: _Record, binding: _Binding | None = None) -> None:
        self.host, self.owner, self.binding = host, owner, binding
        self.released = False
        owner.calls += 1
        if binding is not None:
            binding.invocations += 1

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self.owner.calls -= 1
        if self.binding is not None:
            self.binding.invocations -= 1
        self.host._signal()


class CallScope:
    def __init__(self, host: RuntimeHost, keys: Sequence[ServiceKey[Any]], context: ComponentContext | None = None):
        self.host, self.keys, self.context = host, tuple(keys), context
        self.bindings: dict[ServiceKey[Any], _Binding] = {}
        self.pins: list[_Pin] = []
        self._token: Any = None
        self._entered = False

    async def __aenter__(self) -> CallScope:
        if self._entered:
            raise RuntimeError("a call scope can only be entered once")
        self._entered = True
        if self.host._stopping:
            raise RuntimeClosed("runtime is stopping")
        record = self.context._record if self.context else None
        if record is not None:
            assert self.context is not None
            self.context._assert_current()
            if not record.admission or record.state != ComponentState.ACTIVE:
                raise RuntimeClosed("component is not accepting new calls")
        for key in self.keys:
            binding = self.context._resolve(key) if self.context else self.host._lookup(key, "default")
            if binding is None or not binding.available():
                raise MissingService(str(key))
            self.bindings[key] = binding
        # All checks precede all increments; there is no await in this boundary.
        if record is not None:
            self.pins.append(_Pin(self.host, record))
        self.pins.extend(_Pin(self.host, binding.owner, binding) for binding in self.bindings.values())
        owners = tuple(dict.fromkeys((*_current_owners.get(), *(p.owner.owner_id for p in self.pins))))
        self._token = _current_owners.set(owners)
        return self

    def require(self, key: ServiceKey[Any]) -> Any:
        if key not in self.bindings or not self.pins or all(pin.released for pin in self.pins):
            raise MissingService("service is not pinned by this call: " + str(key))
        return self.bindings[key].value

    @property
    def identities(self) -> tuple[BindingIdentity, ...]:
        return tuple(binding.identity for binding in self.bindings.values())

    async def __aexit__(self, *args: Any) -> None:
        for pin in self.pins:
            pin.release()
        if self._token is not None:
            _current_owners.reset(self._token)


class ProviderHandle:
    def __init__(self, host: RuntimeHost, binding: _Binding):
        self.host, self._binding = host, binding
        self._close_task: asyncio.Task[CloseReport] | None = None

    @property
    def identity(self) -> BindingIdentity:
        return self._binding.identity

    def update_value(self, value: Any) -> None:
        binding = self._binding
        if binding.retired or binding.owner.generation != binding.identity.generation:
            raise RuntimeClosed("stale provider registration")
        binding.value = value

    def notify(self) -> None:
        if self._binding.retired:
            raise RuntimeClosed("stale provider registration")
        self.host._reconcile()

    def request_close(self) -> asyncio.Task[CloseReport]:
        if self._close_task is None:
            binding = self._binding
            self.host._withdraw_binding(binding)
            affected = self.host._invalidate_consumers(binding)
            self._close_task = asyncio.create_task(self._close(affected), context=Context())
        return self._close_task

    async def _close(self, affected: set[_Record]) -> CloseReport:
        await self.host._wait_shutdowns(affected)
        binding = self._binding
        while binding.consumers or binding.invocations:
            await self.host._wait_change()
        return self.host._report(affected)

    async def close(self, timeout: float | None = 10) -> CloseReport:
        self.host._check_wait({self._binding.owner})
        task = self.request_close()
        try:
            async with asyncio.timeout(timeout):
                return await asyncio.shield(task)
        except TimeoutError:
            return CloseReport(pending=(self._binding.owner.owner_id,))


class PluginHandle:
    def __init__(self, record: _Record):
        self._record = record

    @property
    def owner_id(self) -> str:
        return self._record.owner_id

    @property
    def name(self) -> str:
        return self._record.spec.name

    @property
    def state(self) -> ComponentState:
        return self._record.state

    @property
    def generation(self) -> int:
        return self._record.generation

    @property
    def change_revision(self) -> int:
        return self._record.revision

    @property
    def context(self) -> ComponentContext | None:
        return self._record.context

    def request_close(self) -> asyncio.Task[None]:
        if self._record.state == ComponentState.DISPOSED:

            async def finished() -> None:
                return None

            return asyncio.create_task(finished(), context=Context())
        self._record.retired = True
        self._record.host._invalidate({self._record})
        assert self._record.shutdown is not None
        return self._record.shutdown

    async def close(self, timeout: float | None = 10) -> CloseReport:
        host = self._record.host
        host._check_wait({self._record})
        self.request_close()
        return await host._await_close({self._record}, timeout)

    async def restart(self, timeout: float | None = 10) -> ReadyReport:
        record, host = self._record, self._record.host
        host._check_wait({record})
        if record.retired or record.state == ComponentState.DISPOSED:
            raise RuntimeClosed("disposed components cannot restart")
        if record.blocked or (record.failure is not None and record.failure.phase == "cleanup"):
            return ReadyReport("failed", failures=(record.failure,) if record.failure else ())
        record.restart_requested = True
        record.failure = None
        host._invalidate({record})
        return await host.await_ready((self,), timeout)

    def update_config(self, config: Mapping[str, Any]) -> None:
        record = self._record
        if record.retired or record.blocked:
            raise RuntimeClosed("component cannot accept configuration changes")
        record.spec = replace(record.spec, config=dict(config))
        record.restart_requested = True
        record.failure = None
        record.host._invalidate({record})

    async def await_ready(self, timeout: float | None = 10) -> ReadyReport:
        return await self._record.host.await_ready((self,), timeout)


class ComponentContext:
    def __init__(
        self,
        host: RuntimeHost,
        record: _Record | None = None,
        generation: int = 0,
        realms: Mapping[ServiceKey[Any], str] | None = None,
        configs: Mapping[ServiceKey[Any], tuple[Mapping[str, Any], ...]] | None = None,
    ) -> None:
        self.host, self._record, self.generation = host, record, generation
        self._realms = dict(realms or {})
        self._configs = dict(configs or {})
        self._config = dict(record.spec.config) if record else {}

    @property
    def owner_id(self) -> str:
        return self._record.owner_id if self._record else "root"

    @property
    def scope(self) -> EffectScope:
        self._assert_current()
        if self._record is None or self._record.scope is None:
            raise RuntimeClosed("root context has no component effect scope")
        return self._record.scope

    @property
    def events(self) -> Any:
        return self.host.events

    @property
    def config(self) -> Mapping[str, Any]:
        return dict(self._config)

    def _realm(self, key: ServiceKey[Any]) -> str:
        return self._realms.get(key, "default")

    def _assert_current(self, *, registration: bool = False) -> None:
        record = self._record
        if record is None:
            if self.host._stopping:
                raise RuntimeClosed("runtime is stopping")
            return
        if record.generation != self.generation or record.state in (ComponentState.DISPOSED, ComponentState.PENDING):
            raise RuntimeClosed("stale component context")
        if registration and record.state not in (ComponentState.LOADING, ComponentState.ACTIVE):
            raise RuntimeClosed("component is not accepting resource registrations")

    def _resolve(self, key: ServiceKey[Any]) -> _Binding | None:
        self._assert_current()
        record = self._record
        if record is None:
            return self.host._lookup(key, self._realm(key))
        for binding in reversed(record.bindings):
            if binding.identity.generation == self.generation and (
                not binding.retired or record.state == ComponentState.UNLOADING
            ):
                if binding.identity.key == key and binding.identity.realm == self._realm(key):
                    return binding
        if key in record.spec.requires:
            committed = record.committed.get(key)
            if committed is None or committed.identity.realm != self._realm(key):
                raise MissingService("required service is unavailable: " + str(key))
            return committed
        inherited = record.inherited.get((self._realm(key), key))
        if inherited is not None:
            return inherited
        if record.parent is not None and record.base._realm(key) == self._realm(key):
            try:
                inherited = record.base._resolve(key)
            except MissingService:
                ancestor: ComponentContext | None = record.base
                while ancestor is not None and ancestor._record is not None:
                    parent = ancestor._record
                    if key in (*parent.spec.requires, *parent.spec.provides, *parent.inferred_requirements):
                        record.inferred_requirements[key] = self._realm(key)
                        raise _WaitingDependency(str(key)) from None
                    ancestor = parent.base if parent.base._realm(key) == self._realm(key) else None
                raise
            if inherited is not None:
                record.inferred_requirements[key] = self._realm(key)
                if not inherited.available():
                    raise _WaitingDependency(str(key))
                record.inherited[(self._realm(key), key)] = inherited
                record.committed[key] = inherited
                inherited.consumers.add(record)
            return inherited
        raise MissingService("undeclared service: " + str(key))

    def require(self, key: ServiceKey[Any]) -> Any:
        binding = self._resolve(key)
        if binding is None:
            raise MissingService(str(key))
        return binding.value

    def get(self, key: ServiceKey[Any]) -> Any:
        binding = self.host._lookup(key, self._realm(key))
        return binding.value if binding and binding.published and binding.owner.state == ComponentState.ACTIVE else None

    def provide(self, key: ServiceKey[Any], value: Any, check: Callable[[], bool] | None = None) -> ProviderHandle:
        self._assert_current(registration=True)
        record = self._record
        if record is None or key not in record.spec.provides:
            raise MissingService("provided service must be declared: " + str(key))
        slot = (self._realm(key), key)
        existing = self.host._slots.get(slot)
        if existing is not None and (not isinstance(existing, _Record) or existing is not record):
            raise DuplicateProvider(str(key))
        if isinstance(existing, _Binding):
            raise DuplicateProvider(str(key))
        binding = _Binding(
            BindingIdentity(key, slot[0], record.owner_id, self.generation, str(uuid4())), record, value, check
        )
        record.bindings.append(binding)
        self.host._slots[slot] = binding
        if record.state == ComponentState.ACTIVE and record.admission:
            binding.published = True
            self.host._reconcile()
        return ProviderHandle(self.host, binding)

    def effect(self, setup: Callable[[], Any], label: str = "effect", **kwargs: Any) -> Any:
        self._assert_current(registration=True)
        return self.scope.effect(setup, label, **kwargs)

    def on_stop(self, callback: Callable[[], Any]) -> None:
        self._assert_current(registration=True)
        assert self._record is not None
        self._record.stop_callbacks.append(callback)

    def spawn(self, awaitable: Awaitable[Any], *, name: str | None = None) -> asyncio.Task[Any]:
        try:
            self._assert_current(registration=True)
        except BaseException:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise
        record = self._record
        assert record is not None

        started = False

        async def owned_task() -> Any:
            nonlocal started
            started = True
            token = _current_owners.set((record.owner_id,))
            try:
                return await awaitable
            finally:
                _current_owners.reset(token)

        task = asyncio.create_task(owned_task(), name=name, context=Context())
        record.tasks.add(task)

        def completed(done: asyncio.Task[Any]) -> None:
            if not started and inspect.iscoroutine(awaitable):
                awaitable.close()
            record.tasks.discard(done)
            if not done.cancelled() and (error := done.exception()) is not None:
                self.host._fail(record, error, "task")
            self.host._signal()

        task.add_done_callback(completed, context=Context())
        return task

    def plugin(self, spec: PluginSpec) -> PluginHandle:
        self._assert_current(registration=True)
        return self.host.load(spec, context=self)

    def isolate(self, key: ServiceKey[Any], label: str | None = None) -> ComponentContext:
        realms = dict(self._realms)
        realms[key] = label if label is not None else str(uuid4())
        return ComponentContext(self.host, self._record, self.generation, realms, self._configs)

    def intercept(self, key: ServiceKey[Any], config: Mapping[str, Any]) -> ComponentContext:
        configs = dict(self._configs)
        configs[key] = (*configs.get(key, ()), dict(config))
        return ComponentContext(self.host, self._record, self.generation, self._realms, configs)

    def config_for(
        self,
        key: ServiceKey[Any],
        *,
        base: Mapping[str, Any] | None = None,
        head: Mapping[str, Any] | None = None,
        merge: Callable[..., Any] | None = None,
    ) -> Any:
        layers = (base or {}, *self._configs.get(key, ()), head or {})
        if merge is not None:
            return merge(*layers)
        result: dict[str, Any] = {}
        for layer in layers:
            result.update(layer)
        return result

    def call(self, *keys: ServiceKey[Any]) -> CallScope:
        return CallScope(self.host, keys, self)

    def can_dispatch(self) -> bool:
        record = self._record
        return bool(
            record
            and record.generation == self.generation
            and record.admission
            and record.state == ComponentState.ACTIVE
            and not self.host._stopping
        )

    def acquire_event_pin(self, registration_id: str) -> _Pin:
        if not self.can_dispatch():
            raise RuntimeClosed("listener owner is not accepting invocations")
        assert self._record is not None
        return _Pin(self.host, self._record)

    @contextmanager
    def dispatch_context(self, selected_owners: tuple[str, ...]) -> Any:
        token = _current_owners.set(tuple(dict.fromkeys((*_current_owners.get(), *selected_owners))))
        try:
            yield
        finally:
            _current_owners.reset(token)

    def on(self, key: Any, callback: Callable[..., Any], **kwargs: Any) -> Any:
        self._assert_current(registration=True)
        return self.host.events.on(self, key, callback, **kwargs)


class RuntimeHost:
    def __init__(self) -> None:
        from app.runtime.events import EventBus

        self.events = EventBus()
        self.context = ComponentContext(self)
        self._records: list[_Record] = []
        self._slots: dict[tuple[str, ServiceKey[Any]], _Record | _Binding] = {}
        self._changed = asyncio.Event()
        self._stopping = False
        self._reconciling = False
        self.observations: list[RuntimeFailure] = []

    def load(self, spec: PluginSpec, *, context: ComponentContext | None = None) -> PluginHandle:
        if self._stopping:
            raise RuntimeClosed("runtime is stopping")
        base = context or self.context
        base._assert_current(registration=base._record is not None)
        record = _Record(self, spec, base, base._record)
        self._records.append(record)
        if record.parent is not None:
            record.parent.children.add(record)
        self._reconcile()
        return PluginHandle(record)

    def _signal(self) -> None:
        self._changed.set()

    async def _wait_change(self) -> None:
        self._changed.clear()
        await self._changed.wait()

    def _lookup(self, key: ServiceKey[Any], realm: str) -> _Binding | None:
        binding = self._slots.get((realm, key))
        return binding if isinstance(binding, _Binding) and binding.published and not binding.retired else None

    def _targets(self, record: _Record) -> dict[ServiceKey[Any], _Binding] | None:
        targets = {}
        requirements = {key: record.base._realm(key) for key in record.spec.requires}
        requirements.update(record.inferred_requirements)
        for key, realm in requirements.items():
            binding = self._lookup(key, realm)
            if binding is None or not binding.available():
                return None
            targets[key] = binding
        return targets

    def _observe(self, record: _Record, error: BaseException, phase: str) -> RuntimeFailure:
        failure = RuntimeFailure(record.owner_id, record.spec.name, phase, type(error).__name__, str(error))
        self.observations.append(failure)
        return failure

    def _fail(self, record: _Record, error: BaseException, phase: str) -> None:
        if record.failure is None:
            record.failure = self._observe(record, error, phase)
        self._invalidate({record})

    def _reconcile(self) -> None:
        self._signal()
        if self._reconciling or self._stopping:
            return
        self._reconciling = True
        try:
            for record in tuple(self._records):
                if record.retired or record.failure or record.shutdown is not None:
                    continue
                targets = self._targets(record)
                if record.state in (ComponentState.ACTIVE, ComponentState.LOADING):
                    if targets is None or targets != record.committed:
                        record.restart_requested = True
                        self._invalidate({record})
                elif record.state == ComponentState.PENDING and targets is not None:
                    self._start(record, targets)
        finally:
            self._reconciling = False

    def _start(self, record: _Record, targets: dict[ServiceKey[Any], _Binding]) -> None:
        try:
            for key in record.spec.provides:
                slot = (record.base._realm(key), key)
                if slot in self._slots:
                    raise DuplicateProvider(str(key))
            for key in record.spec.provides:
                self._slots[(record.base._realm(key), key)] = record
        except DuplicateProvider as error:
            record.failure = self._observe(record, error, "setup")
            record.state = ComponentState.FAILED
            return
        record.generation += 1
        record.revision += 1
        record.state = ComponentState.LOADING
        record.admission = False
        record.stopped = False
        record.committed = targets
        for binding in targets.values():
            binding.consumers.add(record)
        generation = record.generation
        record.scope = EffectScope(
            record.owner_id,
            generation,
            is_open=lambda: (
                record.generation == generation and record.state in (ComponentState.LOADING, ComponentState.ACTIVE)
            ),
        )
        configs = dict(record.base._configs)
        for key, config in record.spec.inject.items():
            configs[key] = (*configs.get(key, ()), dict(config))
        record.context = ComponentContext(self, record, generation, record.base._realms, configs)
        record.activation = asyncio.create_task(self._activate(record, record.revision), context=Context())

    async def _activate(self, record: _Record, revision: int) -> None:
        token = _current_owners.set((record.owner_id,))
        try:
            assert record.context is not None and record.scope is not None
            handle = record.scope.effect(lambda: record.spec.setup(record.context), "plugin.setup")
            await handle.ready()
            await record.scope.ready()
            if record.revision == revision and not record.retired and record.shutdown is None:
                record.state = ComponentState.ACTIVE
                record.admission = True
                for binding in record.bindings:
                    if binding.identity.generation == record.generation and not binding.retired:
                        binding.published = True
        except BaseException as error:
            if isinstance(error, _WaitingDependency):
                record.restart_requested = True
            elif not isinstance(error, asyncio.CancelledError):
                failure = self._observe(record, error, "setup")
                if record.failure is None:
                    record.failure = failure
            self._invalidate({record})
        finally:
            _current_owners.reset(token)
            record.activation = None
            self._reconcile()

    def _dependents(self, record: _Record) -> set[_Record]:
        return {
            other
            for other in self._records
            if other is not record
            and any(binding.owner is record for binding in (*other.committed.values(), *other.inherited.values()))
        }

    def _closure(self, records: set[_Record]) -> set[_Record]:
        result = set(records)
        changed = True
        while changed:
            before = len(result)
            for record in tuple(result):
                result.update(child for child in record.children if child.state != ComponentState.DISPOSED)
                result.update(self._dependents(record))
            changed = len(result) != before
        return result

    def _withdraw_binding(self, binding: _Binding) -> None:
        binding.published = False
        binding.retired = True
        slot = (binding.identity.realm, binding.identity.key)
        if self._slots.get(slot) is binding:
            del self._slots[slot]
        self._signal()

    def _invalidate_consumers(self, binding: _Binding) -> set[_Record]:
        affected = set(binding.consumers)
        for record in affected:
            record.restart_requested = True
        return self._invalidate(affected)

    def _invalidate(self, records: set[_Record]) -> set[_Record]:
        affected = self._closure(records)
        # Atomic admission withdrawal for the entire dependent/structural closure.
        for record in affected:
            if record.state == ComponentState.DISPOSED:
                continue
            record.admission = False
            record.revision += 1
            for binding in record.bindings:
                self._withdraw_binding(binding)
            for slot, owner in tuple(self._slots.items()):
                if owner is record:
                    del self._slots[slot]
            if record not in records and record.parent in affected:
                record.retired = True
            elif record not in records and not record.retired:
                record.restart_requested = True
            record.state = ComponentState.UNLOADING
        # Issue every stop/cancel before any cleanup operation can start.
        for record in affected:
            self._begin_stop(record)
        for record in affected:
            if record.state != ComponentState.DISPOSED and record.shutdown is None:
                record.shutdown = asyncio.create_task(self._shutdown(record), context=Context())
        self._signal()
        return affected

    def _begin_stop(self, record: _Record) -> None:
        if record.stopped or record.state == ComponentState.DISPOSED:
            return
        record.stopped = True
        token = _current_owners.set((record.owner_id,))
        try:
            for callback in record.stop_callbacks:
                try:
                    result = callback()
                    if inspect.isawaitable(result):
                        record.stop_tasks.append(asyncio.ensure_future(result))
                except BaseException as error:
                    record.failure = self._observe(record, error, "drain")
                    record.blocked = True
            for task in tuple(record.tasks):
                task.cancel()
        finally:
            _current_owners.reset(token)

    async def _shutdown(self, record: _Record) -> None:
        token = _current_owners.set((record.owner_id,))
        try:
            results = await asyncio.gather(*record.stop_tasks, *tuple(record.tasks), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    if record.failure is None:
                        record.failure = self._observe(record, result, "drain")
                    record.blocked = True
            if record.activation is not None:
                await asyncio.shield(record.activation)
            dependents = self._dependents(record) | record.children
            await self._wait_shutdowns(dependents)
            if any(
                other.blocked or (other.failure and other.state != ComponentState.DISPOSED)
                for other in dependents
                if other.committed or other.parent is record
            ):
                record.blocked = True
            while record.calls or any(b.invocations or b.consumers for b in record.bindings):
                if record.blocked:
                    break
                await self._wait_change()
            if record.blocked:
                record.state = ComponentState.FAILED
                return
            if record.scope is not None:
                report = await record.scope.close()
                if not report.cleanup_complete:
                    record.blocked = True
                    errors = report.cleanup_errors
                    for cleanup_error in errors or (RuntimeError("effect cleanup incomplete"),):
                        record.cleanup_failures.append(self._observe(record, cleanup_error, "cleanup"))
                    record.failure = record.cleanup_failures[0]
                    record.state = ComponentState.FAILED
                    return
            for binding in (*record.committed.values(), *record.inherited.values()):
                binding.consumers.discard(record)
            record.committed = {}
            record.inherited = {}
            record.stop_callbacks.clear()
            record.stop_tasks.clear()
            record.state = (
                ComponentState.DISPOSED
                if record.retired
                else ComponentState.FAILED
                if record.failure
                else ComponentState.PENDING
            )
        except BaseException as error:
            record.blocked = True
            record.failure = self._observe(record, error, "cleanup")
            record.state = ComponentState.FAILED
        finally:
            _current_owners.reset(token)
            record.shutdown = None
            self._signal()
            if not record.retired and record.restart_requested and not record.failure and not record.blocked:
                record.restart_requested = False
                self._reconcile()

    async def _wait_shutdowns(self, records: set[_Record]) -> None:
        current = asyncio.current_task()
        tasks = [
            record.shutdown for record in records if record.shutdown is not None and record.shutdown is not current
        ]
        if tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in tasks))

    def _check_wait(self, records: set[_Record]) -> None:
        current = set(_current_owners.get())
        if current & {record.owner_id for record in self._closure(records)}:
            raise LifecycleWaitCycle("close would wait for the current lifecycle or leased invocation")

    def call(self, *keys: ServiceKey[Any]) -> CallScope:
        return CallScope(self, keys)

    def _ready(self, required: Sequence[PluginHandle | ServiceKey[Any]] | None) -> ReadyReport:
        if self._stopping:
            return ReadyReport("stopping")
        missing, failures, identities = [], [], []
        items = required if required is not None else [PluginHandle(r) for r in self._records if r.parent is None]
        for item in items:
            if isinstance(item, ServiceKey):
                binding = self._lookup(item, "default")
                if binding is None or not binding.available():
                    missing.append(str(item))
                    for record in self._providers_for(item, "default"):
                        if record.failure:
                            failures.append(record.failure)
                else:
                    identities.append(binding.identity)
            else:
                record = item._record
                failures.extend(self._dependency_failures(record, set()))
                if (
                    record.state != ComponentState.ACTIVE
                    or not record.admission
                    or self._targets(record) != record.committed
                ):
                    missing.append(record.spec.name)
                else:
                    identities.extend(b.identity for b in record.committed.values())
        if failures:
            return ReadyReport("failed", tuple(missing), tuple(failures), tuple(identities))
        cycles = self._cycles()
        if missing and cycles:
            failure = RuntimeFailure(
                "dependency-graph", "runtime", "dependency", "DependencyCycle", " -> ".join(cycles)
            )
            return ReadyReport("failed", tuple(missing), (failure,), tuple(identities))
        return ReadyReport("pending" if missing else "ready", tuple(missing), (), tuple(identities))

    def _dependency_failures(self, record: _Record, seen: set[_Record]) -> list[RuntimeFailure]:
        if record in seen:
            return []
        seen.add(record)
        failures = [record.failure] if record.failure else []
        for key in (*record.spec.requires, *record.inferred_requirements):
            realm = record.inferred_requirements.get(key, record.base._realm(key))
            for provider in self._providers_for(key, realm):
                failures.extend(self._dependency_failures(provider, seen))
        return failures

    def _providers_for(self, key: ServiceKey[Any], realm: str) -> tuple[_Record, ...]:
        slot = self._slots.get((realm, key))
        if isinstance(slot, _Binding):
            return (slot.owner,)
        if isinstance(slot, _Record):
            return (slot,)
        return tuple(
            r for r in self._records if not r.retired and key in r.spec.provides and r.base._realm(key) == realm
        )

    def is_ready(self, required: Sequence[PluginHandle | ServiceKey[Any]] | None = None) -> bool:
        return self._ready(required).ready

    async def await_ready(
        self, required: Sequence[PluginHandle | ServiceKey[Any]] | None = None, timeout: float | None = 10
    ) -> ReadyReport:
        try:
            async with asyncio.timeout(timeout):
                while True:
                    self._changed.clear()
                    report = self._ready(required)
                    if report.outcome != "pending":
                        return report
                    self._check_ready_wait(required)
                    await self._changed.wait()
        except TimeoutError:
            report = self._ready(required)
            return ReadyReport("timeout", report.missing, report.failures, report.generations)

    def _check_ready_wait(self, required: Sequence[PluginHandle | ServiceKey[Any]] | None) -> None:
        if _current_owners.get():
            targets = {item._record for item in (required or ()) if isinstance(item, PluginHandle)}
            for item in required or ():
                if isinstance(item, ServiceKey):
                    targets.update(r for r in self._records if item in r.spec.provides)
            stack = list(targets)
            seen = set(targets)
            while stack:
                record = stack.pop()
                if record.owner_id in _current_owners.get():
                    raise LifecycleWaitCycle("readiness would wait for the current startup")
                for key in (*record.spec.requires, *record.inferred_requirements):
                    for provider in self._records:
                        if (
                            key in provider.spec.provides
                            and provider not in seen
                            and provider.base._realm(key)
                            == record.inferred_requirements.get(key, record.base._realm(key))
                        ):
                            seen.add(provider)
                            stack.append(provider)

    def _cycles(self) -> tuple[str, ...]:
        graph = {
            r: {
                p
                for key in (*r.spec.requires, *r.inferred_requirements)
                for p in self._records
                if key in p.spec.provides
                and p.base._realm(key) == r.inferred_requirements.get(key, r.base._realm(key))
                and not p.retired
            }
            for r in self._records
            if not r.retired
        }
        visiting: list[_Record] = []
        visited: set[_Record] = set()

        def visit(record: _Record) -> tuple[str, ...]:
            if record in visiting:
                return tuple(r.spec.name for r in visiting[visiting.index(record) :]) + (record.spec.name,)
            if record in visited:
                return ()
            visiting.append(record)
            for child in graph.get(record, set()):
                cycle = visit(child)
                if cycle:
                    return cycle
            visiting.pop()
            visited.add(record)
            return ()

        for record in graph:
            cycle = visit(record)
            if cycle:
                return cycle
        return ()

    def _report(self, records: set[_Record]) -> CloseReport:
        return CloseReport(
            completed=tuple(r.owner_id for r in records if r.state == ComponentState.DISPOSED),
            failed=tuple(
                failure
                for r in records
                for failure in (r.cleanup_failures if r.cleanup_failures else ([r.failure] if r.failure else []))
            ),
            blocked=tuple(r.owner_id for r in records if r.blocked),
            pending=tuple(r.owner_id for r in records if r.shutdown is not None or r.state == ComponentState.UNLOADING),
        )

    async def _await_close(self, records: set[_Record], timeout: float | None) -> CloseReport:
        affected = self._closure(records)
        try:
            async with asyncio.timeout(timeout):
                await self._wait_shutdowns(affected)
        except TimeoutError:
            pass
        return self._report(affected)

    def request_close(self) -> tuple[asyncio.Task[None], ...]:
        self._stopping = True
        for record in self._records:
            record.retired = True
        self._invalidate(set(self._records))
        return tuple(r.shutdown for r in self._records if r.shutdown is not None)

    async def close(self, timeout: float | None = 10) -> CloseReport:
        records = set(self._records)
        self._check_wait(records)
        self.request_close()
        return await self._await_close(records, timeout)

    async def retire(self, handle: PluginHandle, timeout: float | None = 10) -> CloseReport:
        return await handle.close(timeout)

    async def restart(self, handle: PluginHandle, timeout: float | None = 10) -> ReadyReport:
        return await handle.restart(timeout)

    async def replace(self, handle: PluginHandle, spec: PluginSpec, timeout: float | None = 10) -> PluginHandle:
        report = await handle.close(timeout)
        if not report.successful:
            raise RuntimeClosed("old component did not finish cleanup")
        return self.load(spec, context=handle._record.base)

    def snapshot(self) -> dict[str, Any]:
        return {
            "stopping": self._stopping,
            "components": [
                {
                    "owner_id": r.owner_id,
                    "name": r.spec.name,
                    "state": r.state.value,
                    "generation": r.generation,
                    "change_revision": r.revision,
                    "admission": r.admission,
                    "calls": r.calls,
                    "tasks": len(r.tasks),
                    "blocked": r.blocked,
                    "failure": r.failure,
                    "missing": tuple(
                        str(k)
                        for k in r.spec.requires
                        if (b := self._lookup(k, r.base._realm(k))) is None or not b.available()
                    ),
                    "bindings": tuple(b.identity for b in r.bindings if not b.retired),
                }
                for r in self._records
            ],
            "cycles": self._cycles(),
        }
