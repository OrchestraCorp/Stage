"""Authorization persistence invariants on SQLite and PostgreSQL."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.permissions import BUILTIN_POLICY_RELEASE_ID
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
from app.models.base import Base
from app.models.identity import PrincipalTable
from app.models.identity_foundation import OrganizationMembershipTable, OrganizationRootTable

NOW = datetime(2026, 10, 25, tzinfo=UTC)


async def _insert(sessions: async_sessionmaker[AsyncSession], model: type[Base], **values: Any) -> None:
    async with sessions.begin() as session:
        session.add(model(**values))


@pytest.fixture
async def authorization_sessions(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    sessions = persistence_sessions
    await _insert(sessions, OrganizationRootTable, organization_ref="org:1", display_name="Stage")
    for principal, kind in (("human:1", "human"), ("human:2", "human"), ("service:1", "service")):
        await _insert(sessions, PrincipalTable, principal_ref=principal, principal_type=kind)
        await _insert(
            sessions,
            OrganizationMembershipTable,
            organization_ref="org:1",
            membership_ref=f"membership:{principal}",
            principal_ref=principal,
            principal_type=kind,
            valid_from=NOW,
            source_ref="install:1",
        )
    await _insert(
        sessions, AuthorizationScopeTable, organization_ref="org:1", scope_ref="scope:1", scope_kind="project"
    )
    await _insert(
        sessions,
        AuthorizationResourceTable,
        organization_ref="org:1",
        resource_ref="work_item:1",
        resource_kind="work_item",
        scope_ref="scope:1",
    )
    await _insert(
        sessions,
        PermissionActionTable,
        action_ref="task.view",
        resource_kind="work_item",
        contract_version="1",
    )
    await _insert(
        sessions,
        RoleDefinitionTable,
        organization_ref="org:1",
        role_ref="role:1",
        role_code="engineering-member",
        display_name="Engineering member",
    )
    for revision in (1, 2):
        await _insert(
            sessions,
            RoleVersionTable,
            organization_ref="org:1",
            role_ref="role:1",
            role_revision=revision,
            content_digest=str(revision) * 64,
            policy_release_ref=BUILTIN_POLICY_RELEASE_ID,
        )
        await _insert(
            sessions,
            RoleVersionActionTable,
            organization_ref="org:1",
            role_ref="role:1",
            role_revision=revision,
            action_ref="task.view",
        )
    return sessions


def _assignment(**changes: Any) -> dict[str, Any]:
    return {
        "organization_ref": "org:1",
        "assignment_ref": "assignment:1",
        "membership_ref": "membership:human:1",
        "principal_ref": "human:1",
        "principal_type": "human",
        "role_ref": "role:1",
        "role_revision": 1,
        "role_code": "engineering-member",
        "scope_ref": "scope:1",
        "grant_source_ref": "grant:1",
        "valid_from": NOW,
        **changes,
    }


def _relation(**changes: Any) -> dict[str, Any]:
    return {
        "organization_ref": "org:1",
        "relation_ref": "relation:1",
        "membership_ref": "membership:human:1",
        "subject_ref": "human:1",
        "subject_type": "human",
        "relation": "viewer_of",
        "relation_type": "viewer_of",
        "resource_ref": "work_item:1",
        "valid_from": NOW,
        "source_kind": "direct_acl",
        "source_ref": "grant:1",
        "source_version": 1,
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    (
        {"membership_ref": "membership:human:2"},
        {"organization_ref": "org:missing"},
        {"scope_ref": "scope:missing"},
        {"role_revision": 3},
        {"principal_type": "service"},
        {"principal_ref": "service:1", "membership_ref": "membership:service:1", "principal_type": "service"},
        {"assignment_ref": None},
        {"grant_source_ref": None},
        {"valid_from": None},
        {"status": "revoked"},
        {"status": "custom"},
        {"version": 0},
    ),
)
async def test_bound_assignment_rejects_incomplete_or_mismatched_facts(
    authorization_sessions: async_sessionmaker[AsyncSession], changes: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError):
        await _insert(authorization_sessions, RoleAssignmentTable, **_assignment(**changes))


@pytest.mark.parametrize(
    "changes",
    (
        {"membership_ref": None},
        {"membership_ref": "membership:human:2"},
        {"subject_type": None},
        {"subject_type": "service"},
        {"organization_ref": "org:missing"},
        {"resource_ref": "work_item:missing"},
        {"relation_type": "assignee_of"},
        {"source_version": 0},
        {"source_ref": ""},
        {"valid_from": None},
        {"status": "revoked"},
        {"version": 0},
    ),
)
async def test_bound_relation_rejects_incomplete_or_mismatched_facts(
    authorization_sessions: async_sessionmaker[AsyncSession], changes: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError):
        await _insert(authorization_sessions, RelationTupleTable, **_relation(**changes))


async def test_role_revision_is_explicit_and_historical_assignment_can_be_regranted(
    authorization_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = authorization_sessions
    await _insert(sessions, RoleAssignmentTable, **_assignment())
    # A newly-published role version never changes the assignment's binding.
    async with sessions() as session:
        row = await session.scalar(select(RoleAssignmentTable))
        assert row is not None and row.role_revision == 1
    async with sessions.begin() as session:
        await session.execute(
            update(RoleAssignmentTable)
            .where(RoleAssignmentTable.assignment_ref == "assignment:1")
            .values(status="revoked", revoked_at=NOW + timedelta(hours=1), version=2)
        )
    await _insert(
        sessions,
        RoleAssignmentTable,
        **_assignment(assignment_ref="assignment:2", role_revision=2, grant_source_ref="grant:2"),
    )
    async with sessions() as session:
        assert len((await session.scalars(select(RoleAssignmentTable))).all()) == 2


async def test_current_binding_cannot_be_duplicated_but_relation_history_can_be_regranted(
    authorization_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = authorization_sessions
    await _insert(sessions, RoleAssignmentTable, **_assignment())
    with pytest.raises(IntegrityError):
        await _insert(sessions, RoleAssignmentTable, **_assignment(assignment_ref="assignment:2"))
    await _insert(sessions, RelationTupleTable, **_relation())
    with pytest.raises(IntegrityError):
        await _insert(sessions, RelationTupleTable, **_relation(relation_ref="relation:2"))
    async with sessions.begin() as session:
        await session.execute(
            update(RelationTupleTable)
            .where(RelationTupleTable.relation_ref == "relation:1")
            .values(status="revoked", revoked_at=NOW, version=2)
        )
    await _insert(sessions, RelationTupleTable, **_relation(relation_ref="relation:2", source_ref="grant:2"))


async def test_legacy_rows_remain_unmapped_and_do_not_require_invented_identity(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = persistence_sessions
    await _insert(
        sessions,
        RoleAssignmentTable,
        principal_ref="unmapped:human",
        role_code="old-role",
        scope_ref="old-scope",
    )
    await _insert(
        sessions,
        RelationTupleTable,
        subject_ref="unmapped:human",
        relation="owner_of",
        resource_ref="old-resource",
    )
    async with sessions() as session:
        assignment = await session.scalar(select(RoleAssignmentTable))
        relation = await session.scalar(select(RelationTupleTable))
        assert assignment is not None and assignment.organization_ref is None
        assert relation is not None and relation.organization_ref is None


@pytest.mark.parametrize(
    "changes",
    (
        {"organization_ref": "org:missing"},
        {"parent_scope_ref": "scope:2"},
        {"parent_scope_ref": "scope:missing"},
        {"version": 0},
        {"status": "custom"},
    ),
)
async def test_scope_parent_must_exist_in_same_organization_and_not_self(
    authorization_sessions: async_sessionmaker[AsyncSession], changes: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError):
        await _insert(
            authorization_sessions,
            AuthorizationScopeTable,
            **{"organization_ref": "org:1", "scope_ref": "scope:2", "scope_kind": "project", **changes},
        )


async def test_schema_uses_organization_names_for_new_columns() -> None:
    for model in (
        RoleDefinitionTable,
        RoleVersionTable,
        RoleVersionActionTable,
        AuthorizationScopeTable,
        AuthorizationResourceTable,
        RoleAssignmentTable,
        RelationTupleTable,
    ):
        assert "organization_ref" in model.__table__.columns
        assert "tenant_ref" not in model.__table__.columns
