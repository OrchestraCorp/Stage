"""Verified credentials require current identity facts in the deployment's sole organization."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.identity.authentication import AuthenticationRejected, AuthenticationUnavailable
from app.identity.service import (
    PROFILE_SCHEMA,
    IdentityDenied,
    IdentityService,
    ProfileConfiguration,
    content_digest,
    profile_content,
)
from app.main import create_app
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
from app.providers.identity import IDENTITY
from app.runtime import ComponentContext, PluginSpec, RuntimeHost
from tests.identity.test_authentication import jwks, profile, token


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


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
