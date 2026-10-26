from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.context import AuthorizationContext
from app.authz.decision import AuthorizationEngine
from app.authz.loader import DatabaseAuthorizationLoader, role_permissions_digest
from app.authz.permissions import BUILTIN_POLICY_RELEASE_ID
from app.core.enums import DecisionOutcome
from app.execution.contracts import ExecutionContext
from app.models.authorization import (
    PermissionActionTable,
    RoleAssignmentTable,
    RoleVersionTable,
)
from app.models.identity_foundation import OrganizationMembershipTable
from app.models.work import WorkItemTable
from app.storage.coordination import LockMode, lock_organization
from tests.support.authorization import (
    NOW,
    ORG,
    PERSON,
    TARGET,
    execution_context,
    seed_authorization,
)


async def load_context(
    sessions: async_sessionmaker[AsyncSession],
    context: ExecutionContext,
    *,
    action: str = "task.assign",
    target_ref: str = TARGET,
    expected_version: int | None = 1,
) -> AuthorizationContext:
    loader = DatabaseAuthorizationLoader()
    async with sessions.begin() as session:
        await loader.acquire_locks(session, context)
        await lock_organization(session, context.tenant_ref, mode=LockMode.EXCLUSIVE)
        return await loader.load(
            session,
            context,
            action=action,
            target_ref=target_ref,
            expected_version=expected_version,
            now=NOW,
        )


