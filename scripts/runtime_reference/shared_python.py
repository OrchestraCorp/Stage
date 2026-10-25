"""Execute serializable common operation descriptions against Stage's Core."""

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any

from app.runtime.contracts import ComponentState, PluginSpec, ServiceKey
from app.runtime.events import Continue, EventKey, Stop
from app.runtime.host import RuntimeHost


class Scenario:
    def __init__(self, description: dict[str, Any]) -> None:
        self.description = description
        self.host = RuntimeHost()
        self.scopes: dict[str, Any] = {"root": self.host.context}
        self.handles: dict[str, Any] = {}
        self.contexts: dict[str, Any] = {}
        self.stats: dict[str, dict[str, Any]] = {}
        self.availability: dict[str, bool] = {}
        self.providers: dict[str, Any] = {}
        self.effects: dict[str, Any] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self.events: dict[str, EventKey[Any]] = {}
        self.listener_handles: dict[str, Any] = {}
        self.log: list[Any] = []
        self.observations: list[dict[str, Any]] = []
        self.initial_generation: dict[str, int] = {}

    @staticmethod
    def key(name: str) -> ServiceKey[Any]:
        return ServiceKey("reference", name)

    def gate(self, name: str) -> asyncio.Event:
        return self.gates.setdefault(name, asyncio.Event())

    def event(self, descriptor: dict[str, Any]) -> EventKey[Any]:
        name = descriptor.get("event", "probe")
        if name not in self.events:
            self.events[name] = EventKey(name, descriptor.get("mode", "emit"), descriptor.get("asynchronous", False))
        return self.events[name]

    def inverse(self, descriptor: dict[str, Any], ctx: Any = None) -> Any:
        def before() -> None:
            if descriptor.get("read"):
                self.log.append(
                    {
                        "phase": "before",
                        "value": ctx.require(self.key(descriptor["read"])),
                        "public": self.host.context.get(self.key(descriptor["read"])),
                    }
                )
            elif descriptor.get("release"):
                self.log.append(descriptor["label"] + ".enter")
            else:
                self.log.append(descriptor["label"])
            if descriptor.get("entered"):
                self.gate(descriptor["entered"]).set()
            if descriptor.get("error"):
                raise RuntimeError("fixture cleanup failure")

        async def cleanup_async() -> None:
            before()
            await self.gate(descriptor["release"]).wait()
            if descriptor.get("read"):
                self.log.append({"phase": "after", "value": ctx.require(self.key(descriptor["read"]))})
            else:
                self.log.append(descriptor["label"] + ".end")

        return cleanup_async if descriptor.get("release") else before

    def listener(self, descriptor: dict[str, Any]) -> Any:
        behavior = descriptor.get("behavior", "log")

        def begin() -> None:
            self.log.append(descriptor.get("label", behavior))
            if descriptor.get("entered"):
                self.gate(descriptor["entered"]).set()
            if behavior == "error":
                raise RuntimeError("fixture listener failure")

        if behavior == "around":

            async def around(next: Any) -> Any:
                self.log.append(descriptor["label"] + ".before")
                if descriptor.get("veto"):
                    return descriptor["value"]
                value = await next()
                self.log.append(descriptor["label"] + ".after")
                return value + descriptor.get("add", 0)

            return around
        if behavior == "wait":

            async def wait() -> None:
                begin()
                await self.gate(descriptor["release"]).wait()
                self.log.append(descriptor["label"] + ".end")

            return wait

        def sync() -> Any:
            begin()
            if behavior == "remove":
                self.listener_handles[descriptor["target"]].unregister()
            if behavior == "decision":
                return Continue() if descriptor["decision"] == "continue" else Stop(descriptor.get("value"))
            return descriptor.get("value")

        return sync

    def load(self, action: dict[str, Any], parent: Any = None) -> None:
        name = action["id"]
        self.stats[name] = {"runs": 0, "cleaned": 0, "calls": 0}
        self.availability[name] = action.get("available", True)

        async def setup(ctx: Any) -> Any:
            self.contexts[name] = ctx
            data = self.stats[name]
            data["runs"] += 1
            if "provide" in action:
                value = action["value"]
                if action.get("service_type") == "caller":
                    scenario = self

                    class CallerService:
                        def register(self, owner: Any, label: str) -> None:
                            owner.effect(lambda: lambda: scenario.log.append(label))

                    value = CallerService()
                elif action.get("service_type") == "query":
                    scenario = self

                    class QueryService:
                        async def query(self) -> None:
                            scenario.gate("query.entered").set()
                            await scenario.gate("query.release").wait()
                            scenario.log.append("query.end")

                    value = QueryService()
                self.providers[name] = ctx.provide(self.key(action["provide"]), value, lambda: self.availability[name])
            if action.get("listen"):

                def called() -> None:
                    data["calls"] += 1

                ctx.on(self.event({}), called)
            for descriptor in action.get("listeners", []):
                handle = ctx.on(self.event(descriptor), self.listener(descriptor))
                if "id" in descriptor:
                    self.listener_handles[descriptor["id"]] = handle
            if "read" in action:
                data["discovered"] = ctx.get(self.key(action["read"]))
                try:
                    ctx.require(self.key(action["read"]))
                    data["denied"] = False
                except RuntimeError:
                    data["denied"] = True
            if "capture_required" in action:
                data.setdefault("values", []).append(ctx.require(self.key(action["capture_required"])))
            if "register_call" in action:
                descriptor = action["register_call"]
                await asyncio.sleep(0)
                ctx.require(self.key(descriptor["service"])).register(ctx, descriptor["label"])
            if "config_for" in action:
                descriptor = action["config_for"]
                merge = (
                    (lambda *layers: {"sources": [layer.get("common") for layer in layers]})
                    if descriptor.get("merge")
                    else None
                )
                data["config"] = ctx.config_for(
                    self.key(descriptor["key"]), base=descriptor.get("base"), head=descriptor.get("head"), merge=merge
                )
            for descriptor in action.get("effects", []):
                self.effects[name + ":" + descriptor["label"]] = ctx.effect(lambda d=descriptor: self.inverse(d, ctx))
            if action.get("generator"):
                descriptors = action["generator"]

                def sequence() -> Any:
                    for descriptor in descriptors:
                        yield self.inverse(descriptor, ctx)

                self.effects[name + ":generator"] = ctx.effect(sequence)
            for child in action.get("children", []):
                self.load(child, ctx)
            if action.get("setup_entered"):
                self.gate(action["setup_entered"]).set()
            if action.get("setup_release"):
                await self.gate(action["setup_release"]).wait()
            if action.get("fail_setup"):
                raise RuntimeError("fixture setup failure")

            inverse = self.inverse(action["cleanup"], ctx) if "cleanup" in action else None

            async def cleanup() -> None:
                data["cleaned"] += 1
                if inverse is not None:
                    result = inverse()
                    if inspect.isawaitable(result):
                        await result

            return cleanup

        provides = (self.key(action["provide"]),) if "provide" in action else ()
        plugin = PluginSpec(
            name,
            setup,
            requires=tuple(self.key(v) for v in action.get("requires", [])),
            provides=provides,
            inject={self.key(k): v for k, v in action.get("inject", {}).items()},
        )
        self.handles[name] = self.host.load(plugin, context=parent or self.scopes[action.get("scope", "root")])
        self.initial_generation[name] = self.handles[name].generation

    async def dispatch(self, action: dict[str, Any]) -> Any:
        event = self.event(action)
        bus = self.host.events
        mode = action["mode"]
        if mode == "waterfall":

            async def terminal() -> Any:
                self.log.append("terminal")
                return action["terminal"]

            result = await bus.waterfall(event, next=terminal)
        elif mode in ("parallel", "serial"):
            result = await getattr(bus, mode)(event)
        else:
            result = getattr(bus, mode)(event)
        if isinstance(result, Stop):
            return result.value
        return None if isinstance(result, Continue) else result

    async def invoke_query(self, key: ServiceKey[Any]) -> None:
        async with self.host.call(key) as call:
            await call.require(key).query()

    async def execute(self) -> list[dict[str, Any]]:
        async with asyncio.timeout(15):
            for action in self.description["actions"]:
                op = action["op"]
                if op == "scope":
                    base = self.scopes[action.get("base", "root")]
                    self.scopes[action["id"]] = base.isolate(self.key(action["key"]), action["label"])
                elif op == "intercept":
                    base = self.scopes[action.get("base", "root")]
                    self.scopes[action["id"]] = base.intercept(self.key(action["key"]), action["value"])
                elif op == "load":
                    self.load(action)
                elif op == "ready":
                    for name in action["ids"]:
                        report = await self.handles[name].await_ready(timeout=2)
                        assert report.ready, report
                elif op == "ready_failed":
                    report = await self.handles[action["id"]].await_ready(timeout=2)
                    assert report.outcome == "failed", report
                elif op == "duplicate_failed":
                    report = await self.handles[action["id"]].await_ready(timeout=2)
                    assert report.outcome == "failed", report
                    self.observations.append({"duplicate_rejected": True})
                elif op == "pending":
                    assert self.handles[action["id"]].state == ComponentState.PENDING
                    self.observations.append({"pending": action["id"]})
                elif op == "wait_absent":
                    while self.host.context.get(self.key(action["key"])) is not None:
                        await asyncio.sleep(0)
                elif op in ("retire", "retire_begin"):
                    closing = self.handles[action["id"]].close(timeout=None)
                    if op == "retire_begin":
                        self.tasks[action.get("task", "closing")] = asyncio.create_task(closing)
                    else:
                        report = await closing
                        if action.get("observe_failure"):
                            self.observations.append({"cleanup_failed": not report.successful})
                        elif not action.get("allow_failed"):
                            assert report.successful, report
                elif op == "restart":
                    before = self.handles[action["id"]].generation
                    assert (await self.handles[action["id"]].restart()).ready
                    self.observations.append(
                        {"activation_identity_changed": self.handles[action["id"]].generation != before}
                    ) if action.get("observe") else None
                elif op in ("effect_close", "effect_close_begin"):
                    closing = self.effects[action["effect"]].close()
                    if action.get("observe_shape"):
                        self.observations.append({"returns_awaitable": inspect.isawaitable(closing)})
                    if op == "effect_close_begin":
                        self.tasks[action.get("task", "effect")] = asyncio.create_task(closing)
                    else:
                        await closing
                elif op == "wait_gate":
                    await self.gate(action["id"]).wait()
                elif op == "release":
                    self.gate(action["id"]).set()
                elif op == "join":
                    try:
                        await self.tasks[action["task"]]
                        if action.get("expect_error"):
                            raise AssertionError("expected dispatch failure")
                    except BaseExceptionGroup as error:
                        if not action.get("expect_error"):
                            raise
                        self.observations.append({"raised": True, "errors": len(error.exceptions)})
                elif op == "observe_task":
                    self.observations.append({"pending": not self.tasks[action["task"]].done()})
                elif op == "await_task_timeout":
                    try:
                        await asyncio.wait_for(asyncio.shield(self.tasks[action["task"]]), timeout=0.01)
                        outcome = "done"
                    except TimeoutError:
                        outcome = "timeout"
                    self.observations.append({"task_outcome": outcome})
                elif op == "call_begin":
                    self.tasks[action["task"]] = asyncio.create_task(self.invoke_query(self.key(action["key"])))
                elif op == "availability":
                    self.availability[action["id"]] = action["value"]
                    self.providers[action["id"]].notify()
                elif op == "emit":
                    self.host.events.emit(self.event({}))
                elif op in ("dispatch", "dispatch_begin"):
                    if op == "dispatch_begin":
                        self.tasks[action["task"]] = asyncio.create_task(self.dispatch(action))
                    else:
                        self.observations.append({"value": await self.dispatch(action)})
                elif op == "dispatch_shape":
                    result = self.host.events.bail(self.event(action))
                    self.observations.append({"returns_explicit_decision": isinstance(result, (Continue, Stop))})
                elif op == "get":
                    self.observations.append(
                        {"value": self.scopes[action.get("scope", "root")].get(self.key(action["key"]))}
                    )
                elif op == "observe":
                    self.observations.append({"id": action["id"], **self.stats[action["id"]]})
                elif op == "observe_log":
                    self.observations.append({"log": sorted(self.log) if action.get("sort") else list(self.log)})
                elif op == "stale_effect":
                    try:
                        self.contexts[action["id"]].effect(lambda: None)
                        denied = False
                    except RuntimeError:
                        denied = True
                    self.observations.append({"rejected": denied})
                elif op in ("stale_require", "stale_listener"):
                    try:
                        if op == "stale_require":
                            self.contexts[action["id"]].require(self.key(action["key"]))
                        else:
                            self.contexts[action["id"]].on(self.event({}), lambda: None)
                        denied = False
                    except RuntimeError:
                        denied = True
                    self.observations.append({"rejected": denied})
                elif op == "await_without_provider":
                    report = await self.handles[action["id"]].await_ready(timeout=0.01)
                    self.observations.append({"outcome": report.outcome})
                else:
                    raise ValueError("unsupported shared operation: " + op)
            report = await self.host.close(timeout=2)
            if not self.description.get("allow_final_failure"):
                assert report.successful, report
        return self.observations


async def run() -> None:
    cases = json.loads(Path(__file__).with_name("shared-cases.json").read_text())
    failed = 0
    for description in cases:
        try:
            observations = await Scenario(description).execute()
            result = {
                "scenario_id": description["id"],
                "scope": description["scope"],
                "outcome": "observed",
                "observations": observations,
            }
        except Exception as error:
            failed += 1
            result = {"scenario_id": description["id"], "outcome": "failed", "error": str(error)}
        print(json.dumps(result))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(run())
