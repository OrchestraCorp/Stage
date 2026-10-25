"""Offline development identity installation and revocation; never a public IAM API."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.execution.storage import lock_tenant
from app.identity.authentication import KeycloakProfile
from app.identity.service import (
    PROFILE_SCHEMA,
    AdmissionConstraints,
    ProfileConfiguration,
    content_digest,
    profile_content,
)
from app.models.execution import TenantGuardTable
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

Reference = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]
Environment = Literal["dev", "test"]
BOOTSTRAP_TYPE = "identity-foundation"


class IdentityAdministrationError(Exception):
    """Stable public error code; underlying database or configuration data stays private."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class IdentityOperationUnknown(Exception):
    """A database failure does not prove whether the operation committed."""

    def __init__(self, operation_type: str, idempotency_key: str) -> None:
        self.operation_type = operation_type
        self.idempotency_key = idempotency_key
        super().__init__("result_unknown")


class StrictPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProfilePlan(StrictPlan):
    profile_ref: Reference
    profile_version: int = Field(ge=1)
    subject_namespace: Reference
    credential_kind: Literal["human_oidc", "workload_oauth2"]
    allowed_clients: tuple[Reference, ...] = Field(min_length=1, max_length=32)


class IdentityPlan(StrictPlan):
    principal_ref: Reference
    principal_type: Literal["human", "service"]
    subject: str = Field(min_length=1, max_length=256)
    profile_ref: Reference
    profile_version: int = Field(ge=1)
    client_ref: Reference
    external_identity_ref: Reference
    membership_ref: Reference
    workload_ref: Reference | None = None


def _organization_input(values: Any) -> Any:
    """Accept old operator input without exposing two mutable model fields."""
    if not isinstance(values, dict):
        return values
    data = dict(values)
    if "tenant_ref" in data:
        legacy = data.pop("tenant_ref")
        if "organization_ref" in data and data["organization_ref"] != legacy:
            raise ValueError("organization_ref_tenant_ref_conflict")
        data["organization_ref"] = legacy
    return data


class BootstrapPlan(StrictPlan):
    environment: Environment
    organization_ref: Reference
    display_name: str = Field(min_length=1, max_length=256)
    issuer: str = Field(min_length=1, max_length=512)
    jwks_url: str = Field(min_length=1, max_length=1024)
    audience: str = Field(min_length=1, max_length=256)
    provider_ref: Reference
    trust_ref: Reference
    profiles: tuple[ProfilePlan, ...] = Field(min_length=1, max_length=32)
    identities: tuple[IdentityPlan, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="before")
    @classmethod
    def normalize_organization_ref(cls, values: Any) -> Any:
        return _organization_input(values)

    def digest_payload(self) -> dict[str, Any]:
        # This persisted encoding predates the model rename. New and legacy
        # inputs must hash identically, including nested optional null fields.
        data = self.model_dump(mode="json")
        data["tenant_ref"] = data.pop("organization_ref")
        return data

    @model_validator(mode="after")
    def validate_links(self) -> Self:
        profiles = {(p.profile_ref, p.profile_version): p for p in self.profiles}
        if len(profiles) != len(self.profiles):
            raise ValueError("duplicate_profile")
        namespaces: dict[str, str] = {}
        for profile in self.profiles:
            previous = namespaces.setdefault(profile.subject_namespace, profile.credential_kind)
            if previous != profile.credential_kind:
                raise ValueError("namespace_semantics_conflict")
            KeycloakProfile(
                profile_ref=profile.profile_ref,
                profile_version=profile.profile_version,
                issuer=self.issuer,
                jwks_url=self.jwks_url,
                audience=self.audience,
                credential_kind=profile.credential_kind,
                allowed_clients=profile.allowed_clients,
                subject_namespace=profile.subject_namespace,
                environment=self.environment,
                allow_loopback_http=True,
            )
        unique_fields = ("principal_ref", "external_identity_ref", "membership_ref", "workload_ref")
        for field in unique_fields:
            values = [getattr(identity, field) for identity in self.identities if getattr(identity, field) is not None]
            if len(set(values)) != len(values):
                raise ValueError("duplicate_identity_reference")
        subjects: set[tuple[str, str]] = set()
        for identity in self.identities:
            selected_profile = profiles.get((identity.profile_ref, identity.profile_version))
            if selected_profile is None or identity.client_ref not in selected_profile.allowed_clients:
                raise ValueError("identity_profile_mismatch")
            expected = "human" if selected_profile.credential_kind == "human_oidc" else "service"
            if identity.principal_type != expected or (identity.workload_ref is not None) != (expected == "service"):
                raise ValueError("identity_type_mismatch")
            subject = (selected_profile.subject_namespace, identity.subject)
            if subject in subjects or not identity.subject.strip() or len(identity.subject.encode()) > 256:
                raise ValueError("invalid_or_duplicate_subject")
            subjects.add(subject)
        return self


