"""Shared synthetic builders for database commands."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.loader import role_permissions_digest
from app.authz.permissions import BUILTIN_POLICY_RELEASE_ID
from app.models.authorization import PermissionActionTable, RoleVersionActionTable, RoleVersionTable
from app.models.execution import AuditRecordTable, CommandExecutionTable, OutboxRecordTable
from app.models.work import WorkItemTable
from tests.support.authorization import ORG, seed_authorization


async def seed(sessions: async_sessionmaker[AsyncSession]) -> None:
    await seed_authorization(sessions)
    async with sessions.begin() as session:
        session.add(
            PermissionActionTable(
                action_ref="task.view", resource_kind="work_item", contract_version="1", status="active"
            )
        )
        await session.flush()
        session.add(
            RoleVersionActionTable(
                organization_ref=ORG, role_ref="role:engineering", role_revision=1, action_ref="task.view"
            )
        )
        role = await session.get(RoleVersionTable, (ORG, "role:engineering", 1))
        assert role is not None
        role.content_digest = role_permissions_digest(
            ORG, "role:engineering", 1, BUILTIN_POLICY_RELEASE_ID, ("task.assign", "task.view")
        )


async def counts(sessions: async_sessionmaker[AsyncSession]) -> tuple[int, int, int, int]:
    async with sessions() as session:
        item = await session.get(WorkItemTable, 1)
        assert item is not None
        return (
            item.version,
            int(await session.scalar(select(func.count()).select_from(CommandExecutionTable)) or 0),
            int(await session.scalar(select(func.count()).select_from(AuditRecordTable)) or 0),
            int(await session.scalar(select(func.count()).select_from(OutboxRecordTable)) or 0),
        )
