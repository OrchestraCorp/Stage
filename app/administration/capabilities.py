"""Explicit OS-authorized development/test upgrade of an accepted manager to v2."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Self

from pydantic import Field, TypeAdapter, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.administration.bootstrap import (
    ManagementIdentityPlan,
    _install_management_role,
    authority_snapshot,
    lock_management_identity,
    validate_management_identity,
)
from app.administration.errors import AdminDenied
from app.authz.context import validity_failure
from app.authz.management import (
    MANAGEMENT_ROLE_CODE,
    MANAGEMENT_ROLE_REF,
    MANAGEMENT_SCOPE_REF,
    ManagementReference,
    parse_grant_rules,
    validate_management_role,
)
from app.control.contracts import AdministrationError, AdministrationResult, Environment, StrictPlan
from app.control.operations import (
    acquire_admin_lock,
    append_journal,
    check_database_environment,
    find_replay,
    operation_transaction,
    validate_operator,
)
from app.identity.service import content_digest
from app.models.administration import ManagementAuthorityTable
from app.models.authorization import RoleAssignmentTable, RoleDefinitionTable
from app.models.execution import TenantGuardTable
from app.storage.coordination import LockMode, assert_organization_lock

UPGRADE_OPERATION = "upgrade.management-capabilities"
_MAX_VERSION = 2**31 - 1


class UpgradeManagementCapabilitiesPlan(StrictPlan):
    environment: Environment
    organization_ref: ManagementReference
    authority_ref: ManagementReference
    assignment_ref: ManagementReference
    expected_authority_version: int = Field(strict=True, ge=1, le=_MAX_VERSION)
    expected_assignment_version: int = Field(strict=True, ge=1, le=_MAX_VERSION)
    target_role_revision: int = Field(strict=True, ge=2, le=2)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def explicit_change(self) -> Self:
        if not self.reason.strip() or any(
            ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in self.reason
        ):
            raise ValueError("capability_upgrade_reason_required")
        return self


def _identity_plan(authority: ManagementAuthorityTable, environment: Environment) -> ManagementIdentityPlan:
    return ManagementIdentityPlan(
        environment=environment,
        organization_ref=authority.organization_ref,
        principal_ref=authority.principal_ref,
        membership_ref=authority.membership_ref,
        profile_ref=authority.profile_ref,
        profile_version=authority.profile_version,
        external_identity_ref=authority.external_identity_ref,
    )


def _snapshot(authority: ManagementAuthorityTable, assignment: RoleAssignmentTable) -> dict[str, object]:
    return {
        **authority_snapshot(authority),
        "assignment_version": assignment.version,
        "role_revision": assignment.role_revision,
    }


async def upgrade_management_capabilities(
    sessions: async_sessionmaker[AsyncSession],
    plan: UpgradeManagementCapabilitiesPlan,
    *,
    actor_ref: str,
    idempotency_key: str,
) -> AdministrationResult:
    """Install v2 and bind one accepted assignment atomically; never nominate or accept."""
    validate_operator(actor_ref, idempotency_key)
    if plan.environment not in {"dev", "test"}:
        raise AdministrationError("development_environment_required")
    try:
        reference = TypeAdapter(ManagementReference)
        reference.validate_python(actor_ref)
        reference.validate_python(idempotency_key)
    except (ValidationError, ValueError, UnicodeError):
        raise AdministrationError("invalid_operation_identity") from None
    if type(plan.target_role_revision) is not int or plan.target_role_revision != 2:
        raise AdministrationError("management_role_revision_unknown")
    digest = content_digest(plan.model_dump(mode="json"))
    try:
        async with operation_transaction(sessions, UPGRADE_OPERATION, idempotency_key) as session:
            await acquire_admin_lock(session)
            await check_database_environment(session, plan.environment)
            replay = await find_replay(session, actor_ref, UPGRADE_OPERATION, idempotency_key, digest)
            if replay is not None:
                return replay
            hint = await session.get(ManagementAuthorityTable, plan.organization_ref)
            if hint is None:
                raise AdministrationError("accepted_management_authority_required")
            identity = _identity_plan(hint, plan.environment)
            await lock_management_identity(session, identity)
            authority = await session.get(ManagementAuthorityTable, plan.organization_ref, populate_existing=True)
            if (
                authority is None
                or authority.status != "active"
                or authority.accepted_at is None
                or authority.authority_ref != plan.authority_ref
                or authority.assignment_ref != plan.assignment_ref
                or _identity_plan(authority, plan.environment) != identity
            ):
                raise AdministrationError("accepted_management_authority_required")
            assignment = await session.scalar(
                select(RoleAssignmentTable)
                .where(
                    RoleAssignmentTable.organization_ref == plan.organization_ref,
                    RoleAssignmentTable.assignment_ref == plan.assignment_ref,
                )
                .execution_options(populate_existing=True)
            )
            if (
                assignment is None
                or assignment.principal_ref != authority.principal_ref
                or assignment.principal_type != "human"
                or assignment.membership_ref != authority.membership_ref
                or assignment.role_ref != MANAGEMENT_ROLE_REF
                or assignment.role_code != MANAGEMENT_ROLE_CODE
                or assignment.scope_ref != MANAGEMENT_SCOPE_REF
                or assignment.grant_source_ref != authority.authority_ref
                or assignment.role_revision != 1
                or assignment.expires_at is not None
                or assignment.revoked_at is not None
                or assignment.valid_from is None
                or validity_failure(
                    datetime.now(UTC), valid_from=assignment.valid_from, expires_at=None, status=assignment.status
                )
                is not None
            ):
                raise AdministrationError("current_management_revision_one_required")
            if (
                authority.version != plan.expected_authority_version
                or assignment.version != plan.expected_assignment_version
            ):
                raise AdministrationError("version_conflict")
            if authority.version >= _MAX_VERSION or assignment.version >= _MAX_VERSION:
                raise AdministrationError("management_version_exhausted")
            await validate_management_identity(session, identity, datetime.now(UTC))
            parse_grant_rules(authority.grant_rules_json)
            await validate_management_role(session, plan.organization_ref, 1)
            role = await session.get(RoleDefinitionTable, (plan.organization_ref, MANAGEMENT_ROLE_REF))
            guard = await session.get(TenantGuardTable, plan.organization_ref, populate_existing=True)
            if guard is None or guard.epoch >= _MAX_VERSION:
                raise AdministrationError("organization_epoch_exhausted")
            if role is None or role.version >= _MAX_VERSION:
                raise AdministrationError("management_version_exhausted")
            before = _snapshot(authority, assignment)
            await _install_management_role(session, plan.organization_ref, role_revision=2)
            assignment.role_revision = 2
            assignment.version += 1
            authority.version += 1
            role.version += 1
            guard.epoch += 1
            await session.flush()
            contract = await validate_management_role(session, plan.organization_ref, 2)
            after = _snapshot(authority, assignment)
            result = append_journal(
                session,
                actor_ref=actor_ref,
                operation_type=UPGRADE_OPERATION,
                idempotency_key=idempotency_key,
                digest=digest,
                environment=plan.environment,
                target_ref=authority.authority_ref,
                before=before,
                after={**after, "reason": plan.reason},
                result={
                    **after,
                    "policy_release_ref": contract.policy_release_ref,
                    "actions": sorted(contract.actions),
                    "organization_authz_epoch": guard.epoch,
                },
            )
            await session.flush()
            deadline = await validate_management_identity(session, identity, datetime.now(UTC))
            if deadline is not None and deadline <= datetime.now(UTC):
                raise AdministrationError("management_authentication_expired")
            assert_organization_lock(session, plan.organization_ref, mode=LockMode.EXCLUSIVE)
            return result
    except AdminDenied:
        raise AdministrationError("management_authority_not_current") from None
    except IntegrityError:
        raise AdministrationError("management_capability_conflict") from None
