"""A verified human must remain the exact current identity at command commit."""

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.execution.storage import lock_tenant
from app.identity.authentication import AuthenticationRejected
from app.identity.evidence import HumanAuthenticationEvidence
from app.identity.service import IdentityDenied, IdentityService
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
from tests.support.identity_service import seed
from tests.support.identity_tokens import jwks, token


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


async def authenticate(service: IdentityService, bearer: str) -> HumanAuthenticationEvidence:
    return await service.authenticate_human(bearer, profile_ref="local-human", profile_version=1)


async def validate(
    service: IdentityService,
    sessions: async_sessionmaker[AsyncSession],
    evidence: HumanAuthenticationEvidence,
) -> None:
    async with sessions() as session, session.begin():
        await service.acquire_human_locks(session, evidence)
        await lock_tenant(session, evidence.organization_ref)
        await service.validate_human(session, evidence, datetime.now(UTC))


async def test_human_evidence_is_frozen_token_free_and_revalidated_without_network(
    persistence_sessions: async_sessionmaker[AsyncSession], key: rsa.RSAPrivateKey
) -> None:
    await seed(persistence_sessions)
    requests = 0

    def fetch(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json=jwks(key))

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        service = IdentityService(persistence_sessions, environment="test", http_client=client)
        bearer = token(key)
        evidence = await authenticate(service, bearer)
        assert evidence.principal_ref == "person" and evidence.membership_ref == "member-a"
        assert evidence.organization_ref == "tenant-a" and evidence.external_identity_ref == "identity"
        with pytest.raises(FrozenInstanceError):
            evidence.membership_ref = "forged"  # type: ignore[misc]
        audit = json.dumps(evidence.audit_snapshot())
        assert bearer not in audit and "subject-1" not in audit
        assert "subject-1" not in repr(evidence)
        assert requests == 1
        await client.aclose()
        await validate(service, persistence_sessions, evidence)
        assert requests == 1


@pytest.mark.parametrize(
    "model,values",
    [
        (PrincipalTable, {"status": "revoked", "version": 2}),
        (PrincipalTable, {"identity_epoch": 1}),
        (PrincipalTable, {"recovery_epoch": 1}),
        (PrincipalTable, {"version": 2}),
        (OrganizationRootTable, {"status": "revoked", "version": 2}),
        (OrganizationRootTable, {"version": 2}),
        (OrganizationMembershipTable, {"status": "suspended", "version": 2}),
        (OrganizationMembershipTable, {"version": 2}),
        (ExternalIdentityTable, {"status": "revoked", "version": 2}),
        (ExternalIdentityTable, {"version": 2}),
        (ExternalIdentityTable, {"subject_exact": "different-subject"}),
        (AuthenticationProfileTable, {"status": "revoked", "version": 2}),
        (AuthenticationProfileTable, {"version": 2}),
        (IdentityProviderTable, {"status": "revoked", "version": 2}),
        (TrustStateTable, {"key_status_revision": 2}),
        (TrustStateTable, {"trust_config_revision": 2}),
        (OrganizationAuthenticationBindingTable, {"status": "revoked", "version": 2}),
        (OrganizationAuthenticationBindingTable, {"version": 2}),
        (OrganizationAuthenticationBindingTable, {"environment": "production"}),
        (OrganizationAuthenticationBindingTable, {"constraints_json": {"allowed_clients": ["other"]}}),
        (
            OrganizationAuthenticationBindingTable,
            {"constraints_json": {"allowed_clients": ["maestro"], "required_acr": "mfa"}},
        ),
    ],
)
async def test_changed_identity_dependency_rejects_previously_verified_evidence(
    persistence_sessions: async_sessionmaker[AsyncSession],
    key: rsa.RSAPrivateKey,
    model: Any,
    values: dict[str, Any],
) -> None:
    await seed(persistence_sessions)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as client:
        service = IdentityService(persistence_sessions, environment="test", http_client=client)
        evidence = await authenticate(service, token(key))
        async with persistence_sessions.begin() as session:
            await session.execute(update(model).values(**values))
        with pytest.raises((IdentityDenied, AuthenticationRejected)):
            await validate(service, persistence_sessions, evidence)


