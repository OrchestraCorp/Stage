"""Organization-wide MFA policy changes are explicit, versioned and recoverable."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.identity.administration import IdentityAdministrationError, IdentityOperationUnknown, operation_result
from app.identity.assurance import HumanAssurancePolicy
from app.identity.assurance_administration import (
    OPERATION_TYPE,
    ConfigureHumanAuthenticationRequest,
    configure_human_authentication,
)
from app.models.execution import TenantGuardTable
from app.models.identity_foundation import (
    AuthenticationProfileTable,
    IdentityControlOperationTable,
    IdentityProviderTable,
    OrganizationAuthenticationBindingTable,
    OrganizationHumanAuthenticationPolicyTable,
    OrganizationRootTable,
    TrustStateTable,
)
from tests.support.identity_administration import OPERATOR, install


def request_data(**changes: Any) -> dict[str, Any]:
    return {
        "environment": "test",
        "organization_ref": "tenant-a",
        "expected_version": 0,
        "policy": {
            "mode": "required",
            "rules": [
                {"profile_ref": "human", "profile_version": 1, "required_acr": "2", "required_amr": ["pwd", "otp"]}
            ],
            "max_auth_age_seconds": 600,
        },
        **changes,
    }


async def configure(sessions: async_sessionmaker[AsyncSession], *, key: str = "configure", **changes: Any) -> Any:
    return await configure_human_authentication(
        sessions,
        ConfigureHumanAuthenticationRequest.model_validate(request_data(**changes)),
        actor_ref=OPERATOR,
        idempotency_key=key,
    )


async def test_change_journals_exact_constraints_once_and_explicitly_disables(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await install(persistence_sessions)
    first = await configure(persistence_sessions)
    assert first == await configure(persistence_sessions)
    assert first == await operation_result(
        persistence_sessions,
        environment="test",
        actor_ref=OPERATOR,
        operation_type=OPERATION_TYPE,
        idempotency_key="configure",
    )
    async with persistence_sessions() as session:
        policy = await session.get(OrganizationHumanAuthenticationPolicyTable, "tenant-a")
        guard = await session.get(TenantGuardTable, "tenant-a")
        assert policy is not None and policy.version == 1 and policy.mode == "required"
        assert policy.rules_json == request_data()["policy"]["rules"]
        assert guard is not None and guard.epoch == 2
        journal = await session.get(IdentityControlOperationTable, first.operation_ref)
        assert journal is not None and journal.before_json is not None and journal.before_json["version"] == 0
        assert journal.after_json is not None and journal.after_json["mode"] == "required"
    with pytest.raises(IdentityAdministrationError, match="idempotency_conflict"):
        await configure(persistence_sessions, expected_version=1, policy={"mode": "disabled"})
    with pytest.raises(IdentityAdministrationError, match="version_conflict"):
        await configure(persistence_sessions, key="stale-disable", policy={"mode": "disabled"})
    disabled = await configure(persistence_sessions, key="disable", expected_version=1, policy={"mode": "disabled"})
    assert disabled.result["version"] == 2 and disabled.result["mode"] == "disabled"
    assert disabled.result["organization_authz_epoch"] == 3
    # Replay reports the original committed result; it never re-enables an old policy.
    assert await configure(persistence_sessions) == first
    async with persistence_sessions() as session:
        policy = await session.get(OrganizationHumanAuthenticationPolicyTable, "tenant-a")
        assert policy is not None and policy.mode == "disabled" and policy.version == 2


@pytest.mark.parametrize(
    ("changes", "code"),
    (
        ({"environment": "dev"}, "identity_database_environment_mismatch"),
        ({"organization_ref": "other"}, "identity_database_organization_mismatch"),
        ({"expected_version": 1}, "version_conflict"),
    ),
)
async def test_change_rejects_wrong_deployment_or_version_without_mutation(
    persistence_sessions: async_sessionmaker[AsyncSession], changes: dict[str, Any], code: str
) -> None:
    await install(persistence_sessions)
    with pytest.raises(IdentityAdministrationError, match=code):
        await configure(persistence_sessions, **changes)
    async with persistence_sessions() as session:
        assert await session.get(OrganizationHumanAuthenticationPolicyTable, "tenant-a") is None
        guard = await session.get(TenantGuardTable, "tenant-a")
        assert guard is not None and guard.epoch == 1
        assert await session.scalar(select(func.count()).select_from(IdentityControlOperationTable)) == 1


@pytest.mark.parametrize(
    "invalid",
    (
        "workload",
        "unknown-profile",
        "wrong-profile-version",
        "suspended-profile",
        "tampered-profile",
        "suspended-provider",
        "release-trust",
        "suspended-binding",
        "expired-binding",
        "future-binding",
        "wrong-environment-binding",
        "disjoint-clients",
        "binding-acr-conflict",
    ),
)
async def test_only_current_admitted_human_profiles_can_define_assurance(
    persistence_sessions: async_sessionmaker[AsyncSession], invalid: str
) -> None:
    await install(persistence_sessions)
    data = request_data()
    rule = data["policy"]["rules"][0]
    async with persistence_sessions.begin() as session:
        profile = await session.get(AuthenticationProfileTable, ("human", 1))
        provider = await session.get(IdentityProviderTable, "provider-a")
        trust = await session.get(TrustStateTable, "trust-a")
        binding = await session.scalar(
            select(OrganizationAuthenticationBindingTable).where(
                OrganizationAuthenticationBindingTable.profile_ref == "human"
            )
        )
        assert profile is not None and provider is not None and trust is not None and binding is not None
        now = datetime.now(UTC)
        if invalid == "workload":
            rule["profile_ref"] = "service"
        elif invalid == "unknown-profile":
            rule["profile_ref"] = "unknown"
        elif invalid == "wrong-profile-version":
            rule["profile_version"] = 2
        elif invalid == "suspended-profile":
            profile.status = "suspended"
        elif invalid == "tampered-profile":
            profile.stage_audience = "forged"
        elif invalid == "suspended-provider":
            provider.status = "suspended"
        elif invalid == "release-trust":
            trust.purpose = "release"
        elif invalid == "suspended-binding":
            binding.status = "suspended"
        elif invalid == "expired-binding":
            binding.valid_from = now - timedelta(days=2)
            binding.expires_at = now - timedelta(days=1)
        elif invalid == "future-binding":
            binding.valid_from = now + timedelta(days=1)
        elif invalid == "wrong-environment-binding":
            binding.environment = "dev"
        elif invalid == "disjoint-clients":
            binding.constraints_json = {"allowed_clients": ["other-client"]}
        elif invalid == "binding-acr-conflict":
            binding.constraints_json = {"allowed_clients": ["maestro-test"], "required_acr": "3"}
    code = "assurance_binding_acr_conflict" if invalid == "binding-acr-conflict" else "assurance_profile_not_admitted"
    with pytest.raises(IdentityAdministrationError, match=code):
        await configure_human_authentication(
            persistence_sessions,
            ConfigureHumanAuthenticationRequest.model_validate(data),
            actor_ref=OPERATOR,
            idempotency_key="reject",
        )
    async with persistence_sessions() as session:
        assert await session.get(OrganizationHumanAuthenticationPolicyTable, "tenant-a") is None
        guard = await session.get(TenantGuardTable, "tenant-a")
        assert guard is not None and guard.epoch == 1


async def test_policy_cannot_be_configured_before_bootstrap_or_for_inactive_root(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(IdentityAdministrationError, match="identity_not_initialized"):
        await configure(persistence_sessions)
    await install(persistence_sessions)
    async with persistence_sessions.begin() as session:
        organization = await session.get(OrganizationRootTable, "tenant-a")
        assert organization is not None
        organization.status = "suspended"
    with pytest.raises(IdentityAdministrationError, match="organization_not_active"):
        await configure(persistence_sessions)


@pytest.mark.parametrize(
    "policy",
    (
        {"mode": "required"},
        {"mode": "required", "rules": request_data()["policy"]["rules"]},
        {**request_data()["policy"], "max_auth_age_seconds": 0},
        {**request_data()["policy"], "max_auth_age_seconds": True},
        {**request_data()["policy"], "max_auth_age_seconds": 86401},
        {**request_data()["policy"], "rules": request_data()["policy"]["rules"] * 2},
        {**request_data()["policy"], "rules": [{"profile_ref": "human", "profile_version": 1, "required_acr": ""}]},
        {
            **request_data()["policy"],
            "rules": [{"profile_ref": "human", "profile_version": True, "required_acr": "2"}],
        },
        {
            **request_data()["policy"],
            "rules": [{"profile_ref": "human\u0000", "profile_version": 1, "required_acr": "2"}],
        },
        {
            **request_data()["policy"],
            "rules": [{"profile_ref": "human", "profile_version": 1, "required_acr": "\ud800"}],
        },
        {
            **request_data()["policy"],
            "rules": [
                {"profile_ref": "human", "profile_version": 1, "required_acr": "2", "required_amr": ["otp", "otp"]}
            ],
        },
        {"mode": "disabled", "max_auth_age_seconds": 600},
        {"mode": "disabled", "rules": request_data()["policy"]["rules"]},
        {**request_data()["policy"], "allow_weak_fallback": True},
    ),
)
def test_policy_rejects_ambiguous_or_invalid_assurance(policy: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        HumanAssurancePolicy.model_validate(policy)


def test_policy_uses_issuer_acr_semantics_not_amr_factor_count() -> None:
    policy = request_data()["policy"]
    policy["rules"][0]["required_amr"] = ["hwk"]
    assert HumanAssurancePolicy.model_validate(policy).rules[0].required_amr == ("hwk",)
    policy["rules"][0]["required_amr"] = []
    assert HumanAssurancePolicy.model_validate(policy).rules[0].required_amr == ()


@pytest.mark.parametrize("value", ("bad\x00ref", "bad\nref", "\ud800"))
async def test_operation_identity_rejects_invalid_storage_characters(
    persistence_sessions: async_sessionmaker[AsyncSession], value: str
) -> None:
    with pytest.raises(IdentityAdministrationError, match="invalid_operation_identity"):
        await configure(persistence_sessions, key=value)


async def test_policy_epoch_and_audit_rollback_together_on_journal_failure(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await install(persistence_sessions)
    engine = persistence_sessions.kw["bind"]

    def fail_journal(*args: Any) -> None:
        statement = args[2]
        if "INSERT INTO identity_control_operation" in statement:
            raise RuntimeError("journal-unavailable")

    event.listen(engine.sync_engine, "before_cursor_execute", fail_journal)
    try:
        with pytest.raises(RuntimeError, match="journal-unavailable"):
            await configure(persistence_sessions)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail_journal)
    async with persistence_sessions() as session:
        assert await session.get(OrganizationHumanAuthenticationPolicyTable, "tenant-a") is None
        guard = await session.get(TenantGuardTable, "tenant-a")
        assert guard is not None and guard.epoch == 1


async def test_lost_commit_response_recovers_original_policy_result(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await install(persistence_sessions)

    class LostCommitSession(AsyncSession):
        async def __aexit__(self, *args: Any) -> None:
            await super().__aexit__(*args)
            raise SQLAlchemyError("response lost after transaction context committed")

    uncertain_sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
        persistence_sessions.kw["bind"], class_=LostCommitSession, expire_on_commit=False
    )
    with pytest.raises(IdentityOperationUnknown) as failure:
        await configure(uncertain_sessions)
    assert failure.value.operation_type == OPERATION_TYPE
    recovered = await operation_result(
        persistence_sessions,
        environment="test",
        actor_ref=OPERATOR,
        operation_type=OPERATION_TYPE,
        idempotency_key="configure",
    )
    assert recovered.result["version"] == 1
    assert await configure(persistence_sessions) == recovered


async def test_competing_policy_edits_require_current_version(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await install(persistence_sessions)
    results = await asyncio.gather(
        configure(persistence_sessions, key="edit-1"),
        configure(persistence_sessions, key="edit-2"),
        return_exceptions=True,
    )
    rejected = [result for result in results if isinstance(result, IdentityAdministrationError)]
    assert len(rejected) == 1 and rejected[0].code == "version_conflict"
    assert not any(isinstance(result, BaseException) and result not in rejected for result in results)
    async with persistence_sessions() as session:
        policy = await session.get(OrganizationHumanAuthenticationPolicyTable, "tenant-a")
        guard = await session.get(TenantGuardTable, "tenant-a")
        assert policy is not None and policy.version == 1
        assert guard is not None and guard.epoch == 2
