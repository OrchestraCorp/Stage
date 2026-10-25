import asyncio
from typing import Any

import pytest

from app.runtime import (
    ComponentState,
    LifecycleWaitCycle,
    MissingService,
    PluginSpec,
    RuntimeClosed,
    RuntimeHost,
    ServiceKey,
)

DB = ServiceKey[Any]("test", "db")
LOG = ServiceKey[Any]("test", "log")


async def test_c2_01_c2_02_pending_reactive_service_recovery() -> None:
    host = RuntimeHost()
    seen = []
    consumer = host.load(PluginSpec("consumer", lambda ctx: seen.append(ctx.require(DB)), requires=(DB,)))
    assert (await host.await_ready((consumer,), timeout=0.001)).outcome == "timeout"
    provider = host.load(PluginSpec("db", lambda ctx: ctx.provide(DB, "first") and None, provides=(DB,)))
    assert (await host.await_ready((consumer,), timeout=1)).ready
    assert seen == ["first"]
    assert (await provider.close()).successful
    assert consumer.state == ComponentState.PENDING
    host.load(PluginSpec("db2", lambda ctx: ctx.provide(DB, "second") and None, provides=(DB,)))
    assert (await host.await_ready((consumer,), timeout=1)).ready
    assert seen == ["first", "second"]
    assert (await host.close()).successful


async def test_c2_03_availability_get_and_refresh() -> None:
    host = RuntimeHost()
    healthy = True
    handles = []

    def setup(ctx: Any) -> None:
        handles.append(ctx.provide(DB, "db", check=lambda: healthy))

    host.load(PluginSpec("db", setup, provides=(DB,)))
    consumer = host.load(PluginSpec("consumer", lambda ctx: None, requires=(DB,)))
    assert (await host.await_ready((consumer,), timeout=1)).ready
    healthy = False
    handles[0].notify()
    assert not host.is_ready((consumer,))
    assert host.context.get(DB) == "db"  # Observation does not evaluate check.
    assert (await host.await_ready((consumer,), timeout=0.01)).outcome == "timeout"
    healthy = True
    handles[0].notify()
    assert (await host.await_ready((consumer,), timeout=1)).ready
    await host.close()


async def test_c2_04_duplicate_and_cycle_readiness() -> None:
    host = RuntimeHost()
    host.load(PluginSpec("db", lambda ctx: ctx.provide(DB, "one") and None, provides=(DB,)))
    duplicate = host.load(PluginSpec("duplicate", lambda ctx: None, provides=(DB,)))
    assert (await host.await_ready((duplicate,), timeout=1)).outcome == "failed"
    await host.close()
    host = RuntimeHost()
    a = host.load(PluginSpec("a", lambda ctx: None, requires=(DB,), provides=(LOG,)))
    host.load(PluginSpec("b", lambda ctx: None, requires=(LOG,), provides=(DB,)))
    report = await host.await_ready((a,), timeout=1)
    assert report.outcome == "failed" and report.failures[0].error_type == "DependencyCycle"
    await host.close()


async def test_c3_01_c4_01_c5_01_old_committed_dependency_cleanup() -> None:
    host = RuntimeHost()
    cleaning, release = asyncio.Event(), asyncio.Event()
    trace = []

    def provider(ctx: Any) -> Any:
        ctx.provide(DB, "old")
        return lambda: trace.append("db.close")

    def consumer(ctx: Any) -> Any:
        async def cleanup() -> None:
            cleaning.set()
            await release.wait()
            trace.append(ctx.require(DB))

        return cleanup

    db = host.load(PluginSpec("db", provider, provides=(DB,)))
    app = host.load(PluginSpec("app", consumer, requires=(DB,)))
    assert (await host.await_ready((app,), timeout=1)).ready
    closing = asyncio.create_task(db.close())
    await cleaning.wait()
    assert "db.close" not in trace
    assert host.context.get(DB) is None
    release.set()
    assert (await closing).successful
    assert trace == ["old", "db.close"]
    await host.close()