class RevocationRequest(StrictPlan):
    environment: Environment
    kind: Literal["principal", "organization", "membership", "profile"]
    target_ref: Reference
    expected_version: int = Field(ge=1)
    organization_ref: Reference | None = None
    profile_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_input(cls, values: Any) -> Any:
        data = _organization_input(values)
        if isinstance(data, dict) and data.get("kind") == "tenant":
            data["kind"] = "organization"
        return data

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if (self.organization_ref is not None) != (self.kind == "membership"):
            raise ValueError("membership_requires_organization")
        if (self.profile_version is not None) != (self.kind == "profile"):
            raise ValueError("profile_requires_version")
        return self

    @property
    def journal_kind(self) -> str:
        # Keep committed journal identities stable across the terminology change.
        return "tenant" if self.kind == "organization" else self.kind

    def digest_payload(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["tenant_ref"] = data.pop("organization_ref")
        data["kind"] = self.journal_kind
        return data


class AdministrationResult(StrictPlan):
    operation_ref: str
    result: dict[str, Any]


def _public_result(operation_ref: str, value: dict[str, Any]) -> AdministrationResult:
    # Adapt immutable historical journal fields at the operator boundary.
    result = dict(value)
    for old, new in (("tenant_ref", "organization_ref"), ("tenant_authz_epoch", "organization_authz_epoch")):
        if old in result:
            result[new] = result.pop(old)
    if result.get("kind") == "tenant":
        result["kind"] = "organization"
    return AdministrationResult(operation_ref=operation_ref, result=result)


def _validate_operator(actor_ref: str, idempotency_key: str) -> None:
    if any(not value.strip() or len(value) > 128 for value in (actor_ref, idempotency_key)):
        raise IdentityAdministrationError("invalid_operation_identity")


async def _admin_lock(session: AsyncSession) -> None:
    # A single offline installation/operator mutex also serializes journal lookup.
    # PostgreSQL reads still coordinate through the trust/principal/tenant row locks.
    if session.get_bind().dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(b"stage-development-identity-admin").digest()[:8], signed=True)
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    else:
        await session.execute(text("BEGIN IMMEDIATE"))


async def _check_database_environment(session: AsyncSession, environment: Environment) -> None:
    # A Stage database belongs to one deployment environment. Check under the
    # installation mutex, before replay as well as before changing identity facts.
    environments = set(
        (
            await session.scalars(
                select(BootstrapRecordTable.environment)
                .where(BootstrapRecordTable.bootstrap_type == BOOTSTRAP_TYPE)
                .distinct()
            )
        ).all()
    )
    if len(environments) > 1:
        raise IdentityAdministrationError("identity_database_environment_conflict")
    if environments and environment not in environments:
        raise IdentityAdministrationError("identity_database_environment_mismatch")


@asynccontextmanager
async def _operation_transaction(
    sessions: async_sessionmaker[AsyncSession],
    operation_type: str,
    idempotency_key: str,
) -> AsyncIterator[AsyncSession]:
    try:
        async with sessions() as session, session.begin():
            yield session
    except IntegrityError:
        # A rejected uniqueness/foreign-key statement cannot have committed this transaction.
        raise
    except SQLAlchemyError:
        # Connection loss may occur after COMMIT. Recover with the original operator/key.
        raise IdentityOperationUnknown(operation_type, idempotency_key) from None


