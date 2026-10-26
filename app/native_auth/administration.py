"""Explicit offline native-account migration; never infer identity from usernames."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any, Self
from uuid import uuid4

from pydantic import Field, SecretStr, model_validator
from sqlalchemy import select
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
from app.identity.native import NATIVE_ACR, NATIVE_PROFILE_SCHEMA
from app.identity.service import AdmissionConstraints, ProfileConfiguration, content_digest, current, profile_content
from app.models.execution import TenantGuardTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import (
    AuthenticationProfileTable,
    ExternalIdentityTable,
    IdentityProviderTable,
    OrganizationAuthenticationBindingTable,
    OrganizationMembershipTable,
    OrganizationRootTable,
    TrustStateTable,
)
from app.models.native_auth import NativeAccountTable, NativeClientTable
from app.native_auth.config import NativeAuthConfig
from app.native_auth.credentials import authentication_event
from app.native_auth.crypto import hash_password, normalize_username, validate_password
from app.native_auth.redirects import validate_issuer_redirect
from app.storage.coordination import LockMode, lock_organization

TRUST = "trust:native-auth"
PROVIDER = "provider:native-auth"
PROFILE = "stage-native-human"
NAMESPACE = "stage-native-human-v1"
BINDING = "binding:native-auth"
INSTALL_OPERATION = "install.native-account"


class InstallPlan(StrictPlan):
    environment: Environment
    organization_ref: Reference
    issuer: str = Field(min_length=1, max_length=512)
    client_id: Reference
    redirect_uris: tuple[str, ...] = Field(min_length=1, max_length=8)
    account_ref: Reference
    principal_ref: Reference
    membership_ref: Reference
    external_identity_ref: Reference
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(repr=False)

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        NativeAuthConfig(issuer=self.issuer, organization_ref=self.organization_ref, environment=self.environment)
        normalize_username(self.username)
        validate_password(self.password.get_secret_value())
        if any(len(value.encode("utf-8")) > 128 for value in (self.account_ref, self.client_id)):
            raise ValueError("native_token_identifier_exceeds_utf8_limit")
        if len(set(self.redirect_uris)) != len(self.redirect_uris):
            raise ValueError("duplicate_redirect_uri")
        for redirect in self.redirect_uris:
            validate_issuer_redirect(self.issuer, redirect, self.environment)
        return self

    def digest_payload(self, key: bytes) -> dict[str, Any]:
        if len(key) < 32:
            raise AdministrationError("private_credential_digest_key_required")
        data = self.model_dump(mode="json", exclude={"password"})
        # A plain SHA of a password-containing plan would become a fast offline
        # guessing oracle. The deployment-private key is independent of the DB.
        material = ("native-install-password\0" + self.account_ref + "\0" + self.password.get_secret_value()).encode()
        data["password_commitment"] = hmac.new(key, material, hashlib.sha256).hexdigest()
        return data


def _profile(plan: InstallPlan) -> AuthenticationProfileTable:
    row = AuthenticationProfileTable(
        profile_ref=PROFILE,
        profile_version=1,
        provider_ref=PROVIDER,
        subject_namespace=NAMESPACE,
        credential_kind="human_oidc",
        stage_audience="stage-api",
        config_schema=NATIVE_PROFILE_SCHEMA,
        config_json=ProfileConfiguration(
            jwks_url=plan.issuer + "/oauth/jwks", allowed_clients=(plan.client_id,), clock_skew_seconds=0
        ).model_dump(mode="json"),
    )
    row.digest = content_digest(profile_content(row))
    return row


async def install(
    sessions: async_sessionmaker[AsyncSession],
    plan: InstallPlan,
    *,
    actor_ref: str,
    idempotency_key: str,
    credential_digest_key: bytes,
) -> AdministrationResult:
    """Bind a new native account to a verified existing membership, with no grants."""
    validate_operator(actor_ref, idempotency_key)
    digest = content_digest(plan.digest_payload(credential_digest_key))
    encoded_password = await hash_password(plan.password.get_secret_value())
    try:
        async with operation_transaction(sessions, INSTALL_OPERATION, idempotency_key) as session:
            await acquire_admin_lock(session)
            await check_database_environment(session, plan.environment)
            roots = (await session.scalars(select(OrganizationRootTable.organization_ref).limit(2))).all()
            if roots != [plan.organization_ref]:
                raise AdministrationError("existing_organization_required")
            replay = await find_replay(session, actor_ref, INSTALL_OPERATION, idempotency_key, digest)
            if replay is not None:
                return replay
            return await _install(session, plan, encoded_password, actor_ref, idempotency_key, digest)
    except IntegrityError:
        raise AdministrationError("native_configuration_conflict") from None


async def _install(
    session: AsyncSession,
    plan: InstallPlan,
    encoded_password: str,
    actor_ref: str,
    idempotency_key: str,
    digest: str,
) -> AdministrationResult:
    expected_trust_digest = content_digest({"issuer": plan.issuer, "purpose": "native-authentication"})
    trust = await session.scalar(select(TrustStateTable).where(TrustStateTable.trust_ref == TRUST).with_for_update())
    if trust is None:
        trust = TrustStateTable(trust_ref=TRUST, purpose="authentication", configuration_digest=expected_trust_digest)
        session.add(trust)
        await session.flush()
    elif trust.purpose != "authentication" or trust.configuration_digest != expected_trust_digest:
        raise AdministrationError("native_trust_configuration_conflict")
    principal = await session.scalar(
        select(PrincipalTable).where(PrincipalTable.principal_ref == plan.principal_ref).with_for_update()
    )
    if (
        principal is None
        or principal.principal_type != "human"
        or principal.status != "active"
        or principal.disabled_at is not None
    ):
        raise AdministrationError("existing_active_human_required")
    await lock_organization(session, plan.organization_ref, mode=LockMode.EXCLUSIVE)
    now = datetime.now(UTC)
    root = await session.get(OrganizationRootTable, plan.organization_ref, populate_existing=True)
    membership = await session.get(OrganizationMembershipTable, (plan.organization_ref, plan.membership_ref))
    if (
        root is None
        or root.status != "active"
        or not current(membership, now)
        or membership is None
        or membership.principal_ref != plan.principal_ref
        or membership.principal_type != "human"
        or membership.ended_at is not None
    ):
        raise AdministrationError("existing_current_membership_required")
    if await session.get(NativeAccountTable, plan.account_ref) is not None:
        raise AdministrationError("native_account_already_installed")

    provider = await session.get(IdentityProviderTable, PROVIDER)
    if provider is None:
        session.add(IdentityProviderTable(provider_ref=PROVIDER, issuer_exact=plan.issuer, trust_ref=TRUST))
        await session.flush()
    elif provider.status != "active" or provider.issuer_exact != plan.issuer or provider.trust_ref != TRUST:
        raise AdministrationError("native_provider_configuration_conflict")
    desired_profile = _profile(plan)
    profile = await session.get(AuthenticationProfileTable, (PROFILE, 1))
    if profile is None:
        session.add(desired_profile)
        await session.flush()
    elif (
        profile.status != "active"
        or profile.digest != desired_profile.digest
        or profile_content(profile) != profile_content(desired_profile)
    ):
        raise AdministrationError("native_profile_configuration_conflict")
    constraints = AdmissionConstraints(allowed_clients=(plan.client_id,), required_acr=NATIVE_ACR).model_dump(
        mode="json"
    )
    binding = await session.get(OrganizationAuthenticationBindingTable, (plan.organization_ref, BINDING))
    if binding is None:
        session.add(
            OrganizationAuthenticationBindingTable(
                organization_ref=plan.organization_ref,
                binding_ref=BINDING,
                profile_ref=PROFILE,
                profile_version=1,
                environment=plan.environment,
                constraints_json=constraints,
                valid_from=now,
            )
        )
    elif (
        not current(binding, now)
        or binding.profile_ref != PROFILE
        or binding.profile_version != 1
        or binding.environment != plan.environment
        or binding.constraints_json != constraints
    ):
        raise AdministrationError("native_binding_configuration_conflict")
    client = await session.get(NativeClientTable, plan.client_id)
    if client is None:
        session.add(NativeClientTable(client_id=plan.client_id, redirect_uris=list(plan.redirect_uris), enabled=True))
    elif not client.enabled or client.redirect_uris != list(plan.redirect_uris):
        raise AdministrationError("native_client_configuration_conflict")
    operation_ref = str(uuid4())
    session.add(
        NativeAccountTable(
            account_ref=plan.account_ref,
            principal_ref=plan.principal_ref,
            username=plan.username,
            password_hash=encoded_password,
            credential_version=1,
            status="active",
        )
    )
    session.add(
        ExternalIdentityTable(
            external_identity_ref=plan.external_identity_ref,
            provider_ref=PROVIDER,
            subject_namespace=NAMESPACE,
            subject_exact=plan.account_ref,
            principal_ref=plan.principal_ref,
            principal_type="human",
            link_evidence_ref=operation_ref,
        )
    )
    await session.flush()
    authentication_event(
        session, "account.installed", account_ref=plan.account_ref, client_id=plan.client_id, version=1
    )
    guard = await session.get(TenantGuardTable, plan.organization_ref)
    assert guard is not None
    guard.epoch += 1
    result = {
        "organization_ref": plan.organization_ref,
        "principal_ref": plan.principal_ref,
        "membership_ref": plan.membership_ref,
        "account_ref": plan.account_ref,
        "external_identity_ref": plan.external_identity_ref,
        "provider_ref": PROVIDER,
        "profile_ref": PROFILE,
        "profile_version": 1,
        "client_id": plan.client_id,
        "credential_version": 1,
        "totp_enrollment_required": True,
        "business_grants": [],
    }
    return append_journal(
        session,
        actor_ref=actor_ref,
        operation_type=INSTALL_OPERATION,
        idempotency_key=idempotency_key,
        digest=digest,
        environment=plan.environment,
        target_ref=plan.account_ref,
        result=result,
        after=result,
        operation_ref=operation_ref,
    )