async def test_c3_03_same_instance_generation_and_c3_04_unrelated() -> None:
    host = RuntimeHost()
    db = host.load(PluginSpec("db", lambda ctx: ctx.provide(DB, []) and None, provides=(DB,)))
    app = host.load(PluginSpec("app", lambda ctx: None, requires=(DB,)))
    assert (await host.await_ready((app,), timeout=1)).ready
    generation = app.generation
    host.load(PluginSpec("irrelevant", lambda ctx: ctx.provide(LOG, 1) and None, provides=(LOG,)))
    await host.await_ready(timeout=1)
    assert app.generation == generation
    assert (await db.restart(timeout=1)).ready
    assert (await host.await_ready((app,), timeout=1)).ready
    assert db.generation == 2 and app.generation == generation + 1
    await host.close()


async def test_c4_03_s1_03_undeclared_inherited_and_isolated_reads() -> None:
    host = RuntimeHost()
    children, errors = [], []

    async def parent(ctx: Any) -> None:
        ctx.provide(DB, "parent")

        def child(child_ctx: Any) -> None:
            assert child_ctx.require(DB) == "parent"
            assert child_ctx.isolate(DB, "private").get(DB) is None
            with pytest.raises(MissingService):
                child_ctx.isolate(DB, "private").require(DB)
            with pytest.raises(MissingService):
                child_ctx.require(LOG)
            errors.append("checked")

        children.append(ctx.plugin(PluginSpec("child", child)))

    host.load(PluginSpec("parent", parent, provides=(DB,)))
    assert (await host.await_ready(timeout=1)).ready
    assert (await host.await_ready(children, timeout=1)).ready
    assert errors == ["checked"]
    await host.close()


async def test_c5_02_c5_03_call_lease_delays_provider_resources() -> None:
    host = RuntimeHost()
    trace = []

    def setup(ctx: Any) -> Any:
        ctx.provide(DB, object())
        ctx.on_stop(lambda: trace.append("stop"))
        return lambda: trace.append("dispose")

    handle = host.load(PluginSpec("db", setup, provides=(DB,)))
    await host.await_ready((DB,), timeout=1)
    lease = host.call(DB)
    await lease.__aenter__()
    # Closing from the lease owner would wait for itself.
    with pytest.raises(LifecycleWaitCycle):
        await handle.close()
    # An independent caller can request close; its wait times out without cancelling ownership.
    task = handle.request_close()
    for _ in range(3):
        await asyncio.sleep(0)
    assert trace == ["stop"] and not task.done()
    with pytest.raises((RuntimeClosed, MissingService)):
        async with host.call(DB):
            pass
    await lease.__aexit__()
    await task
    assert trace == ["stop", "dispose"]
    await host.close()


async def test_c5_04_s1_06_uncooperative_task_retains_dependencies() -> None:
    host = RuntimeHost()
    started, release = asyncio.Event(), asyncio.Event()
    trace = []

    def db_setup(ctx: Any) -> Any:
        ctx.provide(DB, "db")
        return lambda: trace.append("db.close")

    async def background() -> None:
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    def app_setup(ctx: Any) -> None:
        ctx.spawn(background())

    db = host.load(PluginSpec("db", db_setup, provides=(DB,)))
    app = host.load(PluginSpec("app", app_setup, requires=(DB,)))
    await host.await_ready((app,), timeout=1)
    await started.wait()
    closing = asyncio.create_task(db.close(timeout=0.01))
    report = await closing
    assert report.pending and "db.close" not in trace
    assert not host.is_ready((app,))
    release.set()
    await host.close(timeout=1)
    assert trace == ["db.close"]


async def test_c6_01_c6_02_c6_03_scoped_realms_and_intercepts() -> None:
    host = RuntimeHost()
    host.load(PluginSpec("global", lambda ctx: ctx.provide(LOG, "logger") and None, provides=(LOG,)))
    shared = host.context.isolate(DB, "shared")
    host.load(PluginSpec("db", lambda ctx: ctx.provide(DB, "shared-db") and None, provides=(DB,)), context=shared)
    seen = []
    configured = shared.intercept(DB, {"a": 1, "nested": {"first": 1}}).intercept(DB, {"a": 2})

    def setup(ctx: Any) -> None:
        seen.append((ctx.require(DB), ctx.require(LOG)))
        assert ctx.config_for(DB, base={"base": True, "a": 0}, head={"a": 3}) == {
            "base": True,
            "a": 3,
            "nested": {"first": 1},
        }

    app = host.load(PluginSpec("app", setup, requires=(DB, LOG)), context=configured)
    assert (await host.await_ready((app,), timeout=1)).ready
    assert seen == [("shared-db", "logger")]
    assert host.context.get(DB) is None
    await host.close()


