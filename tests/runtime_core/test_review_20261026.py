"""Readiness isolation and ownership after a component generation has drained."""

import gc
import weakref

import pytest

from app.runtime import ComponentContext, PluginSpec, RuntimeHost, ServiceKey


@pytest.mark.parametrize("service_target", (False, True))
async def test_unrelated_optional_cycle_does_not_fail_required_startup(service_target: bool) -> None:
    host = RuntimeHost()
    required_key = ServiceKey[object]("review", "required")
    a = ServiceKey[object]("review", "optional-a")
    b = ServiceKey[object]("review", "optional-b")

    def setup(ctx: ComponentContext) -> None:
        ctx.provide(required_key, object())

    required = host.load(PluginSpec("required", setup, provides=(required_key,)))
    host.load(PluginSpec("optional-a", lambda ctx: None, requires=(b,), provides=(a,)))
    host.load(PluginSpec("optional-b", lambda ctx: None, requires=(a,), provides=(b,)))
    try:
        report = await host.await_ready((required_key if service_target else required,), timeout=1)
        assert report.ready, report
        # A complete-host diagnostic still exposes the broken optional composition.
        assert host.snapshot()["cycles"]
        assert (await host.await_ready(timeout=1)).outcome == "failed"
    finally:
        await host.close()


@pytest.mark.parametrize("service_target", (False, True))
async def test_required_transitive_dependency_cycle_still_fails(service_target: bool) -> None:
    host = RuntimeHost()
    required_key = ServiceKey[object]("review", "required")
    a = ServiceKey[object]("review", "cycle-a")
    b = ServiceKey[object]("review", "cycle-b")
    required = host.load(PluginSpec("required", lambda ctx: None, requires=(a,), provides=(required_key,)))
    host.load(PluginSpec("cycle-a", lambda ctx: None, requires=(b,), provides=(a,)))
    host.load(PluginSpec("cycle-b", lambda ctx: None, requires=(a,), provides=(b,)))
    try:
        report = await host.await_ready((required_key if service_target else required,), timeout=1)
        assert report.outcome == "failed"
        assert report.failures[0].error_type == "DependencyCycle"
    finally:
        await host.close()


class _Value:
    pass


async def test_drained_generations_release_provider_values_while_host_stays_alive() -> None:
    host = RuntimeHost()
    key = ServiceKey[_Value]("review", "value")
    references: list[weakref.ReferenceType[_Value]] = []

    def setup(ctx: ComponentContext) -> None:
        value = _Value()
        references.append(weakref.ref(value))
        ctx.provide(key, value)

    owner = host.load(PluginSpec("value", setup, provides=(key,)))
    try:
        assert (await owner.await_ready(timeout=1)).ready
        for _ in range(3):
            assert (await owner.restart(timeout=1)).ready
            gc.collect()
            assert all(reference() is None for reference in references[:-1])
            assert references[-1]() is not None
        assert (await owner.close(timeout=1)).successful
        gc.collect()
        assert all(reference() is None for reference in references)
        # Lifecycle diagnostics survive without retaining released service objects.
        snapshot = host.snapshot()["components"][0]
        assert snapshot["state"] == "disposed" and snapshot["generation"] == 4
    finally:
        await host.close()


async def test_failed_cleanup_retains_provider_value_and_blocked_diagnostics() -> None:
    host = RuntimeHost()
    key = ServiceKey[_Value]("review", "value")
    references: list[weakref.ReferenceType[_Value]] = []

    def setup(ctx: ComponentContext) -> None:
        value = _Value()
        references.append(weakref.ref(value))
        ctx.provide(key, value)

        def fail_cleanup() -> None:
            raise RuntimeError("still-owned-resource")

        ctx.effect(lambda: fail_cleanup, label="failing-resource")

    owner = host.load(PluginSpec("value", setup, provides=(key,)))
    assert (await owner.await_ready(timeout=1)).ready
    report = await host.close(timeout=1)
    gc.collect()
    assert report.blocked and report.failed
    assert references[0]() is not None
    assert host.snapshot()["components"][0]["blocked"]
