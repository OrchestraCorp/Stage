from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.identity.administration import (
    BOOTSTRAP_TYPE,
    BootstrapPlan,
    IdentityAdministrationError,
    IdentityOperationUnknown,
    RevocationRequest,
    bootstrap,
    operation_result,
    revoke,
)
from app.identity.service import content_digest, profile_content
from app.models.authorization import RoleAssignmentTable
from app.models.execution import TenantGuardTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import (
    AuthenticationProfileTable,
    BootstrapRecordTable,
    ExternalIdentityTable,
    IdentityControlOperationTable,
    OrganizationMembershipTable,
    OrganizationRootTable,
    TrustStateTable,
    WorkloadRegistrationTable,
)
from tests.support.identity_administration import (
    OPERATOR,
    install,
    plan_data,
)


async def test_bootstrap_persists_only_explicit_identity_facts_and_replays_without_grants(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    plan = BootstrapPlan.model_validate(plan_data())
    first = await bootstrap(persistence_sessions, plan, actor_ref=OPERATOR, idempotency_key="install")
    assert await bootstrap(persistence_sessions, plan, actor_ref=OPERATOR, idempotency_key="install") == first
    replay = await bootstrap(persistence_sessions, plan, actor_ref="other-local-operator", idempotency_key="new-key")
    assert replay.result == first.result and replay.operation_ref != first.operation_ref
    assert (
        await operation_result(
            persistence_sessions,
            environment="test",
            actor_ref="other-local-operator",
            operation_type="bootstrap.identity-foundation",
            idempotency_key="new-key",
        )
        == replay
    )
    assert first.result["business_grants"] == []
    assert (
        await operation_result(
            persistence_sessions,
            environment="test",
            actor_ref=OPERATOR,
            operation_type="bootstrap.identity-foundation",
            idempotency_key="install",
        )
        == first
    )
    async with persistence_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PrincipalTable)) == 2
        assert await session.scalar(select(func.count()).select_from(OrganizationMembershipTable)) == 2
        assert await session.scalar(select(func.count()).select_from(RoleAssignmentTable)) == 0
        assert await session.scalar(select(func.count()).select_from(IdentityControlOperationTable)) == 2
        record = await session.scalar(select(BootstrapRecordTable))
        assert record is not None and record.initial_grant_refs == []
        profile = await session.get(AuthenticationProfileTable, ("human", 1))
        assert profile is not None and profile.digest == content_digest(profile_content(profile))
        mapping = await session.get(ExternalIdentityTable, "external-service")
        member = await session.get(OrganizationMembershipTable, ("tenant-a", "membership-service"))
        workload = await session.get(WorkloadRegistrationTable, "workload-a")
        assert mapping is not None and mapping.link_evidence_ref == first.operation_ref
        assert member is not None and member.source_ref == first.operation_ref
        assert workload is not None and workload.service_principal_ref == "service-a"


