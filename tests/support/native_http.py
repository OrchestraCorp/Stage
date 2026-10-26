"""Shared synthetic builders for native http."""

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from html import unescape
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pyotp
import pytest
from authlib.oauth2.rfc7636.challenge import create_s256_code_challenge
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.native_auth import NativeAuthSecurityMiddleware, account_router, router
from app.core.config import Settings
from app.core.errors import register_exception_handlers
from app.models.execution import TenantGuardTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import OrganizationRootTable, TrustStateTable
from app.models.native_auth import (
    NativeClientTable,
)
from app.native_auth.config import NativeAuthConfig
from app.native_auth.credentials import NativeCredentials
from app.native_auth.keys import KeyRing
from app.providers.native_auth import NATIVE_AUTH, NativeAuthFacade
from app.providers.storage import STORAGE, Storage
from app.runtime import ComponentContext, PluginSpec, RuntimeHost

ISSUER = "http://127.0.0.1:8080"


CLIENT = "maestro-http"


CALLBACK = "http://127.0.0.1:49152/callback"


VERIFIER = "http-verifier-" + "x" * 48


PASSWORD = "synthetic-http-password"


@pytest.fixture(scope="module")
def http_keyring() -> KeyRing:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return KeyRing("http-test-key", [{"kid": "http-test-key", "private_pem": pem}])


@dataclass
class Harness:
    client: httpx.AsyncClient
    app: FastAPI
    sessions: async_sessionmaker[AsyncSession]
    credentials: NativeCredentials


@pytest.fixture
async def native_http(
    persistence_sessions: async_sessionmaker[AsyncSession], http_keyring: KeyRing
) -> AsyncIterator[Harness]:
    async with persistence_sessions.begin() as session:
        session.add(OrganizationRootTable(organization_ref="org:http", display_name="HTTP test"))
        session.add(PrincipalTable(principal_ref="person:http", principal_type="human"))
        session.add(
            TrustStateTable(trust_ref="trust:native-auth", purpose="authentication", configuration_digest="a" * 64)
        )
        await session.flush()
        session.add(TenantGuardTable(tenant_ref="org:http"))
        session.add(NativeClientTable(client_id=CLIENT, redirect_uris=[CALLBACK], enabled=True))
    encryption_key = Fernet.generate_key()
    config = NativeAuthConfig(
        issuer=ISSUER,
        organization_ref="org:http",
        environment="test",
        peer_attempt_limit=30,
        authorization_attempt_limit=30,
    )
    credentials = NativeCredentials(
        persistence_sessions,
        fernet_key=encryption_key,
        organization_ref=config.organization_ref,
        trust_ref=config.trust_ref,
    )
    await credentials.create_account("account:http", "person:http", "http-user", PASSWORD)
    engine = persistence_sessions.kw["bind"]
    assert isinstance(engine, AsyncEngine)

    def storage(ctx: ComponentContext) -> None:
        ctx.provide(STORAGE, Storage(engine, persistence_sessions))

    def native(ctx: ComponentContext) -> None:
        ctx.provide(NATIVE_AUTH, NativeAuthFacade(ctx, config, http_keyring, encryption_key))

    host = RuntimeHost()
    host.load(PluginSpec("http-test.storage", storage, provides=(STORAGE,)))
    host.load(PluginSpec("http-test.native", native, requires=(STORAGE,), provides=(NATIVE_AUTH,)))
    assert (await host.await_ready([NATIVE_AUTH], timeout=2)).ready
    app = FastAPI()
    app.state.runtime = SimpleNamespace(host=host, ready=True)
    app.state.settings = Settings(
        env="test",
        native_auth_enabled=True,
        native_auth_issuer=ISSUER,
        native_auth_organization_ref="org:http",
        native_auth_key_file="unused-injected-key",
        native_auth_encryption_key_file="unused-injected-encryption-key",
    )
    app.include_router(router)
    app.include_router(account_router, prefix="/api/v1")
    app.add_middleware(NativeAuthSecurityMiddleware)
    register_exception_handlers(app)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as client:
            yield Harness(client, app, persistence_sessions, credentials)
    finally:
        assert (await host.close(timeout=2)).successful


def parameters() -> dict[str, str]:
    return {
        "client_id": CLIENT,
        "redirect_uri": CALLBACK,
        "response_type": "code",
        "scope": "openid",
        "state": "http-state",
        "nonce": "http-nonce",
        "code_challenge": create_s256_code_challenge(VERIFIER),
        "code_challenge_method": "S256",
    }


def fields(response: httpx.Response) -> dict[str, str]:
    return {
        name: unescape(value)
        for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', response.text)
    }


async def begin(harness: Harness) -> dict[str, str]:
    # Credential tests intentionally exercise a fresh ceremony even when an
    # earlier authorization established the independent browser SSO cookie.
    response = await harness.client.get("/oauth/authorize", params={**parameters(), "prompt": "login"})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"] and "SameSite=lax" in response.headers["set-cookie"]
    return fields(response)


async def password(harness: Harness, form: dict[str, str], value: str = PASSWORD) -> httpx.Response:
    return await harness.client.post(
        "/oauth/login", data={**form, "stage": "password", "username": "http-user", "password": value}
    )


async def exchange(harness: Harness, response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["state"] == ["http-state"]
    response = await harness.client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT,
            "redirect_uri": CALLBACK,
            "code": query["code"][0],
            "code_verifier": VERIFIER,
        },
    )
    assert response.status_code == 200
    return response.json()


async def enroll(harness: Harness) -> tuple[dict[str, Any], str, list[str]]:
    form = await begin(harness)
    response = await password(harness, form)
    assert response.status_code == 200
    secret = re.findall(r'<code id="enrollment-secret">([^<]+)</code>', response.text)[0]
    response = await harness.client.post(
        "/oauth/login", data={**form, "stage": "otp", "code": pyotp.TOTP(secret).now()}
    )
    assert response.status_code == 200
    codes = re.findall(r"<li><code>([^<]+)</code></li>", response.text)
    assert len(codes) == 10
    response = await harness.client.post("/oauth/login", data={**form, "stage": "continue"})
    return await exchange(harness, response), secret, codes