async def _replay(
    session: AsyncSession,
    actor_ref: str,
    operation_type: str,
    idempotency_key: str,
    digest: str,
) -> AdministrationResult | None:
    row = await session.scalar(
        select(IdentityControlOperationTable).where(
            IdentityControlOperationTable.actor_kind == "bootstrap_operator",
            IdentityControlOperationTable.actor_ref == actor_ref,
            IdentityControlOperationTable.operation_type == operation_type,
            IdentityControlOperationTable.idempotency_key == idempotency_key,
        )
    )
    if row is None:
        return None
    if row.input_digest != digest:
        raise IdentityAdministrationError("idempotency_conflict")
    return _public_result(row.operation_ref, row.result_json)


def _journal(
    session: AsyncSession,
    *,
    actor_ref: str,
    operation_type: str,
    idempotency_key: str,
    digest: str,
    environment: str,
    target_ref: str,
    result: dict[str, Any],
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    operation_ref: str | None = None,
) -> AdministrationResult:
    reference = operation_ref or str(uuid4())
    session.add(
        IdentityControlOperationTable(
            operation_ref=reference,
            operation_type=operation_type,
            idempotency_key=idempotency_key,
            input_digest=digest,
            actor_kind="bootstrap_operator",
            actor_ref=actor_ref,
            authentication_json={"source": "local_os_operator", "environment": environment},
            target_ref=target_ref,
            before_json=before,
            after_json=after,
            result_json=result,
            completed_at=datetime.now(UTC),
        )
    )
    return _public_result(reference, result)


async def bootstrap(
    sessions: async_sessionmaker[AsyncSession],
    plan: BootstrapPlan,
    *,
    actor_ref: str,
    idempotency_key: str,
) -> AdministrationResult:
    """Install one explicit development/test identity foundation, with no business grants."""
    _validate_operator(actor_ref, idempotency_key)
    # The digest deliberately preserves the legacy wire encoding for old
    # tenant_ref plans, so replaying a committed operation remains exact.
    digest = content_digest(plan.digest_payload())
    try:
        async with _operation_transaction(sessions, "bootstrap.identity-foundation", idempotency_key) as session:
            await _admin_lock(session)
            await _check_database_environment(session, plan.environment)
            organization_ref = await session.scalar(select(OrganizationRootTable.organization_ref))
            if organization_ref is not None and organization_ref != plan.organization_ref:
                raise IdentityAdministrationError("identity_database_organization_mismatch")
            replay = await _replay(session, actor_ref, "bootstrap.identity-foundation", idempotency_key, digest)
            if replay is not None:
                return replay
            previous = await session.scalar(
                select(BootstrapRecordTable).where(
                    BootstrapRecordTable.environment == plan.environment,
                    BootstrapRecordTable.bootstrap_type == BOOTSTRAP_TYPE,
                )
            )
            if previous is not None:
                if previous.configuration_digest != digest:
                    raise IdentityAdministrationError("bootstrap_already_completed")
                operation = await session.scalar(
                    select(IdentityControlOperationTable)
                    .where(
                        IdentityControlOperationTable.operation_type == "bootstrap.identity-foundation",
                        IdentityControlOperationTable.target_ref == previous.bootstrap_ref,
                    )
                    .order_by(IdentityControlOperationTable.created_at, IdentityControlOperationTable.operation_ref)
                    .limit(1)
                )
                if operation is None:
                    raise IdentityAdministrationError("bootstrap_record_incomplete")
                return _journal(
                    session,
                    actor_ref=actor_ref,
                    operation_type="bootstrap.identity-foundation",
                    idempotency_key=idempotency_key,
                    digest=digest,
                    environment=plan.environment,
                    target_ref=previous.bootstrap_ref,
                    result=operation.result_json,
                )
            return await _install(session, plan, actor_ref, idempotency_key, digest)
    except IntegrityError:
        raise IdentityAdministrationError("identity_configuration_conflict") from None


