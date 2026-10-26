from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.decision import AuthorizationEngine
from app.authz.loader import DatabaseAuthorizationLoader, role_permissions_digest
from app.authz.permissions import BUILTIN_POLICY_RELEASE_ID
from app.core.enums import DecisionOutcome, PrincipalType
from app.execution.contracts import ExecutionContext
from app.execution.errors import AuthorizationDenied
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
from app.storage.coordination import LockMode, lock_organization

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
ORG = "org:stage"
SUBJECT = "principal:alice"
WORK = "work_item:1"


def execution_context(*, assignment_ref: str | None = "assignment:alice") -> ExecutionContext:
    return ExecutionContext(
        tenant_ref=ORG,
        actor_ref=SUBJECT,
        request_id="request:loader",
        principal_type=PrincipalType.HUMAN,
        credential_expires_at=NOW + timedelta(hours=1),
        effective_subject_ref=SUBJECT,
        assignment_ref=assignment_ref,
    )


async def seed_authorization(session: AsyncSession, *, relation: str = "owner_of") -> None:
    session.add_all(
        [
            OrganizationRootTable(
                organization_ref=ORG,
                deployment_slot=1,
                display_name="Stage",
                status="active",
                version=1,
            ),
        ]
    )
    await session.flush()
    session.add(TenantGuardTable(tenant_ref=ORG, epoch=1))
    await session.flush()
    session.add_all(
        [
            PrincipalTable(
                principal_ref=SUBJECT,
                principal_type="human",
                status="active",
                version=1,
                identity_epoch=0,
                recovery_epoch=0,
            ),
            OrganizationMembershipTable(
                organization_ref=ORG,
                membership_ref="membership:alice",
                principal_ref=SUBJECT,
                principal_type="human",
                source_ref="test",
                status="active",
                valid_from=NOW - timedelta(days=1),
                version=1,
            ),
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
        ]
    )
    await session.flush()
    session.add_all(
        [
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
            ),
            AuthorizationScopeTable(
                organization_ref=ORG,
                scope_ref="scope:engineering",
                scope_kind="department",
                status="active",
                version=1,
            ),
        ]
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
    await session.flush()
    session.add(
        AuthorizationResourceTable(
            organization_ref=ORG,
            resource_ref=WORK,
            resource_kind="work_item",
            scope_ref="scope:engineering",
            status="active",
            version=1,
        )
    )
    await session.flush()
    session.add_all(
        [
            RoleAssignmentTable(
                principal_ref=SUBJECT,
                role_code="department-owner",
                scope_ref="scope:engineering",
                organization_ref=ORG,
                assignment_ref="assignment:alice",
                grant_source_ref="test:grant",
                membership_ref="membership:alice",
                principal_type="human",
                role_ref="role:engineering",
                role_revision=1,
                status="active",
                valid_from=NOW - timedelta(days=1),
                version=1,
            ),
            RelationTupleTable(
                subject_ref=SUBJECT,
                relation=relation,
                resource_ref=WORK,
                organization_ref=ORG,
                relation_ref="relation:alice",
                membership_ref="membership:alice",
                subject_type="human",
                relation_type=relation,
                source_kind="direct_acl",
                source_ref="test",
                source_version=1,
                status="active",
                valid_from=NOW - timedelta(days=1),
                version=1,
            ),
            WorkItemTable(
                id=1,
                title="Delivery",
                lifecycle="open",
                classification="internal",
                scope_ref="scope:engineering",
                version=7,
            ),
        ]
    )
    await session.flush()