async def test_c7_01_c7_02_c7_03_children_resources_and_stale_owner() -> None:
    host = RuntimeHost()
    trace, contexts = [], []

    def child(ctx: Any) -> Any:
        contexts.append(ctx)
        return lambda: trace.append("child.close")

    def parent(ctx: Any) -> Any:
        ctx.plugin(PluginSpec("child", child))
        return lambda: trace.append("parent.close")

    owner = host.load(PluginSpec("parent", parent))
    await host.await_ready(timeout=1)
    while not contexts:
        await asyncio.sleep(0)
    assert (await owner.close()).successful
    assert trace == ["child.close", "parent.close"]
    with pytest.raises(RuntimeClosed):
        contexts[0].effect(lambda: None)
    await host.close()


async def test_s1_01_parent_startup_wait_cycle_and_cancel_ready_waiter() -> None:
    host = RuntimeHost()

    async def parent(ctx: Any) -> None:
        ctx.provide(DB, "db")
        child = ctx.plugin(PluginSpec("child", lambda c: None, requires=(DB,)))
        await child.await_ready()

    handle = host.load(PluginSpec("parent", parent, provides=(DB,)))
    report = await host.await_ready((handle,), timeout=1)
    assert report.outcome == "failed" and report.failures[0].error_type == "LifecycleWaitCycle"
    await host.close()
    host = RuntimeHost()
    release = asyncio.Event()

    async def slow(ctx: Any) -> None:
        await release.wait()

    handle = host.load(PluginSpec("slow", slow))
    waiter = asyncio.create_task(handle.await_ready())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    assert (await handle.await_ready(timeout=1)).ready
    await host.close()


async def test_s1_04_get_does_not_check_and_value_update_keeps_old_reference() -> None:
    host = RuntimeHost()
    checks: list[str] = []
    providers: list[Any] = []

    def setup(ctx: Any) -> None:
        def check() -> bool:
            checks.append("check")
            return True

        providers.append(ctx.provide(DB, ["old"], check=check))

    owner = host.load(PluginSpec("db", setup, provides=(DB,)))
    await owner.await_ready(timeout=1)
    checks.clear()
    old = host.context.get(DB)
    assert checks == []
    providers[0].update_value(["new"])
    assert host.context.get(DB) == ["new"] and old == ["old"]
    await host.close()
    with pytest.raises(RuntimeClosed):
        providers[0].update_value("stale")


async def test_s1_05_all_stops_before_provider_destruction() -> None:
    host = RuntimeHost()
    trace = []

    def provider(ctx: Any) -> Any:
        ctx.on_stop(lambda: trace.append("db.stop"))
        ctx.provide(DB, "db")
        return lambda: trace.append("db.close")

    def consumer(ctx: Any) -> Any:
        ctx.on_stop(lambda: trace.append("app.stop"))
        return lambda: trace.append("app.close")

    host.load(PluginSpec("db", provider, provides=(DB,)))
    host.load(PluginSpec("app", consumer, requires=(DB,)))
    await host.await_ready(timeout=1)
    assert (await host.close()).successful
    assert set(trace[:2]) == {"db.stop", "app.stop"}
    assert trace[-1] == "db.close"


async def test_s1_07_cleanup_self_wait_and_shared_close_waiter_cancel() -> None:
    host = RuntimeHost()
    cleaning, release = asyncio.Event(), asyncio.Event()
    failures: list[str] = []
    handle = None

    def setup(ctx: Any) -> Any:
        async def cleanup() -> None:
            assert handle is not None
            with pytest.raises(LifecycleWaitCycle):
                await handle.close()
            failures.append("cycle")
            cleaning.set()
            await release.wait()

        return cleanup

    handle = host.load(PluginSpec("app", setup))
    await handle.await_ready(timeout=1)
    first = asyncio.create_task(handle.close())
    await cleaning.wait()
    second = asyncio.create_task(handle.close())
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert (await second).successful and failures == ["cycle"]
    await host.close()


