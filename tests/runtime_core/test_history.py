"""Successful lifecycle history cannot retain resources or grow without bound."""

import gc
import weakref
from typing import Any

import pytest

from app.runtime import (
    ComponentContext,
    ComponentState,
    MissingService,
    PluginSpec,
    RuntimeClosed,
    RuntimeHost,
    ServiceKey,
)


class _Resource:
    pass


def _resource_spec(name: str, references: list[weakref.ReferenceType[_Resource]]) -> PluginSpec:
    captured, configured = _Resource(), _Resource()
    references.extend((weakref.ref(captured), weakref.ref(configured)))

    def setup(ctx: ComponentContext) -> None:
        assert captured is not ctx.config["resource"]

    return PluginSpec(name, setup, config={"resource": configured})


async def test_replacement_releases_setup_and_config_with_old_handles_alive() -> None:
    host = RuntimeHost(history_limit=4)
    references: list[weakref.ReferenceType[_Resource]] = []
    owner = host.load(_resource_spec("generation-0", references))
    assert (await owner.await_ready()).ready
    handles = [owner]
    for index in range(1, 21):
        owner = await host.replace(owner, _resource_spec(f"generation-{index}", references))
        handles.append(owner)
        assert (await owner.await_ready()).ready
        gc.collect()
        assert all(reference() is None for reference in references[:-2])
        assert all(reference() is not None for reference in references[-2:])
        assert len(host.snapshot()["components"]) <= 5
        assert host.is_ready()

    assert len(host._records) == 1
    assert host.snapshot()["history"] == {"limit": 4, "omitted": 16}
    first = handles[0]
    assert first.name == "generation-0" and first.state == ComponentState.DISPOSED and first.generation == 1
    assert first.context is None
    assert (await first.close()).completed == (first.owner_id,)
    with pytest.raises(RuntimeClosed):
        await first.restart()

    assert (await host.close()).successful
    gc.collect()
    assert all(reference() is None for reference in references)
    assert len(host.snapshot()["components"]) == 4
    assert host.snapshot()["history"] == {"limit": 4, "omitted": 17}
    # Snapshot callers cannot modify the retained diagnostic records.
    host.snapshot()["components"][0]["state"] = "active"
    assert all(component["state"] == "disposed" for component in host.snapshot()["components"])


async def test_disposed_history_does_not_retain_original_inherited_configuration() -> None:
    host = RuntimeHost()
    key = ServiceKey[Any]("history", "configuration")
    resource = _Resource()
    reference = weakref.ref(resource)
    base = host.context.intercept(key, {"resource": resource})
    owner = host.load(PluginSpec("configured", lambda ctx: None), context=base)
    del resource, base
    assert (await owner.await_ready()).ready
    assert (await owner.close()).successful
    # A retained caller handle intentionally remembers its replacement context.
    gc.collect()
    assert reference() is not None
    replacement = await host.replace(owner, PluginSpec("replacement", lambda ctx: None))
    assert (await replacement.await_ready()).ready
    assert replacement.context is not None
    assert replacement.context.config_for(key)["resource"] is reference()
    assert (await replacement.close()).successful
    del owner, replacement
    gc.collect()
    assert reference() is None
    assert len(host.snapshot()["components"]) == 2
    await host.close()


async def test_disposed_children_are_not_retained_by_active_parent() -> None:
    host = RuntimeHost(history_limit=2)
    owner = host.load(PluginSpec("parent", lambda ctx: None))
    assert (await owner.await_ready()).ready
    assert owner.context is not None
    for index in range(10):
        child = owner.context.plugin(PluginSpec(f"child-{index}", lambda ctx: None))
        reference = weakref.ref(child._record)
        assert (await child.await_ready()).ready
        assert (await child.close()).successful
        del child
        gc.collect()
        assert reference() is None
    assert not owner._record.children
    assert len(host._records) == 1
    assert len(host.snapshot()["components"]) == 3
    assert (await host.close()).successful


async def test_failed_cleanup_stays_owned_when_success_history_and_observations_roll_over() -> None:
    host = RuntimeHost(history_limit=2, observation_limit=2)
    resource = _Resource()
    reference = weakref.ref(resource)

    def setup(ctx: ComponentContext) -> None:
        def fail() -> None:
            assert ctx.config["resource"] is not None
            raise RuntimeError("cleanup-failed")

        ctx.effect(lambda: fail)

    owner = host.load(PluginSpec("failed", setup, config={"resource": resource}))
    del resource
    assert (await owner.await_ready()).ready
    failed_id = owner.owner_id
    assert (await owner.close()).blocked == (failed_id,)
    del owner
    for index in range(8):
        successful = host.load(PluginSpec(f"success-{index}", lambda ctx: None))
        assert (await successful.await_ready()).ready
        assert (await successful.close()).successful
        host._observe(successful._record, ValueError(f"later-{index}"), "availability")
    gc.collect()
    assert reference() is not None
    failed = next(component for component in host.snapshot()["components"] if component["owner_id"] == failed_id)
    assert failed["state"] == "failed" and failed["blocked"]
    assert failed["failure"].message == "cleanup-failed"
    assert len(host.snapshot()["components"]) == 3
    assert all(observation.message != "cleanup-failed" for observation in host.observations)
    assert (await host.close()).blocked == (failed_id,)


async def test_repeated_availability_failures_keep_only_bounded_latest_observations() -> None:
    host = RuntimeHost(observation_limit=3)
    key = ServiceKey[object]("history", "unavailable")
    attempts = 0

    def unavailable() -> bool:
        nonlocal attempts
        attempts += 1
        raise RuntimeError(f"attempt-{attempts}")

    def setup(ctx: ComponentContext) -> None:
        ctx.provide(key, object(), check=unavailable)

    owner = host.load(PluginSpec("unavailable", setup, provides=(key,)))
    assert (await owner.await_ready()).ready
    for _ in range(10):
        with pytest.raises(MissingService):
            async with host.call(key):
                pytest.fail("unavailable service admitted a call")
    assert [entry.message for entry in host.observations] == ["attempt-8", "attempt-9", "attempt-10"]
    assert host.snapshot()["observations"] == {"limit": 3, "omitted": 7}
    assert (await host.close()).successful


@pytest.mark.parametrize("limit", (0, -1, True, 1.5))
def test_diagnostic_limits_require_positive_integer_capacities(limit: Any) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        RuntimeHost(history_limit=limit)
    with pytest.raises(ValueError, match="positive integers"):
        RuntimeHost(observation_limit=limit)
