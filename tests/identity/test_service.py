"""Verified credentials require current identity facts in the deployment's sole organization."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.identity.authentication import AuthenticationRejected, AuthenticationUnavailable
from app.identity.service import (
    IdentityDenied,
    IdentityService,
)
from app.main import create_app
from app.models.execution import TenantGuardTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import (
    AuthenticationProfileTable,
    ExternalIdentityTable,
    OrganizationAuthenticationBindingTable,
    OrganizationMembershipTable,
    OrganizationRootTable,
    TrustStateTable,
    WorkloadRegistrationTable,
)
from app.providers.identity import IDENTITY
from app.runtime import ComponentContext, PluginSpec, RuntimeHost
from tests.support.identity_service import (
    read,
    seed,
)
from tests.support.identity_tokens import jwks, token


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


async def test_self_projection_resolves_the_deployment_organization(
    persistence_sessions: async_sessionmaker[AsyncSession],
    key: rsa.RSAPrivateKey,
) -> None:
    await seed(persistence_sessions)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as client:
        service = IdentityService(persistence_sessions, environment="test", http_client=client)
        result = await read(service, token(key))
        assert result.organization_ref == "tenant-a" and result.membership_ref == "member-a"
        assert result.principal_ref == "person" and result.authenticated_workload_ref is None
        assert "subject-1" not in result.model_dump_json() and "tenant_ref" not in result.model_dump()
        with pytest.raises(IdentityDenied):
            await read(service, token(key, claims={"sub": "unknown", "email": "person@example.com"}))
    async with persistence_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PrincipalTable)) == 1
        assert await session.scalar(select(func.count()).select_from(TenantGuardTable)) == 1


async def test_verified_identity_without_membership_is_denied(
    persistence_sessions: async_sessionmaker[AsyncSession],
    key: rsa.RSAPrivateKey,
) -> None:
    await seed(persistence_sessions)
    async with persistence_sessions.begin() as session:
        await session.execute(delete(OrganizationMembershipTable))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as client:
        with pytest.raises(IdentityDenied):
            await read(IdentityService(persistence_sessions, environment="test", http_client=client), token(key))


async def test_external_subject_preserves_unicode_and_exact_case(
    persistence_sessions: async_sessionmaker[AsyncSession],
    key: rsa.RSAPrivateKey,
) -> None:
    await seed(persistence_sessions)
    subject = "Subject:用户é"
    async with persistence_sessions.begin() as session:
        await session.execute(update(ExternalIdentityTable).values(subject_exact=subject))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as client:
        service = IdentityService(persistence_sessions, environment="test", http_client=client)
        assert (await read(service, token(key, claims={"sub": subject}))).principal_ref == "person"
        for different in (subject.lower(), "Subject:用户e\u0301"):
            with pytest.raises(IdentityDenied):
                await read(service, token(key, claims={"sub": different}))


async def test_missing_deployment_organization_is_unavailable(
    persistence_sessions: async_sessionmaker[AsyncSession],
    key: rsa.RSAPrivateKey,
) -> None:
    await seed(persistence_sessions, include_organization=False)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as client:
        with pytest.raises(AuthenticationUnavailable):
            await read(IdentityService(persistence_sessions, environment="test", http_client=client), token(key))


@pytest.mark.parametrize("target", ["principal", "organization", "membership", "mapping", "profile", "binding"])
async def test_cached_signature_keys_do_not_bypass_current_revocation(
    persistence_sessions: async_sessionmaker[AsyncSession],
    key: rsa.RSAPrivateKey,
    target: str,
) -> None:
    await seed(persistence_sessions)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as client:
        service = IdentityService(persistence_sessions, environment="test", http_client=client)
        bearer = token(key)
        await read(service, bearer)
        model = {
            "principal": PrincipalTable,
            "organization": OrganizationRootTable,
            "membership": OrganizationMembershipTable,
            "mapping": ExternalIdentityTable,
            "profile": AuthenticationProfileTable,
            "binding": OrganizationAuthenticationBindingTable,
        }[target]
        async with persistence_sessions.begin() as session:
            values: dict[str, Any] = {"status": "revoked", "version": 2}
            if target == "membership":
                values["ended_at"] = datetime.now(UTC)
            await session.execute(update(model).values(**values))
        with pytest.raises(IdentityDenied):
            await read(service, bearer)


@pytest.mark.parametrize(
    "constraints",
    [
        {"allowed_clients": ["another-client"]},
        {"allowed_clients": ["maestro"], "required_acr": "mfa"},
        {"allowed_clients": ["maestro"], "unsupported_rule": True},
    ],
)
async def test_organization_admission_is_not_implied_by_membership(
    persistence_sessions: async_sessionmaker[AsyncSession],
    key: rsa.RSAPrivateKey,
    constraints: dict[str, Any],
) -> None:
    await seed(persistence_sessions)
    async with persistence_sessions.begin() as session:
        await session.execute(update(OrganizationAuthenticationBindingTable).values(constraints_json=constraints))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as client:
        with pytest.raises(IdentityDenied):
            await read(IdentityService(persistence_sessions, environment="test", http_client=client), token(key))


async def test_workload_needs_an_exact_current_registration(
    persistence_sessions: async_sessionmaker[AsyncSession],
    key: rsa.RSAPrivateKey,
) -> None:
    await seed(persistence_sessions, service=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as client:
        service = IdentityService(persistence_sessions, environment="test", http_client=client)
        bearer = token(key, claims={"stage_credential_kind": "workload_oauth2"})
        assert (await read(service, bearer)).authenticated_workload_ref == "workload"
        async with persistence_sessions.begin() as session:
            await session.execute(update(WorkloadRegistrationTable).values(environment="another-environment"))
        with pytest.raises(IdentityDenied):
            await read(service, bearer)


async def test_profile_change_during_key_fetch_rejects_old_evidence_without_holding_a_connection(
    persistence_sessions: async_sessionmaker[AsyncSession],
    key: rsa.RSAPrivateKey,
) -> None:
    await seed(persistence_sessions)

    async def fetch(_: httpx.Request) -> httpx.Response:
        pool = persistence_sessions.kw["bind"].pool
        assert pool.checkedout() == 0
        async with persistence_sessions.begin() as session:
            await session.execute(update(TrustStateTable).values(key_status_revision=2, version=2))
        return httpx.Response(200, json=jwks(key))

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        with pytest.raises(AuthenticationRejected, match="authentication_changed"):
            await read(IdentityService(persistence_sessions, environment="test", http_client=client), token(key))


async def test_http_identity_requires_bearer_and_rejects_organization_selection(
    persistence_sessions: async_sessionmaker[AsyncSession],
    key: rsa.RSAPrivateKey,
) -> None:
    await seed(persistence_sessions)
    host = RuntimeHost()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(key)))
    ) as verifier:
        service = IdentityService(persistence_sessions, environment="test", http_client=verifier)

        def setup(ctx: ComponentContext) -> None:
            ctx.provide(IDENTITY, service)

        host.load(PluginSpec("test.identity", setup, provides=(IDENTITY,)))
        assert (await host.await_ready([IDENTITY], timeout=5)).ready
        app = create_app()
        app.state.runtime = SimpleNamespace(host=host, ready=True)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://stage") as client:
                path = "/api/v1/identity/me?profile_ref=local-human&profile_version=1"
                missing = await client.get(path, headers={"X-Principal-ID": "person"})
                assert missing.status_code == 401 and missing.headers["www-authenticate"] == "Bearer"
                wrong = await client.get(path, headers={"Authorization": "Bearer private-invalid-token"})
                assert wrong.status_code == 401 and "private-invalid-token" not in wrong.text
                headers = {"Authorization": f"Bearer {token(key)}"}
                success = await client.get(path, headers=headers)
                assert success.status_code == 200 and success.headers["cache-control"] == "no-store"
                assert success.json()["organization_ref"] == "tenant-a"
                assert "tenant_ref" not in success.json()
                for selector in ("tenant_ref", "organization_ref"):
                    denied = await client.get(path + f"&{selector}=another-organization", headers=headers)
                    assert denied.status_code == 422 and denied.headers["cache-control"] == "no-store"
                spoofed = await client.get(
                    path,
                    headers={**headers, "X-Tenant-ID": "another-organization", "X-Organization-ID": "forged"},
                )
                assert spoofed.status_code == 200 and spoofed.json()["organization_ref"] == "tenant-a"
                async with persistence_sessions.begin() as session:
                    await session.execute(delete(OrganizationMembershipTable))
                denied = await client.get(path, headers=headers)
                assert denied.status_code == 403 and denied.json()["error"]["code"] == "IDENTITY_ACCESS_DENIED"
                assert "member-a" not in denied.text and "person" not in denied.text
        finally:
            assert (await host.close(timeout=5)).successful