async def test_s1_10_registration_identity_changes_without_stale_disposer_damage() -> None:
    host = RuntimeHost()
    providers, contexts = [], []

    def setup(ctx: Any) -> None:
        contexts.append(ctx)
        providers.append(ctx.provide(DB, "old"))

    owner = host.load(PluginSpec("db", setup, provides=(DB,)))
    await owner.await_ready(timeout=1)
    assert (await providers[0].close()).successful
    new = contexts[0].provide(DB, "new")
    assert new.identity.registration_id != providers[0].identity.registration_id
    providers[0].request_close()
    with pytest.raises(RuntimeClosed):
        providers[0].update_value("bad")
    assert host.context.get(DB) == "new"
    await host.close()


async def test_task_failure_is_immediately_reported_and_isolated() -> None:
    host = RuntimeHost()
    fail = asyncio.Event()

    async def monitor() -> None:
        await fail.wait()
        raise OSError("test monitor failed")

    def setup(ctx: Any) -> None:
        ctx.spawn(monitor())

    failed = host.load(PluginSpec("optional", setup))
    unaffected = host.load(PluginSpec("required", lambda ctx: None))
    await host.await_ready(timeout=1)
    fail.set()
    while not failed._record.failure:
        await asyncio.sleep(0)
    assert (await host.await_ready((failed,), timeout=1)).outcome == "failed"
    assert host.is_ready((unaffected,))
    await host.close()


async def test_c3_02_s1_10_loading_aba_and_config_revision_cannot_publish_stale_start() -> None:
    host = RuntimeHost()
    entered, release = asyncio.Event(), asyncio.Event()
    seen: list[tuple[str, str]] = []

    async def setup(ctx: Any) -> Any:
        config = ctx.config["value"]
        entered.set()
        if ctx.generation == 1:
            await release.wait()
        seen.append(("setup", config))
        return lambda: seen.append(("close", config))

    handle = host.load(PluginSpec("slow", setup, config={"value": "a"}))
    await entered.wait()
    handle.update_config({"value": "b"})
    handle.update_config({"value": "a"})
    revision = handle.change_revision
    release.set()
    assert (await handle.await_ready(timeout=1)).ready
    assert handle.generation == 2 and handle.change_revision > revision
    assert seen == [("setup", "a"), ("close", "a"), ("setup", "a")]
    assert (await handle.close()).successful
    assert (await handle.close()).successful
    await host.close()


async def test_c4_02_c4_04_s1_11_independent_provider_can_publish_while_old_cleanup_is_pinned() -> None:
    host = RuntimeHost()
    entered, release = asyncio.Event(), asyncio.Event()
    contexts: list[Any] = []
    trace: list[str] = []

    def consumer(ctx: Any) -> Any:
        contexts.append(ctx)
        value = ctx.require(DB)
        trace.append(value)

        async def cleanup() -> None:
            if value == "old":
                entered.set()
                await release.wait()
                assert ctx.require(DB) == "old"

        return cleanup

    old = host.load(PluginSpec("old", lambda ctx: ctx.provide(DB, "old") and None, provides=(DB,)))
    app = host.load(PluginSpec("app", consumer, requires=(DB,)))
    await app.await_ready(timeout=1)
    closing = asyncio.create_task(old.close())
    await entered.wait()
    new = host.load(PluginSpec("new", lambda ctx: ctx.provide(DB, "new") and None, provides=(DB,)))
    assert (await new.await_ready(timeout=1)).ready
    assert host.context.get(DB) == "new"
    assert contexts[0].require(DB) == "old"
    release.set()
    assert (await closing).successful
    assert (await app.await_ready(timeout=1)).ready and trace == ["old", "new"]
    with pytest.raises(RuntimeClosed):
        contexts[0].require(DB)
    await host.close()