async def test_database_loader_builds_authorization_from_bound_facts(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_authorization(persistence_sessions)
    facts = await load_context(persistence_sessions, execution_context())

    assert facts.organization_ref == ORG
    assert facts.assignment is not None
    assert facts.assignment.assignment_ref == "assignment:engineering"
    assert facts.assignment.scope_refs == ("scope:engineering",)
    assert facts.resource_scope_refs == ("scope:engineering",)
    assert facts.attributes.object_version == 1
    assert facts.relations[0].relation == "owner_of"
    assert AuthorizationEngine().decide(facts).outcome is DecisionOutcome.ALLOWED


async def test_selected_assignment_is_required_and_assignments_are_not_implicitly_merged(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_authorization(persistence_sessions, second_assignment=True)
    engineering = await load_context(persistence_sessions, execution_context(assignment_ref="assignment:engineering"))
    finance = await load_context(persistence_sessions, execution_context(assignment_ref="assignment:finance"))

    assert AuthorizationEngine().decide(engineering).outcome is DecisionOutcome.ALLOWED
    denied = AuthorizationEngine().decide(finance)
    assert denied.outcome is DecisionOutcome.DENIED
    assert denied.failed_constraints
    missing = await load_context(persistence_sessions, execution_context(assignment_ref=None))
    assert missing.assignment is None
    assert AuthorizationEngine().decide(missing).outcome is DecisionOutcome.DENIED


@pytest.mark.parametrize(
    ("resource_scope", "assignment_scope", "with_relation", "reason"),
    [
        ("scope:finance", "scope:engineering", True, "assignment.missing"),
        ("scope:engineering", "scope:engineering", False, "relation.missing:owner_of"),
    ],
)
async def test_scope_and_relation_mismatch_fail_closed(
    persistence_sessions: async_sessionmaker[AsyncSession],
    resource_scope: str,
    assignment_scope: str,
    with_relation: bool,
    reason: str,
) -> None:
    await seed_authorization(
        persistence_sessions,
        resource_scope=resource_scope,
        assignment_scope=assignment_scope,
        with_relation=with_relation,
    )
    facts = await load_context(persistence_sessions, execution_context())
    decision = AuthorizationEngine().decide(facts)
    assert decision.outcome is DecisionOutcome.DENIED
    assert reason in decision.failed_constraints


async def test_role_revision_is_pinned_and_revocation_is_immediate(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_authorization(persistence_sessions)
    async with persistence_sessions.begin() as session:
        session.add(
            RoleVersionTable(
                organization_ref=ORG,
                role_ref="role:engineering",
                role_revision=2,
                content_digest=role_permissions_digest(ORG, "role:engineering", 2, BUILTIN_POLICY_RELEASE_ID, ()),
                policy_release_ref=BUILTIN_POLICY_RELEASE_ID,
                status="active",
                version=1,
            )
        )

    pinned = await load_context(persistence_sessions, execution_context())
    assert AuthorizationEngine().decide(pinned).outcome is DecisionOutcome.ALLOWED

    # A new role revision must not inherit v1 actions by table-order accident.
    # The assignment points at v2, whose action set is empty in this fixture.
    async with persistence_sessions.begin() as session:
        assignment = await session.scalar(
            select(RoleAssignmentTable).where(RoleAssignmentTable.assignment_ref == "assignment:engineering")
        )
        assert assignment is not None
        assignment.role_revision = 2
        assignment.version += 1
    switched = await load_context(persistence_sessions, execution_context())
    assert switched.assignment is None
    assert AuthorizationEngine().decide(switched).outcome is DecisionOutcome.DENIED

    async with persistence_sessions.begin() as session:
        assignment = await session.scalar(
            select(RoleAssignmentTable).where(RoleAssignmentTable.assignment_ref == "assignment:engineering")
        )
        assert assignment is not None
        assignment.role_revision = 1
        assignment.version += 1

    async with persistence_sessions.begin() as session:
        role = await session.scalar(
            select(RoleVersionTable).where(
                RoleVersionTable.organization_ref == ORG,
                RoleVersionTable.role_ref == "role:engineering",
                RoleVersionTable.role_revision == 1,
            )
        )
        assert role is not None
        role.status = "revoked"
        role.revoked_at = NOW
        role.version += 1
    revoked = await load_context(persistence_sessions, execution_context())
    assert revoked.assignment is None
    assert AuthorizationEngine().decide(revoked).outcome is DecisionOutcome.DENIED


async def test_membership_revocation_and_forged_organization_are_denied(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_authorization(persistence_sessions)
    async with persistence_sessions.begin() as session:
        membership = await session.scalar(
            select(OrganizationMembershipTable).where(
                OrganizationMembershipTable.organization_ref == ORG,
                OrganizationMembershipTable.membership_ref == "membership:alice",
            )
        )
        assert membership is not None
        membership.status = "revoked"
        membership.ended_at = NOW
    revoked = await load_context(persistence_sessions, execution_context())
    assert revoked.assignment is None
    assert AuthorizationEngine().decide(revoked).outcome is DecisionOutcome.DENIED

    forged = await load_context(persistence_sessions, replace(execution_context(), tenant_ref="org:forged"))
    assert forged.resource_organization_ref is None
    assert AuthorizationEngine().decide(forged).outcome is DecisionOutcome.DENIED


async def test_rejoining_does_not_revive_assignment_from_an_ended_membership(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_authorization(persistence_sessions)
    async with persistence_sessions.begin() as session:
        old_membership = await session.scalar(
            select(OrganizationMembershipTable).where(
                OrganizationMembershipTable.organization_ref == ORG,
                OrganizationMembershipTable.membership_ref == "membership:alice",
            )
        )
        assert old_membership is not None
        old_membership.status = "left"
        old_membership.ended_at = NOW - timedelta(minutes=2)
        old_membership.version += 1
        await session.flush()
        session.add(
            OrganizationMembershipTable(
                organization_ref=ORG,
                membership_ref="membership:alice:rejoined",
                principal_ref=PERSON,
                principal_type="human",
                source_ref="invitation:rejoin",
                status="active",
                valid_from=NOW - timedelta(minutes=1),
                version=1,
            )
        )

    old_assignment = await load_context(persistence_sessions, execution_context())
    assert AuthorizationEngine().decide(old_assignment).outcome is DecisionOutcome.DENIED

    # Granting a fresh assignment is not enough to revive the old tenure's
    # resource ACL. The old owner relation remains tied to membership:alice.
    async with persistence_sessions.begin() as session:
        old_assignment_row = await session.scalar(
            select(RoleAssignmentTable).where(RoleAssignmentTable.assignment_ref == "assignment:engineering")
        )
        assert old_assignment_row is not None
        old_assignment_row.status = "revoked"
        old_assignment_row.revoked_at = NOW
        old_assignment_row.version += 1
        session.add(
            RoleAssignmentTable(
                principal_ref=PERSON,
                role_code="department-owner",
                scope_ref="scope:engineering",
                organization_ref=ORG,
                assignment_ref="assignment:engineering:rejoined",
                membership_ref="membership:alice:rejoined",
                principal_type="human",
                role_ref="role:engineering",
                role_revision=1,
                grant_source_ref="invitation:rejoin",
                valid_from=NOW - timedelta(minutes=1),
                status="active",
                version=1,
            )
        )
    fresh_assignment = await load_context(
        persistence_sessions, execution_context(assignment_ref="assignment:engineering:rejoined")
    )
    assert fresh_assignment.assignment is not None
    assert fresh_assignment.relations == ()
    assert AuthorizationEngine().decide(fresh_assignment).outcome is DecisionOutcome.DENIED


async def test_unknown_or_inactive_actions_cannot_be_granted_by_database_rows(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed_authorization(persistence_sessions)
    async with persistence_sessions.begin() as session:
        action = await session.get(PermissionActionTable, "task.assign")
        assert action is not None
        action.status = "revoked"
        action.version += 1
    denied = await load_context(persistence_sessions, execution_context())
    assert AuthorizationEngine().decide(denied).outcome is DecisionOutcome.DENIED


@pytest.mark.parametrize(
    ("expected_version", "lifecycle", "classification", "reason"),
    [
        (2, "open", "internal", "object.version"),
        (1, "closed", "internal", "resource.state"),
        (1, "open", "restricted", "resource.classification"),
    ],
)
async def test_loader_exposes_current_object_attributes_to_abac(
    persistence_sessions: async_sessionmaker[AsyncSession],
    expected_version: int,
    lifecycle: str,
    classification: str,
    reason: str,
) -> None:
    await seed_authorization(persistence_sessions)
    async with persistence_sessions.begin() as session:
        item = await session.get(WorkItemTable, 1)
        assert item is not None
        item.lifecycle = lifecycle
        item.classification = classification
    facts = await load_context(persistence_sessions, execution_context(), expected_version=expected_version)
    decision = AuthorizationEngine().decide(facts)
    assert decision.outcome is DecisionOutcome.DENIED
    assert any(reason in constraint for constraint in decision.failed_constraints)