async def _install(
    session: AsyncSession,
    plan: BootstrapPlan,
    actor_ref: str,
    idempotency_key: str,
    digest: str,
) -> AdministrationResult:
    now = datetime.now(UTC)
    bootstrap_ref = str(uuid4())
    operation_ref = str(uuid4())
    # Explicit insertion order keeps all foreign keys enforced, including SQLite fast checks.
    session.add(
        TrustStateTable(
            trust_ref=plan.trust_ref,
            purpose="authentication",
            configuration_digest=digest,
        )
    )
    session.add(OrganizationRootTable(organization_ref=plan.organization_ref, display_name=plan.display_name))
    session.add_all(
        PrincipalTable(principal_ref=i.principal_ref, principal_type=i.principal_type) for i in plan.identities
    )
    await session.flush()
    session.add(
        IdentityProviderTable(
            provider_ref=plan.provider_ref,
            issuer_exact=plan.issuer,
            trust_ref=plan.trust_ref,
        )
    )
    session.add(TenantGuardTable(tenant_ref=plan.organization_ref, epoch=1))
    await session.flush()
    profiles = {(p.profile_ref, p.profile_version): p for p in plan.profiles}
    for profile in plan.profiles:
        row = AuthenticationProfileTable(
            profile_ref=profile.profile_ref,
            profile_version=profile.profile_version,
            provider_ref=plan.provider_ref,
            subject_namespace=profile.subject_namespace,
            credential_kind=profile.credential_kind,
            stage_audience=plan.audience,
            config_schema=PROFILE_SCHEMA,
            config_json=ProfileConfiguration(
                jwks_url=plan.jwks_url,
                allowed_clients=profile.allowed_clients,
            ).model_dump(mode="json"),
        )
        row.digest = content_digest(profile_content(row))
        session.add(row)
    for identity in plan.identities:
        profile = profiles[(identity.profile_ref, identity.profile_version)]
        session.add(
            ExternalIdentityTable(
                external_identity_ref=identity.external_identity_ref,
                provider_ref=plan.provider_ref,
                subject_namespace=profile.subject_namespace,
                subject_exact=identity.subject,
                principal_ref=identity.principal_ref,
                principal_type=identity.principal_type,
                link_evidence_ref=operation_ref,
            )
        )
        session.add(
            OrganizationMembershipTable(
                organization_ref=plan.organization_ref,
                membership_ref=identity.membership_ref,
                principal_ref=identity.principal_ref,
                principal_type=identity.principal_type,
                valid_from=now,
                source_ref=operation_ref,
            )
        )
    await session.flush()
    for profile in plan.profiles:
        session.add(
            OrganizationAuthenticationBindingTable(
                organization_ref=plan.organization_ref,
                binding_ref=str(uuid4()),
                profile_ref=profile.profile_ref,
                profile_version=profile.profile_version,
                environment=plan.environment,
                valid_from=now,
                constraints_json=AdmissionConstraints(allowed_clients=profile.allowed_clients).model_dump(mode="json"),
            )
        )
    for identity in plan.identities:
        if identity.workload_ref is not None:
            session.add(
                WorkloadRegistrationTable(
                    workload_ref=identity.workload_ref,
                    service_principal_ref=identity.principal_ref,
                    profile_ref=identity.profile_ref,
                    profile_version=identity.profile_version,
                    external_identity_ref=identity.external_identity_ref,
                    verified_client_ref=identity.client_ref,
                    environment=plan.environment,
                    valid_from=now,
                )
            )
    session.add(
        BootstrapRecordTable(
            bootstrap_ref=bootstrap_ref,
            environment=plan.environment,
            bootstrap_type=BOOTSTRAP_TYPE,
            configuration_digest=digest,
            initial_identity_ref=plan.identities[0].external_identity_ref,
            initial_grant_refs=[],
            completed_at=now,
        )
    )
    result = {
        "bootstrap_ref": bootstrap_ref,
        "tenant_ref": plan.organization_ref,
        "principal_refs": [i.principal_ref for i in plan.identities],
        "business_grants": [],
    }
    return _journal(
        session,
        actor_ref=actor_ref,
        operation_type="bootstrap.identity-foundation",
        idempotency_key=idempotency_key,
        digest=digest,
        environment=plan.environment,
        target_ref=bootstrap_ref,
        result=result,
        operation_ref=operation_ref,
    )