@pytest.mark.asyncio
async def test_loader_reads_bound_role_scope_relation_and_work_item(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with persistence_sessions() as session:
        async with session.begin():
            await seed_authorization(session)
            loader = DatabaseAuthorizationLoader()
            await loader.acquire_locks(session, execution_context())
            await lock_organization(session, ORG, mode=LockMode.EXCLUSIVE)
            facts = await loader.load(
                session,
                execution_context(),
                action="task.assign",
                target_ref=WORK,
                expected_version=7,
                now=NOW,
            )
    assert facts.organization_ref == ORG
    assert facts.assignment is not None
    assert facts.assignment.assignment_ref == "assignment:alice"
    assert facts.assignment.role_code == "department-owner"
    assert facts.resource_scope_refs == ("scope:engineering",)
    assert facts.attributes.object_version == 7
    assert facts.relations[0].relation == "owner_of"
    assert AuthorizationEngine().decide(facts).outcome is DecisionOutcome.ALLOWED


@pytest.mark.asyncio
async def test_loader_fails_closed_for_stale_version_and_missing_selection(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with persistence_sessions() as session:
        async with session.begin():
            await seed_authorization(session)
            loader = DatabaseAuthorizationLoader()
            await loader.acquire_locks(session, execution_context())
            await lock_organization(session, ORG, mode=LockMode.EXCLUSIVE)
            stale = await loader.load(
                session,
                execution_context(),
                action="task.assign",
                target_ref=WORK,
                expected_version=6,
                now=NOW,
            )
            missing = await loader.load(
                session,
                execution_context(assignment_ref=None),
                action="task.assign",
                target_ref=WORK,
                expected_version=7,
                now=NOW,
            )
    assert stale.assignment is not None
    assert stale.attributes.object_version == 7
    assert stale.attributes.expected_object_version == 6
    assert missing.assignment is None
    assert missing.resource_organization_ref is None


@pytest.mark.asyncio
async def test_loader_does_not_turn_an_unrelated_relation_into_authority(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with persistence_sessions() as session:
        async with session.begin():
            await seed_authorization(session, relation="viewer_of")
            loader = DatabaseAuthorizationLoader()
            await loader.acquire_locks(session, execution_context())
            await lock_organization(session, ORG, mode=LockMode.EXCLUSIVE)
            facts = await loader.load(
                session,
                execution_context(),
                action="task.assign",
                target_ref=WORK,
                expected_version=7,
                now=NOW,
            )
    assert facts.assignment is not None
    assert [item.relation for item in facts.relations] == ["viewer_of"]
    assert AuthorizationEngine().decide(facts).outcome is DecisionOutcome.DENIED


@pytest.mark.asyncio
async def test_loader_requires_transaction_lock_coordinator(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with persistence_sessions() as session, session.begin():
        await seed_authorization(session)
        with pytest.raises(AuthorizationDenied):
            await DatabaseAuthorizationLoader().load(
                session,
                execution_context(),
                action="task.assign",
                target_ref=WORK,
                expected_version=7,
                now=NOW,
            )


@pytest.mark.asyncio
async def test_membership_expiry_is_preserved_for_final_transaction_check(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    expires_at = NOW + timedelta(seconds=1)
    async with persistence_sessions() as session, session.begin():
        await seed_authorization(session)
        await session.execute(update(OrganizationMembershipTable).values(expires_at=expires_at))
        loader = DatabaseAuthorizationLoader()
        await loader.acquire_locks(session, execution_context())
        await lock_organization(session, ORG, mode=LockMode.EXCLUSIVE)
        facts = await loader.load(
            session,
            execution_context(),
            action="task.assign",
            target_ref=WORK,
            expected_version=7,
            now=NOW,
        )
        assert AuthorizationEngine().decide(facts).outcome is DecisionOutcome.ALLOWED
        assert AuthorizationEngine().decide(replace(facts, now=expires_at)).outcome is DecisionOutcome.EXPIRED


@pytest.mark.asyncio
async def test_role_action_content_must_match_its_published_digest(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with persistence_sessions() as session, session.begin():
        await seed_authorization(session)
        await session.execute(update(RoleVersionTable).values(content_digest="0" * 64))
        loader = DatabaseAuthorizationLoader()
        await loader.acquire_locks(session, execution_context())
        await lock_organization(session, ORG, mode=LockMode.EXCLUSIVE)
        facts = await loader.load(
            session,
            execution_context(),
            action="task.assign",
            target_ref=WORK,
            expected_version=7,
            now=NOW,
        )
        assert facts.assignment is None
        assert AuthorizationEngine().decide(facts).outcome is DecisionOutcome.DENIED


@pytest.mark.asyncio
async def test_coordinator_marker_cannot_be_reused_after_transaction_end(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    loader = DatabaseAuthorizationLoader()
    async with persistence_sessions() as session:
        async with session.begin():
            await seed_authorization(session)
            await loader.acquire_locks(session, execution_context())
            await lock_organization(session, ORG, mode=LockMode.EXCLUSIVE)
        async with session.begin():
            with pytest.raises(AuthorizationDenied):
                await loader.load(
                    session,
                    execution_context(),
                    action="task.assign",
                    target_ref=WORK,
                    expected_version=7,
                    now=NOW,
                )


@pytest.mark.asyncio
async def test_published_role_rejects_unknown_extra_capability_even_with_matching_digest(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    actions = ("task.assign", "task.future-unregistered")
    async with persistence_sessions() as session, session.begin():
        await seed_authorization(session)
        session.add(
            PermissionActionTable(
                action_ref=actions[1], resource_kind="work_item", contract_version="1", status="active", version=1
            )
        )
        await session.flush()
        session.add(
            RoleVersionActionTable(
                organization_ref=ORG, role_ref="role:engineering", role_revision=1, action_ref=actions[1]
            )
        )
        await session.flush()
        await session.execute(
            update(RoleVersionTable).values(
                content_digest=role_permissions_digest(ORG, "role:engineering", 1, BUILTIN_POLICY_RELEASE_ID, actions)
            )
        )
        loader = DatabaseAuthorizationLoader()
        await loader.acquire_locks(session, execution_context())
        await lock_organization(session, ORG, mode=LockMode.EXCLUSIVE)
        facts = await loader.load(
            session,
            execution_context(),
            action="task.assign",
            target_ref=WORK,
            expected_version=7,
            now=NOW,
        )
        assert AuthorizationEngine().decide(facts).outcome is DecisionOutcome.DENIED


@pytest.mark.asyncio
async def test_fact_versions_capture_actual_rows_ancestors_and_set_epoch(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with persistence_sessions() as session, session.begin():
        await seed_authorization(session)
        # Distinct revisions make it possible to detect accidental constants
        # or use of the role revision where the row's lifecycle version belongs.
        await session.execute(update(OrganizationRootTable).values(version=2))
        await session.execute(update(PrincipalTable).values(version=3, identity_epoch=4))
        await session.execute(update(OrganizationMembershipTable).values(version=5))
        await session.execute(update(RoleAssignmentTable).values(version=6))
        await session.execute(update(RoleDefinitionTable).values(version=8))
        await session.execute(update(RoleVersionTable).values(version=9))
        await session.execute(update(AuthorizationResourceTable).values(version=10))
        await session.execute(update(AuthorizationScopeTable).values(version=11))
        await session.execute(update(RelationTupleTable).values(version=12))
        await session.execute(update(PermissionActionTable).values(version=13))
        await session.execute(update(TenantGuardTable).values(epoch=14))
        session.add(
            AuthorizationScopeTable(
                organization_ref=ORG, scope_ref="scope:organization", scope_kind="organization", version=15
            )
        )
        await session.flush()
        await session.execute(
            update(AuthorizationScopeTable)
            .where(AuthorizationScopeTable.scope_ref == "scope:engineering")
            .values(parent_scope_ref="scope:organization")
        )
        loader = DatabaseAuthorizationLoader()
        context = execution_context()
        await loader.acquire_locks(session, context)
        await lock_organization(session, context.tenant_ref, mode=LockMode.EXCLUSIVE)
        facts = await loader.load(session, context, action="task.assign", target_ref=WORK, expected_version=7, now=NOW)
        expected = {
            ("organization_root", ORG, 2),
            ("principal", SUBJECT, 3),
            ("principal.identity_epoch", SUBJECT, 4),
            ("tenant_membership", "membership:alice", 5),
            ("role_assignment", "assignment:alice", 6),
            ("work_item", WORK, 7),
            ("role_definition", "role:engineering", 8),
            ("role_version", "role:engineering@1", 9),
            ("authorization_resource", WORK, 10),
            ("authorization_scope", "scope:engineering", 11),
            ("relation_tuple", "relation:alice", 12),
            ("permission_action", "task.assign", 13),
            ("execution_tenant_guard.epoch", ORG, 14),
            ("authorization_scope", "scope:organization", 15),
        }
        assert facts.fact_versions == tuple(sorted(expected))
        assert facts.now == NOW
        assert facts.resource_scope_refs == ("scope:engineering", "scope:organization")
        assert AuthorizationEngine().decide(facts).outcome is DecisionOutcome.ALLOWED
        # Stored evidence remains immutable while a later load observes the
        # current facts, rather than reusing the previous snapshot as a token.
        await session.execute(update(OrganizationMembershipTable).values(version=16))
        await session.execute(update(TenantGuardTable).values(epoch=17))
        latest = await loader.load(session, context, action="task.assign", target_ref=WORK, expected_version=7, now=NOW)
        assert facts.fact_versions == tuple(sorted(expected))
        assert ("tenant_membership", "membership:alice", 16) in latest.fact_versions
        assert ("execution_tenant_guard.epoch", ORG, 17) in latest.fact_versions
