"""SSO retains bounded original authentication and cannot revive revoked credentials."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.identity_foundation import OrganizationHumanAuthenticationPolicyTable
from app.models.native_auth import (
    NativeAccountTable,
    NativeBrowserSessionTable,
    NativeClientTable,
    NativeLoginRequestTable,
    NativeSessionTable,
)
from app.native_auth.crypto import digest_secret
from app.native_auth.errors import NativeCredentialError
from app.native_auth.oauth import NativeOAuth, OAuthError
from tests.support import native_oauth as oauth_support
from tests.support.native_browser import (
    established,
)

native_keyring = oauth_support.native_keyring
oauth = oauth_support.oauth


async def test_sso_cookie_is_hash_only_and_retains_original_authentication(oauth: NativeOAuth) -> None:
    browser, cookie = await established(oauth)
    async with oauth.sessions() as session:
        stored = await session.get(NativeBrowserSessionTable, digest_secret(cookie))
        assert stored and cookie != stored.session_hash
        auth_time, expiry = stored.auth_time, stored.expires_at
    request = await oauth.prepare_authorization(**oauth_support.authorize_values())
    assert await browser.resume(request.request_ref, request.browser_secret, request.csrf_token, cookie)
    outcome = await browser.advance(request.request_ref, request.browser_secret, request.csrf_token)
    assert outcome.kind == "complete" and outcome.cookie_secret is None
    response = await oauth.finish_authorization_response(
        request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
    )
    async with oauth.sessions() as session:
        issued = await session.get(NativeSessionTable, response.session_ref)
        assert issued and issued.auth_time == auth_time and issued.expires_at <= expiry


@pytest.mark.parametrize(
    "cause",
    [
        "expired",
        "revoked",
        "version",
        "disabled",
        "no_factor",
        "force_login",
        "max_age_zero",
        "wrong_hint",
        "wrong_cookie",
    ],
)
async def test_sso_cannot_bypass_current_state_or_explicit_reauthentication(oauth: NativeOAuth, cause: str) -> None:
    browser, cookie = await established(oauth)
    params = oauth_support.authorize_values()
    if cause == "force_login":
        params["prompt"] = "login"
    if cause == "max_age_zero":
        params["max_age"] = "0"
    if cause == "wrong_hint":
        params["login_hint"] = "different-person"
    async with oauth.sessions.begin() as session:
        account = await session.get(NativeAccountTable, "account:native")
        sso = await session.get(NativeBrowserSessionTable, digest_secret(cookie))
        assert account and sso
        if cause == "expired":
            sso.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        if cause == "revoked":
            sso.revoked_at = datetime.now(UTC)
        if cause == "version":
            account.credential_version += 1
        if cause == "disabled":
            account.status = "disabled"
        if cause == "no_factor":
            account.totp_secret = None
    request = await oauth.prepare_authorization(**params)
    assert not await browser.resume(
        request.request_ref,
        request.browser_secret,
        request.csrf_token,
        "wrong-cookie" if cause == "wrong_cookie" else cookie,
    )
    with pytest.raises(OAuthError):
        await oauth.finish_authorization_response(
            request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
        )


async def test_sso_pins_deployment_freshness_for_final_authorization(oauth: NativeOAuth) -> None:
    browser, cookie = await established(oauth)
    async with oauth.sessions.begin() as session:
        session.add(
            OrganizationHumanAuthenticationPolicyTable(
                organization_ref=oauth.config.organization_ref,
                version=1,
                mode="required",
                rules_json=[],
                max_auth_age_seconds=60,
            )
        )
    request = await oauth.prepare_authorization(**oauth_support.authorize_values())
    assert await browser.resume(request.request_ref, request.browser_secret, request.csrf_token, cookie)
    async with oauth.sessions() as session:
        row = await session.get(NativeLoginRequestTable, request.request_ref)
        assert row and row.max_age == 60 and row.authentication_reused


async def test_credential_reset_revokes_browser_cookie_and_password_cannot_reuse_it(oauth: NativeOAuth) -> None:
    browser, cookie = await established(oauth)
    result = await oauth.credentials.admin_reset("account:native", 1, reset_totp=True)
    assert result.enrollment_code
    request = await oauth.prepare_authorization(**oauth_support.authorize_values())
    assert not await browser.resume(request.request_ref, request.browser_secret, request.csrf_token, cookie)
    async with oauth.sessions() as session:
        sso = await session.get(NativeBrowserSessionTable, digest_secret(cookie))
        assert sso and sso.revoked_at


async def test_client_change_blocks_pending_password_and_sso_requests(oauth: NativeOAuth) -> None:
    browser, cookie = await established(oauth)
    request = await oauth.prepare_authorization(**oauth_support.authorize_values())
    async with oauth.sessions.begin() as session:
        client = await session.get(NativeClientTable, oauth_support.CLIENT)
        assert client
        client.version += 1
    with pytest.raises(NativeCredentialError):
        await browser.resume(request.request_ref, request.browser_secret, request.csrf_token, cookie)
    async with oauth.sessions() as session:
        assert (
            await session.scalars(
                select(NativeLoginRequestTable).where(NativeLoginRequestTable.request_ref == request.request_ref)
            )
        ).one().status == "password"
