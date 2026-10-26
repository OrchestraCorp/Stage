"""Shared synthetic builders for native oauth."""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from authlib.oauth2.rfc7636.challenge import create_s256_code_challenge
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.execution import TenantGuardTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import OrganizationRootTable, TrustStateTable
from app.models.native_auth import NativeAccountTable, NativeClientTable, NativeLoginRequestTable
from app.native_auth.config import NativeAuthConfig
from app.native_auth.credentials import NativeCredentials
from app.native_auth.keys import KeyRing
from app.native_auth.oauth import AuthorizationRequest, NativeOAuth

CLIENT = "maestro-native"


REDIRECT = "http://127.0.0.1:49152/callback"


VERIFIER = "v" * 64


@pytest.fixture(scope="module")
def native_keyring() -> KeyRing:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return KeyRing("native-test-key", [{"kid": "native-test-key", "private_pem": pem}])


@pytest.fixture
async def oauth(persistence_sessions: async_sessionmaker[AsyncSession], native_keyring: KeyRing) -> NativeOAuth:
    async with persistence_sessions.begin() as session:
        session.add(OrganizationRootTable(organization_ref="org:native", display_name="Native test"))
        session.add(PrincipalTable(principal_ref="person:native", principal_type="human"))
        session.add(
            TrustStateTable(trust_ref="trust:native-auth", purpose="authentication", configuration_digest="a" * 64)
        )
        await session.flush()
        session.add(TenantGuardTable(tenant_ref="org:native"))
        session.add(NativeClientTable(client_id=CLIENT, redirect_uris=[REDIRECT], enabled=True))
        session.add(
            NativeAccountTable(
                account_ref="account:native",
                principal_ref="person:native",
                username="native-test",
                password_hash="not-used-by-oauth",
                totp_secret="encrypted-test-placeholder",
                credential_version=1,
            )
        )
    config = NativeAuthConfig(issuer="http://127.0.0.1:8080", organization_ref="org:native", environment="test")
    credentials = NativeCredentials(
        persistence_sessions,
        fernet_key=Fernet.generate_key(),
        organization_ref=config.organization_ref,
        trust_ref=config.trust_ref,
    )
    return NativeOAuth(persistence_sessions, config, native_keyring, credentials=credentials)


def authorize_values() -> dict[str, str]:
    return {
        "client_id": CLIENT,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": "openid",
        "state": "caller-state",
        "nonce": "caller-nonce",
        "code_challenge": create_s256_code_challenge(VERIFIER),
        "code_challenge_method": "S256",
    }


async def authenticated_request(oauth: NativeOAuth) -> AuthorizationRequest:
    request = await oauth.prepare_authorization(**authorize_values())
    # Credential-factor tests cover the transition. OAuth must never accept a
    # caller's claim of authentication: only this persisted completed state.
    async with oauth.sessions.begin() as session:
        row = await session.get(NativeLoginRequestTable, request.request_ref)
        assert row is not None
        row.account_ref = "account:native"
        row.credential_version = 1
        row.password_verified_at = datetime.now(UTC) - timedelta(seconds=2)
        row.status = "authenticated"
    return request


async def authorization_code(oauth: NativeOAuth) -> str:
    request = await authenticated_request(oauth)
    redirect = await oauth.finish_authorization(
        request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
    )
    query = parse_qs(urlsplit(redirect).query)
    assert query["state"] == ["caller-state"]
    return query["code"][0]


async def tokens(oauth: NativeOAuth) -> dict[str, Any]:
    return await oauth.exchange_code(
        client_id=CLIENT, redirect_uri=REDIRECT, code=await authorization_code(oauth), code_verifier=VERIFIER
    )


async def configure_protocol_client(oauth: NativeOAuth, **changes: Any) -> None:
    async with oauth.sessions.begin() as session:
        client = await session.get(NativeClientTable, CLIENT)
        assert client is not None
        for name, value in changes.items():
            setattr(client, name, value)


async def scoped_code(oauth: NativeOAuth, scope: str, *, consent: bool = False) -> str:
    request = await oauth.prepare_authorization(**{**authorize_values(), "scope": scope})
    async with oauth.sessions.begin() as session:
        row = await session.get(NativeLoginRequestTable, request.request_ref)
        assert row is not None
        row.account_ref, row.credential_version = "account:native", 1
        row.password_verified_at, row.status = datetime.now(UTC), "authenticated"
        row.consent_granted = consent
    result = await oauth.finish_authorization_response(
        request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
    )
    return result.parameters["code"]
