"""Private-deployment identity: verify outside transactions, reload under current locks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.context import validity_failure
from app.authz.identity import permits_self_identity
from app.execution.storage import lock_tenant
from app.identity.assurance_verification import verify_human_assurance
from app.identity.authentication import (
    AccessTokenProfile,
    AuthenticationRejected,
    AuthenticationUnavailable,
    KeycloakAuthenticator,
    VerifiedCredential,
)
from app.identity.evidence import HumanAuthenticationEvidence
from app.identity.native import NATIVE_PROFILE_SCHEMA, NativeAuthenticator, revalidate_native_credential
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
    WorkloadRegistrationTable,
)
from app.native_auth.keys import KeyRing

PROFILE_SCHEMA = "stage.keycloak-access.v1"


class IdentityDenied(Exception):
    """Uniform error for unavailable mappings, memberships and organization admission."""


class ProfileConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    jwks_url: str = Field(min_length=1, max_length=1024)
    allowed_clients: tuple[str, ...] = Field(min_length=1, max_length=32)
    evidence_ttl_seconds: int = Field(default=60, ge=1, le=300)
    clock_skew_seconds: int = Field(default=5, ge=0, le=30)


class AdmissionConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_clients: tuple[str, ...] = Field(min_length=1, max_length=32)
    required_acr: str | None = Field(default=None, min_length=1, max_length=128)


class SelfIdentity(BaseModel):
    principal_ref: str
    principal_type: Literal["human", "service"]
    organization_ref: str = Field(description="This deployment's authorization root; not a legal-entity identifier.")
    membership_ref: str
    membership_version: int
    authenticated_workload_ref: str | None
    authentication_valid_until: datetime


def content_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def profile_content(row: AuthenticationProfileTable) -> dict[str, Any]:
    return {
        "profile_ref": row.profile_ref,
        "profile_version": row.profile_version,
        "provider_ref": row.provider_ref,
        "subject_namespace": row.subject_namespace,
        "credential_kind": row.credential_kind,
        "stage_audience": row.stage_audience,
        "config_schema": row.config_schema,
        "config_json": row.config_json,
    }


@dataclass(frozen=True)
class VerificationSnapshot:
    profile: AccessTokenProfile
    trust_ref: str
    provider_ref: str
    fingerprint: str
    config_schema: str = PROFILE_SCHEMA


def current(row: Any, now: datetime) -> bool:
    return (
        row is not None
        and validity_failure(
            now,
            valid_from=getattr(row, "valid_from", None),
            expires_at=getattr(row, "expires_at", None),
            status=row.status,
        )
        is None
    )


class IdentityService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        environment: str,
        http_client: httpx.AsyncClient | None = None,
        request_timeout_seconds: float = 10,
        native_keyring: KeyRing | None = None,
        native_issuer: str | None = None,
    ) -> None:
        if not math.isfinite(request_timeout_seconds) or not 0 < request_timeout_seconds <= 60:
            raise ValueError("invalid_identity_request_timeout")
        self.sessions = sessions
        self.environment = environment
        self._request_timeout_seconds = request_timeout_seconds
        self._native_keyring = native_keyring
        self._native_issuer = native_issuer
        self._client = http_client or httpx.AsyncClient(
            timeout=3,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self._owns_client = http_client is None
        self._verifiers: OrderedDict[str, KeycloakAuthenticator | NativeAuthenticator] = OrderedDict()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _snapshot(
        self,
        session: AsyncSession,
        profile_ref: str,
        profile_version: int,
    ) -> VerificationSnapshot:
        row = await session.get(AuthenticationProfileTable, (profile_ref, profile_version), populate_existing=True)
        if row is None or row.status != "active" or row.config_schema not in {PROFILE_SCHEMA, NATIVE_PROFILE_SCHEMA}:
            raise IdentityDenied()
        provider = await session.get(IdentityProviderTable, row.provider_ref, populate_existing=True)
        if provider is None or provider.status != "active":
            raise IdentityDenied()
        trust = await session.get(TrustStateTable, provider.trust_ref, populate_existing=True)
        if trust is None or trust.purpose != "authentication" or content_digest(profile_content(row)) != row.digest:
            raise IdentityDenied()
        try:
            config = ProfileConfiguration.model_validate(row.config_json)
            profile = AccessTokenProfile(
                profile_ref=row.profile_ref,
                profile_version=row.profile_version,
                issuer=provider.issuer_exact,
                jwks_url=config.jwks_url,
                audience=row.stage_audience,
                credential_kind=row.credential_kind,  # type: ignore[arg-type]
                allowed_clients=config.allowed_clients,
                subject_namespace=row.subject_namespace,
                environment=self.environment,
                evidence_ttl_seconds=config.evidence_ttl_seconds,
                clock_skew_seconds=config.clock_skew_seconds,
                allow_loopback_http=self.environment in {"dev", "test"},
            )
            if row.config_schema == NATIVE_PROFILE_SCHEMA and (
                self._native_keyring is None
                or self._native_issuer != provider.issuer_exact
                or profile.jwks_url != provider.issuer_exact + "/oauth/jwks"
                or profile.credential_kind != "human_oidc"
                or profile.subject_namespace != "stage-native-human-v1"
            ):
                raise ValueError("native_profile_not_configured")
        except (ValueError, ValidationError):
            raise AuthenticationUnavailable() from None
        fingerprint = content_digest(
            {
                "profile": profile_content(row),
                "digest": row.digest,
                "version": row.version,
                "issuer": provider.issuer_exact,
                "provider_version": provider.version,
                "trust_ref": trust.trust_ref,
                "trust_revision": trust.trust_config_revision,
                "key_revision": trust.key_status_revision,
                "trust_version": trust.version,
                "trust_digest": trust.configuration_digest,
            }
        )
        return VerificationSnapshot(profile, trust.trust_ref, row.provider_ref, fingerprint, row.config_schema)

    async def read_self(
        self,
        access_token: str,
        *,
        profile_ref: str,
        profile_version: int,
    ) -> SelfIdentity:
        # Bound pool acquisition, remote verification and row-lock waits together.
        # Session context managers finish rollback/close before timeout escapes to HTTP.
        async with asyncio.timeout(self._request_timeout_seconds):
            identity, _ = await self._authenticate_identity(
                access_token,
                profile_ref=profile_ref,
                profile_version=profile_version,
            )
            return identity

    async def authenticate_human(
        self,
        access_token: str,
        *,
        profile_ref: str,
        profile_version: int,
    ) -> HumanAuthenticationEvidence:
        async with asyncio.timeout(self._request_timeout_seconds):
            _, evidence = await self._authenticate_identity(
                access_token,
                profile_ref=profile_ref,
                profile_version=profile_version,
                require_human=True,
            )
            assert evidence is not None
            return evidence

    async def _authenticate_identity(
        self,
        access_token: str,
        *,
        profile_ref: str,
        profile_version: int,
        require_human: bool = False,
    ) -> tuple[SelfIdentity, HumanAuthenticationEvidence | None]:
        # No DB connection/transaction remains open while fetching keys or verifying a token.
        async with self.sessions() as session:
            organizations = (await session.scalars(select(OrganizationRootTable.organization_ref).limit(2))).all()
            if len(organizations) != 1:
                raise AuthenticationUnavailable("deployment_organization_unavailable")
            # A root is the instance's authority boundary, independent of legal entities or units.
            organization_ref = organizations[0]
            snapshot = await self._snapshot(session, profile_ref, profile_version)
        verifier = self._verifiers.get(snapshot.fingerprint)
        if verifier is None:
            if snapshot.config_schema == NATIVE_PROFILE_SCHEMA:
                assert self._native_keyring is not None
                verifier = NativeAuthenticator(snapshot.profile, self._native_keyring)
            else:
                verifier = KeycloakAuthenticator(snapshot.profile, http_client=self._client)
            self._verifiers[snapshot.fingerprint] = verifier
            if len(self._verifiers) > 32:
                self._verifiers.popitem(last=False)
        else:
            self._verifiers.move_to_end(snapshot.fingerprint)
        credential = await verifier.authenticate(access_token)
        if require_human and credential.subject_kind != "human":
            raise IdentityDenied()
        # Only discover the candidate principal here; its identity mapping is reloaded under locks.
        async with self.sessions() as session:
            candidate = await session.scalar(
                select(ExternalIdentityTable).where(
                    ExternalIdentityTable.provider_ref == snapshot.provider_ref,
                    ExternalIdentityTable.subject_namespace == credential.subject_namespace,
                    ExternalIdentityTable.subject_exact == credential.subject,
                )
            )
            if candidate is None:
                raise IdentityDenied()
            principal_ref = candidate.principal_ref
            external_ref = candidate.external_identity_ref
        async with self.sessions() as session, session.begin():
            trust = await session.scalar(
                select(TrustStateTable)
                .where(
                    TrustStateTable.trust_ref == snapshot.trust_ref,
                )
                .with_for_update(read=True)
            )
            if trust is None:
                raise IdentityDenied()
            principal = await session.scalar(
                select(PrincipalTable)
                .where(
                    PrincipalTable.principal_ref == principal_ref,
                )
                .with_for_update(read=True)
            )
            if principal is None:
                raise IdentityDenied()
            # Installation must have provisioned the organization's guard.
            guard = await session.get(TenantGuardTable, organization_ref)
            if guard is None:
                raise IdentityDenied()
            await lock_tenant(session, organization_ref)
            # Re-read using a fresh session identity map for any rows loaded before locking.
            session.expire_all()
            latest = await self._snapshot(session, profile_ref, profile_version)
            if latest.fingerprint != snapshot.fingerprint:
                raise AuthenticationRejected("authentication_changed")
            principal = await session.scalar(
                select(PrincipalTable).where(
                    PrincipalTable.principal_ref == principal_ref,
                )
            )
            external = await session.get(ExternalIdentityTable, external_ref)
            organization = await session.get(OrganizationRootTable, organization_ref)
            now = datetime.now(UTC)
            if now >= min(credential.credential_expires_at, credential.evidence_valid_until):
                raise AuthenticationRejected()
            if (
                principal is None
                or principal.principal_type != credential.subject_kind
                or external is None
                or external.status != "active"
                or external.principal_ref != principal_ref
                or external.principal_type != credential.subject_kind
                or external.provider_ref != snapshot.provider_ref
                or external.subject_namespace != credential.subject_namespace
                or external.subject_exact != credential.subject
            ):
                raise IdentityDenied()
            binding = await session.scalar(
                select(OrganizationAuthenticationBindingTable).where(
                    OrganizationAuthenticationBindingTable.organization_ref == organization_ref,
                    OrganizationAuthenticationBindingTable.profile_ref == profile_ref,
                    OrganizationAuthenticationBindingTable.profile_version == profile_version,
                    OrganizationAuthenticationBindingTable.environment == self.environment,
                    OrganizationAuthenticationBindingTable.status == "active",
                )
            )
            if not current(binding, now):
                raise IdentityDenied()
            assert binding is not None
            try:
                constraints = AdmissionConstraints.model_validate(binding.constraints_json)
            except ValidationError:
                raise IdentityDenied() from None
            if credential.client_ref not in constraints.allowed_clients or (
                constraints.required_acr is not None and credential.acr != constraints.required_acr
            ):
                raise IdentityDenied()
            membership = await session.scalar(
                select(OrganizationMembershipTable).where(
                    OrganizationMembershipTable.organization_ref == organization_ref,
                    OrganizationMembershipTable.principal_ref == principal_ref,
                    OrganizationMembershipTable.principal_type == credential.subject_kind,
                    OrganizationMembershipTable.status == "active",
                )
            )
            workload = await self._workload(session, credential, external_ref, principal_ref, now)
            if not permits_self_identity(
                principal_type=principal.principal_type,
                principal_active=current(principal, now) and principal.disabled_at is None,
                organization_active=current(organization, now),
                membership_active=current(membership, now),
                workload_active=workload is not None,
            ):
                raise IdentityDenied()
            assert membership is not None
            if snapshot.config_schema == NATIVE_PROFILE_SCHEMA:
                await revalidate_native_credential(session, credential, principal_ref, datetime.now(UTC))
            deadlines = [credential.credential_expires_at, credential.evidence_valid_until]
            deadlines.extend(
                row.expires_at
                for row in (membership, binding, workload)
                if row is not None and row.expires_at is not None
            )
            assurance = None
            if credential.subject_kind == "human":
                assurance = await verify_human_assurance(session, credential, organization_ref, datetime.now(UTC))
                if assurance.valid_until is not None:
                    deadlines.append(assurance.valid_until)
            if datetime.now(UTC) >= min(deadlines):
                raise AuthenticationRejected("identity_evidence_expired")
            identity = SelfIdentity(
                principal_ref=principal_ref,
                principal_type=credential.subject_kind,
                organization_ref=organization_ref,
                membership_ref=membership.membership_ref,
                membership_version=membership.version,
                authenticated_workload_ref=workload.workload_ref if workload is not None else None,
                authentication_valid_until=min(deadlines),
            )
            evidence = None
            if credential.subject_kind == "human":
                assert organization is not None
                assert assurance is not None
                evidence = HumanAuthenticationEvidence(
                    credential=credential,
                    trust_ref=snapshot.trust_ref,
                    provider_ref=snapshot.provider_ref,
                    profile_fingerprint=snapshot.fingerprint,
                    principal_ref=principal_ref,
                    principal_version=principal.version,
                    identity_epoch=principal.identity_epoch,
                    recovery_epoch=principal.recovery_epoch,
                    organization_ref=organization_ref,
                    organization_version=organization.version,
                    membership_ref=membership.membership_ref,
                    membership_version=membership.version,
                    external_identity_ref=external.external_identity_ref,
                    external_identity_version=external.version,
                    binding_ref=binding.binding_ref,
                    binding_version=binding.version,
                    environment=self.environment,
                    valid_until=identity.authentication_valid_until,
                    assurance_policy_version=assurance.policy_version,
                    assurance_policy_digest=assurance.policy_digest,
                )
            return identity, evidence

    async def acquire_human_locks(self, session: AsyncSession, evidence: HumanAuthenticationEvidence) -> None:
        """Caller owns the transaction; organization guard must be acquired next."""
        if not session.in_transaction():
            raise RuntimeError("identity_evidence_requires_transaction")
        trust = await session.scalar(
            select(TrustStateTable)
            .where(TrustStateTable.trust_ref == evidence.trust_ref)
            .execution_options(populate_existing=True)
            .with_for_update(read=True)
        )
        if trust is None:
            raise IdentityDenied()
        principal = await session.scalar(
            select(PrincipalTable)
            .where(PrincipalTable.principal_ref == evidence.principal_ref)
            .execution_options(populate_existing=True)
            .with_for_update(read=True)
        )
        if principal is None:
            raise IdentityDenied()

    async def validate_human(self, session: AsyncSession, evidence: HumanAuthenticationEvidence, now: datetime) -> None:
        """Revalidate exact evidence under trust, principal and organization locks.

        No network access or transaction commit occurs here. A changed identity
        dependency requires fresh authentication; it is never rebound silently.
        """
        if not session.in_transaction():
            raise RuntimeError("identity_evidence_requires_transaction")
        credential = evidence.credential
        if now >= min(evidence.valid_until, credential.credential_expires_at, credential.evidence_valid_until):
            raise AuthenticationRejected("identity_evidence_expired")
        if (
            credential.subject_kind != "human"
            or credential.credential_kind != "human_oidc"
            or evidence.environment != self.environment
            or credential.environment != self.environment
            or now < credential.validated_at
        ):
            raise IdentityDenied()
        snapshot = await self._snapshot(session, credential.profile_ref, credential.profile_version)
        if (
            snapshot.fingerprint != evidence.profile_fingerprint
            or snapshot.trust_ref != evidence.trust_ref
            or snapshot.provider_ref != evidence.provider_ref
            or snapshot.profile.issuer != credential.issuer
            or snapshot.profile.subject_namespace != credential.subject_namespace
            or snapshot.profile.credential_kind != credential.credential_kind
            or snapshot.profile.audience not in credential.audiences
            or credential.client_ref not in snapshot.profile.allowed_clients
        ):
            raise AuthenticationRejected("authentication_changed")
        principal = await session.scalar(
            select(PrincipalTable)
            .where(PrincipalTable.principal_ref == evidence.principal_ref)
            .execution_options(populate_existing=True)
        )
        external = await session.get(ExternalIdentityTable, evidence.external_identity_ref, populate_existing=True)
        organization = await session.get(OrganizationRootTable, evidence.organization_ref, populate_existing=True)
        membership = await session.get(
            OrganizationMembershipTable,
            (evidence.organization_ref, evidence.membership_ref),
            populate_existing=True,
        )
        binding = await session.get(
            OrganizationAuthenticationBindingTable,
            (evidence.organization_ref, evidence.binding_ref),
            populate_existing=True,
        )
        if (
            not current(principal, now)
            or principal is None
            or principal.disabled_at is not None
            or principal.principal_type != "human"
            or principal.version != evidence.principal_version
            or principal.identity_epoch != evidence.identity_epoch
            or principal.recovery_epoch != evidence.recovery_epoch
            or not current(external, now)
            or external is None
            or external.version != evidence.external_identity_version
            or external.principal_ref != evidence.principal_ref
            or external.principal_type != "human"
            or external.provider_ref != evidence.provider_ref
            or external.subject_namespace != credential.subject_namespace
            or external.subject_exact != credential.subject
            or not current(organization, now)
            or organization is None
            or organization.version != evidence.organization_version
            or not current(membership, now)
            or membership is None
            or membership.ended_at is not None
            or membership.version != evidence.membership_version
            or membership.principal_ref != evidence.principal_ref
            or membership.principal_type != "human"
            or not current(binding, now)
            or binding is None
            or binding.version != evidence.binding_version
            or binding.profile_ref != credential.profile_ref
            or binding.profile_version != credential.profile_version
            or binding.environment != self.environment
        ):
            raise IdentityDenied()
        try:
            constraints = AdmissionConstraints.model_validate(binding.constraints_json)
        except ValidationError:
            raise IdentityDenied() from None
        if credential.client_ref not in constraints.allowed_clients or (
            constraints.required_acr is not None and credential.acr != constraints.required_acr
        ):
            raise IdentityDenied()
        await verify_human_assurance(
            session,
            credential,
            evidence.organization_ref,
            now,
            expected_version=evidence.assurance_policy_version,
            expected_digest=evidence.assurance_policy_digest,
        )
        if snapshot.config_schema == NATIVE_PROFILE_SCHEMA:
            await revalidate_native_credential(session, credential, evidence.principal_ref, now)

    async def _workload(
        self,
        session: AsyncSession,
        credential: VerifiedCredential,
        external_ref: str,
        principal_ref: str,
        now: datetime,
    ) -> WorkloadRegistrationTable | None:
        if credential.subject_kind != "service":
            return None
        rows = (
            await session.scalars(
                select(WorkloadRegistrationTable)
                .where(
                    WorkloadRegistrationTable.external_identity_ref == external_ref,
                    WorkloadRegistrationTable.service_principal_ref == principal_ref,
                    WorkloadRegistrationTable.profile_ref == credential.profile_ref,
                    WorkloadRegistrationTable.profile_version == credential.profile_version,
                    WorkloadRegistrationTable.verified_client_ref == credential.client_ref,
                    WorkloadRegistrationTable.environment == self.environment,
                    WorkloadRegistrationTable.status == "active",
                )
                .limit(2)
            )
        ).all()
        if len(rows) != 1 or not current(rows[0], now):
            raise IdentityDenied()
        return rows[0]