async def revoke(
    sessions: async_sessionmaker[AsyncSession],
    request: RevocationRequest,
    *,
    actor_ref: str,
    idempotency_key: str,
) -> AdministrationResult:
    """Terminal local revocation with expected version, current row locks and atomic journal."""
    _validate_operator(actor_ref, idempotency_key)
    digest = content_digest(request.digest_payload())
    # The old tenant operation name remains the durable journal identity. New
    # callers use revoke.organization; operation_result accepts both forms.
    operation_type = f"revoke.{request.journal_kind}"
    async with _operation_transaction(sessions, operation_type, idempotency_key) as session:
        await _admin_lock(session)
        await _check_database_environment(session, request.environment)
        replay = await _replay(session, actor_ref, operation_type, idempotency_key, digest)
        if replay is not None:
            return replay
        installed = await session.scalar(
            select(BootstrapRecordTable).where(
                BootstrapRecordTable.environment == request.environment,
                BootstrapRecordTable.bootstrap_type == BOOTSTRAP_TYPE,
            )
        )
        if installed is None:
            raise IdentityAdministrationError("identity_not_initialized")
        row = await _lock_target(session, request)
        if row.version != request.expected_version:
            raise IdentityAdministrationError("version_conflict")
        if row.status not in {"active", "suspended"}:
            raise IdentityAdministrationError("identity_already_ended")
        before = await _state_snapshot(session, row)
        row.status = "revoked"
        row.version += 1
        now = datetime.now(UTC)
        if isinstance(row, PrincipalTable):
            row.identity_epoch += 1
            row.disabled_at = now
        elif isinstance(row, OrganizationMembershipTable):
            row.ended_at = now
        elif isinstance(row, AuthenticationProfileTable):
            provider = await session.get(IdentityProviderTable, row.provider_ref)
            assert provider is not None
            trust = await session.get(TrustStateTable, provider.trust_ref)
            assert trust is not None
            trust.trust_config_revision += 1
            trust.version += 1
        if isinstance(row, OrganizationRootTable | OrganizationMembershipTable):
            guard = await session.get(TenantGuardTable, row.organization_ref)
            assert guard is not None
            guard.epoch += 1
        after = await _state_snapshot(session, row)
        result = {"kind": request.journal_kind, "target_ref": request.target_ref, **after}
        if request.organization_ref is not None:
            result["tenant_ref"] = request.organization_ref
        if request.profile_version is not None:
            result["profile_version"] = request.profile_version
        return _journal(
            session,
            actor_ref=actor_ref,
            operation_type=operation_type,
            idempotency_key=idempotency_key,
            digest=digest,
            environment=request.environment,
            target_ref=request.target_ref,
            result=result,
            before=before,
            after=after,
        )


async def _state_snapshot(
    session: AsyncSession,
    row: PrincipalTable | OrganizationRootTable | OrganizationMembershipTable | AuthenticationProfileTable,
) -> dict[str, Any]:
    state: dict[str, Any] = {"status": row.status, "version": row.version}
    if isinstance(row, PrincipalTable):
        state["identity_epoch"] = row.identity_epoch
        state["disabled_at"] = row.disabled_at.isoformat() if row.disabled_at is not None else None
    if isinstance(row, OrganizationMembershipTable):
        state["ended_at"] = row.ended_at.isoformat() if row.ended_at is not None else None
    if isinstance(row, OrganizationRootTable | OrganizationMembershipTable):
        guard = await session.get(TenantGuardTable, row.organization_ref)
        assert guard is not None
        state["tenant_authz_epoch"] = guard.epoch
    if isinstance(row, AuthenticationProfileTable):
        provider = await session.get(IdentityProviderTable, row.provider_ref)
        assert provider is not None
        trust = await session.get(TrustStateTable, provider.trust_ref)
        assert trust is not None
        state.update(
            trust_ref=trust.trust_ref,
            trust_config_revision=trust.trust_config_revision,
            key_status_revision=trust.key_status_revision,
            trust_version=trust.version,
        )
    return state