async def test_bootstrap_conflicting_replays_do_not_reconfigure(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await install(persistence_sessions)
    changed = plan_data()
    changed["display_name"] = "Unapproved replacement"
    for key, code in (("install", "idempotency_conflict"), ("other", "bootstrap_already_completed")):
        with pytest.raises(IdentityAdministrationError, match=code):
            await bootstrap(
                persistence_sessions, BootstrapPlan.model_validate(changed), actor_ref=OPERATOR, idempotency_key=key
            )
    async with persistence_sessions() as session:
        tenant = await session.get(OrganizationRootTable, "tenant-a")
        assert tenant is not None and tenant.display_name == plan_data()["display_name"]
        assert await session.scalar(select(func.count()).select_from(IdentityControlOperationTable)) == 1


async def test_canonical_organization_plan_is_the_new_wire_shape(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    data = plan_data()
    data["organization_ref"] = data.pop("tenant_ref")
    plan = BootstrapPlan.model_validate(data)
    result = await bootstrap(persistence_sessions, plan, actor_ref=OPERATOR, idempotency_key="organization-install")
    assert result.result["organization_ref"] == "tenant-a"
    assert "tenant_ref" not in result.result
    assert "tenant_ref" not in plan.model_dump()


async def test_historical_bootstrap_digest_replays_both_spellings_without_reopening_identity(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await install(persistence_sessions)
    # Exact pre-rename model_dump: optional nested workload_ref was serialized
    # as null even when absent in the installation file.
    historical = plan_data()
    historical["identities"][0]["workload_ref"] = None
    legacy_digest = content_digest(historical)
    async with persistence_sessions.begin() as session:
        record = (await session.scalars(select(BootstrapRecordTable))).one()
        operation = (await session.scalars(select(IdentityControlOperationTable))).one()
        legacy_result = {
            "bootstrap_ref": record.bootstrap_ref,
            "tenant_ref": "tenant-a",
            "principal_refs": ["human-a", "service-a"],
            "business_grants": [],
        }
        record.configuration_digest = legacy_digest
        operation.input_digest = legacy_digest
        operation.result_json = legacy_result
        operation_ref = operation.operation_ref
        principal = (
            await session.scalars(select(PrincipalTable).where(PrincipalTable.principal_ref == "human-a"))
        ).one()
        principal.status = "revoked"
        principal.version = 2
        principal.identity_epoch = 1
        principal.disabled_at = datetime.now(UTC)
    canonical = plan_data()
    canonical["organization_ref"] = canonical.pop("tenant_ref")
    for data in (historical, canonical):
        result = await bootstrap(
            persistence_sessions, BootstrapPlan.model_validate(data), actor_ref=OPERATOR, idempotency_key="install"
        )
        assert result.operation_ref == operation_ref
        assert result.result["organization_ref"] == "tenant-a" and "tenant_ref" not in result.result
    await bootstrap(
        persistence_sessions, BootstrapPlan.model_validate(canonical), actor_ref=OPERATOR, idempotency_key="new-key"
    )
    async with persistence_sessions() as session:
        original = await session.get(IdentityControlOperationTable, operation_ref)
        assert original is not None and original.input_digest == legacy_digest and original.result_json == legacy_result
        assert await session.scalar(select(func.count()).select_from(BootstrapRecordTable)) == 1
        assert await session.scalar(select(func.count()).select_from(RoleAssignmentTable)) == 0
        principal = (
            await session.scalars(select(PrincipalTable).where(PrincipalTable.principal_ref == "human-a"))
        ).one()
        assert principal.status == "revoked" and principal.version == 2 and principal.identity_epoch == 1


async def test_legacy_tenant_revocation_journal_is_recoverable_as_organization(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await install(persistence_sessions)
    historical = {
        "environment": "test",
        "kind": "tenant",
        "target_ref": "tenant-a",
        "expected_version": 1,
        "tenant_ref": None,
        "profile_version": None,
    }
    legacy_result = {
        "kind": "tenant",
        "target_ref": "tenant-a",
        "status": "revoked",
        "version": 2,
        "tenant_authz_epoch": 2,
    }
    # Insert the original durable encoding independently of the new writer.
    async with persistence_sessions.begin() as session:
        organization = await session.get(OrganizationRootTable, "tenant-a")
        guard = await session.get(TenantGuardTable, "tenant-a")
        assert organization is not None and guard is not None
        organization.status = "revoked"
        organization.version = 2
        guard.epoch = 2
        session.add(
            IdentityControlOperationTable(
                operation_ref="historical-revoke",
                operation_type="revoke.tenant",
                idempotency_key="legacy-revoke",
                input_digest=content_digest(historical),
                actor_kind="bootstrap_operator",
                actor_ref=OPERATOR,
                authentication_json={"source": "local_os_operator", "environment": "test"},
                target_ref="tenant-a",
                before_json={"status": "active", "version": 1, "tenant_authz_epoch": 1},
                after_json={"status": "revoked", "version": 2, "tenant_authz_epoch": 2},
                result_json=legacy_result,
                completed_at=datetime.now(UTC),
            )
        )
    canonical = {**historical, "kind": "organization"}
    canonical["organization_ref"] = canonical.pop("tenant_ref")
    for data in (historical, canonical):
        replay = await revoke(
            persistence_sessions,
            RevocationRequest.model_validate(data),
            actor_ref=OPERATOR,
            idempotency_key="legacy-revoke",
        )
        assert replay.operation_ref == "historical-revoke"
        assert replay.result["kind"] == "organization" and replay.result["organization_authz_epoch"] == 2
    for name in ("revoke.tenant", "revoke.organization"):
        recovered = await operation_result(
            persistence_sessions,
            environment="test",
            actor_ref=OPERATOR,
            operation_type=name,
            idempotency_key="legacy-revoke",
        )
        assert recovered == replay
    async with persistence_sessions() as session:
        row = await session.get(IdentityControlOperationTable, "historical-revoke")
        organization = await session.get(OrganizationRootTable, "tenant-a")
        guard = await session.get(TenantGuardTable, "tenant-a")
        assert row is not None and row.result_json == legacy_result and row.input_digest == content_digest(historical)
        assert organization is not None and organization.version == 2 and organization.status == "revoked"
        assert guard is not None and guard.epoch == 2
        assert await session.scalar(select(func.count()).select_from(IdentityControlOperationTable)) == 2


def test_organization_and_legacy_alias_conflicts_are_rejected() -> None:
    data = plan_data()
    data["organization_ref"] = "organization-a"
    with pytest.raises(ValidationError, match="organization_ref_tenant_ref_conflict"):
        BootstrapPlan.model_validate(data)
    with pytest.raises(ValidationError, match="organization_ref_tenant_ref_conflict"):
        RevocationRequest.model_validate(
            {
                "environment": "test",
                "kind": "membership",
                "target_ref": "membership-human",
                "expected_version": 1,
                "organization_ref": "organization-a",
                "tenant_ref": "tenant-a",
            }
        )


@pytest.mark.parametrize("idempotency_key", ("install", "second-organization"))
async def test_bootstrap_refuses_a_second_organization_before_install_or_replay(
    persistence_sessions: async_sessionmaker[AsyncSession],
    idempotency_key: str,
) -> None:
    await install(persistence_sessions)
    data = plan_data()
    data["tenant_ref"] = "another-enterprise"
    with pytest.raises(IdentityAdministrationError, match="identity_database_organization_mismatch"):
        await bootstrap(
            persistence_sessions,
            BootstrapPlan.model_validate(data),
            actor_ref=OPERATOR,
            idempotency_key=idempotency_key,
        )
    async with persistence_sessions() as session:
        organization = (await session.scalars(select(OrganizationRootTable))).one()
        assert organization.organization_ref == "tenant-a" and organization.deployment_slot == 1
        assert await session.scalar(select(func.count()).select_from(BootstrapRecordTable)) == 1
        assert await session.scalar(select(func.count()).select_from(IdentityControlOperationTable)) == 1


@pytest.mark.parametrize("idempotency_key", ("install", "install-dev"))
async def test_bootstrap_refuses_a_second_environment_before_install_or_replay(
    persistence_sessions: async_sessionmaker[AsyncSession],
    idempotency_key: str,
) -> None:
    await install(persistence_sessions)
    data = plan_data()
    data["environment"] = "dev"
    data["issuer"] = "http://127.0.0.1:18080/realms/stage-dev"
    data["jwks_url"] = f"{data['issuer']}/protocol/openid-connect/certs"
    for field in ("tenant_ref", "provider_ref", "trust_ref"):
        data[field] = f"dev:{data[field]}"
    for profile in data["profiles"]:
        profile["profile_ref"] = f"dev:{profile['profile_ref']}"
    for identity in data["identities"]:
        for field in ("principal_ref", "profile_ref", "external_identity_ref", "membership_ref", "workload_ref"):
            if field in identity:
                identity[field] = f"dev:{identity[field]}"
    with pytest.raises(IdentityAdministrationError, match="identity_database_environment_mismatch"):
        await bootstrap(
            persistence_sessions,
            BootstrapPlan.model_validate(data),
            actor_ref=OPERATOR,
            idempotency_key=idempotency_key,
        )
    async with persistence_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(BootstrapRecordTable)) == 1
        assert await session.scalar(select(func.count()).select_from(PrincipalTable)) == 2
        assert await session.scalar(select(func.count()).select_from(IdentityControlOperationTable)) == 1


async def test_revocation_refuses_the_wrong_database_environment(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await install(persistence_sessions)
    request = RevocationRequest(environment="dev", kind="principal", target_ref="human-a", expected_version=1)
    with pytest.raises(IdentityAdministrationError, match="identity_database_environment_mismatch"):
        await revoke(persistence_sessions, request, actor_ref=OPERATOR, idempotency_key="wrong-environment")
    async with persistence_sessions() as session:
        principal = await session.scalar(select(PrincipalTable).where(PrincipalTable.principal_ref == "human-a"))
        assert principal is not None and principal.status == "active" and principal.version == 1
        assert await session.scalar(select(func.count()).select_from(IdentityControlOperationTable)) == 1


@pytest.mark.parametrize("operation", ("bootstrap", "bootstrap-replay", "revoke", "revoke-replay"))
async def test_mixed_database_environments_block_all_writes_but_preserve_result_recovery(
    persistence_sessions: async_sessionmaker[AsyncSession],
    operation: str,
) -> None:
    plan = BootstrapPlan.model_validate(plan_data())
    installed = await bootstrap(persistence_sessions, plan, actor_ref=OPERATOR, idempotency_key="install")
    request = RevocationRequest(environment="test", kind="principal", target_ref="human-a", expected_version=1)
    prior_revocation = None
    if operation == "revoke-replay":
        prior_revocation = await revoke(persistence_sessions, request, actor_ref=OPERATOR, idempotency_key="revoke")
    # Represent a database written before the single-environment deployment rule.
    async with persistence_sessions() as session, session.begin():
        session.add(
            BootstrapRecordTable(
                bootstrap_ref="historic-dev-installation",
                environment="dev",
                bootstrap_type=BOOTSTRAP_TYPE,
                configuration_digest="a" * 64,
                initial_identity_ref="external-human",
                initial_grant_refs=[],
                completed_at=datetime.now(UTC),
            )
        )
    with pytest.raises(IdentityAdministrationError, match="identity_database_environment_conflict"):
        if operation.startswith("bootstrap"):
            await bootstrap(
                persistence_sessions,
                plan,
                actor_ref=OPERATOR,
                idempotency_key="install" if operation.endswith("replay") else "new-install",
            )
        else:
            await revoke(persistence_sessions, request, actor_ref=OPERATOR, idempotency_key="revoke")
    assert (
        await operation_result(
            persistence_sessions,
            environment="test",
            actor_ref=OPERATOR,
            operation_type="bootstrap.identity-foundation",
            idempotency_key="install",
        )
        == installed
    )
    if prior_revocation is not None:
        assert (
            await operation_result(
                persistence_sessions,
                environment="test",
                actor_ref=OPERATOR,
                operation_type="revoke.principal",
                idempotency_key="revoke",
            )
            == prior_revocation
        )
    with pytest.raises(IdentityAdministrationError, match="operation_not_found"):
        await operation_result(
            persistence_sessions,
            environment="dev",
            actor_ref=OPERATOR,
            operation_type="bootstrap.identity-foundation",
            idempotency_key="install",
        )
    async with persistence_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(BootstrapRecordTable)) == 2
        assert await session.scalar(select(func.count()).select_from(IdentityControlOperationTable)) == (
            2 if prior_revocation is not None else 1
        )
        principal = await session.scalar(select(PrincipalTable).where(PrincipalTable.principal_ref == "human-a"))
        assert principal is not None and principal.version == (2 if prior_revocation is not None else 1)
        assert principal.status == ("revoked" if prior_revocation is not None else "active")


async def test_existing_principal_is_not_implicitly_linked_and_failed_install_is_atomic(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with persistence_sessions() as session, session.begin():
        session.add(PrincipalTable(principal_ref="human-a", principal_type="human"))
    with pytest.raises(IdentityAdministrationError, match="identity_configuration_conflict"):
        await install(persistence_sessions)
    async with persistence_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PrincipalTable)) == 1
        assert await session.scalar(select(func.count()).select_from(TrustStateTable)) == 0
        assert await session.scalar(select(func.count()).select_from(OrganizationRootTable)) == 0
        assert await session.scalar(select(func.count()).select_from(BootstrapRecordTable)) == 0
        assert await session.scalar(select(func.count()).select_from(IdentityControlOperationTable)) == 0


@pytest.mark.parametrize(
    "case",
    [
        "prod",
        "namespace",
        "kind",
        "extra",
        "client",
        "duplicate",
        "workload",
        "nul-subject",
        "surrogate-subject",
        "nul-reference",
    ],
)
def test_bootstrap_rejects_ambiguous_or_unadmitted_identity_plans(case: str) -> None:
    data = plan_data()
    if case == "prod":
        data["environment"] = "prod"
    elif case == "namespace":
        data["profiles"][1]["subject_namespace"] = "human/sub"
    elif case == "kind":
        data["identities"][0]["principal_type"] = "service"
    elif case == "extra":
        data["identities"][0]["email"] = "not-an-identity@example.invalid"
    elif case == "client":
        data["identities"][0]["client_ref"] = "unknown-client"
    elif case == "duplicate":
        data["identities"][1]["principal_ref"] = "human-a"
    elif case == "workload":
        data["identities"][1].pop("workload_ref")
    elif case == "nul-subject":
        data["identities"][0]["subject"] = "subject\x00suffix"
    elif case == "surrogate-subject":
        data["identities"][0]["subject"] = "subject\ud800suffix"
    else:
        data["identities"][0]["principal_ref"] = "person\x00suffix"
    with pytest.raises(ValidationError):
        BootstrapPlan.model_validate(data)


@pytest.mark.parametrize(
    ("kind", "target", "extra"),
    [
        ("principal", "human-a", {}),
        ("tenant", "tenant-a", {}),
        ("organization", "tenant-a", {}),
        ("membership", "membership-human", {"tenant_ref": "tenant-a"}),
        ("profile", "human", {"profile_version": 1}),
    ],
)
async def test_revocation_updates_current_state_and_audit_once(
    persistence_sessions: async_sessionmaker[AsyncSession],
    kind: str,
    target: str,
    extra: dict[str, Any],
) -> None:
    await install(persistence_sessions)
    request = RevocationRequest.model_validate(
        {
            "environment": "test",
            "kind": kind,
            "target_ref": target,
            "expected_version": 1,
            **extra,
        }
    )
    first = await revoke(persistence_sessions, request, actor_ref=OPERATOR, idempotency_key="revoke")
    assert first.result["status"] == "revoked" and first.result["version"] == 2
    assert await revoke(persistence_sessions, request, actor_ref=OPERATOR, idempotency_key="revoke") == first
    assert (
        await operation_result(
            persistence_sessions,
            environment="test",
            actor_ref=OPERATOR,
            operation_type=f"revoke.{kind}",
            idempotency_key="revoke",
        )
        == first
    )
    with pytest.raises(IdentityAdministrationError, match="version_conflict"):
        await revoke(persistence_sessions, request, actor_ref=OPERATOR, idempotency_key="another-key")
    async with persistence_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(IdentityControlOperationTable)) == 2
        guard = await session.get(TenantGuardTable, "tenant-a")
        assert guard is not None and guard.epoch == (2 if kind in {"tenant", "organization", "membership"} else 1)
        if kind == "principal":
            principal = await session.scalar(select(PrincipalTable).where(PrincipalTable.principal_ref == "human-a"))
            assert principal is not None and principal.identity_epoch == 1 and principal.disabled_at is not None
        elif kind == "membership":
            member = await session.get(OrganizationMembershipTable, ("tenant-a", "membership-human"))
            assert member is not None and member.ended_at is not None
        elif kind == "profile":
            trust = await session.get(TrustStateTable, "trust-a")
            assert trust is not None and trust.trust_config_revision == 2 and trust.version == 2


async def test_revocation_conflicts_and_result_lookup_do_not_change_state(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await install(persistence_sessions)
    request = RevocationRequest(environment="test", kind="principal", target_ref="human-a", expected_version=2)
    with pytest.raises(IdentityAdministrationError, match="version_conflict"):
        await revoke(persistence_sessions, request, actor_ref=OPERATOR, idempotency_key="revoke")
    request = RevocationRequest(environment="test", kind="principal", target_ref="human-a", expected_version=1)
    await revoke(persistence_sessions, request, actor_ref=OPERATOR, idempotency_key="revoke")
    other = RevocationRequest(environment="test", kind="principal", target_ref="service-a", expected_version=1)
    with pytest.raises(IdentityAdministrationError, match="idempotency_conflict"):
        await revoke(persistence_sessions, other, actor_ref=OPERATOR, idempotency_key="revoke")
    with pytest.raises(IdentityAdministrationError, match="operation_not_found"):
        await operation_result(
            persistence_sessions,
            environment="test",
            actor_ref="other-local-operator",
            operation_type="revoke.principal",
            idempotency_key="revoke",
        )
    async with persistence_sessions() as session:
        principal = await session.scalar(select(PrincipalTable).where(PrincipalTable.principal_ref == "service-a"))
        assert principal is not None and principal.status == "active" and principal.version == 1
        assert await session.scalar(select(func.count()).select_from(IdentityControlOperationTable)) == 2


async def test_concurrent_bootstrap_is_one_atomic_install(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    plan = BootstrapPlan.model_validate(plan_data())
    results = await asyncio.gather(
        *(
            bootstrap(persistence_sessions, plan, actor_ref=OPERATOR, idempotency_key=f"install-{index}")
            for index in range(2)
        )
    )
    assert results[0].result == results[1].result
    assert results[0].operation_ref != results[1].operation_ref
    async with persistence_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(BootstrapRecordTable)) == 1
        assert await session.scalar(select(func.count()).select_from(IdentityControlOperationTable)) == 2


@pytest.mark.parametrize("operation", ["bootstrap", "bootstrap-replay", "revoke"])
async def test_committed_operation_with_lost_response_recovers_by_original_key(
    persistence_sessions: async_sessionmaker[AsyncSession],
    operation: str,
) -> None:
    if operation in {"revoke", "bootstrap-replay"}:
        await install(persistence_sessions)
    plan = BootstrapPlan.model_validate(plan_data())
    request = RevocationRequest(environment="test", kind="principal", target_ref="human-a", expected_version=1)

    def lose_commit_response(_session: Any) -> None:
        raise SQLAlchemyError("Simulated response loss after a successful COMMIT")

    event.listen(AsyncSession.sync_session_class, "after_commit", lose_commit_response)
    try:
        with pytest.raises(IdentityOperationUnknown, match="result_unknown"):
            if operation.startswith("bootstrap"):
                await bootstrap(persistence_sessions, plan, actor_ref=OPERATOR, idempotency_key="lost-response")
            else:
                await revoke(persistence_sessions, request, actor_ref=OPERATOR, idempotency_key="lost-response")
    finally:
        event.remove(AsyncSession.sync_session_class, "after_commit", lose_commit_response)
    recovered = await operation_result(
        persistence_sessions,
        environment="test",
        actor_ref=OPERATOR,
        operation_type="bootstrap.identity-foundation" if operation.startswith("bootstrap") else "revoke.principal",
        idempotency_key="lost-response",
    )
    if operation.startswith("bootstrap"):
        assert (
            await bootstrap(persistence_sessions, plan, actor_ref=OPERATOR, idempotency_key="lost-response")
            == recovered
        )
    else:
        assert (
            await revoke(persistence_sessions, request, actor_ref=OPERATOR, idempotency_key="lost-response")
            == recovered
        )
    async with persistence_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(IdentityControlOperationTable)) == (
            1 if operation == "bootstrap" else 2
        )


@pytest.mark.parametrize("kind", ["principal", "profile"])
async def test_postgresql_revocation_waits_for_shared_identity_reader(
    persistence_sessions: async_sessionmaker[AsyncSession],
    kind: str,
) -> None:
    async with persistence_sessions() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("PostgreSQL shared/exclusive row locks require a real PostgreSQL database")
    await install(persistence_sessions)
    request = RevocationRequest.model_validate(
        {
            "environment": "test",
            "kind": kind,
            "target_ref": "human-a" if kind == "principal" else "human",
            "expected_version": 1,
            **({"profile_version": 1} if kind == "profile" else {}),
        }
    )
    model = PrincipalTable if kind == "principal" else TrustStateTable
    async with persistence_sessions() as reader, reader.begin():
        await reader.scalar(select(model).with_for_update(read=True))
        task = asyncio.create_task(revoke(persistence_sessions, request, actor_ref=OPERATOR, idempotency_key="revoke"))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.15)
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
    result = await asyncio.wait_for(task, timeout=5)
    assert result.result["status"] == "revoked"


