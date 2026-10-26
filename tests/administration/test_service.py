"""The human management boundary uses current authentication and bounded grants."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.administration.contracts import ActivationRequest
from app.administration.errors import AdminCommitUnknown, AdminConflict, AdminDenied, AdminNotFound
from app.authz.loader import role_permissions_digest
from app.authz.management import MANAGEMENT_ROLE_REF
from app.authz.permissions import BUILTIN_POLICY_RELEASE_ID
from app.execution.errors import AuthorizationDenied
from app.execution.storage import UnitOfWork
from app.identity.authentication import AuthenticationAssuranceRequired, AuthenticationRejected
from app.identity.service import IdentityDenied
from app.models.administration import ManagementAuthorityTable
from app.models.authorization import (
    AuthorizationScopeTable,
    RoleAssignmentTable,
    RoleVersionActionTable,
    RoleVersionTable,
)
from app.models.identity import PrincipalTable
from app.models.identity_foundation import (
    IdentityControlOperationTable,
    OrganizationHumanAuthenticationPolicyTable,
    OrganizationMembershipTable,
)
from app.providers.human import create_human_kernel, human_execution_context
from tests.support.authorization import ORG, PERSON
from tests.support.human_administration import BOB, BOB_MEMBERSHIP, administration_case
from tests.support.human_administration import signing_key as signing_key


async def test_nomination_requires_personal_acceptance_and_exact_replay(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key, activate=False) as case:
        before = await case.service.me(case.evidence)
        assert before.authority is not None and before.authority.status == "pending"
        with pytest.raises(AdminDenied):
            await case.service.people(case.evidence, assignment_ref=case.assignment_ref, limit=20, after=None)
        bob = await case.authenticate(subject="subject-bob")
        assert (await case.service.me(bob)).authority is None
        with pytest.raises(AdminDenied):
            await case.service.activate(
                bob,
                request=ActivationRequest(expected_authority_version=1),
                request_id="request:wrong-person",
                idempotency_key="wrong-person",
            )
        with pytest.raises(AdminConflict):
            await case.service.activate(
                case.evidence,
                request=ActivationRequest(expected_authority_version=2),
                request_id="request:wrong-version",
                idempotency_key="wrong-version",
            )
        accepted = await case.activate()
        assert accepted.operation_type == "activation" and accepted.result.status == "active"
        replay = await case.service.activate(
            case.evidence,
            request=ActivationRequest(expected_authority_version=1),
            request_id="request:retry",
            idempotency_key="accept-first-administrator",
        )
        assert replay.operation_ref == accepted.operation_ref and replay.replayed
        current = await case.service.me(case.evidence)
        assert current.authority is not None and current.authority.status == "active"
        assert current.authority.accepted_at is not None and current.authority.version == 2


async def test_existing_person_grant_revoke_results_and_durable_human_audit(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        page = await case.service.people(case.evidence, assignment_ref=case.assignment_ref, limit=1, after=None)
        assert len(page.items) == 1 and page.next_after is not None
        next_page = await case.service.people(
            case.evidence, assignment_ref=case.assignment_ref, limit=1, after=page.next_after
        )
        assert {page.items[0].principal_ref, next_page.items[0].principal_ref} == {PERSON, BOB}
        person = await case.service.person(case.evidence, assignment_ref=case.assignment_ref, principal_ref=BOB)
        assert person.membership_ref == BOB_MEMBERSHIP and person.membership_version == 1
        request = case.grant_request()
        granted = await case.grant(request=request)
        assert granted.result.principal_ref == BOB and granted.result.status == "active"
        assert granted.result.version == 1 and not granted.replayed
        replay = await case.grant(request=request)
        assert replay.operation_ref == granted.operation_ref and replay.replayed
        assignments = await case.service.assignments(
            case.evidence, assignment_ref=case.assignment_ref, principal_ref=BOB, limit=20, after=None
        )
        assert [item.assignment_ref for item in assignments.items] == [granted.result.assignment_ref]
        recovered = await case.service.operation(
            case.evidence, assignment_ref=case.assignment_ref, idempotency_key="grant-bob"
        )
        assert recovered.operation_ref == granted.operation_ref
        revoked = await case.revoke(granted.result.assignment_ref)
        assert revoked.result.status == "revoked" and revoked.result.version == 2
        revoke_replay = await case.revoke(granted.result.assignment_ref)
        assert revoke_replay.operation_ref == revoked.operation_ref and revoke_replay.replayed
        history = await case.service.audit(case.evidence, assignment_ref=case.assignment_ref, limit=20, after=None)
        assert {item.operation_type for item in history.items} == {
            "activation",
            "assignment.grant",
            "assignment.revoke",
        }
        assert len(history.items) == 3 and all(item.actor_ref == PERSON for item in history.items)
        async with persistence_sessions() as session:
            rows = tuple(
                await session.scalars(
                    select(IdentityControlOperationTable).where(IdentityControlOperationTable.actor_kind == "human")
                )
            )
            assert len(rows) == 3
            encoded = json.dumps([row.authentication_json for row in rows])
            assert "subject-1" not in encoded and "Bearer" not in encoded
            assert all(row.before_json is not None or row.after_json is not None for row in rows)


@pytest.mark.parametrize("change", ["reason", "scope", "version", "target", "operation"])
async def test_one_key_cannot_represent_a_different_intent(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey, change: str
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        request = case.grant_request()
        granted = await case.grant(request=request)
        changes: dict[str, dict[str, Any]] = {
            "reason": {"reason": "A different intended change"},
            "scope": {"scope_ref": "scope:finance"},
            "version": {"expected_membership_version": 2},
        }
        with pytest.raises((AdminConflict, AdminDenied)):
            if change == "operation":
                await case.revoke(granted.result.assignment_ref, key="grant-bob")
            else:
                await case.grant(
                    request=request.model_copy(update=changes.get(change, {})),
                    principal_ref=PERSON if change == "target" else BOB,
                )
        async with persistence_sessions() as session:
            row = await session.scalar(
                select(RoleAssignmentTable).where(RoleAssignmentTable.assignment_ref == granted.result.assignment_ref)
            )
            assert row is not None and row.status == "active" and row.version == 1


@pytest.mark.parametrize(
    "case_name",
    ["self", "management-role", "scope", "revision", "expired", "duration", "member-version", "authority-version"],
)
async def test_grants_are_explicit_finite_and_within_the_registered_rule(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey, case_name: str
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        variants: dict[str, dict[str, Any]] = {
            "self": {"membership_ref": "membership:alice"},
            "management-role": {"role_ref": MANAGEMENT_ROLE_REF},
            "scope": {"scope_ref": "scope:finance"},
            "revision": {"role_revision": 2},
            "expired": {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
            "duration": {"expires_at": datetime.now(UTC) + timedelta(days=8)},
            "member-version": {"expected_membership_version": 2},
            "authority-version": {"expected_authority_version": 1},
        }
        changes = variants[case_name]
        with pytest.raises((AdminDenied, AdminConflict)):
            await case.grant(
                principal_ref=PERSON if case_name == "self" else BOB, request=case.grant_request(**changes)
            )
        async with persistence_sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(RoleAssignmentTable)
                    .where(RoleAssignmentTable.principal_ref == BOB)
                )
                == 0
            )


@pytest.mark.parametrize(
    "mutation",
    [
        "principal",
        "membership",
        "member-expiry",
        "member-version",
        "scope",
        "role-digest",
        "role-status",
        "republished-content",
    ],
)
async def test_target_and_published_grant_facts_are_current_at_submission(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey, mutation: str
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        request = case.grant_request()
        async with persistence_sessions.begin() as session:
            if mutation == "principal":
                principal = await session.scalar(select(PrincipalTable).where(PrincipalTable.principal_ref == BOB))
                assert principal is not None
                principal.status = "suspended"
            elif mutation.startswith("member"):
                member = await session.get(OrganizationMembershipTable, (ORG, BOB_MEMBERSHIP))
                assert member is not None
                if mutation == "membership":
                    member.status = "suspended"
                elif mutation == "member-expiry":
                    member.expires_at = datetime.now(UTC) + timedelta(hours=1)
                else:
                    member.version += 1
            elif mutation == "scope":
                scope = await session.get(AuthorizationScopeTable, (ORG, "scope:engineering"))
                assert scope is not None
                scope.status = "revoked"
            else:
                role = await session.get(RoleVersionTable, (ORG, "role:engineering", 1))
                assert role is not None
                if mutation == "republished-content":
                    action = await session.get(RoleVersionActionTable, (ORG, "role:engineering", 1, "task.assign"))
                    assert action is not None
                    await session.delete(action)
                    role.content_digest = role_permissions_digest(
                        ORG, "role:engineering", 1, BUILTIN_POLICY_RELEASE_ID, ("task.view", "task.metadata.update")
                    )
                elif mutation == "role-digest":
                    role.content_digest = "f" * 64
                else:
                    role.status = "suspended"
        with pytest.raises((AdminDenied, AdminConflict)):
            await case.grant(request=request)


async def test_revoke_cannot_touch_self_management_or_a_stale_assignment_version(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        for assignment in (case.assignment_ref, "assignment:engineering"):
            with pytest.raises(AdminDenied):
                await case.revoke(assignment, principal_ref=PERSON)
        granted = await case.grant()
        with pytest.raises(AdminConflict):
            await case.revoke(granted.result.assignment_ref, expected_version=2)
        assert (await case.revoke(granted.result.assignment_ref)).result.status == "revoked"


@pytest.mark.parametrize("mode", ["disabled", "old-authentication", "insufficient-policy-methods"])
async def test_management_requires_its_own_recent_password_and_totp_boundary(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey, mode: str
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        if mode != "old-authentication":
            async with persistence_sessions.begin() as session:
                policy = await session.get(OrganizationHumanAuthenticationPolicyTable, ORG)
                assert policy is not None
                policy.version += 1
                if mode == "disabled":
                    policy.mode, policy.rules_json, policy.max_auth_age_seconds = "disabled", [], None
                else:
                    policy.rules_json = [{**rule, "required_amr": ["pwd"]} for rule in policy.rules_json]
        claims: dict[str, Any] = (
            {"auth_time": int((datetime.now(UTC) - timedelta(seconds=301)).timestamp())}
            if mode == "old-authentication"
            else {}
        )
        evidence = await case.authenticate(**claims)
        error = AuthenticationAssuranceRequired if mode == "old-authentication" else AdminDenied
        with pytest.raises(error):
            await case.service.me(evidence)
        with pytest.raises(error):
            await case.service.people(evidence, assignment_ref=case.assignment_ref, limit=20, after=None)


@pytest.mark.parametrize("entry", ["people", "person", "assignments", "audit", "operation", "replay"])
async def test_revoked_management_assignment_blocks_reads_recovery_and_replay(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey, entry: str
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        request = case.grant_request()
        await case.grant(request=request)
        async with persistence_sessions.begin() as session:
            assignment = await session.scalar(
                select(RoleAssignmentTable).where(RoleAssignmentTable.assignment_ref == case.assignment_ref)
            )
            assert assignment is not None
            assignment.status, assignment.revoked_at, assignment.version = (
                "revoked",
                datetime.now(UTC),
                assignment.version + 1,
            )
        with pytest.raises(AdminDenied):
            if entry == "replay":
                await case.grant(request=request)
            elif entry == "operation":
                await case.service.operation(
                    case.evidence, assignment_ref=case.assignment_ref, idempotency_key="grant-bob"
                )
            elif entry == "person":
                await case.service.person(case.evidence, assignment_ref=case.assignment_ref, principal_ref=BOB)
            elif entry == "assignments":
                await case.service.assignments(
                    case.evidence, assignment_ref=case.assignment_ref, principal_ref=BOB, limit=20, after=None
                )
            else:
                await getattr(case.service, entry)(
                    case.evidence, assignment_ref=case.assignment_ref, limit=20, after=None
                )


async def test_another_person_cannot_borrow_the_management_assignment_or_results(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        await case.grant()
        bob = await case.authenticate(subject="subject-bob")
        with pytest.raises(AdminDenied):
            await case.service.people(bob, assignment_ref=case.assignment_ref, limit=20, after=None)
        with pytest.raises(AdminDenied):
            await case.service.operation(bob, assignment_ref=case.assignment_ref, idempotency_key="grant-bob")
        with pytest.raises(AdminNotFound):
            await case.service.operation(case.evidence, assignment_ref=case.assignment_ref, idempotency_key="missing")


async def test_expired_nomination_cannot_be_accepted(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key, activate=False) as case:
        async with persistence_sessions.begin() as session:
            authority = await session.get(ManagementAuthorityTable, ORG)
            assert authority is not None
            authority.nomination_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(AdminConflict, match="BOOTSTRAP_CLOSED"):
            await case.activate()


async def test_identity_revocation_after_authentication_blocks_mutation(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        async with persistence_sessions.begin() as session:
            principal = await session.scalar(select(PrincipalTable).where(PrincipalTable.principal_ref == PERSON))
            assert principal is not None
            principal.identity_epoch += 1
        with pytest.raises((AuthenticationRejected, IdentityDenied, AdminDenied)):
            await case.grant()


@pytest.mark.parametrize("committed", [True, False])
async def test_uncertain_commit_is_recovered_by_original_key_without_duplicate_grant(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        request = case.grant_request()
        original_commit = UnitOfWork._commit_by_owner

        async def lose_reply(uow: UnitOfWork) -> None:
            if committed:
                await original_commit(uow)
            raise ConnectionError("synthetic connection loss during commit")

        with monkeypatch.context() as patch:
            patch.setattr(UnitOfWork, "_commit_by_owner", lose_reply)
            with pytest.raises(AdminCommitUnknown) as error:
                await case.grant(request=request)
            assert error.value.assignment_ref == case.assignment_ref
        if committed:
            recovered = await case.service.operation(
                case.evidence, assignment_ref=case.assignment_ref, idempotency_key="grant-bob"
            )
            retry = await case.grant(request=request)
            assert retry.replayed and retry.operation_ref == recovered.operation_ref
        else:
            with pytest.raises(AdminNotFound):
                await case.service.operation(
                    case.evidence, assignment_ref=case.assignment_ref, idempotency_key="grant-bob"
                )
            assert not (await case.grant(request=request)).replayed
        async with persistence_sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(RoleAssignmentTable)
                    .where(RoleAssignmentTable.principal_ref == BOB)
                )
                == 1
            )


async def test_old_membership_cannot_receive_grants_but_its_existing_assignment_can_be_revoked(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        granted = await case.grant()
        async with persistence_sessions.begin() as session:
            old = await session.get(OrganizationMembershipTable, (ORG, BOB_MEMBERSHIP))
            assert old is not None
            old.status, old.ended_at, old.version = "left", datetime.now(UTC), 2
            await session.flush()
            session.add(
                OrganizationMembershipTable(
                    organization_ref=ORG,
                    membership_ref="membership:bob-returned",
                    principal_ref=BOB,
                    principal_type="human",
                    source_ref="fixture:rehired",
                    valid_from=datetime.now(UTC),
                )
            )
        with pytest.raises(AdminDenied):
            await case.grant(key="old-member-grant")
        assert (await case.revoke(granted.result.assignment_ref)).result.status == "revoked"


async def test_revocation_is_bounded_by_the_same_exact_scope_rule(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        granted = await case.grant()
        async with persistence_sessions.begin() as session:
            row = await session.scalar(
                select(RoleAssignmentTable).where(RoleAssignmentTable.assignment_ref == granted.result.assignment_ref)
            )
            assert row is not None
            row.scope_ref = "scope:finance"
        with pytest.raises(AdminDenied):
            await case.revoke(granted.result.assignment_ref)


async def test_management_grant_power_does_not_grant_work_item_reading_power(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        context = human_execution_context(
            case.evidence,
            assignment_ref=case.assignment_ref,
            request_id="request:management-is-not-business",
            deadline=datetime.now(UTC) + timedelta(seconds=10),
        )
        kernel = create_human_kernel(persistence_sessions, case.identity, ())

        async def forbidden_reader(session: AsyncSession) -> str:
            pytest.fail("management assignment reached a business reader")

        with pytest.raises(AuthorizationDenied):
            await kernel.read(context, action="task.view", target_ref="work_item:1", reader=forbidden_reader)


async def test_suspended_role_does_not_hide_its_existing_assignment_from_management(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        granted = await case.grant()
        async with persistence_sessions.begin() as session:
            role = await session.get(RoleVersionTable, (ORG, "role:engineering", 1))
            assert role is not None
            role.status = "suspended"
            role.version += 1
        assignments = await case.service.assignments(
            case.evidence, assignment_ref=case.assignment_ref, principal_ref=BOB, limit=20, after=None
        )
        # Management lists historical grant facts. Current role validity still
        # belongs to the authorization decision on each actual operation.
        assert [item.assignment_ref for item in assignments.items] == [granted.result.assignment_ref]
        with pytest.raises(AdminDenied):
            await case.grant(key="grant-after-role-suspension")
