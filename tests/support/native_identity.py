"""Shared synthetic builders for native identity."""

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pyotp
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.execution.contracts import ExecutionContext
from app.identity.native import NATIVE_PROFILE_SCHEMA
from app.identity.service import IdentityService, content_digest, profile_content
from app.models.identity_foundation import (
    AuthenticationProfileTable,
    ExternalIdentityTable,
    IdentityProviderTable,
)
from app.models.native_auth import NativeClientTable
from app.native_auth.config import NativeAuthConfig
from app.native_auth.credentials import NativeCredentials
from app.native_auth.keys import KeyRing
from app.native_auth.oauth import NativeOAuth
from tests.support.authenticated_commands import authenticated_context, seed_authenticated_command
from tests.support.authorization import ORG, PERSON
from tests.support.native_oauth import VERIFIER, authorize_values

PASSWORD = "native-integration-password-2026"


@pytest.fixture(scope="module")
def native_keyring() -> KeyRing:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return KeyRing("integration-key", [{"kid": "integration-key", "private_pem": pem}])


async def native_context(
    sessions: async_sessionmaker[AsyncSession], keyring: KeyRing
) -> tuple[IdentityService, NativeOAuth, ExecutionContext, dict[str, Any]]:
    await seed_authenticated_command(sessions)
    config = NativeAuthConfig(
        issuer="http://127.0.0.1:8080", organization_ref=ORG, trust_ref="trust", environment="test"
    )
    values = authorize_values()
    async with sessions.begin() as session:
        profile = await session.get(AuthenticationProfileTable, ("local-human", 1))
        provider = await session.get(IdentityProviderTable, "provider")
        external = await session.get(ExternalIdentityTable, "identity")
        assert profile and provider and external
        provider.issuer_exact = config.issuer
        profile.config_schema = NATIVE_PROFILE_SCHEMA
        profile.subject_namespace = "stage-native-human-v1"
        profile.config_json = {**profile.config_json, "jwks_url": config.issuer + "/oauth/jwks"}
        profile.digest = content_digest(profile_content(profile))
        external.subject_namespace = profile.subject_namespace
        external.subject_exact = "native:alice"
        session.add(NativeClientTable(client_id="maestro", redirect_uris=[values["redirect_uri"]]))
    credentials = NativeCredentials(sessions, fernet_key=Fernet.generate_key(), organization_ref=ORG, trust_ref="trust")
    await credentials.create_account("native:alice", PERSON, "alice", PASSWORD)
    oauth = NativeOAuth(sessions, config, keyring, credentials=credentials)
    request = await oauth.prepare_authorization(**{**values, "client_id": "maestro"})
    progress = await credentials.password(
        request.request_ref, request.browser_secret, request.csrf_token, "alice", PASSWORD
    )
    assert progress.enrollment_secret
    await credentials.otp(
        request.request_ref, request.browser_secret, request.csrf_token, pyotp.TOTP(progress.enrollment_secret).now()
    )
    redirect = await oauth.finish_authorization(
        request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
    )
    tokens = await oauth.exchange_code(
        client_id="maestro",
        redirect_uri=values["redirect_uri"],
        code=parse_qs(urlsplit(redirect).query)["code"][0],
        code_verifier=VERIFIER,
    )
    identity = IdentityService(sessions, environment="test", native_keyring=keyring, native_issuer=config.issuer)
    context = await authenticated_context(identity, tokens["access_token"])
    return identity, oauth, context, tokens
