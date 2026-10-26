"""Shared synthetic builders for command kernel."""

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.context import AttributeContext, AuthorizationContext, RelationTuple, RoleAssignment
from app.authz.permissions import Permission
from app.core.enums import PrincipalType
from app.execution.contracts import (
    CommandEffect,
    CommandEnvelope,
    CommandSpec,
    ExecutionContext,
    OutboxIntent,
)
from app.execution.storage import UnitOfWork
from app.models.execution import AuditRecordTable, CommandExecutionTable, OutboxRecordTable, TenantGuardTable
from app.models.identity import PrincipalTable
from app.models.work import WorkItemTable


class ClaimInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str


class ClaimResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int


class TestFactsLoader:
    """Integration fixture over current tables, NOT a production identity loader."""

    async def load(
        self,
        session: AsyncSession,
        context: ExecutionContext,
        *,
        action: str,
        target_ref: str,
        expected_version: int | None,
        now: datetime,
    ) -> AuthorizationContext:
        principal = await session.scalar(
            select(PrincipalTable).where(PrincipalTable.principal_ref == context.actor_ref)
        )
        item = await session.scalar(select(WorkItemTable).where(WorkItemTable.id == int(target_ref)).with_for_update())
        return AuthorizationContext(
            request_id=context.request_id,
            subject_ref=context.actor_ref,
            principal_type=context.principal_type,
            action=action,
            resource_ref=target_ref,
            organization_ref=context.tenant_ref,
            resource_organization_ref=item.scope_ref if item else None,
            resource_scope_refs=(item.scope_ref,) if item else (),
            assignment=RoleAssignment(
                assignment_ref="assignment:1",
                principal_ref=context.actor_ref,
                role_code="engineering-member",
                organization_ref=context.tenant_ref,
                scope_refs=(context.tenant_ref,),
            ),
            relations=(
                RelationTuple(context.actor_ref, "eligible_claimant_of", target_ref),
                RelationTuple(context.actor_ref, "viewer_of", target_ref),
            ),
            now=now,
            attributes=AttributeContext(
                subject_active=principal is not None and principal.status == "active",
                resource_classification="internal",
                classification_ceiling="internal",
                object_version=item.version if item else None,
                expected_object_version=expected_version,
                resource_state=item.lifecycle if item else None,
            ),
        )


async def seed(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions.begin() as session:
        session.add_all(
            [
                TenantGuardTable(tenant_ref="tenant:a"),
                TenantGuardTable(tenant_ref="tenant:b"),
                PrincipalTable(principal_ref="person:1", principal_type="human", status="active"),
                PrincipalTable(principal_ref="person:2", principal_type="human", status="active"),
                WorkItemTable(id=1, title="Fixture", lifecycle="open", classification="internal", scope_ref="tenant:a"),
            ]
        )


def context() -> ExecutionContext:
    return ExecutionContext(
        tenant_ref="tenant:a",
        actor_ref="person:1",
        effective_subject_ref="person:1",
        request_id="request:1",
        principal_type=PrincipalType.HUMAN,
        credential_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        assignment_ref="assignment:1",
    )


def command() -> CommandEnvelope:
    return CommandEnvelope("claim", "v1", "1", 1, "stable-key", {"note": "test"})


async def claim(uow: UnitOfWork, ctx: ExecutionContext, cmd: CommandEnvelope, payload: BaseModel) -> CommandEffect:
    item = await uow.session.get(WorkItemTable, int(cmd.target_ref))
    assert item is not None
    before = {"version": item.version}
    item.version += 1
    item.lifecycle = "committed"
    return CommandEffect(
        result={"version": item.version},
        before=before,
        after={"version": item.version},
        messages=(
            OutboxIntent(
                "task.changed",
                "test-sink",
                cmd.target_ref,
                Permission.TASK_VIEW,
                "claim:" + cmd.idempotency_key,
                {"version": item.version},
            ),
        ),
    )


def spec(handler: Any = claim) -> CommandSpec:
    return CommandSpec(
        "claim", "v1", Permission.TASK_COMMIT_CREATE, Permission.TASK_VIEW, ClaimInput, ClaimResult, handler
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