async def test_rejoining_cannot_rebind_old_human_evidence(
    persistence_sessions: async_sessionmaker[AsyncSession], key: rsa.RSAPrivateKey
) -> None:
    await seed(persistence_sessions)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as client:
        service = IdentityService(persistence_sessions, environment="test", http_client=client)
        bearer = token(key)
        old = await authenticate(service, bearer)
        async with persistence_sessions.begin() as session:
            now = datetime.now(UTC)
            await session.execute(update(OrganizationMembershipTable).values(status="left", ended_at=now, version=2))
            session.add(
                OrganizationMembershipTable(
                    organization_ref="tenant-a",
                    membership_ref="member-new",
                    principal_ref="person",
                    principal_type="human",
                    source_ref="fixture-rejoin",
                    valid_from=now,
                )
            )
        with pytest.raises(IdentityDenied):
            await validate(service, persistence_sessions, old)
        fresh = await authenticate(service, bearer)
        assert fresh.membership_ref == "member-new"
        await validate(service, persistence_sessions, fresh)


async def test_same_identity_refresh_gets_new_evidence_without_changing_actor_identity(
    persistence_sessions: async_sessionmaker[AsyncSession], key: rsa.RSAPrivateKey
) -> None:
    await seed(persistence_sessions)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as client:
        service = IdentityService(persistence_sessions, environment="test", http_client=client)
        original = await authenticate(service, token(key))
        refreshed = await authenticate(service, token(key, claims={"jti": "fresh-token"}))
        assert refreshed.credential.evidence_ref != original.credential.evidence_ref
        assert (
            refreshed.principal_ref,
            refreshed.organization_ref,
            refreshed.membership_ref,
        ) == (original.principal_ref, original.organization_ref, original.membership_ref)
        await validate(service, persistence_sessions, refreshed)


async def test_service_credential_cannot_issue_human_command_evidence(
    persistence_sessions: async_sessionmaker[AsyncSession], key: rsa.RSAPrivateKey
) -> None:
    await seed(persistence_sessions, service=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as client:
        service = IdentityService(persistence_sessions, environment="test", http_client=client)
        bearer = token(key, claims={"stage_credential_kind": "workload_oauth2"})
        assert (
            await service.read_self(bearer, profile_ref="local-human", profile_version=1)
        ).principal_type == "service"
        with pytest.raises(IdentityDenied):
            await authenticate(service, bearer)


async def test_expired_evidence_and_wrong_environment_fail_closed(
    persistence_sessions: async_sessionmaker[AsyncSession], key: rsa.RSAPrivateKey
) -> None:
    await seed(persistence_sessions)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as client:
        service = IdentityService(persistence_sessions, environment="test", http_client=client)
        evidence = await authenticate(service, token(key))
        expired = replace(evidence, valid_until=datetime.now(UTC) - timedelta(seconds=1))
        with pytest.raises(AuthenticationRejected, match="identity_evidence_expired"):
            await validate(service, persistence_sessions, expired)
        other = IdentityService(persistence_sessions, environment="production", http_client=client)
        with pytest.raises(IdentityDenied):
            await validate(other, persistence_sessions, evidence)


async def test_revalidation_refreshes_previously_loaded_identity_map(
    persistence_sessions: async_sessionmaker[AsyncSession], key: rsa.RSAPrivateKey
) -> None:
    await seed(persistence_sessions)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as client:
        service = IdentityService(persistence_sessions, environment="test", http_client=client)
        evidence = await authenticate(service, token(key))
        async with persistence_sessions() as session, session.begin():
            stale = await session.scalar(select(PrincipalTable))
            assert stale is not None and stale.identity_epoch == 0
            async with persistence_sessions.begin() as writer:
                await writer.execute(update(PrincipalTable).values(identity_epoch=1))
            assert stale.identity_epoch == 0
            await service.acquire_human_locks(session, evidence)
            await lock_tenant(session, evidence.organization_ref)
            with pytest.raises(IdentityDenied):
                await service.validate_human(session, evidence, datetime.now(UTC))
