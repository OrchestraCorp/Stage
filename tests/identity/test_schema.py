"""Identity storage invariants run unchanged against SQLite and PostgreSQL."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.base import Base
from app.models.identity import PrincipalTable
from app.models.identity_foundation import (
    AuthenticationProfileTable,
    BootstrapRecordTable,
    ExternalIdentityTable,
    IdentityControlOperationTable,
    IdentityProviderTable,
    OrganizationAuthenticationBindingTable,
    OrganizationMembershipTable,
    OrganizationRootTable,
    TrustStateTable,
    WorkloadRegistrationTable,
)

NOW = datetime(2026, 10, 25, tzinfo=UTC)


async def _insert(sessions: async_sessionmaker[AsyncSession], model: type[Base], **values: Any) -> None:
    async with sessions.begin() as session:
        session.add(model(**values))


@pytest.fixture
async def identity_sessions(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    sessions = persistence_sessions
    for ref, kind in (("human:1", "human"), ("service:1", "service"), ("service:2", "service")):
        await _insert(sessions, PrincipalTable, principal_ref=ref, principal_type=kind)
    await _insert(
        sessions,
        OrganizationRootTable,
        organization_ref="tenant:a",
        display_name="Private deployment authorization root",
    )
    await _insert(
        sessions, TrustStateTable, trust_ref="trust:1", purpose="authentication", configuration_digest="a" * 64
    )
    await _insert(
        sessions,
        IdentityProviderTable,
        provider_ref="provider:1",
        issuer_exact="https://issuer.example",
        trust_ref="trust:1",
    )
    for profile, kind, namespace in (
        ("human", "human_oidc", "human/sub"),
        ("service", "workload_oauth2", "service/sub"),
    ):
        await _insert(
            sessions,
            AuthenticationProfileTable,
            profile_ref=profile,
            profile_version=1,
            provider_ref="provider:1",
            subject_namespace=namespace,
            credential_kind=kind,
            stage_audience="stage",
            config_schema="stage.jwt.v1",
            config_json={},
            digest="b" * 64,
        )
    for principal, kind, namespace in (
        ("human:1", "human", "human/sub"),
        ("service:1", "service", "service/sub"),
        ("service:2", "service", "service/sub"),
    ):
        await _insert(
            sessions,
            ExternalIdentityTable,
            external_identity_ref=f"identity:{principal}",
            provider_ref="provider:1",
            subject_namespace=namespace,
            subject_exact=principal,
            principal_ref=principal,
            principal_type=kind,
            link_evidence_ref="installation:1",
        )
    return sessions


def _membership(**changes: Any) -> dict[str, Any]:
    return dict(
        organization_ref="tenant:a",
        membership_ref="membership:1",
        principal_ref="human:1",
        principal_type="human",
        valid_from=NOW,
        source_ref="adoption:1",
        **changes,
    )


def _binding(**changes: Any) -> dict[str, Any]:
    return {
        "organization_ref": "tenant:a",
        "binding_ref": "binding:1",
        "profile_ref": "human",
        "profile_version": 1,
        "environment": "test",
        "constraints_json": {},
        "valid_from": NOW,
        **changes,
    }


def _workload(**changes: Any) -> dict[str, Any]:
    return {
        "workload_ref": "workload:1",
        "service_principal_ref": "service:1",
        "profile_ref": "service",
        "profile_version": 1,
        "external_identity_ref": "identity:service:1",
        "verified_client_ref": "client:1",
        "environment": "test",
        "valid_from": NOW,
        **changes,
    }


@pytest.mark.parametrize("kind", ("agent", "service"))
async def test_membership_cannot_retype_its_principal(
    identity_sessions: async_sessionmaker[AsyncSession],
    kind: str,
) -> None:
    values = _membership()
    values["principal_type"] = kind
    with pytest.raises(IntegrityError):
        await _insert(identity_sessions, OrganizationMembershipTable, **values)


async def test_membership_retains_separate_joining_experiences_in_the_installed_organization(
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _insert(identity_sessions, OrganizationMembershipTable, **_membership())
    for status in ("active", "suspended"):
        with pytest.raises(IntegrityError):
            await _insert(
                identity_sessions,
                OrganizationMembershipTable,
                **{**_membership(), "membership_ref": "other", "status": status},
            )
    async with identity_sessions.begin() as session:
        await session.execute(
            update(OrganizationMembershipTable)
            .where(OrganizationMembershipTable.organization_ref == "tenant:a")
            .values(status="left", ended_at=NOW + timedelta(days=1), version=2)
        )
    await _insert(
        identity_sessions,
        OrganizationMembershipTable,
        **{**_membership(), "membership_ref": "membership:2"},
    )
    async with identity_sessions() as session:
        rows = (
            (
                await session.execute(
                    select(OrganizationMembershipTable).where(
                        OrganizationMembershipTable.organization_ref == "tenant:a"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {(row.membership_ref, row.status) for row in rows} == {
            ("membership:1", "left"),
            ("membership:2", "active"),
        }


@pytest.mark.parametrize(
    "changes",
    (
        {"organization_ref": "tenant:unknown"},
        {"expires_at": NOW},
        {"status": "revoked"},
        {"status": "active", "ended_at": NOW},
        {"version": 0},
        {"source_ref": " "},
    ),
)
async def test_invalid_membership_facts_are_rejected(
    identity_sessions: async_sessionmaker[AsyncSession],
    changes: dict[str, Any],
) -> None:
    with pytest.raises(IntegrityError):
        await _insert(identity_sessions, OrganizationMembershipTable, **{**_membership(), **changes})


@pytest.mark.parametrize(
    "changes",
    ({"organization_ref": "tenant:unknown"}, {"profile_version": 2}, {"expires_at": NOW}),
)
async def test_organization_binding_requires_a_real_root_and_exact_profile_version(
    identity_sessions: async_sessionmaker[AsyncSession],
    changes: dict[str, Any],
) -> None:
    with pytest.raises(IntegrityError):
        await _insert(identity_sessions, OrganizationAuthenticationBindingTable, **_binding(**changes))


async def test_organization_binding_preserves_exact_profile_and_environment_uniqueness(
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _insert(identity_sessions, OrganizationAuthenticationBindingTable, **_binding())
    with pytest.raises(IntegrityError):
        await _insert(identity_sessions, OrganizationAuthenticationBindingTable, **_binding(binding_ref="binding:2"))
    await _insert(
        identity_sessions,
        OrganizationAuthenticationBindingTable,
        **_binding(binding_ref="binding:3", environment="dev"),
    )


@pytest.mark.parametrize("status", ("active", "suspended", "revoked"))
async def test_deployment_cannot_store_a_second_authorization_root_even_when_the_first_is_disabled(
    identity_sessions: async_sessionmaker[AsyncSession],
    status: str,
) -> None:
    async with identity_sessions.begin() as session:
        await session.execute(update(OrganizationRootTable).values(status=status))
    with pytest.raises(IntegrityError):
        await _insert(
            identity_sessions,
            OrganizationRootTable,
            organization_ref="tenant:b",
            display_name="Another authorization root",
        )
    async with identity_sessions() as session:
        organization = (await session.scalars(select(OrganizationRootTable))).one()
        assert organization.organization_ref == "tenant:a" and organization.deployment_slot == 1


@pytest.mark.parametrize("deployment_slot", (None, 0, 2))
async def test_deployment_slot_cannot_be_changed_to_allow_another_authorization_root(
    identity_sessions: async_sessionmaker[AsyncSession],
    deployment_slot: int | None,
) -> None:
    with pytest.raises(IntegrityError):
        async with identity_sessions.begin() as session:
            await session.execute(update(OrganizationRootTable).values(deployment_slot=deployment_slot))


async def test_external_identity_exact_subject_cannot_be_linked_to_a_second_principal(
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    values = {
        "external_identity_ref": "identity:new",
        "provider_ref": "provider:1",
        "subject_namespace": "service/sub",
        "subject_exact": "service:1",
        "principal_ref": "service:2",
        "principal_type": "service",
        "link_evidence_ref": "adoption:2",
    }
    with pytest.raises(IntegrityError):
        await _insert(identity_sessions, ExternalIdentityTable, **values)
    values.update(subject_exact="Service:1")
    await _insert(identity_sessions, ExternalIdentityTable, **values)
    values.update(external_identity_ref="identity:trailing", subject_exact="service:1 ")
    await _insert(identity_sessions, ExternalIdentityTable, **values)


@pytest.mark.parametrize(
    "changes",
    (
        {"service_principal_ref": "human:1"},
        {"service_principal_ref": "service:2"},
        {"principal_type": "human"},
        {"profile_version": 2},
        {"expires_at": NOW},
    ),
)
async def test_workload_cannot_change_its_mapped_service_identity_or_reference_missing_profile(
    identity_sessions: async_sessionmaker[AsyncSession],
    changes: dict[str, Any],
) -> None:
    with pytest.raises(IntegrityError):
        await _insert(identity_sessions, WorkloadRegistrationTable, **_workload(**changes))


async def test_workload_and_membership_prevent_cascading_identity_history_deletion(
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _insert(identity_sessions, WorkloadRegistrationTable, **_workload())
    await _insert(identity_sessions, OrganizationMembershipTable, **_membership())
    with pytest.raises(IntegrityError):
        async with identity_sessions.begin() as session:
            await session.execute(delete(PrincipalTable).where(PrincipalTable.principal_ref == "human:1"))
    with pytest.raises(IntegrityError):
        async with identity_sessions.begin() as session:
            await session.execute(
                delete(ExternalIdentityTable).where(ExternalIdentityTable.external_identity_ref == "identity:service:1")
            )


@pytest.mark.parametrize("changes", ({"identity_epoch": -1}, {"recovery_epoch": -1}, {"version": 0}))
async def test_principal_epoch_and_version_ranges(
    identity_sessions: async_sessionmaker[AsyncSession],
    changes: dict[str, int],
) -> None:
    with pytest.raises(IntegrityError):
        async with identity_sessions.begin() as session:
            await session.execute(
                update(PrincipalTable).where(PrincipalTable.principal_ref == "human:1").values(**changes)
            )


async def test_platform_journal_idempotency_is_actor_and_operation_scoped(
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    values = {
        "operation_ref": "operation:1",
        "operation_type": "identity.bootstrap",
        "idempotency_key": "request:1",
        "input_digest": "a" * 64,
        "actor_kind": "bootstrap_operator",
        "actor_ref": "installer:1",
        "authentication_json": {"source": "local_installation"},
        "target_ref": "profile:1",
        "result_json": {"status": "succeeded"},
        "completed_at": NOW,
    }
    await _insert(identity_sessions, IdentityControlOperationTable, **values)
    with pytest.raises(IntegrityError):
        await _insert(identity_sessions, IdentityControlOperationTable, **{**values, "operation_ref": "operation:2"})
    await _insert(
        identity_sessions,
        IdentityControlOperationTable,
        **{**values, "operation_ref": "operation:3", "actor_ref": "installer:2"},
    )
    await _insert(
        identity_sessions,
        IdentityControlOperationTable,
        **{**values, "operation_ref": "operation:4", "operation_type": "identity.disable"},
    )


async def test_bootstrap_completion_cannot_be_reused_for_another_identity(
    identity_sessions: async_sessionmaker[AsyncSession],
) -> None:
    values = {
        "bootstrap_ref": "bootstrap:1",
        "environment": "test",
        "bootstrap_type": "identity.foundation",
        "configuration_digest": "a" * 64,
        "initial_identity_ref": "identity:human:1",
        "initial_grant_refs": [],
        "completed_at": NOW,
    }
    await _insert(identity_sessions, BootstrapRecordTable, **values)
    with pytest.raises(IntegrityError):
        await _insert(
            identity_sessions,
            BootstrapRecordTable,
            **{**values, "bootstrap_ref": "bootstrap:2", "initial_identity_ref": "identity:service:1"},
        )
    async with identity_sessions() as session:
        row = (await session.execute(select(BootstrapRecordTable))).scalar_one()
        assert row.initial_grant_refs == []
        assert row.completed_at == NOW