async def test_s1_02_loading_slots_and_whole_generation_publication() -> None:
    host = RuntimeHost()
    entered, release = asyncio.Event(), asyncio.Event()
    contexts: list[Any] = []

    async def setup(ctx: Any) -> None:
        contexts.append(ctx)
        ctx.provide(DB, "db")
        entered.set()
        await release.wait()
        ctx.provide(LOG, "logger")

    provider = host.load(PluginSpec("slow", setup, provides=(DB, LOG)))
    await entered.wait()
    assert host.context.get(DB) is None and host.context.get(LOG) is None
    duplicate = host.load(PluginSpec("duplicate", lambda ctx: None, provides=(DB,)))
    assert (await duplicate.await_ready(timeout=1)).outcome == "failed"
    release.set()
    assert (await provider.await_ready(timeout=1)).ready
    assert host.context.get(DB) == "db" and host.context.get(LOG) == "logger"
    await host.close()


async def test_s1_06_transitive_admission_is_closed_before_dependent_notification_awaits() -> None:
    host = RuntimeHost()
    contexts: list[Any] = []
    host.load(PluginSpec("db", lambda ctx: ctx.provide(DB, "db") and None, provides=(DB,)))

    def middle(ctx: Any) -> None:
        ctx.provide(LOG, "api")

    host.load(PluginSpec("middle", middle, requires=(DB,), provides=(LOG,)))
    leaf = host.load(PluginSpec("leaf", lambda ctx: contexts.append(ctx), requires=(LOG,)))
    await leaf.await_ready(timeout=1)
    db = next(r for r in host._records if r.spec.name == "db")
    task = host._invalidate({db})
    assert task
    with pytest.raises((RuntimeClosed, MissingService)):
        async with contexts[0].call(LOG):
            pytest.fail("new call crossed atomic admission withdrawal")
    await host.close()


async def test_c6_04_scoped_children_remove_bindings_and_resource_owners() -> None:
    host = RuntimeHost()
    children: list[Any] = []

    def parent(ctx: Any) -> None:
        view = ctx.isolate(DB, "isolated")
        children.append(view.plugin(PluginSpec("private", lambda c: c.provide(DB, 1) and None, provides=(DB,))))

    owner = host.load(PluginSpec("parent", parent))
    await owner.await_ready(timeout=1)
    assert (await host.await_ready(children, timeout=1)).ready
    assert host.context.isolate(DB, "isolated").get(DB) == 1
    await owner.close()
    assert host.context.isolate(DB, "isolated").get(DB) is None
    assert not host._slots
    assert all(row["state"] == "disposed" for row in host.snapshot()["components"])
    await host.close()


async def test_c7_01_c7_02_c7_04_shared_service_contributes_to_explicit_calling_owner() -> None:
    host = RuntimeHost()
    registrations: dict[str, bool] = {}
    contexts: list[Any] = []

    class Timer:
        def register(self, owner: Any, label: str) -> None:
            def setup() -> Any:
                registrations[label] = True
                return lambda: registrations.pop(label)

            owner.effect(setup)

    host.load(PluginSpec("timer", lambda ctx: ctx.provide(DB, Timer()) and None, provides=(DB,)))

    async def setup(ctx: Any) -> None:
        contexts.append(ctx)
        await asyncio.sleep(0)
        ctx.require(DB).register(ctx, ctx.config["label"])

    a = host.load(PluginSpec("a", setup, requires=(DB,), config={"label": "a"}))
    b = host.load(PluginSpec("b", setup, requires=(DB,), config={"label": "b"}))
    await host.await_ready((a, b), timeout=1)
    assert registrations == {"a": True, "b": True}
    await a.close()
    assert registrations == {"b": True} and host.is_ready((b,))
    with pytest.raises(RuntimeClosed):
        contexts[0].require(DB).register(contexts[0], "escaped")
    await host.close()
    assert registrations == {}


async def test_s1_11_cleanup_failure_blocks_provider_and_prevents_automatic_rebind() -> None:
    host = RuntimeHost()
    destroyed: list[str] = []

    def db_setup(ctx: Any) -> Any:
        ctx.provide(DB, "old")
        return lambda: destroyed.append("pool")

    def app_setup(ctx: Any) -> Any:
        def cleanup() -> None:
            raise OSError("unknown resource termination")

        return cleanup

    old = host.load(PluginSpec("db", db_setup, provides=(DB,)))
    app = host.load(PluginSpec("app", app_setup, requires=(DB,)))
    await app.await_ready(timeout=1)
    report = await old.close(timeout=1)
    assert report.blocked and destroyed == []
    host.load(PluginSpec("new", lambda ctx: ctx.provide(DB, "new") and None, provides=(DB,)))
    assert (await app.await_ready(timeout=1)).outcome == "failed"
    assert app.generation == 1 and destroyed == []
    assert (await host.close(timeout=1)).blocked