def test_cli_refuses_production_without_leaking_connection_secret() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/identity_admin.py",
            "--environment",
            "dev",
            "result",
            "--operation-type",
            "bootstrap.identity-foundation",
            "--idempotency-key",
            "install",
        ],
        env={
            **os.environ,
            "STAGE_ENV": "prod",
            "STAGE_DATABASE_URL": "postgresql+asyncpg://private-user:private-password@127.0.0.1/stage",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1 and "development_environment_required" in result.stderr
    assert "private-user" not in result.stderr and "private-password" not in result.stderr


@pytest.mark.parametrize(
    ("kind", "target", "organization_ref", "profile_version"),
    [
        ("principal", "human-a", None, None),
        ("organization", "tenant-a", None, None),
        ("membership", "membership-human", "tenant-a", None),
        ("profile", "human", None, 1),
    ],
)
def test_revocation_digest_preserves_historical_nulls_and_namespace(
    kind: str, target: str, organization_ref: str | None, profile_version: int | None
) -> None:
    historical = {
        "environment": "test",
        "kind": "tenant" if kind == "organization" else kind,
        "target_ref": target,
        "expected_version": 1,
        "tenant_ref": organization_ref,
        "profile_version": profile_version,
    }
    canonical = {**historical, "kind": kind}
    canonical["organization_ref"] = canonical.pop("tenant_ref")
    for data in (canonical, historical):
        request = RevocationRequest.model_validate(data)
        assert request.digest_payload() == historical
        assert "tenant_ref" not in request.model_dump() and request.kind == kind
