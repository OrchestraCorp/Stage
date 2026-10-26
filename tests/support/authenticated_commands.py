"""Shared synthetic builders for authenticated commands."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.authenticated import AuthenticatedAuthorizationLoader
from app.core.enums import PrincipalType
from app.execution.contracts import ExecutionContext, Handler
from app.execution.kernel import CommandKernel
from app.features.work.details import WORK_DETAILS_COMMAND, update_work_details
from app.identity import administration
from app.identity.service import (
    PROFILE_SCHEMA,
    IdentityService,
    ProfileConfiguration,
    content_digest,
    profile_content,
)
from app.models.authorization import RelationTupleTable, RoleAssignmentTable
from app.models.execution import AuditRecordTable
from app.models.identity_foundation import (
    AuthenticationProfileTable,
    BootstrapRecordTable,
    ExternalIdentityTable,
    IdentityProviderTable,
    OrganizationAuthenticationBindingTable,
    OrganizationMembershipTable,
    TrustStateTable,
)
from tests.support.authorization import ORG, PERSON
from tests.support.identity_tokens import jwks, profile
from tests.support.work_details import seed as seed_work_details


async def seed_authenticated_command(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Use the real role/resource seed and a protocol-verifiable human identity."""
    await seed_work_details(sessions)
    valid_from = datetime.now(UTC) - timedelta(days=1)
    authentication = AuthenticationProfileTable(
        profile_ref="local-human",
        profile_version=1,
        provider_ref="provider",
        subject_namespace="keycloak-human-v1",
        credential_kind="human_oidc",
        stage_audience="stage-api",
        config_schema=PROFILE_SCHEMA,
        config_json=ProfileConfiguration(jwks_url=profile().jwks_url, allowed_clients=("maestro",)).model_dump(
            mode="json"
        ),
    )
    authentication.digest = content_digest(profile_content(authentication))
    async with sessions.begin() as session:
        for model in (OrganizationMembershipTable, RoleAssignmentTable, RelationTupleTable):
            await session.execute(update(model).values(valid_from=valid_from))
        session.add(TrustStateTable(trust_ref="trust", purpose="authentication", configuration_digest="a" * 64))
        await session.flush()
        session.add(IdentityProviderTable(provider_ref="provider", issuer_exact=profile().issuer, trust_ref="trust"))
        await session.flush()
        session.add(authentication)
        session.add(
            ExternalIdentityTable(
                external_identity_ref="identity",
                provider_ref="provider",
                subject_namespace="keycloak-human-v1",
                subject_exact="subject-1",
                principal_ref=PERSON,
                principal_type="human",
                link_evidence_ref="fixture",
            )
        )
        await session.flush()
        session.add(
            OrganizationAuthenticationBindingTable(
                organization_ref=ORG,
                binding_ref="binding",
                profile_ref="local-human",
                profile_version=1,
                environment="test",
                constraints_json={"allowed_clients": ["maestro"]},
                valid_from=valid_from,
            )
        )
        session.add(
            BootstrapRecordTable(
                bootstrap_ref="bootstrap:fixture",
                environment="test",
                bootstrap_type=administration.BOOTSTRAP_TYPE,
                configuration_digest="a" * 64,
                initial_identity_ref="identity",
                initial_grant_refs=[],
                completed_at=datetime.now(UTC),
            )
        )


def jwt_client(key: rsa.RSAPrivateKey) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key))))


def identity_service(sessions: async_sessionmaker[AsyncSession], client: httpx.AsyncClient) -> IdentityService:
    return IdentityService(sessions, environment="test", http_client=client)


async def audit_snapshot(sessions: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    async with sessions() as session:
        audit = await session.scalar(select(AuditRecordTable))
        assert audit is not None and audit.authorization_json is not None
        return deepcopy(audit.authorization_json)


async def authenticated_context(identity: IdentityService, bearer: str) -> ExecutionContext:
    evidence = await identity.authenticate_human(bearer, profile_ref="local-human", profile_version=1)
    return ExecutionContext(
        tenant_ref=evidence.organization_ref,
        actor_ref=evidence.principal_ref,
        effective_subject_ref=evidence.principal_ref,
        request_id="request:authenticated-command",
        principal_type=PrincipalType.HUMAN,
        credential_expires_at=evidence.valid_until,
        assignment_ref="assignment:engineering",
        authentication=evidence,
    )


def authenticated_kernel(
    sessions: async_sessionmaker[AsyncSession],
    identity: IdentityService,
    *,
    handler: Handler = update_work_details,
    **kwargs: Any,
) -> CommandKernel:
    loader = AuthenticatedAuthorizationLoader(identity)
    return CommandKernel(
        sessions,
        loader=loader,
        coordinator=loader,
        identity_validator=loader,
        commands=[replace(WORK_DETAILS_COMMAND, handler=handler)],
        **kwargs,
    )
