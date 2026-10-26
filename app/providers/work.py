"""Human work metadata operations assembled with identity and storage lifecycle leases."""

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.execution.contracts import CommandEnvelope, CommandResult
from app.execution.errors import AuthorizationDenied
from app.features.work.details import WORK_DETAILS_COMMAND, WorkDetailsInput
from app.models.work import WorkItemTable
from app.providers.human import HumanOperations
from app.providers.identity import IDENTITY
from app.providers.storage import STORAGE
from app.runtime import ComponentContext, PluginSpec, ServiceKey

WORK: ServiceKey["HumanWorkService"] = ServiceKey("stage", "human-work", "1")


class WorkItemView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_ref: str
    title: str
    description: str | None
    lifecycle: str
    classification: str
    version: int


class HumanWorkService:
    def __init__(self, context: ComponentContext, timeout_seconds: float) -> None:
        self._operations = HumanOperations(context, timeout_seconds, (WORK_DETAILS_COMMAND,))

    async def update_details(
        self,
        token: str,
        *,
        work_item_id: int,
        profile_ref: str,
        profile_version: int,
        assignment_ref: str,
        request_id: str,
        idempotency_key: str,
        expected_version: int,
        details: WorkDetailsInput,
    ) -> CommandResult:
        async with self._operations.request(
            token,
            profile_ref=profile_ref,
            profile_version=profile_version,
            assignment_ref=assignment_ref,
            request_id=request_id,
        ) as operation:
            return await operation.kernel.execute(
                operation.context,
                CommandEnvelope(
                    WORK_DETAILS_COMMAND.name,
                    WORK_DETAILS_COMMAND.version,
                    f"work_item:{work_item_id}",
                    expected_version,
                    idempotency_key,
                    details.model_dump(mode="json"),
                ),
            )

    async def get_operation(
        self,
        token: str,
        *,
        work_item_id: int,
        profile_ref: str,
        profile_version: int,
        assignment_ref: str,
        request_id: str,
        idempotency_key: str,
    ) -> CommandResult:
        async with self._operations.request(
            token,
            profile_ref=profile_ref,
            profile_version=profile_version,
            assignment_ref=assignment_ref,
            request_id=request_id,
        ) as operation:
            return await operation.kernel.get_result_by_key(
                operation.context,
                command_name=WORK_DETAILS_COMMAND.name,
                command_version=WORK_DETAILS_COMMAND.version,
                target_ref=f"work_item:{work_item_id}",
                idempotency_key=idempotency_key,
            )

    async def get_item(
        self,
        token: str,
        *,
        work_item_id: int,
        profile_ref: str,
        profile_version: int,
        assignment_ref: str,
        request_id: str,
    ) -> WorkItemView:
        async def read_item(session: AsyncSession) -> WorkItemView:
            item = await session.get(WorkItemTable, work_item_id)
            if item is None:
                raise AuthorizationDenied()
            return WorkItemView(
                resource_ref=f"work_item:{item.id}",
                title=item.title,
                description=item.description,
                lifecycle=item.lifecycle,
                classification=item.classification,
                version=item.version,
            )

        async with self._operations.request(
            token,
            profile_ref=profile_ref,
            profile_version=profile_version,
            assignment_ref=assignment_ref,
            request_id=request_id,
        ) as operation:
            return await operation.kernel.read(
                operation.context,
                action=WORK_DETAILS_COMMAND.read_action,
                target_ref=f"work_item:{work_item_id}",
                reader=read_item,
            )


def human_work_plugin(config: Settings) -> PluginSpec:
    def setup(ctx: ComponentContext) -> None:
        ctx.provide(WORK, HumanWorkService(ctx, config.command_request_timeout_seconds))

    return PluginSpec("human-work", setup, requires=(IDENTITY, STORAGE), provides=(WORK,))
