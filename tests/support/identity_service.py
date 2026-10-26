"""Shared synthetic builders for identity service."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.identity.service import (
    PROFILE_SCHEMA,
    IdentityService,
    ProfileConfiguration,
    content_digest,
    profile_content,
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
from tests.support.identity_tokens import profile


async def seed(
    sessions: async_sessionmaker[AsyncSession], *, service: bool = False, include_organization: bool = True
) -> None:
    now = datetime.now(UTC) - timedelta(seconds=10)
    kind = "service" if service else "human"
    profile_row = AuthenticationProfileTable(
        profile_ref="local-human",
        profile_version=1,
        provider_ref="provider",
        subject_namespace="keycloak-human-v1",
        credential_kind="workload_oauth2" if service else "human_oidc",
        stage_audience="stage-api",
        config_schema=PROFILE_SCHEMA,
        config_json=ProfileConfiguration(jwks_url=profile().jwks_url, allowed_clients=("maestro",)).model_dump(
            mode="json"
        ),
    )
    profile_row.digest = content_digest(profile_content(profile_row))
    # Flush dependency groups explicitly; no ORM relationships imply an insertion order.
    async with sessions.begin() as session:
        session.add_all(
            [
                PrincipalTable(principal_ref="person", principal_type=kind),
                TrustStateTable(trust_ref="trust", purpose="authentication", configuration_digest="a" * 64),
            ]
        )
        if include_organization:
            session.add_all(
                [
                    OrganizationRootTable(organization_ref="tenant-a", display_name="A"),
                    TenantGuardTable(tenant_ref="tenant-a"),
                ]
            )
        await session.flush()
        session.add(IdentityProviderTable(provider_ref="provider", issuer_exact=profile().issuer, trust_ref="trust"))
        await session.flush()
        session.add(profile_row)
        session.add(
            ExternalIdentityTable(
                external_identity_ref="identity",
                provider_ref="provider",
                subject_namespace="keycloak-human-v1",
                subject_exact="subject-1",
                principal_ref="person",
                principal_type=kind,
                link_evidence_ref="fixture",
            )
        )
        await session.flush()
        if include_organization:
            session.add(
                OrganizationAuthenticationBindingTable(
                    organization_ref="tenant-a",
                    binding_ref="binding",
                    profile_ref="local-human",
                    profile_version=1,
                    environment="test",
                    constraints_json={"allowed_clients": ["maestro"]},
                    valid_from=now,
                )
            )
            session.add(
                OrganizationMembershipTable(
                    organization_ref="tenant-a",
                    membership_ref="member-a",
                    principal_ref="person",
                    principal_type=kind,
                    source_ref="fixture",
                    valid_from=now,
                )
            )
        if service:
            session.add(
                WorkloadRegistrationTable(
                    workload_ref="workload",
                    service_principal_ref="person",
                    profile_ref="local-human",
                    profile_version=1,
                    external_identity_ref="identity",
                    verified_client_ref="maestro",
                    environment="test",
                    valid_from=now,
                )
            )


async def read(service: IdentityService, bearer: str) -> Any:
    return await service.read_self(bearer, profile_ref="local-human", profile_version=1)
