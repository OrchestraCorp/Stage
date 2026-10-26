"""Shared synthetic builders for work details."""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.loader import role_permissions_digest
from app.authz.permissions import BUILTIN_POLICY_RELEASE_ID
from app.execution.contracts import CommandEnvelope
from app.models.authorization import RoleVersionActionTable, RoleVersionTable
from app.models.execution import AuditRecordTable, CommandExecutionTable, OutboxRecordTable
from app.models.work import WorkItemTable
from tests.support.authorization import ORG, TARGET
from tests.support.database_commands import seed as seed_read_authorization


def command(**changes: Any) -> CommandEnvelope:
    return CommandEnvelope(
        **{
            "name": "work.details.update",
            "version": "1",
            "target_ref": TARGET,
            "expected_version": 1,
            "idempotency_key": "update-details-once",
            "payload": {"title": "  Updated title  ", "description": "  first line\nsecond line  "},
            **changes,
        }
    )


async def seed(sessions: async_sessionmaker[AsyncSession], *, grant: bool = True) -> None:
    from app.models.authorization import PermissionActionTable

    await seed_read_authorization(sessions)
    async with sessions.begin() as session:
        session.add(
            PermissionActionTable(action_ref="task.metadata.update", resource_kind="work_item", contract_version="1")
        )
        await session.flush()
        if grant:
            session.add(
                RoleVersionActionTable(
                    organization_ref=ORG,
                    role_ref="role:engineering",
                    role_revision=1,
                    action_ref="task.metadata.update",
                )
            )
            role = await session.get(RoleVersionTable, (ORG, "role:engineering", 1))
            assert role is not None
            role.content_digest = role_permissions_digest(
                ORG,
                "role:engineering",
                1,
                BUILTIN_POLICY_RELEASE_ID,
                ("task.assign", "task.view", "task.metadata.update"),
            )


async def assert_unchanged(sessions: async_sessionmaker[AsyncSession], *, lifecycle: str = "open") -> None:
    async with sessions() as session:
        work = await session.get(WorkItemTable, 1)
        assert work is not None
        assert (work.title, work.description, work.version, work.lifecycle) == ("Loader fixture", None, 1, lifecycle)
        assert work.scope_ref == "scope:engineering" and work.classification == "internal"
        for model in (CommandExecutionTable, AuditRecordTable, OutboxRecordTable):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
