"""Explicit trusted command/worker composition; no permissive production IAM defaults."""

from collections.abc import Mapping, Sequence

from app.delivery.contracts import CurrentDeliveryAuthorizer, DeliverySink
from app.delivery.worker import OutboxWorker
from app.execution.contracts import AuthorizationLoader, CommandEnvelope, CommandResult, CommandSpec, ExecutionContext
from app.execution.kernel import CommandKernel
from app.providers.storage import STORAGE
from app.runtime import ComponentContext, PluginSpec, ServiceKey

COMMANDS: ServiceKey["CommandService"] = ServiceKey("stage", "commands", "1")
DELIVERY: ServiceKey[OutboxWorker] = ServiceKey("stage", "delivery", "1")


class CommandService:
    """Command facade that holds a storage lease for each operation."""

    def __init__(self, context: ComponentContext, loader: AuthorizationLoader, commands: Sequence[CommandSpec]) -> None:
        self._context = context
        self._loader = loader
        self._commands = tuple(commands)

    async def execute(self, context: ExecutionContext, command: CommandEnvelope) -> CommandResult:
        async with self._context.call(STORAGE) as call:
            kernel = CommandKernel(call.require(STORAGE).sessions, loader=self._loader, commands=self._commands)
            return await kernel.execute(context, command)

    async def get_result(self, context: ExecutionContext, execution_id: str) -> CommandResult:
        async with self._context.call(STORAGE) as call:
            kernel = CommandKernel(call.require(STORAGE).sessions, loader=self._loader, commands=self._commands)
            return await kernel.get_result(context, execution_id)


def command_plugin(loader: AuthorizationLoader, commands: Sequence[CommandSpec]) -> PluginSpec:
    def setup(ctx: ComponentContext) -> None:
        ctx.provide(COMMANDS, CommandService(ctx, loader, commands))

    return PluginSpec("commands", setup, requires=(STORAGE,), provides=(COMMANDS,))


def delivery_plugin(
    *,
    authorizer: CurrentDeliveryAuthorizer,
    sinks: Mapping[str, DeliverySink],
    service_ref: str,
    worker_ref: str,
    tenants: Sequence[str],
    close_timeout: float = 5,
    poll_interval: float = 1,
) -> PluginSpec:
    """Mount only after a trusted authorizer and explicit sink/tenant scope exist."""

    def setup(ctx: ComponentContext) -> None:
        worker = OutboxWorker(
            ctx.require(STORAGE).sessions,
            authorizer=authorizer,
            sinks=sinks,
            service_ref=service_ref,
            worker_ref=worker_ref,
            close_timeout=close_timeout,
            poll_interval=poll_interval,
        )
        ctx.effect(lambda: worker.close, label="delivery.worker")
        ctx.on_stop(worker.request_stop)
        ctx.provide(DELIVERY, worker)
        ctx.spawn(worker.run(tenants), name="delivery.poll")

    return PluginSpec("delivery", setup, requires=(STORAGE,), provides=(DELIVERY,))