async def test_s1_07_s1_08_serial_snapshot_protects_later_owner_and_its_pool() -> None:
    from app.runtime.events import Continue, EventKey

    host = RuntimeHost()
    entered, release = asyncio.Event(), asyncio.Event()
    event = EventKey[Any]("serial", "serial")
    trace: list[str] = []
    second = None

    async def first_callback() -> Any:
        assert second is not None
        with pytest.raises(LifecycleWaitCycle):
            await second.close()
        entered.set()
        await release.wait()
        return Continue()

    def later_callback() -> Any:
        trace.append("later")
        return Continue()

    def db_setup(ctx: Any) -> Any:
        ctx.provide(DB, "db")
        return lambda: trace.append("pool.close")

    db = host.load(PluginSpec("db", db_setup, provides=(DB,)))
    host.load(PluginSpec("first", lambda ctx: ctx.on(event, first_callback) and None))
    second = host.load(PluginSpec("second", lambda ctx: ctx.on(event, later_callback) and None, requires=(DB,)))
    await host.await_ready(timeout=1)
    dispatch = asyncio.create_task(host.events.serial(event))
    await entered.wait()
    closing = asyncio.create_task(db.close())
    for _ in range(3):
        await asyncio.sleep(0)
    assert trace == []
    release.set()
    await dispatch
    assert (await closing).successful
    assert trace == ["later", "pool.close"]
    await host.close()


async def test_s1_09_noncooperative_parallel_listener_keeps_pool_owned() -> None:
    from app.runtime.events import EventKey

    host = RuntimeHost()
    entered, release = asyncio.Event(), asyncio.Event()
    event = EventKey[Any]("parallel", "parallel")
    trace: list[str] = []

    async def callback() -> None:
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    def provider(ctx: Any) -> Any:
        ctx.provide(DB, "db")
        return lambda: trace.append("pool.close")

    db = host.load(PluginSpec("db", provider, provides=(DB,)))
    app = host.load(PluginSpec("app", lambda ctx: ctx.on(event, callback) and None, requires=(DB,)))
    await app.await_ready(timeout=1)
    dispatch = asyncio.create_task(host.events.parallel(event))
    await entered.wait()
    dispatch.cancel()
    report = await db.close(timeout=0.01)
    assert report.pending and trace == []
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await dispatch
    assert (await host.close(timeout=1)).successful
    assert trace == ["pool.close"]


async def test_review_stale_context_scope_cannot_register_into_new_generation() -> None:
    from app.runtime.events import EventKey

    host = RuntimeHost()
    contexts: list[Any] = []
    handle = host.load(PluginSpec("owner", lambda ctx: contexts.append(ctx)))
    await handle.await_ready(timeout=1)
    old = contexts[0]
    await handle.restart(timeout=1)
    with pytest.raises(RuntimeClosed):
        old.scope.effect(lambda: None)
    with pytest.raises(RuntimeClosed):
        host.events.on(old, EventKey[Any]("stale", "emit"), lambda: None)
    await host.close()


async def test_review_manual_provider_close_drains_inherited_child_then_waits_for_reregistration() -> None:
    host = RuntimeHost()
    providers: list[Any] = []
    contexts: list[Any] = []
    children: list[Any] = []
    trace: list[str] = []

    def child(ctx: Any) -> Any:
        value = ctx.require(DB)
        trace.append(value)
        return lambda: trace.append("closed:" + ctx.require(DB))

    def parent(ctx: Any) -> None:
        contexts.append(ctx)
        providers.append(ctx.provide(DB, "old"))
        children.append(ctx.plugin(PluginSpec("child", child)))

    host.load(PluginSpec("parent", parent, provides=(DB,)))
    await host.await_ready(timeout=1)
    await host.await_ready(children, timeout=1)
    assert (await providers[0].close()).successful
    assert trace == ["old", "closed:old"]
    assert children[0].state == ComponentState.PENDING
    contexts[0].provide(DB, "new")
    assert (await host.await_ready(children, timeout=1)).ready
    assert trace == ["old", "closed:old", "new"]
    await host.close()


