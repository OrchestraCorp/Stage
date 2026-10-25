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
from app.identity.authentication import (
    AuthenticationRejected,
    AuthenticationUnavailable,
    KeycloakAuthenticator,
    KeycloakProfile,
    VerifiedCredential,
)
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
    profile: KeycloakProfile
    trust_ref: str
    provider_ref: str
    fingerprint: str


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
    ) -> None:
        if not math.isfinite(request_timeout_seconds) or not 0 < request_timeout_seconds <= 60:
            raise ValueError("invalid_identity_request_timeout")
        self.sessions = sessions
        self.environment = environment
        self._request_timeout_seconds = request_timeout_seconds
        self._client = http_client or httpx.AsyncClient(
            timeout=3,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self._owns_client = http_client is None
        self._verifiers: OrderedDict[str, KeycloakAuthenticator] = OrderedDict()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _snapshot(
        self,
        session: AsyncSession,
        profile_ref: str,
        profile_version: int,
    ) -> VerificationSnapshot:
        row = await session.get(AuthenticationProfileTable, (profile_ref, profile_version))
        if row is None or row.status != "active" or row.config_schema != PROFILE_SCHEMA:
            raise IdentityDenied()
        provider = await session.get(IdentityProviderTable, row.provider_ref)
        if provider is None or provider.status != "active":
            raise IdentityDenied()
        trust = await session.get(TrustStateTable, provider.trust_ref)
        if trust is None or trust.purpose != "authentication" or content_digest(profile_content(row)) != row.digest:
            raise IdentityDenied()
        try:
            config = ProfileConfiguration.model_validate(row.config_json)
            profile = KeycloakProfile(
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
        return VerificationSnapshot(profile, trust.trust_ref, row.provider_ref, fingerprint)

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
            return await self._read_self(
                access_token,
                profile_ref=profile_ref,
                profile_version=profile_version,
            )

    async def _read_self(
        self,
        access_token: str,
        *,
        profile_ref: str,
        profile_version: int,
    ) -> SelfIdentity:
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
            verifier = KeycloakAuthenticator(snapshot.profile, http_client=self._client)
            self._verifiers[snapshot.fingerprint] = verifier
            if len(self._verifiers) > 32:
                self._verifiers.popitem(last=False)
        else:
            self._verifiers.move_to_end(snapshot.fingerprint)
        credential = await verifier.authenticate(access_token)
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
            deadlines = [credential.credential_expires_at, credential.evidence_valid_until]
            deadlines.extend(
                row.expires_at
                for row in (membership, binding, workload)
                if row is not None and row.expires_at is not None
            )
            if datetime.now(UTC) >= min(deadlines):
                raise AuthenticationRejected("identity_evidence_expired")
            return SelfIdentity(
                principal_ref=principal_ref,
                principal_type=credential.subject_kind,
                organization_ref=organization_ref,
                membership_ref=membership.membership_ref,
                membership_version=membership.version,
                authenticated_workload_ref=workload.workload_ref if workload is not None else None,
                authentication_valid_until=min(deadlines),
            )

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