async def _lock_target(
    session: AsyncSession,
    request: RevocationRequest,
) -> PrincipalTable | OrganizationRootTable | OrganizationMembershipTable | AuthenticationProfileTable:
    row: PrincipalTable | OrganizationRootTable | OrganizationMembershipTable | AuthenticationProfileTable | None
    if request.kind == "principal":
        row = await session.scalar(
            select(PrincipalTable)
            .where(
                PrincipalTable.principal_ref == request.target_ref,
            )
            .with_for_update()
        )
    elif request.kind == "profile":
        profile = await session.get(AuthenticationProfileTable, (request.target_ref, request.profile_version))
        if profile is None:
            raise IdentityAdministrationError("target_not_found")
        provider = await session.get(IdentityProviderTable, profile.provider_ref)
        if provider is None:
            raise IdentityAdministrationError("target_not_found")
        await session.scalar(
            select(TrustStateTable)
            .where(
                TrustStateTable.trust_ref == provider.trust_ref,
            )
            .with_for_update()
        )
        row = await session.scalar(
            select(AuthenticationProfileTable)
            .where(
                AuthenticationProfileTable.profile_ref == request.target_ref,
                AuthenticationProfileTable.profile_version == request.profile_version,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    else:
        organization_ref = request.target_ref if request.kind == "organization" else request.organization_ref
        assert organization_ref is not None
        if request.kind == "membership":
            member = await session.get(OrganizationMembershipTable, (organization_ref, request.target_ref))
            if member is None:
                raise IdentityAdministrationError("target_not_found")
            await session.scalar(
                select(PrincipalTable)
                .where(
                    PrincipalTable.principal_ref == member.principal_ref,
                )
                .with_for_update(read=True)
            )
        if await session.get(TenantGuardTable, organization_ref) is None:
            raise IdentityAdministrationError("target_not_found")
        await lock_tenant(session, organization_ref)
        model = OrganizationRootTable if request.kind == "organization" else OrganizationMembershipTable
        criteria = [model.organization_ref == organization_ref]
        if request.kind == "membership":
            criteria.append(OrganizationMembershipTable.membership_ref == request.target_ref)
        row = await session.scalar(select(model).where(*criteria).execution_options(populate_existing=True))
    if row is None:
        raise IdentityAdministrationError("target_not_found")
    return row


async def operation_result(
    sessions: async_sessionmaker[AsyncSession],
    *,
    environment: Environment,
    actor_ref: str,
    operation_type: str,
    idempotency_key: str,
) -> AdministrationResult:
    """Recover only the original operator's result in its recorded environment.

    This read remains available when mixed installation environments block all
    writes; a historical committed result must not disappear during recovery.
    """
    _validate_operator(actor_ref, idempotency_key)
    async with sessions() as session:
        journal_type = "revoke.tenant" if operation_type == "revoke.organization" else operation_type
        row = await session.scalar(
            select(IdentityControlOperationTable).where(
                IdentityControlOperationTable.actor_kind == "bootstrap_operator",
                IdentityControlOperationTable.actor_ref == actor_ref,
                IdentityControlOperationTable.operation_type == journal_type,
                IdentityControlOperationTable.idempotency_key == idempotency_key,
            )
        )
        if row is None or row.authentication_json.get("environment") != environment:
            raise IdentityAdministrationError("operation_not_found")
        return _public_result(row.operation_ref, row.result_json)
