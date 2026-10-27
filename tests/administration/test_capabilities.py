"""Employee onboarding is an explicit revision opt-in, never an upgrade side effect."""

import argparse
from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.administration.capabilities import (
    UPGRADE_OPERATION,
    UpgradeManagementCapabilitiesPlan,
    upgrade_management_capabilities,
)
from app.administration.errors import AdminDenied
from app.authz.loader import role_permissions_digest
from app.authz.management import (
    MANAGEMENT_ACTIONS,
    MANAGEMENT_POLICY_RELEASE,
    MANAGEMENT_ROLE_REF,
    ONBOARDING_ACTIONS,
    authorize_manager,
    management_role_contract,
)
from app.control.contracts import AdministrationError, OperationUnknown
from app.control.operations import operation_result
from app.models.administration import ManagementAuthorityTable
from app.models.authorization import (
    PermissionActionTable,
    RoleAssignmentTable,
    RoleVersionActionTable,
    RoleVersionTable,
)
from app.models.execution import TenantGuardTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import IdentityControlOperationTable, OrganizationMembershipTable
from app.storage.coordination import LockMode, lock_organization
from tests.support.authorization import ORG, PERSON
from tests.support.human_administration import AdministrationCase, administration_case
from tests.support.human_administration import signing_key as signing_key

OPERATOR = "local-os:capability-test"


def upgrade_plan(case: AdministrationCase, **changes: Any) -> UpgradeManagementCapabilitiesPlan:
    return UpgradeManagementCapabilitiesPlan.model_validate(
        {
            "environment": "test",
            "organization_ref": ORG,
            "authority_ref": "unused",
            "assignment_ref": case.assignment_ref,
            "expected_authority_version": case.authority_version,
            "expected_assignment_version": 2,
            "target_role_revision": 2,
            "reason": "Explicitly enable employee onboarding",
            **changes,
        }
    )


async def plan_for(case: AdministrationCase, **changes: Any) -> UpgradeManagementCapabilitiesPlan:
    current = await case.service.me(case.evidence)
    assert current.authority is not None
    return upgrade_plan(case, authority_ref=current.authority.authority_ref, **changes)


async def upgrade(
    sessions: async_sessionmaker[AsyncSession], plan: UpgradeManagementCapabilitiesPlan, key: str = "upgrade-v2"
) -> Any:
    return await upgrade_management_capabilities(sessions, plan, actor_ref=OPERATOR, idempotency_key=key)


async def assert_revision_one(sessions: async_sessionmaker[AsyncSession], assignment_ref: str) -> None:
    async with sessions() as session:
        assignment = await session.scalar(
            select(RoleAssignmentTable).where(RoleAssignmentTable.assignment_ref == assignment_ref)
        )
        assert assignment is not None and assignment.role_revision == 1
        assert await session.get(RoleVersionTable, (ORG, MANAGEMENT_ROLE_REF, 2)) is None
        assert not (
            await session.scalars(
                select(PermissionActionTable).where(PermissionActionTable.action_ref.in_(ONBOARDING_ACTIONS))
            )
        ).all()
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdentityControlOperationTable)
                .where(IdentityControlOperationTable.operation_type == UPGRADE_OPERATION)
            )
            == 0
        )


def test_revision_contract_preserves_v1_and_limits_new_capabilities() -> None:
    first = management_role_contract(1)
    second = management_role_contract(2)
    assert first is not None and second is not None
    assert first.policy_release_ref == MANAGEMENT_POLICY_RELEASE == "stage-administration:builtin-v1"
    assert (
        first.actions
        == MANAGEMENT_ACTIONS
        == frozenset(
            {
                "administration.people.read",
                "administration.assignment.grant",
                "administration.assignment.revoke",
                "administration.audit.read",
            }
        )
    )
    assert second.policy_release_ref == "stage-administration:builtin-v2"
    assert (
        second.actions - first.actions
        == ONBOARDING_ACTIONS
        == frozenset(
            {
                "administration.people.create",
                "administration.people.activation.reissue",
                "administration.people.activation.revoke",
            }
        )
    )
    for unknown in (None, 0, 3, True):
        assert management_role_contract(unknown) is None