async def test_review_inherited_selfprovide_never_exposes_loading_parent_generation() -> None:
    host = RuntimeHost()
    parent_entered, release = asyncio.Event(), asyncio.Event()
    observations: list[str] = []
    children: list[Any] = []

    async def parent(ctx: Any) -> None:
        ctx.provide(DB, "complete-only")
        children.append(ctx.plugin(PluginSpec("child", lambda child_ctx: observations.append(child_ctx.require(DB)))))
        parent_entered.set()
        await release.wait()

    owner = host.load(PluginSpec("parent", parent, provides=(DB,)))
    await parent_entered.wait()
    for _ in range(10):
        await asyncio.sleep(0)
    assert observations == [] and host.context.get(DB) is None
    release.set()
    assert (await owner.await_ready(timeout=1)).ready
    assert (await host.await_ready(children, timeout=1)).ready
    assert observations == ["complete-only"]
    await host.close()


async def test_review_cross_realm_startup_wait_uses_public_provider() -> None:
    host = RuntimeHost()
    host.load(PluginSpec("public", lambda ctx: ctx.provide(DB, "public") and None, provides=(DB,)))
    await host.await_ready((DB,), timeout=1)
    seen: list[str] = []

    async def parent(ctx: Any) -> None:
        ctx.provide(DB, "private")
        child = ctx.isolate(DB, "default").plugin(
            PluginSpec("public-child", lambda child_ctx: seen.append(child_ctx.require(DB)), requires=(DB,))
        )
        assert (await child.await_ready(timeout=1)).ready

    owner = host.load(PluginSpec("private", parent, provides=(DB,)), context=host.context.isolate(DB, "private"))
    assert (await owner.await_ready(timeout=1)).ready
    assert seen == ["public"]
    await host.close()


async def test_review_close_report_preserves_every_cleanup_error() -> None:
    host = RuntimeHost()

    def setup(ctx: Any) -> Any:
        def first() -> None:
            raise ValueError("first")

        def second() -> None:
            raise TypeError("second")

        yield first
        yield second

    owner = host.load(PluginSpec("owner", setup))
    await owner.await_ready(timeout=1)
    report = await owner.close()
    assert {error.error_type for error in report.failed} == {"ValueError", "TypeError"}
    assert (await owner.close()).failed == report.failed
    await host.close()


async def test_c6_03_inject_is_nearest_config_and_custom_merge_is_used() -> None:
    host = RuntimeHost()
    host.load(PluginSpec("db", lambda ctx: ctx.provide(DB, "db") and None, provides=(DB,)))
    ancestor = host.context.intercept(DB, {"order": "ancestor", "keep": True, "nested": {"a": 1}})
    descendant = ancestor.intercept(DB, {"order": "descendant", "nested": {"b": 2}})
    seen: list[Any] = []

    def setup(ctx: Any) -> None:
        merged = ctx.config_for(DB, base={"order": "base"}, head={"head": True})
        assert merged == {"order": "inject", "keep": True, "nested": {"b": 2}, "head": True}
        seen.append(ctx.config_for(DB, base={"order": "base"}, head={"order": "head"}, merge=lambda *layers: layers))

    owner = host.load(
        PluginSpec("configured", setup, requires=(DB,), inject={DB: {"order": "inject"}}), context=descendant
    )
    assert (await owner.await_ready(timeout=1)).ready
    assert [layer["order"] for layer in seen[0]] == ["base", "ancestor", "descendant", "inject", "head"]
    with pytest.raises(ValueError, match="declared"):
        PluginSpec("invalid", lambda ctx: None, inject={DB: {}})
    await host.close()


