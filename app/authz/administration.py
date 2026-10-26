"""Explicit local development authorization installation; never an HTTP grant API.

This bounded registrar supports human assignments and direct WorkItem ACLs.
The operator is the local OS installation identity. It cannot create accounts,
invent domain objects, install arbitrary actions, or replace published grants.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.context import validity_failure
from app.authz.loader import role_permissions_digest
from app.authz.permissions import BUILTIN_POLICY_RELEASE_ID
from app.control.contracts import (
    BOOTSTRAP_TYPE,
    AdministrationError,
    AdministrationResult,
    Environment,
    Reference,
    StrictPlan,
)
from app.control.operations import (
    acquire_admin_lock,
    append_journal,
    check_database_environment,
    find_replay,
    operation_transaction,
    validate_operator,
)
from app.identity.service import content_digest
from app.models.authorization import (
    AuthorizationResourceTable,
    AuthorizationScopeTable,
    PermissionActionTable,
    RelationTupleTable,
    RoleAssignmentTable,
    RoleDefinitionTable,
    RoleVersionActionTable,
    RoleVersionTable,
)
from app.models.execution import TenantGuardTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import BootstrapRecordTable, OrganizationMembershipTable, OrganizationRootTable
from app.models.work import WorkItemTable
from app.storage.coordination import LockMode, lock_organization

ScopeReference = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\s\x00-\x1f\x7f]+$")]
Action = Literal["task.view", "task.assign", "task.metadata.update"]
Relation = Literal["viewer_of", "owner_of"]
INSTALL_OPERATION = "install.authorization"


class ScopePlan(StrictPlan):
    scope_ref: ScopeReference
    scope_kind: Literal["organization", "department", "project"]
    parent_scope_ref: ScopeReference | None = None


class MemberGrantPlan(StrictPlan):
    principal_ref: Reference
    membership_ref: Reference
    expected_membership_version: int = Field(ge=1)
    expires_at: datetime | None = None


class AssignmentPlan(MemberGrantPlan):
    assignment_ref: Reference
    scope_ref: ScopeReference


class ResourcePlan(StrictPlan):
    resource_ref: ScopeReference
    scope_ref: ScopeReference
    expected_work_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        work_id(self.resource_ref)
        return self


class ACLPlan(MemberGrantPlan):
    relation_ref: Reference
    relation: Relation
    resource_ref: ScopeReference

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        work_id(self.resource_ref)
        return self


class InstallAuthorizationPlan(StrictPlan):
    environment: Environment
    organization_ref: Reference
    expected_organization_version: int = Field(ge=1)
    role_ref: Reference
    role_code: Reference
    display_name: str = Field(min_length=1, max_length=256)
    role_revision: int = Field(ge=1)
    expected_role_definition_version: int | None = Field(default=None, ge=1)
    actions: tuple[Action, ...] = Field(min_length=1, max_length=3)
    scopes: tuple[ScopePlan, ...] = Field(default=(), max_length=64)
    assignments: tuple[AssignmentPlan, ...] = Field(min_length=1, max_length=128)
    resources: tuple[ResourcePlan, ...] = Field(default=(), max_length=128)
    acls: tuple[ACLPlan, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        unique_groups = (
            tuple(self.actions),
            tuple(row.scope_ref for row in self.scopes),
            tuple(row.assignment_ref for row in self.assignments),
            tuple(row.resource_ref for row in self.resources),
            tuple(row.relation_ref for row in self.acls),
        )
        if any(len(group) != len(set(group)) for group in unique_groups):
            raise ValueError("duplicate_authorization_reference")
        for row in (*self.assignments, *self.acls):
            if row.expires_at is not None and (row.expires_at.tzinfo is None or row.expires_at.utcoffset() is None):
                raise ValueError("expiry_timezone_required")
        return self


class AuthorizationRevocationRequest(StrictPlan):
    environment: Environment
    organization_ref: Reference
    kind: Literal["assignment", "relation", "role_version", "scope", "resource"]
    target_ref: ScopeReference
    expected_version: int = Field(ge=1)
    role_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_role_revision(self) -> Self:
        if (self.role_revision is not None) != (self.kind == "role_version"):
            raise ValueError("role_version_requires_revision")
        return self


def work_id(reference: str) -> int:
    suffix = reference.removeprefix("work_item:")
    if not suffix.isascii() or not suffix.isdigit() or reference != f"work_item:{int(suffix)}":
        raise ValueError("canonical_work_item_reference_required")
    value = int(suffix)
    if not 1 <= value <= 2**31 - 1:
        raise ValueError("invalid_work_item_reference")
    return value


async def _initialized(session: AsyncSession, environment: Environment) -> None:
    await check_database_environment(session, environment)
    installed = await session.scalar(
        select(BootstrapRecordTable.bootstrap_ref).where(
            BootstrapRecordTable.bootstrap_type == BOOTSTRAP_TYPE,
            BootstrapRecordTable.environment == environment,
        )
    )
    if installed is None:
        raise AdministrationError("identity_not_initialized")


async def _principal_locks(session: AsyncSession, references: set[str]) -> dict[str, PrincipalTable]:
    principals: dict[str, PrincipalTable] = {}
    for reference in sorted(references):
        row = await session.scalar(
            select(PrincipalTable)
            .where(PrincipalTable.principal_ref == reference)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise AdministrationError("principal_not_found")
        principals[reference] = row
    return principals


async def _root_guard(
    session: AsyncSession, organization_ref: str, *, expected_version: int | None = None
) -> TenantGuardTable:
    root = await session.scalar(select(OrganizationRootTable).where(OrganizationRootTable.deployment_slot == 1))
    if root is None or root.organization_ref != organization_ref:
        raise AdministrationError("identity_database_organization_mismatch")
    await lock_organization(session, organization_ref, mode=LockMode.EXCLUSIVE)
    await session.refresh(root)
    if root.status != "active":
        raise AdministrationError("organization_inactive")
    if expected_version is not None and root.version != expected_version:
        raise AdministrationError("organization_version_conflict")
    guard = await session.get(TenantGuardTable, organization_ref)
    if guard is None:
        raise AdministrationError("organization_guard_missing")
    return guard


async def _members(
    session: AsyncSession,
    plan: InstallAuthorizationPlan,
    principals: dict[str, PrincipalTable],
    now: datetime,
) -> None:
    for row in (*plan.assignments, *plan.acls):
        principal = principals[row.principal_ref]
        if principal.principal_type != "human" or principal.status != "active" or principal.disabled_at is not None:
            raise AdministrationError("human_principal_inactive")
        membership = await session.get(OrganizationMembershipTable, (plan.organization_ref, row.membership_ref))
        if (
            membership is None
            or membership.principal_ref != row.principal_ref
            or membership.principal_type != "human"
            or membership.ended_at is not None
            or validity_failure(
                now, valid_from=membership.valid_from, expires_at=membership.expires_at, status=membership.status
            )
        ):
            raise AdministrationError("membership_inactive")
        if membership.version != row.expected_membership_version:
            raise AdministrationError("membership_version_conflict")
        if row.expires_at is not None and row.expires_at <= now:
            raise AdministrationError("authorization_expired")


async def _active_scope(session: AsyncSession, organization_ref: str, scope_ref: str) -> None:
    visited: set[str] = set()
    current: str | None = scope_ref
    for _ in range(64):
        if current is None:
            return
        if current in visited:
            raise AdministrationError("scope_cycle")
        visited.add(current)
        row = await session.get(AuthorizationScopeTable, (organization_ref, current))
        if row is None or row.status != "active":
            raise AdministrationError("scope_inactive")
        current = row.parent_scope_ref
        if current is None:
            return
    raise AdministrationError("scope_depth_exceeded")


async def _install_scopes(session: AsyncSession, plan: InstallAuthorizationPlan) -> None:
    pending = {row.scope_ref: row for row in plan.scopes}
    for reference in pending:
        if await session.get(AuthorizationScopeTable, (plan.organization_ref, reference)) is not None:
            raise AdministrationError("scope_already_exists")
    while pending:
        ready = [row for row in pending.values() if row.parent_scope_ref not in pending]
        if not ready:
            raise AdministrationError("scope_cycle")
        for row in ready:
            if row.parent_scope_ref is not None:
                await _active_scope(session, plan.organization_ref, row.parent_scope_ref)
            session.add(
                AuthorizationScopeTable(
                    organization_ref=plan.organization_ref,
                    scope_ref=row.scope_ref,
                    scope_kind=row.scope_kind,
                    parent_scope_ref=row.parent_scope_ref,
                )
            )
            await session.flush()
            await _active_scope(session, plan.organization_ref, row.scope_ref)
            del pending[row.scope_ref]


async def _install_role(session: AsyncSession, plan: InstallAuthorizationPlan) -> None:
    role = await session.get(RoleDefinitionTable, (plan.organization_ref, plan.role_ref))
    if role is None:
        if plan.expected_role_definition_version is not None:
            raise AdministrationError("role_definition_not_found")
        session.add(
            RoleDefinitionTable(
                organization_ref=plan.organization_ref,
                role_ref=plan.role_ref,
                role_code=plan.role_code,
                display_name=plan.display_name,
                template_source_ref="stage-development-authorization-v1",
            )
        )
    elif (
        role.status != "active"
        or role.version != plan.expected_role_definition_version
        or role.role_code != plan.role_code
        or role.display_name != plan.display_name
    ):
        raise AdministrationError("role_definition_conflict")
    if await session.get(RoleVersionTable, (plan.organization_ref, plan.role_ref, plan.role_revision)) is not None:
        raise AdministrationError("role_version_already_exists")
    for action in plan.actions:
        existing = await session.get(PermissionActionTable, action)
        if existing is None:
            session.add(PermissionActionTable(action_ref=action, resource_kind="work_item", contract_version="1"))
        elif existing.status != "active" or existing.resource_kind != "work_item" or existing.contract_version != "1":
            raise AdministrationError("action_catalog_conflict")
    await session.flush()
    session.add(
        RoleVersionTable(
            organization_ref=plan.organization_ref,
            role_ref=plan.role_ref,
            role_revision=plan.role_revision,
            content_digest=role_permissions_digest(
                plan.organization_ref, plan.role_ref, plan.role_revision, BUILTIN_POLICY_RELEASE_ID, plan.actions
            ),
            policy_release_ref=BUILTIN_POLICY_RELEASE_ID,
        )
    )
    await session.flush()
    session.add_all(
        RoleVersionActionTable(
            organization_ref=plan.organization_ref,
            role_ref=plan.role_ref,
            role_revision=plan.role_revision,
            action_ref=action,
        )
        for action in sorted(plan.actions)
    )


async def _work_resource(session: AsyncSession, organization_ref: str, resource_ref: str) -> AuthorizationResourceTable:
    registered = await session.get(AuthorizationResourceTable, (organization_ref, resource_ref))
    if registered is None or registered.status != "active" or registered.resource_kind != "work_item":
        raise AdministrationError("resource_inactive")
    work = await session.scalar(
        select(WorkItemTable).where(WorkItemTable.id == work_id(resource_ref)).with_for_update()
    )
    if work is None or work.scope_ref != registered.scope_ref:
        raise AdministrationError("work_resource_mismatch")
    await _active_scope(session, organization_ref, registered.scope_ref)
    return registered


async def install_authorization(
    sessions: async_sessionmaker[AsyncSession],
    plan: InstallAuthorizationPlan,
    *,
    actor_ref: str,
    idempotency_key: str,
) -> AdministrationResult:
    """Append one bounded development role release and its explicit grants."""
    validate_operator(actor_ref, idempotency_key)
    plan_digest = content_digest(plan.model_dump(mode="json"))
    try:
        async with operation_transaction(sessions, INSTALL_OPERATION, idempotency_key) as session:
            await acquire_admin_lock(session)
            await _initialized(session, plan.environment)
            replay = await find_replay(session, actor_ref, INSTALL_OPERATION, idempotency_key, plan_digest)
            if replay is not None:
                return replay
            principals = await _principal_locks(session, {row.principal_ref for row in (*plan.assignments, *plan.acls)})
            guard = await _root_guard(
                session, plan.organization_ref, expected_version=plan.expected_organization_version
            )
            now = datetime.now(UTC)
            await _members(session, plan, principals, now)
            operation_ref = str(uuid4())
            await _install_scopes(session, plan)
            await _install_role(session, plan)
            for row in sorted(plan.resources, key=lambda value: value.resource_ref):
                if await session.get(AuthorizationResourceTable, (plan.organization_ref, row.resource_ref)) is not None:
                    raise AdministrationError("resource_already_exists")
                await _active_scope(session, plan.organization_ref, row.scope_ref)
                work = await session.scalar(
                    select(WorkItemTable).where(WorkItemTable.id == work_id(row.resource_ref)).with_for_update()
                )
                if work is None or work.scope_ref != row.scope_ref:
                    raise AdministrationError("work_resource_mismatch")
                if work.version != row.expected_work_version:
                    raise AdministrationError("work_version_conflict")
                session.add(
                    AuthorizationResourceTable(
                        organization_ref=plan.organization_ref,
                        resource_ref=row.resource_ref,
                        resource_kind="work_item",
                        scope_ref=row.scope_ref,
                    )
                )
            await session.flush()
            for assignment in plan.assignments:
                await _active_scope(session, plan.organization_ref, assignment.scope_ref)
                session.add(
                    RoleAssignmentTable(
                        organization_ref=plan.organization_ref,
                        assignment_ref=assignment.assignment_ref,
                        membership_ref=assignment.membership_ref,
                        principal_ref=assignment.principal_ref,
                        principal_type="human",
                        role_ref=plan.role_ref,
                        role_revision=plan.role_revision,
                        role_code=plan.role_code,
                        scope_ref=assignment.scope_ref,
                        valid_from=now,
                        expires_at=assignment.expires_at,
                        grant_source_ref=operation_ref,
                    )
                )
            for acl in sorted(plan.acls, key=lambda value: (value.resource_ref, value.relation_ref)):
                await _work_resource(session, plan.organization_ref, acl.resource_ref)
                session.add(
                    RelationTupleTable(
                        organization_ref=plan.organization_ref,
                        relation_ref=acl.relation_ref,
                        membership_ref=acl.membership_ref,
                        subject_ref=acl.principal_ref,
                        subject_type="human",
                        relation=acl.relation,
                        relation_type=acl.relation,
                        resource_ref=acl.resource_ref,
                        valid_from=now,
                        expires_at=acl.expires_at,
                        source_kind="direct_acl",
                        source_ref=operation_ref,
                        source_version=1,
                    )
                )
            guard.epoch += 1
            await session.flush()
            # Long installations can cross an expiry while awaiting database
            # work. Do not publish grants that have already expired at the end.
            await _members(session, plan, principals, datetime.now(UTC))
            result = {
                "organization_ref": plan.organization_ref,
                "role_ref": plan.role_ref,
                "role_revision": plan.role_revision,
                "assignment_refs": [row.assignment_ref for row in plan.assignments],
                "relation_refs": [row.relation_ref for row in plan.acls],
                "resource_refs": [row.resource_ref for row in plan.resources],
                "scope_refs": [row.scope_ref for row in plan.scopes],
                "organization_authz_epoch": guard.epoch,
            }
            return append_journal(
                session,
                actor_ref=actor_ref,
                operation_type=INSTALL_OPERATION,
                idempotency_key=idempotency_key,
                digest=plan_digest,
                environment=plan.environment,
                target_ref=plan.role_ref,
                operation_ref=operation_ref,
                result=result,
                after={"schema": "stage-authorization-install-v1", "plan": plan.model_dump(mode="json"), **result},
            )
    except IntegrityError:
        raise AdministrationError("authorization_conflict") from None


async def _revoke_target(
    session: AsyncSession, request: AuthorizationRevocationRequest
) -> RoleAssignmentTable | RelationTupleTable | RoleVersionTable | AuthorizationScopeTable | AuthorizationResourceTable:
    row: (
        RoleAssignmentTable
        | RelationTupleTable
        | RoleVersionTable
        | AuthorizationScopeTable
        | AuthorizationResourceTable
        | None
    )
    if request.kind == "assignment":
        row = await session.scalar(
            select(RoleAssignmentTable).where(
                RoleAssignmentTable.organization_ref == request.organization_ref,
                RoleAssignmentTable.assignment_ref == request.target_ref,
            )
        )
    elif request.kind == "relation":
        row = await session.scalar(
            select(RelationTupleTable).where(
                RelationTupleTable.organization_ref == request.organization_ref,
                RelationTupleTable.relation_ref == request.target_ref,
            )
        )
    elif request.kind == "role_version":
        row = await session.get(RoleVersionTable, (request.organization_ref, request.target_ref, request.role_revision))
    elif request.kind == "scope":
        row = await session.get(AuthorizationScopeTable, (request.organization_ref, request.target_ref))
    else:
        row = await session.get(AuthorizationResourceTable, (request.organization_ref, request.target_ref))
    if row is None:
        raise AdministrationError("target_not_found")
    return row


async def revoke_authorization(
    sessions: async_sessionmaker[AsyncSession],
    request: AuthorizationRevocationRequest,
    *,
    actor_ref: str,
    idempotency_key: str,
) -> AdministrationResult:
    """Terminal revocation; the shared organization guard serializes commits."""
    validate_operator(actor_ref, idempotency_key)
    operation_type = f"revoke.authorization.{request.kind}"
    request_digest = content_digest(request.model_dump(mode="json"))
    async with operation_transaction(sessions, operation_type, idempotency_key) as session:
        await acquire_admin_lock(session)
        await _initialized(session, request.environment)
        replay = await find_replay(session, actor_ref, operation_type, idempotency_key, request_digest)
        if replay is not None:
            return replay
        candidate = await _revoke_target(session, request)
        reference = (
            candidate.principal_ref
            if isinstance(candidate, RoleAssignmentTable)
            else candidate.subject_ref
            if isinstance(candidate, RelationTupleTable)
            else None
        )
        if reference is not None:
            await _principal_locks(session, {reference})
        guard = await _root_guard(session, request.organization_ref)
        await session.refresh(candidate)
        if candidate.version != request.expected_version:
            raise AdministrationError("version_conflict")
        if candidate.status not in {"active", "suspended"}:
            raise AdministrationError("authorization_already_ended")
        before = {"status": candidate.status, "version": candidate.version, "organization_authz_epoch": guard.epoch}
        candidate.status = "revoked"
        candidate.version += 1
        if isinstance(candidate, RoleAssignmentTable | RelationTupleTable | RoleVersionTable):
            candidate.revoked_at = datetime.now(UTC)
        guard.epoch += 1
        after = {"status": candidate.status, "version": candidate.version, "organization_authz_epoch": guard.epoch}
        result = {
            "organization_ref": request.organization_ref,
            "kind": request.kind,
            "target_ref": request.target_ref,
            "role_revision": request.role_revision,
            **after,
        }
        return append_journal(
            session,
            actor_ref=actor_ref,
            operation_type=operation_type,
            idempotency_key=idempotency_key,
            digest=request_digest,
            environment=request.environment,
            target_ref=request.target_ref,
            result=result,
            before=before,
            after=after,
        )