async def test_upgrade_changes_one_current_binding_and_preserves_published_v1(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        plan = await plan_for(case)
        async with persistence_sessions.begin() as session:
            await lock_organization(session, ORG, mode=LockMode.SHARED)
            for action in ONBOARDING_ACTIONS:
                with pytest.raises(AdminDenied):
                    await authorize_manager(session, case.evidence, case.assignment_ref, action, datetime.now(UTC))
            original = await session.get(RoleVersionTable, (ORG, MANAGEMENT_ROLE_REF, 1))
            guard = await session.get(TenantGuardTable, ORG)
            assert original is not None and guard is not None
            original_digest, original_epoch = original.content_digest, guard.epoch
        result = await upgrade(persistence_sessions, plan)
        assert result.result["role_revision"] == 2
        assert result.result["version"] == result.result["assignment_version"] == 3
        assert result.result["organization_authz_epoch"] == original_epoch + 1
        assert result.result["actions"] == sorted(MANAGEMENT_ACTIONS | ONBOARDING_ACTIONS)
        async with persistence_sessions.begin() as session:
            await lock_organization(session, ORG, mode=LockMode.SHARED)
            for action in MANAGEMENT_ACTIONS | ONBOARDING_ACTIONS:
                authorized = await authorize_manager(
                    session, case.evidence, case.assignment_ref, action, datetime.now(UTC)
                )
                assert authorized.assignment.role_revision == 2
            first = await session.get(RoleVersionTable, (ORG, MANAGEMENT_ROLE_REF, 1))
            assert first is not None and first.content_digest == original_digest
            assert first.policy_release_ref == MANAGEMENT_POLICY_RELEASE
            assert (
                set(
                    await session.scalars(
                        select(RoleVersionActionTable.action_ref).where(
                            RoleVersionActionTable.role_ref == MANAGEMENT_ROLE_REF,
                            RoleVersionActionTable.role_revision == 1,
                        )
                    )
                )
                == MANAGEMENT_ACTIONS
            )
            journal = await session.scalar(
                select(IdentityControlOperationTable).where(
                    IdentityControlOperationTable.operation_type == UPGRADE_OPERATION
                )
            )
            assert journal is not None and journal.actor_kind == "bootstrap_operator"
            assert journal.before_json is not None and journal.before_json["role_revision"] == 1
            assert journal.after_json is not None and journal.after_json["role_revision"] == 2
            assert journal.after_json["reason"] == plan.reason
        replay = await upgrade(persistence_sessions, plan)
        assert replay == result
        recovered = await operation_result(
            persistence_sessions,
            environment="test",
            actor_ref=OPERATOR,
            operation_type=UPGRADE_OPERATION,
            idempotency_key="upgrade-v2",
        )
        assert recovered == result
        with pytest.raises(AdministrationError, match="idempotency_conflict"):
            await upgrade(persistence_sessions, plan.model_copy(update={"reason": "Different intent"}))
        with pytest.raises(AdministrationError, match="current_management_revision_one_required"):
            await upgrade(persistence_sessions, plan, key="new-key")
        with pytest.raises(AdministrationError, match="operation_not_found"):
            await operation_result(
                persistence_sessions,
                environment="test",
                actor_ref="local-os:other",
                operation_type=UPGRADE_OPERATION,
                idempotency_key="upgrade-v2",
            )


@pytest.mark.parametrize("field", ["expected_authority_version", "expected_assignment_version"])
async def test_stale_expected_version_does_not_install_or_grant(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey, field: str
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        with pytest.raises(AdministrationError, match="version_conflict"):
            await upgrade(persistence_sessions, await plan_for(case, **{field: 1}))
        await assert_revision_one(persistence_sessions, case.assignment_ref)


async def test_pending_authority_cannot_be_accepted_by_capability_upgrade(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key, activate=False) as case:
        with pytest.raises(AdministrationError, match="accepted_management_authority_required"):
            await upgrade(persistence_sessions, await plan_for(case))
        await assert_revision_one(persistence_sessions, case.assignment_ref)


@pytest.mark.parametrize(
    "mutation", ["authority-revoked", "assignment-suspended", "member-suspended", "principal-disabled"]
)
async def test_inactive_identity_or_assignment_never_receives_new_capabilities(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey, mutation: str
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        plan = await plan_for(case)
        async with persistence_sessions.begin() as session:
            if mutation == "authority-revoked":
                authority = await session.get(ManagementAuthorityTable, ORG)
                assert authority is not None
                authority.status = "revoked"
            elif mutation == "assignment-suspended":
                assignment = await session.scalar(
                    select(RoleAssignmentTable).where(RoleAssignmentTable.assignment_ref == case.assignment_ref)
                )
                assert assignment is not None
                assignment.status = "suspended"
            elif mutation == "member-suspended":
                member = await session.get(OrganizationMembershipTable, (ORG, "membership:alice"))
                assert member is not None
                member.status = "suspended"
            else:
                principal = await session.scalar(select(PrincipalTable).where(PrincipalTable.principal_ref == PERSON))
                assert principal is not None
                principal.disabled_at = datetime.now(UTC)
        with pytest.raises(AdministrationError):
            await upgrade(persistence_sessions, plan)
        await assert_revision_one(persistence_sessions, case.assignment_ref)


async def test_failed_journal_rolls_back_role_installation_and_all_version_bumps(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.administration import capabilities

    async with administration_case(persistence_sessions, signing_key) as case:
        plan = await plan_for(case)

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise SQLAlchemyError("synthetic unavailable journal")

        monkeypatch.setattr(capabilities, "append_journal", fail)
        with pytest.raises(OperationUnknown):
            await upgrade(persistence_sessions, plan)
        await assert_revision_one(persistence_sessions, case.assignment_ref)
        async with persistence_sessions() as session:
            authority = await session.get(ManagementAuthorityTable, ORG)
            assert authority is not None and authority.version == 2


async def test_rehashed_extra_action_cannot_expand_revision_two_contract(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        await upgrade(persistence_sessions, await plan_for(case))
        async with persistence_sessions.begin() as session:
            session.add(
                PermissionActionTable(
                    action_ref="administration.unbounded", resource_kind="administration", contract_version="1"
                )
            )
            await session.flush()
            session.add(
                RoleVersionActionTable(
                    organization_ref=ORG,
                    role_ref=MANAGEMENT_ROLE_REF,
                    role_revision=2,
                    action_ref="administration.unbounded",
                )
            )
            revision = await session.get(RoleVersionTable, (ORG, MANAGEMENT_ROLE_REF, 2))
            assert revision is not None
            revision.content_digest = role_permissions_digest(
                ORG,
                MANAGEMENT_ROLE_REF,
                2,
                revision.policy_release_ref,
                MANAGEMENT_ACTIONS | ONBOARDING_ACTIONS | {"administration.unbounded"},
            )
        async with persistence_sessions.begin() as session:
            await lock_organization(session, ORG, mode=LockMode.SHARED)
            with pytest.raises(AdminDenied):
                await authorize_manager(
                    session, case.evidence, case.assignment_ref, "administration.people.read", datetime.now(UTC)
                )


@pytest.mark.parametrize(
    "changes",
    [
        {"environment": "prod"},
        {"target_role_revision": 1},
        {"target_role_revision": 2.0},
        {"target_role_revision": 3},
        {"expected_authority_version": True},
        {"expected_assignment_version": "2"},
        {"reason": " "},
        {"reason": "line\nbreak"},
        {"unknown_capability": "all"},
    ],
)
def test_upgrade_plan_rejects_implicit_or_unbounded_input(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        UpgradeManagementCapabilitiesPlan.model_validate(
            {
                "environment": "test",
                "organization_ref": ORG,
                "authority_ref": "management:first",
                "assignment_ref": "assignment:first",
                "expected_authority_version": 2,
                "expected_assignment_version": 2,
                "target_role_revision": 2,
                "reason": "Explicit onboarding",
                **changes,
            }
        )


async def test_cli_rejects_production_before_reading_plan_or_opening_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.administration_capabilities import run

    monkeypatch.setenv("STAGE_ENV", "prod")
    with pytest.raises(AdministrationError, match="development_environment_required"):
        await run(argparse.Namespace(environment="prod", command="upgrade"))