async def test_c3_02_dependency_changes_during_loading_only_publish_final_generation() -> None:
    host = RuntimeHost()
    started, release = asyncio.Event(), asyncio.Event()
    trace: list[str] = []

    async def app_setup(ctx: Any) -> Any:
        value = ctx.require(DB)
        if ctx.generation == 1:
            started.set()
            await release.wait()
        trace.append("setup:" + value)
        return lambda: trace.append("close:" + value)

    first = host.load(PluginSpec("one", lambda ctx: ctx.provide(DB, "one") and None, provides=(DB,)))
    app = host.load(PluginSpec("app", app_setup, requires=(DB,)))
    await started.wait()
    first.request_close()
    second = host.load(PluginSpec("two", lambda ctx: ctx.provide(DB, "two") and None, provides=(DB,)))
    await second.await_ready(timeout=1)
    second.request_close()
    last = host.load(PluginSpec("three", lambda ctx: ctx.provide(DB, "three") and None, provides=(DB,)))
    await last.await_ready(timeout=1)
    assert app.state != ComponentState.ACTIVE
    release.set()
    assert (await app.await_ready(timeout=1)).ready
    assert trace == ["setup:one", "close:one", "setup:three"]
    assert app.context is not None and app.context.require(DB) == "three"
    await host.close()


async def test_setup_failure_safely_releases_registered_resources_and_can_explicitly_restart() -> None:
    host = RuntimeHost()
    trace: list[str] = []
    attempt = 0

    def setup(ctx: Any) -> None:
        nonlocal attempt
        attempt += 1
        ctx.effect(lambda: lambda: trace.append("clean"))
        if attempt == 1:
            raise ValueError("startup failed")

    owner = host.load(PluginSpec("owner", setup))
    assert (await owner.await_ready(timeout=1)).outcome == "failed"
    while owner._record.shutdown:
        await asyncio.sleep(0)
    assert trace == ["clean"] and not owner._record.blocked
    assert (await owner.restart(timeout=1)).ready
    assert (await owner.close()).successful and trace == ["clean", "clean"]
    await host.close()


async def test_independent_cleanup_continues_while_dependent_resources_remain_blocked() -> None:
    host = RuntimeHost()
    trace: list[str] = []

    def provider(ctx: Any) -> Any:
        ctx.provide(DB, "db")
        return lambda: trace.append("pool")

    def consumer(ctx: Any) -> Any:
        def broken() -> None:
            raise RuntimeError("resource still active")

        return broken

    host.load(PluginSpec("db", provider, provides=(DB,)))
    host.load(PluginSpec("consumer", consumer, requires=(DB,)))
    host.load(PluginSpec("independent", lambda ctx: lambda: trace.append("independent")))
    await host.await_ready(timeout=1)
    report = await host.close(timeout=1)
    assert report.blocked and trace == ["independent"]


async def test_readiness_propagates_current_dependency_failure_without_poisoning_healthy_provider() -> None:
    host = RuntimeHost()

    def fail(ctx: Any) -> None:
        raise OSError("provider startup unavailable")

    host.load(PluginSpec("broken", fail, provides=(DB,)))
    app = host.load(PluginSpec("app", lambda ctx: None, requires=(DB,)))
    report = await app.await_ready(timeout=1)
    assert report.outcome == "failed" and report.failures[0].name == "broken"
    await host.close()
    host = RuntimeHost()
    host.load(PluginSpec("valid", lambda ctx: ctx.provide(DB, "healthy") and None, provides=(DB,)))
    host.load(PluginSpec("duplicate", lambda ctx: None, provides=(DB,)))
    app = host.load(PluginSpec("app", lambda ctx: None, requires=(DB,)))
    assert (await app.await_ready(timeout=1)).ready
    await host.close()


async def test_readiness_ignores_retired_failure_while_replacement_is_loading() -> None:
    host = RuntimeHost()

    def fail(ctx: Any) -> None:
        raise OSError("historical failure")

    broken = host.load(PluginSpec("broken", fail, provides=(DB,)))
    assert (await broken.await_ready(timeout=1)).outcome == "failed"
    await broken.close()
    entered, release = asyncio.Event(), asyncio.Event()

    async def replacement(ctx: Any) -> None:
        entered.set()
        await release.wait()
        ctx.provide(DB, "replacement")

    host.load(PluginSpec("replacement", replacement, provides=(DB,)))
    await entered.wait()
    assert (await host.await_ready((DB,), timeout=0.001)).outcome == "timeout"
    release.set()
    assert (await host.await_ready((DB,), timeout=1)).ready
    await host.close()
