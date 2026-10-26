"""First-management nomination is bounded, recoverable and never self-accepting."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.administration.bootstrap import NOMINATE_OPERATION, NominationPlan, nominate
from app.administration.errors import AdminDenied
from app.authz.loader import load_role_version
from app.authz.management import (
    MANAGEMENT_ACTIONS,
    MANAGEMENT_ROLE_REF,
    GrantRule,
    authorize_manager,
    require_management_assurance,
)
from app.control.contracts import AdministrationError, OperationUnknown
from app.control.operations import operation_result
from app.identity.authentication import AuthenticationAssuranceRequired
from app.models.administration import ManagementAuthorityTable
from app.models.authorization import (
    PermissionActionTable,
    RoleAssignmentTable,
    RoleVersionActionTable,
    RoleVersionTable,
)
from app.models.execution import TenantGuardTable
from app.models.identity_foundation import (
    AuthenticationProfileTable,
    IdentityControlOperationTable,
    OrganizationHumanAuthenticationPolicyTable,
    OrganizationMembershipTable,
)
from app.storage.coordination import LockMode, lock_organization
from tests.support.authenticated_commands import identity_service, jwt_client, seed_authenticated_command
from tests.support.authorization import ORG, PERSON
from tests.support.identity_assurance import configure
from tests.support.identity_tokens import key as signing_key  # noqa: F401
from tests.support.identity_tokens import token

OPERATOR = "local-os:test-first-administrator"


async def baseline(sessions: async_sessionmaker[AsyncSession]) -> NominationPlan:
    await seed_authenticated_command(sessions)
    await configure(sessions)
    async with sessions() as session:
        role = await session.get(RoleVersionTable, (ORG, "role:engineering", 1))
        assert role is not None
        digest = role.content_digest
    return NominationPlan(
        environment="test",
        organization_ref=ORG,
        principal_ref=PERSON,
        membership_ref="membership:alice",
        profile_ref="local-human",
        profile_version=1,
        external_identity_ref="identity",
        expected_version=0,
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
        reason="Explicit synthetic first administrator",
        grant_rules=(
            GrantRule(
                role_ref="role:engineering",
                role_revision=1,
                content_digest=digest,
                scope_ref="scope:engineering",
                max_duration_seconds=86400,
            ),
        ),
    )


async def nomination(sessions: async_sessionmaker[AsyncSession], plan: NominationPlan, key: str = "nominate") -> Any:
    return await nominate(sessions, plan, actor_ref=OPERATOR, idempotency_key=key)


async def accepted(sessions: async_sessionmaker[AsyncSession]) -> ManagementAuthorityTable:
    async with sessions.begin() as session:
        authority = await session.get(ManagementAuthorityTable, ORG)
        assert authority is not None
        assignment = await session.scalar(
            select(RoleAssignmentTable).where(RoleAssignmentTable.assignment_ref == authority.assignment_ref)
        )
        assert assignment is not None
        authority.status, authority.accepted_at = "active", datetime.now(UTC)
        authority.version += 1
        assignment.status, assignment.version = "active", assignment.version + 1
        return authority


async def test_nomination_is_suspended_and_published_management_role_cannot_be_used_for_work(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    plan = await baseline(persistence_sessions)
    result = await nomination(persistence_sessions, plan)
    assert result == await nomination(persistence_sessions, plan)
    async with persistence_sessions() as session:
        authority = await session.get(ManagementAuthorityTable, ORG)
        assert authority is not None and authority.accepted_at is None and authority.status == "pending"
        assignment = await session.scalar(
            select(RoleAssignmentTable).where(RoleAssignmentTable.assignment_ref == authority.assignment_ref)
        )
        assert assignment is not None and assignment.status == "suspended" and assignment.expires_at is None
        assert assignment.grant_source_ref == authority.authority_ref
        assert await load_role_version(session, ORG, assignment) is None
        actions = (
            await session.scalars(
                select(RoleVersionActionTable.action_ref).where(RoleVersionActionTable.role_ref == MANAGEMENT_ROLE_REF)
            )
        ).all()
        assert set(actions) == MANAGEMENT_ACTIONS
        journal = await session.get(IdentityControlOperationTable, result.operation_ref)
        assert journal is not None and journal.actor_ref == OPERATOR and journal.actor_kind == "bootstrap_operator"
        assert journal.after_json is not None and journal.after_json["reason"] == plan.reason
    recovered = await operation_result(
        persistence_sessions,
        environment="test",
        actor_ref=OPERATOR,
        operation_type=NOMINATE_OPERATION,
        idempotency_key="nominate",
    )
    assert recovered == result


async def test_pending_nomination_can_be_replaced_only_with_exact_version_and_old_assignment_is_revoked(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    plan = await baseline(persistence_sessions)
    first = await nomination(persistence_sessions, plan)
    changed = plan.model_copy(update={"expected_version": 1, "reason": "Explicit replacement"})
    second = await nomination(persistence_sessions, changed, "replace")
    assert first.result["authority_ref"] == second.result["authority_ref"]
    assert first.result["assignment_ref"] != second.result["assignment_ref"]
    assert second.result["version"] == 2
    async with persistence_sessions() as session:
        old = await session.scalar(
            select(RoleAssignmentTable).where(RoleAssignmentTable.assignment_ref == first.result["assignment_ref"])
        )
        assert old is not None and old.status == "revoked" and old.revoked_at is not None
    with pytest.raises(AdministrationError, match="version_conflict"):
        await nomination(persistence_sessions, changed, "stale")
    with pytest.raises(AdministrationError, match="idempotency_conflict"):
        await nomination(persistence_sessions, changed)


async def test_expired_pending_nomination_has_explicit_versioned_recovery(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    plan = await baseline(persistence_sessions)
    await nomination(persistence_sessions, plan)
    async with persistence_sessions.begin() as session:
        row = await session.get(ManagementAuthorityTable, ORG)
        assert row is not None
        row.nomination_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    result = await nomination(persistence_sessions, plan.model_copy(update={"expected_version": 1}), "replace-expired")
    assert result.result["version"] == 2 and result.result["status"] == "pending"


@pytest.mark.parametrize("status", ["active", "revoked"])
async def test_acceptance_permanently_closes_bootstrap_even_after_revocation(
    persistence_sessions: async_sessionmaker[AsyncSession],
    status: str,
) -> None:
    plan = await baseline(persistence_sessions)
    await nomination(persistence_sessions, plan)
    await accepted(persistence_sessions)
    async with persistence_sessions.begin() as session:
        row = await session.get(ManagementAuthorityTable, ORG)
        assert row is not None
        row.status = status
    with pytest.raises(AdministrationError, match="first_administrator_already_accepted"):
        await nomination(persistence_sessions, plan.model_copy(update={"expected_version": 2}), "reopen")
    # Reading/replaying a committed nomination never changes accepted authority.
    await nomination(persistence_sessions, plan)
    async with persistence_sessions() as session:
        row = await session.get(ManagementAuthorityTable, ORG)
        assert row is not None and row.accepted_at is not None and row.status == status


@pytest.mark.parametrize(
    "cause",
    [
        "expired",
        "over_hour",
        "wrong_member",
        "expiring_member",
        "wrong_mapping",
        "mfa_disabled",
        "mfa_weak",
        "profile_mutated",
        "role_mutated",
        "scope_unknown",
        "role_missing",
    ],
)
async def test_nomination_rejects_invalid_identity_assurance_or_grant_envelope_without_partial_facts(
    persistence_sessions: async_sessionmaker[AsyncSession],
    cause: str,
) -> None:
    plan = await baseline(persistence_sessions)
    if cause == "expired":
        plan = plan.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    elif cause == "over_hour":
        plan = plan.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(hours=2)})
    elif cause == "wrong_member":
        plan = plan.model_copy(update={"membership_ref": "missing"})
    elif cause == "wrong_mapping":
        plan = plan.model_copy(update={"external_identity_ref": "missing"})
    elif cause == "scope_unknown":
        plan = plan.model_copy(
            update={"grant_rules": (plan.grant_rules[0].model_copy(update={"scope_ref": "missing"}),)}
        )
    elif cause == "role_missing":
        plan = plan.model_copy(
            update={"grant_rules": (plan.grant_rules[0].model_copy(update={"role_ref": "missing"}),)}
        )
    else:
        async with persistence_sessions.begin() as session:
            if cause == "expiring_member":
                member = await session.get(OrganizationMembershipTable, (ORG, "membership:alice"))
                assert member is not None
                member.expires_at = datetime.now(UTC) + timedelta(days=1)
            elif cause in {"mfa_disabled", "mfa_weak"}:
                policy = await session.get(OrganizationHumanAuthenticationPolicyTable, ORG)
                assert policy is not None
                if cause == "mfa_disabled":
                    policy.mode, policy.rules_json, policy.max_auth_age_seconds = "disabled", [], None
                else:
                    policy.rules_json = [{**policy.rules_json[0], "required_amr": ["pwd"]}]
            elif cause == "profile_mutated":
                profile = await session.get(AuthenticationProfileTable, ("local-human", 1))
                assert profile is not None
                profile.digest = "0" * 64
            else:
                role = await session.get(RoleVersionTable, (ORG, "role:engineering", 1))
                assert role is not None
                role.content_digest = "0" * 64
    with pytest.raises(AdministrationError):
        await nomination(persistence_sessions, plan)
    async with persistence_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ManagementAuthorityTable)) == 0
        assert await session.get(RoleVersionTable, (ORG, MANAGEMENT_ROLE_REF, 1)) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"max_duration_seconds": 365 * 86400 + 1},
        {"max_duration_seconds": 0},
        {"role_ref": MANAGEMENT_ROLE_REF},
        {"content_digest": "bad"},
        {"role_revision": True},
        {"scope_ref": "bad\x00scope"},
    ],
)
def test_grant_rule_rejects_unbounded_or_management_authority(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        GrantRule.model_validate(
            {
                "role_ref": "ordinary",
                "role_revision": 1,
                "content_digest": "a" * 64,
                "scope_ref": "scope",
                "max_duration_seconds": 86400,
                **changes,
            }
        )


async def test_journal_failure_rolls_back_nomination_role_assignment_and_epoch(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    plan = await baseline(persistence_sessions)
    async with persistence_sessions() as session:
        guard = await session.get(TenantGuardTable, ORG)
        assert guard is not None
        epoch = guard.epoch
    engine = persistence_sessions.kw["bind"]

    def reject_journal(*args: Any) -> None:
        if "INSERT INTO identity_control_operation" in args[2]:
            raise RuntimeError("journal unavailable")

    event.listen(engine.sync_engine, "before_cursor_execute", reject_journal)
    try:
        with pytest.raises(RuntimeError):
            await nomination(persistence_sessions, plan)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", reject_journal)
    async with persistence_sessions() as session:
        assert await session.get(ManagementAuthorityTable, ORG) is None
        assert await session.get(RoleVersionTable, (ORG, MANAGEMENT_ROLE_REF, 1)) is None
        guard = await session.get(TenantGuardTable, ORG)
        assert guard is not None and guard.epoch == epoch


async def test_lost_commit_response_recovers_original_nomination_without_second_assignment(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    plan = await baseline(persistence_sessions)

    class LostResponse(AsyncSession):
        async def __aexit__(self, *args: Any) -> None:
            await super().__aexit__(*args)
            raise SQLAlchemyError("response lost")

    uncertain: async_sessionmaker[AsyncSession] = async_sessionmaker(
        persistence_sessions.kw["bind"], class_=LostResponse, expire_on_commit=False
    )
    with pytest.raises(OperationUnknown):
        await nomination(uncertain, plan)
    recovered = await nomination(persistence_sessions, plan)
    assert recovered.result["version"] == 1
    async with persistence_sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RoleAssignmentTable)
                .where(RoleAssignmentTable.role_ref == MANAGEMENT_ROLE_REF)
            )
            == 1
        )


async def test_competing_initial_nominations_have_one_winner(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    plan = await baseline(persistence_sessions)
    outcomes = await asyncio.gather(
        nomination(persistence_sessions, plan, "first"),
        nomination(persistence_sessions, plan, "second"),
        return_exceptions=True,
    )
    assert len([value for value in outcomes if isinstance(value, AdministrationError)]) == 1
    async with persistence_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ManagementAuthorityTable)) == 1


async def test_real_mfa_identity_can_authorize_only_after_acceptance_and_only_management_actions(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: Any,  # noqa: F811
) -> None:
    plan = await baseline(persistence_sessions)
    result = await nomination(persistence_sessions, plan)
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        bearer = token(
            signing_key, claims={"auth_time": int(datetime.now(UTC).timestamp()), "acr": "2", "amr": ["pwd", "otp"]}
        )
        evidence = await identity.authenticate_human(bearer, profile_ref="local-human", profile_version=1)
        async with persistence_sessions() as session:
            await session.begin()
            await identity.acquire_human_locks(session, evidence)
            await lock_organization(session, ORG, mode=LockMode.SHARED)
            with pytest.raises(AdminDenied):
                await authorize_manager(
                    session, evidence, result.result["assignment_ref"], "administration.people.read", datetime.now(UTC)
                )
        await accepted(persistence_sessions)
        async with persistence_sessions() as session:
            await session.begin()
            await identity.acquire_human_locks(session, evidence)
            await lock_organization(session, ORG, mode=LockMode.SHARED)
            manager = await authorize_manager(
                session, evidence, result.result["assignment_ref"], "administration.people.read", datetime.now(UTC)
            )
            assert manager.authority.status == "active" and manager.valid_until > datetime.now(UTC)
            with pytest.raises(AdminDenied):
                await authorize_manager(
                    session, evidence, result.result["assignment_ref"], "task.view", datetime.now(UTC)
                )
            with pytest.raises(AdminDenied):
                await authorize_manager(
                    session, evidence, "assignment:engineering", "administration.people.read", datetime.now(UTC)
                )
            with pytest.raises(AuthenticationAssuranceRequired):
                await require_management_assurance(
                    session,
                    replace(
                        evidence,
                        credential=replace(
                            evidence.credential, authenticated_at=datetime.now(UTC) - timedelta(minutes=6)
                        ),
                    ),
                    datetime.now(UTC),
                )
        await identity.aclose()


async def test_mutated_management_action_catalog_denies_current_manager(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: Any,  # noqa: F811
) -> None:
    plan = await baseline(persistence_sessions)
    result = await nomination(persistence_sessions, plan)
    await accepted(persistence_sessions)
    async with persistence_sessions.begin() as session:
        registered = await session.get(PermissionActionTable, "administration.audit.read")
        assert registered is not None
        registered.resource_kind = "work_item"
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        bearer = token(
            signing_key, claims={"auth_time": int(datetime.now(UTC).timestamp()), "acr": "2", "amr": ["pwd", "otp"]}
        )
        evidence = await identity.authenticate_human(bearer, profile_ref="local-human", profile_version=1)
        async with persistence_sessions() as session:
            await session.begin()
            await identity.acquire_human_locks(session, evidence)
            await lock_organization(session, ORG, mode=LockMode.SHARED)
            with pytest.raises(AdminDenied):
                await authorize_manager(
                    session, evidence, result.result["assignment_ref"], "administration.people.read", datetime.now(UTC)
                )
        await identity.aclose()


@pytest.mark.parametrize("cause", ["weak_amr", "missing_time", "future_time"])
async def test_manager_login_assurance_returns_reauthentication_signal(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: Any,  # noqa: F811
    cause: str,
) -> None:
    await baseline(persistence_sessions)
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        bearer = token(
            signing_key, claims={"auth_time": int(datetime.now(UTC).timestamp()), "acr": "2", "amr": ["pwd", "otp"]}
        )
        evidence = await identity.authenticate_human(bearer, profile_ref="local-human", profile_version=1)
        values: dict[str, Any] = (
            {"amr": ("pwd",)}
            if cause == "weak_amr"
            else {"authenticated_at": None if cause == "missing_time" else datetime.now(UTC) + timedelta(seconds=5)}
        )
        evidence = replace(evidence, credential=replace(evidence.credential, **values))
        async with persistence_sessions.begin() as session:
            await identity.acquire_human_locks(session, evidence)
            await lock_organization(session, ORG, mode=LockMode.SHARED)
            with pytest.raises(AuthenticationAssuranceRequired):
                await require_management_assurance(session, evidence, datetime.now(UTC))
        await identity.aclose()


async def test_nomination_expiring_during_flush_rolls_back_role_authority_and_assignment(
    persistence_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.administration.bootstrap as module

    plan = await baseline(persistence_sessions)

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return datetime.now(tz) + timedelta(hours=2)

    class DelayedFlush(AsyncSession):
        async def flush(self, objects: Any = None) -> None:
            await super().flush(objects)
            monkeypatch.setattr(module, "datetime", ExpiredClock)

    delayed: async_sessionmaker[AsyncSession] = async_sessionmaker(
        persistence_sessions.kw["bind"], class_=DelayedFlush, expire_on_commit=False
    )
    with pytest.raises(AdministrationError, match="nomination_expired"):
        await nomination(delayed, plan)
    async with persistence_sessions() as session:
        assert await session.get(ManagementAuthorityTable, ORG) is None
        assert await session.get(RoleVersionTable, (ORG, MANAGEMENT_ROLE_REF, 1)) is None


def test_management_references_keep_existing_unicode_character_contract() -> None:
    rule = GrantRule(
        role_ref="人" * 128, role_revision=1, content_digest="a" * 64, scope_ref="范围" * 128, max_duration_seconds=1
    )
    assert len(rule.role_ref) == 128 and len(rule.scope_ref) == 256


@pytest.mark.parametrize("counter", ["authority", "organization"])
async def test_bootstrap_rejects_exhausted_public_version_before_partial_changes(
    persistence_sessions: async_sessionmaker[AsyncSession],
    counter: str,
) -> None:
    plan = await baseline(persistence_sessions)
    if counter == "authority":
        await nomination(persistence_sessions, plan)
    async with persistence_sessions.begin() as session:
        if counter == "authority":
            row = await session.get(ManagementAuthorityTable, ORG)
            assert row is not None
            row.version = 2**31 - 1
        else:
            guard = await session.get(TenantGuardTable, ORG)
            assert guard is not None
            guard.epoch = 2**31 - 1
    if counter == "authority":
        plan = plan.model_copy(update={"expected_version": 2**31 - 1})
    with pytest.raises(AdministrationError):
        await nomination(persistence_sessions, plan, "counter-exhausted")
    async with persistence_sessions() as session:
        authority = await session.get(ManagementAuthorityTable, ORG)
        if counter == "authority":
            assert authority is not None and authority.version == 2**31 - 1
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(RoleAssignmentTable)
                    .where(RoleAssignmentTable.role_ref == MANAGEMENT_ROLE_REF)
                )
                == 1
            )
        else:
            assert authority is None
            assert await session.get(RoleVersionTable, (ORG, MANAGEMENT_ROLE_REF, 1)) is None


def test_nomination_versions_match_public_management_contract() -> None:
    values: dict[str, Any] = {
        "environment": "test",
        "organization_ref": ORG,
        "principal_ref": PERSON,
        "membership_ref": "membership:alice",
        "profile_ref": "profile",
        "profile_version": 1,
        "external_identity_ref": "external",
        "expected_version": 0,
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "reason": "Boundary test",
        "grant_rules": [
            {
                "role_ref": "ordinary",
                "role_revision": 1,
                "content_digest": "a" * 64,
                "scope_ref": "scope",
                "max_duration_seconds": 1,
            }
        ],
    }
    for field in ("expected_version", "profile_version"):
        with pytest.raises(ValueError):
            NominationPlan.model_validate({**values, field: 2**31})
