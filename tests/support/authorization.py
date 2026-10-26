"""Shared synthetic builders for authorization."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.loader import role_permissions_digest
from app.authz.permissions import BUILTIN_POLICY_RELEASE_ID
from app.core.enums import PrincipalType
from app.execution.contracts import ExecutionContext
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
from app.models.identity_foundation import OrganizationMembershipTable, OrganizationRootTable
from app.models.work import WorkItemTable

NOW = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)


ORG = "org:stage"


PERSON = "person:alice"


TARGET = "work_item:1"


def execution_context(*, assignment_ref: str | None = "assignment:engineering") -> ExecutionContext:
    return ExecutionContext(
        tenant_ref=ORG,
        actor_ref=PERSON,
        effective_subject_ref=PERSON,
        request_id="request:loader",
        principal_type=PrincipalType.HUMAN,
        credential_expires_at=NOW + timedelta(minutes=10),
        assignment_ref=assignment_ref,
    )


async def seed_authorization(
    sessions: async_sessionmaker[AsyncSession],
    *,
    resource_scope: str = "scope:engineering",
    assignment_scope: str = "scope:engineering",
    with_relation: bool = True,
    second_assignment: bool = False,
) -> None:
    async with sessions.begin() as session:
        session.add(OrganizationRootTable(organization_ref=ORG, display_name="Stage", status="active", version=1))
        await session.flush()
        session.add(TenantGuardTable(tenant_ref=ORG))
        session.add(TenantGuardTable(tenant_ref="org:forged"))
        session.add(PrincipalTable(principal_ref=PERSON, principal_type="human", status="active", version=1))
        await session.flush()
        session.add(
            OrganizationMembershipTable(
                organization_ref=ORG,
                membership_ref="membership:alice",
                principal_ref=PERSON,
                principal_type="human",
                source_ref="bootstrap:stage",
                status="active",
                valid_from=NOW - timedelta(days=1),
                version=1,
            )
        )
        # These tables intentionally have database-level foreign keys but no
        # ORM relationships. Flush each dependency layer explicitly so both
        # SQLite foreign-key mode and PostgreSQL see the same insertion order.
        await session.flush()
        session.add_all(
            [
                PermissionActionTable(
                    action_ref="task.assign",
                    resource_kind="work_item",
                    contract_version="1",
                    status="active",
                    version=1,
                ),
                RoleDefinitionTable(
                    organization_ref=ORG,
                    role_ref="role:engineering",
                    role_code="department-owner",
                    display_name="Engineering member",
                    status="active",
                    version=1,
                ),
                AuthorizationScopeTable(
                    organization_ref=ORG,
                    scope_ref="scope:engineering",
                    scope_kind="department",
                    status="active",
                    version=1,
                ),
                AuthorizationScopeTable(
                    organization_ref=ORG,
                    scope_ref="scope:finance",
                    scope_kind="department",
                    status="active",
                    version=1,
                ),
            ]
        )
        await session.flush()
        session.add(
            RoleVersionTable(
                organization_ref=ORG,
                role_ref="role:engineering",
                role_revision=1,
                content_digest=role_permissions_digest(
                    ORG, "role:engineering", 1, BUILTIN_POLICY_RELEASE_ID, ("task.assign",)
                ),
                policy_release_ref=BUILTIN_POLICY_RELEASE_ID,
                status="active",
                version=1,
            )
        )
        await session.flush()
        session.add(
            RoleVersionActionTable(
                organization_ref=ORG,
                role_ref="role:engineering",
                role_revision=1,
                action_ref="task.assign",
            )
        )
        session.add(
            AuthorizationResourceTable(
                organization_ref=ORG,
                resource_ref=TARGET,
                resource_kind="work_item",
                scope_ref=resource_scope,
                status="active",
                version=1,
            )
        )
        session.add(
            WorkItemTable(
                id=1,
                title="Loader fixture",
                lifecycle="open",
                classification="internal",
                scope_ref=resource_scope,
                version=1,
            )
        )
        await session.flush()
        session.add(
            RoleAssignmentTable(
                principal_ref=PERSON,
                role_code="department-owner",
                scope_ref=assignment_scope,
                organization_ref=ORG,
                assignment_ref="assignment:engineering",
                membership_ref="membership:alice",
                principal_type="human",
                role_ref="role:engineering",
                role_revision=1,
                grant_source_ref="bootstrap:authorization",
                valid_from=NOW - timedelta(days=1),
                status="active",
                version=1,
            )
        )
        await session.flush()
        if second_assignment:
            session.add(
                RoleAssignmentTable(
                    principal_ref=PERSON,
                    role_code="department-owner",
                    scope_ref="scope:finance",
                    organization_ref=ORG,
                    assignment_ref="assignment:finance",
                    membership_ref="membership:alice",
                    principal_type="human",
                    role_ref="role:engineering",
                    role_revision=1,
                    grant_source_ref="bootstrap:authorization",
                    valid_from=NOW - timedelta(days=1),
                    status="active",
                    version=1,
                )
            )
        if with_relation:
            session.add(
                RelationTupleTable(
                    subject_ref=PERSON,
                    relation="owner_of",
                    resource_ref=TARGET,
                    organization_ref=ORG,
                    relation_ref="relation:eligible",
                    membership_ref="membership:alice",
                    subject_type="human",
                    relation_type="owner_of",
                    source_kind="direct_acl",
                    source_ref=TARGET,
                    source_version=1,
                    valid_from=NOW - timedelta(days=1),
                    status="active",
                    version=1,
                )
            )
