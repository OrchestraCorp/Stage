"""Explicit OAuth client registration and separate Stage admission by local operators."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import Field, TypeAdapter, ValidationError, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.control.contracts import AdministrationError, AdministrationResult, Environment, Reference, StrictPlan
from app.control.operations import (
    acquire_admin_lock,
    append_journal,
    check_database_environment,
    find_replay,
    operation_transaction,
    validate_operator,
)
from app.identity.assurance import AssuranceReference, HumanAssurancePolicy, HumanAssuranceRule
from app.identity.native import NATIVE_ACR, NATIVE_PROFILE_SCHEMA
from app.identity.service import AdmissionConstraints, ProfileConfiguration, content_digest, profile_content
from app.models.execution import TenantGuardTable
from app.models.identity_foundation import (
    AuthenticationProfileTable,
    IdentityProviderTable,
    OrganizationAuthenticationBindingTable,
    OrganizationHumanAuthenticationPolicyTable,
    OrganizationRootTable,
    TrustStateTable,
)
from app.models.native_auth import NativeAuthEventTable, NativeClientTable, NativeSessionTable
from app.native_auth.administration import NAMESPACE, PROVIDER, TRUST
from app.native_auth.redirects import validate_issuer_redirect, validate_redirect_uri
from app.storage.coordination import LockMode, lock_organization

CONFIGURE_OPERATION = "configure.native-oauth-client"
ROTATE_OPERATION = "rotate.native-oauth-client-secret"
ADMIT_OPERATION = "admit.native-oauth-client"
OPERATIONS = (CONFIGURE_OPERATION, ROTATE_OPERATION, ADMIT_OPERATION)
_MAX_VERSION = 2**63 - 1


class ClientPlan(StrictPlan):
    environment: Environment
    organization_ref: Reference
    client_id: Reference

    @model_validator(mode="after")
    def valid_references(self) -> Self:
        if any(len(value.encode("utf-8")) > 128 for value in (self.organization_ref, self.client_id)):
            raise ValueError("native_identifier_exceeds_utf8_limit")
        return self


class ConfigureClientPlan(ClientPlan):
    expected_version: int = Field(strict=True, ge=0, le=_MAX_VERSION)
    display_name: str = Field(min_length=1, max_length=256)
    token_endpoint_auth_method: Literal["none", "client_secret_basic"] = "none"
    redirect_uris: tuple[str, ...] = Field(min_length=1, max_length=8)
    post_logout_redirect_uris: tuple[str, ...] = Field(default=(), max_length=8)
    allowed_scopes: tuple[Literal["openid", "profile", "offline_access"], ...] = ("openid",)
    require_consent: bool = Field(default=True, strict=True)
    resource_audience: Literal["stage-userinfo", "stage-api"] = "stage-userinfo"
    application_type: Literal["native", "web"] = "native"
    enabled: bool = Field(default=True, strict=True)

    @model_validator(mode="after")
    def valid_metadata(self) -> Self:
        if any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in self.display_name):
            raise ValueError("invalid_client_display_name")
        for uris in (self.redirect_uris, self.post_logout_redirect_uris):
            if len(uris) != len(set(uris)):
                raise ValueError("duplicate_redirect_uri")
            for uri in uris:
                validate_redirect_uri(uri)
        if "openid" not in self.allowed_scopes or len(set(self.allowed_scopes)) != len(self.allowed_scopes):
            raise ValueError("client_scopes_require_unique_openid")
        return self


class RotateClientSecretPlan(ClientPlan):
    expected_version: int = Field(strict=True, ge=1, le=_MAX_VERSION)


class AdmitClientPlan(ClientPlan):
    """Create one immutable native profile without implicitly granting business roles."""

    expected_client_version: int = Field(strict=True, ge=1, le=_MAX_VERSION)
    profile_ref: Reference
    binding_ref: Reference
    expected_policy_version: int = Field(strict=True, ge=0, le=_MAX_VERSION)

    @model_validator(mode="after")
    def valid_admission_references(self) -> Self:
        if any(len(value.encode("utf-8")) > 128 for value in (self.profile_ref, self.binding_ref)):
            raise ValueError("native_identifier_exceeds_utf8_limit")
        return self


def _operator(actor_ref: str, idempotency_key: str) -> None:
    validate_operator(actor_ref, idempotency_key)
    try:
        validator = TypeAdapter(AssuranceReference)
        validator.validate_python(actor_ref)
        validator.validate_python(idempotency_key)
    except ValidationError:
        raise AdministrationError("invalid_operation_identity") from None


def _secret_hash(value: str | None, required: bool) -> None:
    if required != (value is not None) or (
        value is not None and (len(value) != 64 or any(char not in "0123456789abcdef" for char in value))
    ):
        raise AdministrationError("generated_client_secret_required")


def client_snapshot(client: NativeClientTable) -> dict[str, Any]:
    """Safe metadata for inspection and journals; never include a secret or its digest."""
    return {
        "client_id": client.client_id,
        "version": client.version,
        "display_name": client.display_name,
        "token_endpoint_auth_method": client.token_endpoint_auth_method,
        "redirect_uris": list(client.redirect_uris),
        "post_logout_redirect_uris": list(client.post_logout_redirect_uris),
        "allowed_scopes": list(client.allowed_scopes),
        "require_consent": client.require_consent,
        "resource_audience": client.resource_audience,
        "application_type": client.application_type,
        "enabled": client.enabled,
        "secret_configured": client.client_secret_hash is not None,
    }


async def _lock_domain(session: AsyncSession, plan: ClientPlan) -> tuple[TrustStateTable, TenantGuardTable]:
    # Exclusive native trust waits for prior identity/command readers and prevents
    # new readers until metadata and all affected session revocations commit.
    trust = await session.scalar(select(TrustStateTable).where(TrustStateTable.trust_ref == TRUST).with_for_update())
    if trust is None or trust.purpose != "authentication":
        raise AdministrationError("native_identity_not_initialized")
    roots = (await session.scalars(select(OrganizationRootTable))).all()
    if len(roots) != 1 or roots[0].organization_ref != plan.organization_ref:
        raise AdministrationError("identity_database_organization_mismatch")
    await lock_organization(session, plan.organization_ref, mode=LockMode.EXCLUSIVE)
    await session.refresh(roots[0])
    if roots[0].status != "active":
        raise AdministrationError("organization_not_active")
    guard = await session.get(TenantGuardTable, plan.organization_ref, populate_existing=True)
    if guard is None:
        raise AdministrationError("identity_not_initialized")
    return trust, guard


async def _invalidate_client(session: AsyncSession, client: NativeClientTable, guard: TenantGuardTable) -> None:
    if client.version == _MAX_VERSION or guard.epoch == _MAX_VERSION:
        raise AdministrationError("version_exhausted")
    client.version += 1
    guard.epoch += 1
    await session.execute(
        update(NativeSessionTable)
        .where(NativeSessionTable.client_id == client.client_id, NativeSessionTable.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


def _event(session: AsyncSession, client: NativeClientTable, kind: str) -> None:
    session.add(NativeAuthEventTable(event=kind, client_id=client.client_id, detail={"version": client.version}))


async def configure_client(
    sessions: async_sessionmaker[AsyncSession],
    plan: ConfigureClientPlan,
    *,
    actor_ref: str,
    idempotency_key: str,
    client_secret_hash: str | None = None,
) -> AdministrationResult:
    """Create or fully replace registered metadata, invalidating existing sessions."""
    _operator(actor_ref, idempotency_key)
    _secret_hash(
        client_secret_hash, plan.expected_version == 0 and plan.token_endpoint_auth_method == "client_secret_basic"
    )
    digest = content_digest({**plan.model_dump(mode="json"), "client_secret_hash": client_secret_hash})
    try:
        async with operation_transaction(sessions, CONFIGURE_OPERATION, idempotency_key) as session:
            await acquire_admin_lock(session)
            await check_database_environment(session, plan.environment)
            replay = await find_replay(session, actor_ref, CONFIGURE_OPERATION, idempotency_key, digest)
            if replay is not None:
                return replay
            _, guard = await _lock_domain(session, plan)
            provider = await session.get(IdentityProviderTable, PROVIDER, populate_existing=True)
            if provider is None or provider.trust_ref != TRUST:
                raise AdministrationError("native_provider_configuration_conflict")
            try:
                for uri in (*plan.redirect_uris, *plan.post_logout_redirect_uris):
                    validate_issuer_redirect(provider.issuer_exact, uri, plan.environment)
            except ValueError:
                raise AdministrationError("invalid_client_redirect_topology") from None
            client = await session.get(NativeClientTable, plan.client_id, populate_existing=True)
            if (client is None and plan.expected_version != 0) or (
                client is not None and client.version != plan.expected_version
            ):
                raise AdministrationError("version_conflict")
            before = client_snapshot(client) if client is not None else None
            if client is None:
                client = NativeClientTable(
                    client_id=plan.client_id,
                    version=1,
                    token_endpoint_auth_method=plan.token_endpoint_auth_method,
                    client_secret_hash=client_secret_hash,
                )
                session.add(client)
                if guard.epoch == _MAX_VERSION:
                    raise AdministrationError("version_exhausted")
                guard.epoch += 1
            else:
                if client.token_endpoint_auth_method != plan.token_endpoint_auth_method:
                    raise AdministrationError("client_authentication_method_immutable")
                await _invalidate_client(session, client, guard)
            client.display_name = plan.display_name
            client.redirect_uris = list(plan.redirect_uris)
            client.post_logout_redirect_uris = list(plan.post_logout_redirect_uris)
            client.allowed_scopes = list(plan.allowed_scopes)
            client.require_consent = plan.require_consent
            client.resource_audience = plan.resource_audience
            client.application_type = plan.application_type
            client.enabled = plan.enabled
            await session.flush()
            _event(session, client, "client.configured")
            after = client_snapshot(client)
            return append_journal(
                session,
                actor_ref=actor_ref,
                operation_type=CONFIGURE_OPERATION,
                idempotency_key=idempotency_key,
                digest=digest,
                environment=plan.environment,
                target_ref=client.client_id,
                before=before,
                after=after,
                result={**after, "organization_authz_epoch": guard.epoch, "stage_admission_changed": False},
            )
    except IntegrityError:
        raise AdministrationError("native_client_configuration_conflict") from None


async def rotate_client_secret(
    sessions: async_sessionmaker[AsyncSession],
    plan: RotateClientSecretPlan,
    *,
    actor_ref: str,
    idempotency_key: str,
    client_secret_hash: str,
) -> AdministrationResult:
    _operator(actor_ref, idempotency_key)
    _secret_hash(client_secret_hash, True)
    digest = content_digest({**plan.model_dump(mode="json"), "client_secret_hash": client_secret_hash})
    async with operation_transaction(sessions, ROTATE_OPERATION, idempotency_key) as session:
        await acquire_admin_lock(session)
        await check_database_environment(session, plan.environment)
        replay = await find_replay(session, actor_ref, ROTATE_OPERATION, idempotency_key, digest)
        if replay is not None:
            return replay
        _, guard = await _lock_domain(session, plan)
        client = await session.get(NativeClientTable, plan.client_id, populate_existing=True)
        if client is None or client.version != plan.expected_version:
            raise AdministrationError("version_conflict")
        if client.token_endpoint_auth_method != "client_secret_basic":
            raise AdministrationError("confidential_client_required")
        if client.client_secret_hash == client_secret_hash:
            raise AdministrationError("new_client_secret_required")
        before = client_snapshot(client)
        await _invalidate_client(session, client, guard)
        client.client_secret_hash = client_secret_hash
        _event(session, client, "client.secret_rotated")
        after = client_snapshot(client)
        return append_journal(
            session,
            actor_ref=actor_ref,
            operation_type=ROTATE_OPERATION,
            idempotency_key=idempotency_key,
            digest=digest,
            environment=plan.environment,
            target_ref=client.client_id,
            before=before,
            after=after,
            result={**after, "organization_authz_epoch": guard.epoch},
        )


async def admit_client(
    sessions: async_sessionmaker[AsyncSession],
    plan: AdmitClientPlan,
    *,
    actor_ref: str,
    idempotency_key: str,
) -> AdministrationResult:
    """Explicitly admit a client to Stage authentication while preserving MFA strength."""
    _operator(actor_ref, idempotency_key)
    digest = content_digest(plan.model_dump(mode="json"))
    try:
        async with operation_transaction(sessions, ADMIT_OPERATION, idempotency_key) as session:
            await acquire_admin_lock(session)
            await check_database_environment(session, plan.environment)
            replay = await find_replay(session, actor_ref, ADMIT_OPERATION, idempotency_key, digest)
            if replay is not None:
                return replay
            trust, guard = await _lock_domain(session, plan)
            client = await session.get(NativeClientTable, plan.client_id, populate_existing=True)
            if client is None or client.version != plan.expected_client_version:
                raise AdministrationError("version_conflict")
            if not client.enabled:
                raise AdministrationError("client_not_active")
            if client.resource_audience != "stage-api":
                raise AdministrationError("explicit_stage_api_audience_required")
            provider = await session.get(IdentityProviderTable, PROVIDER, populate_existing=True)
            if provider is None or provider.status != "active" or provider.trust_ref != trust.trust_ref:
                raise AdministrationError("native_provider_configuration_conflict")
            if (
                await session.get(AuthenticationProfileTable, (plan.profile_ref, 1)) is not None
                or await session.get(OrganizationAuthenticationBindingTable, (plan.organization_ref, plan.binding_ref))
                is not None
            ):
                raise AdministrationError("native_admission_already_exists")
            policy = await session.get(
                OrganizationHumanAuthenticationPolicyTable, plan.organization_ref, populate_existing=True
            )
            policy_version = policy.version if policy is not None else 0
            if policy_version != plan.expected_policy_version:
                raise AdministrationError("assurance_version_conflict")
            before_policy = (
                {
                    "version": policy.version,
                    "mode": policy.mode,
                    "rules": policy.rules_json,
                    "max_auth_age_seconds": policy.max_auth_age_seconds,
                }
                if policy is not None
                else {"version": 0, "mode": "disabled", "rules": [], "max_auth_age_seconds": None}
            )
            profile = AuthenticationProfileTable(
                profile_ref=plan.profile_ref,
                profile_version=1,
                provider_ref=PROVIDER,
                subject_namespace=NAMESPACE,
                credential_kind="human_oidc",
                stage_audience="stage-api",
                config_schema=NATIVE_PROFILE_SCHEMA,
                config_json=ProfileConfiguration(
                    jwks_url=provider.issuer_exact + "/oauth/jwks",
                    allowed_clients=(client.client_id,),
                    clock_skew_seconds=0,
                ).model_dump(mode="json"),
            )
            profile.digest = content_digest(profile_content(profile))
            session.add(profile)
            await session.flush()
            session.add(
                OrganizationAuthenticationBindingTable(
                    organization_ref=plan.organization_ref,
                    binding_ref=plan.binding_ref,
                    profile_ref=plan.profile_ref,
                    profile_version=1,
                    environment=plan.environment,
                    constraints_json=AdmissionConstraints(
                        allowed_clients=(client.client_id,), required_acr=NATIVE_ACR
                    ).model_dump(mode="json"),
                    valid_from=datetime.now(UTC),
                )
            )
            if policy is not None and policy.mode == "required":
                if len(policy.rules_json) >= 32:
                    raise AdministrationError("assurance_profile_capacity_exceeded")
                if policy.version == _MAX_VERSION:
                    raise AdministrationError("version_exhausted")
                rule = HumanAssuranceRule(
                    profile_ref=plan.profile_ref,
                    profile_version=1,
                    required_acr=NATIVE_ACR,
                    required_amr=("pwd", "otp"),
                )
                updated = HumanAssurancePolicy.model_validate(
                    {
                        "mode": "required",
                        "rules": [*policy.rules_json, rule.model_dump(mode="json")],
                        "max_auth_age_seconds": policy.max_auth_age_seconds,
                    }
                )
                policy.rules_json = [item.model_dump(mode="json") for item in updated.rules]
                policy.version += 1
            if guard.epoch == _MAX_VERSION:
                raise AdministrationError("version_exhausted")
            guard.epoch += 1
            _event(session, client, "client.stage_admitted")
            result = {
                "client_id": client.client_id,
                "client_version": client.version,
                "profile_ref": profile.profile_ref,
                "profile_version": profile.profile_version,
                "binding_ref": plan.binding_ref,
                "assurance_version": policy.version if policy is not None else 0,
                "organization_authz_epoch": guard.epoch,
                "business_grants": [],
            }
            return append_journal(
                session,
                actor_ref=actor_ref,
                operation_type=ADMIT_OPERATION,
                idempotency_key=idempotency_key,
                digest=digest,
                environment=plan.environment,
                target_ref=client.client_id,
                before={"assurance": before_policy},
                after={
                    **result,
                    "assurance": {
                        "version": policy.version,
                        "mode": policy.mode,
                        "rules": policy.rules_json,
                        "max_auth_age_seconds": policy.max_auth_age_seconds,
                    }
                    if policy is not None
                    else before_policy,
                },
                result=result,
            )
    except IntegrityError:
        raise AdministrationError("native_client_configuration_conflict") from None
