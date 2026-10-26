"""A different RP port does not isolate the issuer's host-only login cookie."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.identity.administration import IdentityAdministrationError
from app.models.native_auth import (
    NativeAuthorizationCodeTable,
    NativeClientTable,
    NativeLoginRequestTable,
    NativeLogoutRequestTable,
)
from app.native_auth.administration import InstallPlan
from app.native_auth.browser import BrowserAuthorization
from app.native_auth.client_administration import configure_client
from app.native_auth.crypto import digest_secret
from app.native_auth.logout import NativeLogout
from app.native_auth.oauth import NativeOAuth, OAuthError
from app.native_auth.redirects import validate_issuer_redirect
from tests.support import native_oauth as oauth_support
from tests.support.identity_administration import OPERATOR
from tests.support.identity_administration import install as install_existing_identity
from tests.support.native_administration import install_native, plan_values
from tests.support.native_clients import configuration

native_keyring = oauth_support.native_keyring
oauth = oauth_support.oauth
ISSUER = "https://auth.example:8443"


def test_host_only_secure_cookie_is_still_sent_to_another_port_on_the_same_host() -> None:
    # Extract a real Set-Cookie without Domain rather than constructing a domain cookie.
    with httpx.Client() as browser:
        request = browser.build_request("GET", ISSUER + "/oauth/authorize")
        response = httpx.Response(
            200,
            request=request,
            headers={
                "set-cookie": "__Host-stage-sso=synthetic-test-cookie; Path=/; Secure; HttpOnly; SameSite=Lax",
            },
        )
        browser.cookies.extract_cookies(response)
        other_port = browser.build_request("GET", "https://auth.example:9443/callback")
        other_host = browser.build_request("GET", "https://application.example/callback")
        assert "__Host-stage-sso=synthetic-test-cookie" in other_port.headers.get("cookie", "")
        assert "__Host-stage-sso" not in other_host.headers.get("cookie", "")


@pytest.mark.parametrize("environment", ["dev", "test", "prod"])
@pytest.mark.parametrize(
    "uri",
    [
        "https://auth.example:9443/callback",
        "https://auth.example:8443/callback",
        "https://auth.example/another/path",
        "https://auth.example:9443/callback?application=rp",
        "https://auth.example.:9443/callback",
    ],
)
def test_same_issuer_hostname_is_rejected_regardless_of_port_path_or_query(environment: str, uri: str) -> None:
    with pytest.raises(ValueError, match="native_issuer_requires_dedicated_callback_hostname"):
        validate_issuer_redirect(ISSUER, uri, environment)


@pytest.mark.parametrize(
    "issuer,uri",
    [
        ("https://127.0.0.1:8443", "https://127.0.0.1:9443/cb"),
        ("https://[::1]:8443", "https://[::1]:9443/cb"),
        ("https://localhost:8443", "https://localhost:9443/cb"),
    ],
)
def test_production_loopback_does_not_receive_the_development_exception(issuer: str, uri: str) -> None:
    with pytest.raises(ValueError, match="native_issuer_requires_dedicated_callback_hostname"):
        validate_issuer_redirect(issuer, uri, "prod")
    validate_issuer_redirect(issuer, uri, "test")
    validate_issuer_redirect(issuer, uri, "dev")


@pytest.mark.parametrize(
    "issuer,uri",
    [
        (ISSUER, "https://application.example/callback"),
        (ISSUER, "https://maestro.example:9443/callback"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:49152/callback"),
    ],
)
def test_dedicated_application_hostname_and_explicit_local_development_work(issuer: str, uri: str) -> None:
    validate_issuer_redirect(issuer, uri, "test")
    if issuer.startswith("https"):
        validate_issuer_redirect(issuer, uri, "prod")


@pytest.mark.parametrize("uri", ["https://auth.example:9443/cb", "https://auth.example/cb?app=native"])
def test_legacy_native_account_installation_enforces_the_same_cookie_boundary(uri: str) -> None:
    with pytest.raises(ValueError, match="native_issuer_requires_dedicated_callback_hostname"):
        InstallPlan.model_validate({**plan_values(), "issuer": ISSUER, "redirect_uris": [uri]})


@pytest.mark.parametrize("field", ["redirect_uris", "post_logout_redirect_uris"])
@pytest.mark.parametrize("existing", [False, True])
async def test_client_registration_uses_the_stored_issuer_to_reject_cookie_exposing_callbacks(
    persistence_sessions: async_sessionmaker[AsyncSession],
    field: str,
    existing: bool,
) -> None:
    await install_existing_identity(persistence_sessions)
    await install_native(persistence_sessions, issuer=ISSUER)
    if existing:
        await configure_client(persistence_sessions, configuration(), actor_ref=OPERATOR, idempotency_key="initial")
    plan = configuration(expected_version=1 if existing else 0, **{field: ["https://auth.example:9443/callback"]})
    with pytest.raises(IdentityAdministrationError, match="invalid_client_redirect_topology"):
        await configure_client(persistence_sessions, plan, actor_ref=OPERATOR, idempotency_key="unsafe")
    async with persistence_sessions() as session:
        client = await session.get(NativeClientTable, "knowledge-web")
        if existing:
            assert (
                client is not None
                and client.version == 1
                and client.redirect_uris == ["https://knowledge.example/callback"]
            )
        else:
            assert client is None


def production_oauth(oauth: NativeOAuth) -> NativeOAuth:
    return NativeOAuth(
        oauth.sessions,
        replace(oauth.config, issuer=ISSUER, environment="prod"),
        oauth.keyring,
        credentials=oauth.credentials,
    )


@pytest.mark.parametrize("uri", ["https://auth.example:9443/callback", "https://auth.example:8443/different/path"])
async def test_authorize_rejects_legacy_same_host_registration_without_redirecting_an_error(
    oauth: NativeOAuth, uri: str
) -> None:
    async with oauth.sessions.begin() as session:
        client = await session.get(NativeClientTable, oauth_support.CLIENT)
        assert client is not None
        client.redirect_uris = [uri]
    service = production_oauth(oauth)
    with pytest.raises(OAuthError) as failure:
        await service.prepare_authorization(**{**oauth_support.authorize_values(), "redirect_uri": uri})
    assert failure.value.response is None
    async with oauth.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NativeLoginRequestTable)) == 0


@pytest.mark.parametrize("entry", ["advance", "finish"])
async def test_existing_authenticated_request_cannot_complete_or_redirect_to_the_issuer_host(
    oauth: NativeOAuth, entry: str
) -> None:
    request = await oauth_support.authenticated_request(oauth)
    async with oauth.sessions.begin() as session:
        client = await session.get(NativeClientTable, oauth_support.CLIENT)
        saved = await session.get(NativeLoginRequestTable, request.request_ref)
        assert client is not None and saved is not None
        client.redirect_uris = ["https://auth.example:9443/callback"]
        saved.redirect_uri = client.redirect_uris[0]
    service = production_oauth(oauth)
    with pytest.raises(OAuthError) as failure:
        if entry == "advance":
            browser = BrowserAuthorization(
                service.sessions, service.config, credentials=service.credentials, oauth=service
            )
            await browser.advance(request.request_ref, request.browser_secret, request.csrf_token, consent=True)
        else:
            await service.finish_authorization_response(
                request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
            )
    assert failure.value.response is None
    async with oauth.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NativeAuthorizationCodeTable)) == 0


async def test_previously_issued_code_with_cookie_exposing_callback_cannot_be_exchanged(oauth: NativeOAuth) -> None:
    uri = "https://auth.example:9443/callback"
    async with oauth.sessions.begin() as session:
        client = await session.get(NativeClientTable, oauth_support.CLIENT)
        assert client is not None
        client.redirect_uris = [uri]
    request = await oauth.prepare_authorization(**{**oauth_support.authorize_values(), "redirect_uri": uri})
    async with oauth.sessions.begin() as session:
        saved = await session.get(NativeLoginRequestTable, request.request_ref)
        assert saved is not None
        saved.account_ref, saved.credential_version = "account:native", 1
        saved.password_verified_at = datetime.now(UTC) - timedelta(seconds=1)
        saved.status = "authenticated"
    issued = await oauth.finish_authorization_response(
        request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
    )
    code = issued.parameters["code"]
    with pytest.raises(OAuthError):
        await production_oauth(oauth).exchange_code(
            client_id=oauth_support.CLIENT, redirect_uri=uri, code=code, code_verifier=oauth_support.VERIFIER
        )
    async with oauth.sessions() as session:
        saved_code = await session.get(NativeAuthorizationCodeTable, digest_secret(code))
        assert saved_code is not None and saved_code.consumed_at is None


@pytest.mark.parametrize("entry", ["prepare", "confirm"])
async def test_logout_never_redirects_to_same_host_legacy_registration(oauth: NativeOAuth, entry: str) -> None:
    uri = "https://auth.example:9443/signed-out"
    async with oauth.sessions.begin() as session:
        client = await session.get(NativeClientTable, oauth_support.CLIENT)
        assert client is not None
        client.post_logout_redirect_uris = [uri]
    service = production_oauth(oauth)
    secure_logout = NativeLogout(service.sessions, service.config, credentials=service.credentials, oauth=service)
    if entry == "confirm":
        previous = NativeLogout(oauth.sessions, oauth.config, credentials=oauth.credentials, oauth=oauth)
        challenge = await previous.prepare(client_id=oauth_support.CLIENT, post_logout_redirect_uri=uri)
    with pytest.raises(OAuthError) as failure:
        if entry == "prepare":
            await secure_logout.prepare(client_id=oauth_support.CLIENT, post_logout_redirect_uri=uri)
        else:
            await secure_logout.confirm(
                request_ref=challenge.request_ref,
                browser_secret=challenge.browser_secret,
                csrf_token=challenge.csrf_token,
                confirm=True,
            )
    assert failure.value.response is None
    if entry == "confirm":
        async with oauth.sessions() as session:
            saved = await session.get(NativeLogoutRequestTable, challenge.request_ref)
            assert saved is not None and saved.consumed_at is None
