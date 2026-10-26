"""Independent integration review: current policy and deadlines survive database waits."""

from datetime import datetime, timedelta
from typing import Any, Self

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.identity_foundation import OrganizationHumanAuthenticationPolicyTable
from app.models.native_auth import (
    NativeAuthorizationCodeTable,
    NativeBrowserSessionTable,
    NativeClientTable,
    NativeConsentTable,
    NativeLoginRequestTable,
    NativeRefreshTokenTable,
)
from app.native_auth.browser import BrowserAuthorization
from app.native_auth.crypto import digest_secret
from app.native_auth.oauth import AuthorizationRequest, NativeOAuth, OAuthError
from app.native_auth.sessions import AccountSessions
from tests.support import native_browser as browser_support
from tests.support import native_oauth as oauth_support

native_keyring = oauth_support.native_keyring
oauth = oauth_support.oauth


class WaitClock:
    def __init__(self, after_flush: int = 1) -> None:
        self.advanced = False
        self.flushes = 0
        owner = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> Self:
                instant = datetime.now(tz) + timedelta(seconds=10 if owner.advanced else 0)
                return cls.fromtimestamp(instant.timestamp(), tz=tz)

        class DelayedFlush(AsyncSession):
            async def flush(self, objects: Any = None) -> None:
                await super().flush(objects)
                owner.flushes += 1
                owner.advanced = owner.flushes >= after_flush

        self.clock = Clock
        self.session_class = DelayedFlush

    def sessions(self, source: async_sessionmaker[AsyncSession]) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(source.kw["bind"], class_=self.session_class, expire_on_commit=False)


async def prepare_reused(
    oauth: NativeOAuth, *, consent_required: bool = False
) -> tuple[BrowserAuthorization, AuthorizationRequest, str]:
    service, cookie = await browser_support.established(oauth)
    if consent_required:
        async with oauth.sessions.begin() as session:
            client = await session.get(NativeClientTable, oauth_support.CLIENT)
            assert client is not None
            client.require_consent = True
    values: dict[str, Any] = {**oauth_support.authorize_values(), "max_age": 5}
    request = await oauth.prepare_authorization(**values)
    return service, request, cookie


async def test_sso_freshness_expiring_during_resume_cannot_commit_reused_authentication(
    oauth: NativeOAuth,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.native_auth.browser as module

    _, request, cookie = await prepare_reused(oauth)
    clock = WaitClock()
    monkeypatch.setattr(module, "datetime", clock.clock)
    service = BrowserAuthorization(
        clock.sessions(oauth.sessions), oauth.config, credentials=oauth.credentials, oauth=oauth
    )
    with pytest.raises(OAuthError, match="login_required"):
        await service.resume(request.request_ref, request.browser_secret, request.csrf_token, cookie)
    async with oauth.sessions() as session:
        saved = await session.get(NativeLoginRequestTable, request.request_ref)
        assert saved is not None and saved.status == "password" and not saved.authentication_reused


async def test_sso_freshness_expiring_during_consent_cannot_persist_grant(
    oauth: NativeOAuth,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.native_auth.browser as module

    service, request, cookie = await prepare_reused(oauth, consent_required=True)
    assert await service.resume(request.request_ref, request.browser_secret, request.csrf_token, cookie)
    clock = WaitClock()
    monkeypatch.setattr(module, "datetime", clock.clock)
    delayed = BrowserAuthorization(
        clock.sessions(oauth.sessions), oauth.config, credentials=oauth.credentials, oauth=oauth
    )
    with pytest.raises(OAuthError, match="login_required"):
        await delayed.advance(request.request_ref, request.browser_secret, request.csrf_token, consent=True)
    async with oauth.sessions() as session:
        assert await session.get(NativeConsentTable, ("account:native", oauth_support.CLIENT)) is None
        saved = await session.get(NativeLoginRequestTable, request.request_ref)
        assert saved is not None and not saved.consent_granted


@pytest.mark.parametrize("after_flush", [1, 2])
async def test_sso_freshness_expiring_while_session_is_persisted_cannot_issue_authorization_code(
    oauth: NativeOAuth,
    monkeypatch: pytest.MonkeyPatch,
    after_flush: int,
) -> None:
    import app.native_auth.oauth as module

    service, request, cookie = await prepare_reused(oauth)
    assert await service.resume(request.request_ref, request.browser_secret, request.csrf_token, cookie)
    assert (await service.advance(request.request_ref, request.browser_secret, request.csrf_token)).kind == "complete"
    clock = WaitClock(after_flush)
    monkeypatch.setattr(module, "datetime", clock.clock)
    delayed = NativeOAuth(clock.sessions(oauth.sessions), oauth.config, oauth.keyring, credentials=oauth.credentials)
    with pytest.raises(OAuthError, match="login_required"):
        await delayed.finish_authorization_response(
            request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
        )
    async with oauth.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NativeAuthorizationCodeTable)) == 0
        saved = await session.get(NativeLoginRequestTable, request.request_ref)
        assert saved is not None and saved.status == "authenticated"


@pytest.mark.parametrize("entry", ["consent", "authorization"])
async def test_stricter_deployment_mfa_policy_after_resume_invalidates_pending_sso(
    oauth: NativeOAuth,
    entry: str,
) -> None:
    service, cookie = await browser_support.established(oauth)
    async with oauth.sessions.begin() as session:
        browser = await session.get(NativeBrowserSessionTable, digest_secret(cookie))
        assert browser is not None
        browser.auth_time -= timedelta(minutes=5)
        session.add(
            OrganizationHumanAuthenticationPolicyTable(
                organization_ref=oauth.config.organization_ref,
                version=1,
                mode="required",
                rules_json=[],
                max_auth_age_seconds=1200,
            )
        )
    request = await oauth.prepare_authorization(**oauth_support.authorize_values())
    assert await service.resume(request.request_ref, request.browser_secret, request.csrf_token, cookie)
    async with oauth.sessions.begin() as session:
        policy = await session.get(OrganizationHumanAuthenticationPolicyTable, oauth.config.organization_ref)
        assert policy is not None
        policy.version += 1
        policy.max_auth_age_seconds = 60
    with pytest.raises(OAuthError, match="login_required"):
        if entry == "consent":
            await service.advance(request.request_ref, request.browser_secret, request.csrf_token, consent=True)
        else:
            await oauth.finish_authorization_response(
                request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
            )


async def test_userinfo_only_application_cannot_access_account_security_or_session_management(
    oauth: NativeOAuth,
) -> None:
    async with oauth.sessions.begin() as session:
        client = await session.get(NativeClientTable, oauth_support.CLIENT)
        assert client is not None
        client.resource_audience = "stage-userinfo"
    issued = await oauth_support.tokens(oauth)
    assert await oauth.userinfo(issued["access_token"]) == {"sub": "account:native"}
    operations = AccountSessions(oauth.sessions, oauth.config, credentials=oauth.credentials, oauth=oauth)
    for operation in (
        oauth.authenticate_account,
        operations.list_sessions,
        operations.revoke_all,
        operations.list_consents,
    ):
        with pytest.raises(OAuthError, match="invalid_token"):
            await operation(issued["access_token"])


async def test_max_age_zero_accepts_fresh_factors_without_creating_an_immediate_expiry(oauth: NativeOAuth) -> None:
    request = await oauth_support.authenticated_request(oauth)
    async with oauth.sessions.begin() as session:
        saved = await session.get(NativeLoginRequestTable, request.request_ref)
        assert saved is not None
        saved.max_age = 0
        saved.prompt = "login"
    service = browser_support.browser_service(oauth)
    assert (await service.advance(request.request_ref, request.browser_secret, request.csrf_token)).kind == "complete"
    result = await oauth.finish_authorization_response(
        request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
    )
    assert "code" in result.parameters


@pytest.mark.parametrize("grant", ["code", "refresh"])
async def test_token_grant_expiring_during_write_wait_rolls_back_consumption(
    oauth: NativeOAuth,
    monkeypatch: pytest.MonkeyPatch,
    grant: str,
) -> None:
    import app.native_auth.oauth as module

    table: type[NativeAuthorizationCodeTable] | type[NativeRefreshTokenTable]
    if grant == "code":
        secret = await oauth_support.authorization_code(oauth)
        table = NativeAuthorizationCodeTable
        initial_refresh_count = 0
    else:
        issued = await oauth_support.tokens(oauth)
        secret = issued["refresh_token"]
        table = NativeRefreshTokenTable
        initial_refresh_count = 1
    async with oauth.sessions.begin() as session:
        row = await session.get(table, digest_secret(secret))
        assert isinstance(row, (NativeAuthorizationCodeTable, NativeRefreshTokenTable))
        row.expires_at = datetime.now(row.expires_at.tzinfo) + timedelta(seconds=5)
    clock = WaitClock()
    monkeypatch.setattr(module, "datetime", clock.clock)
    engine = oauth.sessions.kw["bind"]

    def during_write(*args: Any) -> None:
        if "INSERT INTO native_refresh_token" in args[2]:
            clock.advanced = True

    event.listen(engine.sync_engine, "before_cursor_execute", during_write)
    try:
        with pytest.raises(OAuthError, match="invalid_grant"):
            if grant == "code":
                await oauth.exchange_code(
                    client_id=oauth_support.CLIENT,
                    redirect_uri=oauth_support.REDIRECT,
                    code=secret,
                    code_verifier=oauth_support.VERIFIER,
                )
            else:
                await oauth.exchange_refresh(client_id=oauth_support.CLIENT, refresh_token=secret)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", during_write)
    async with oauth.sessions() as session:
        row = await session.get(table, digest_secret(secret))
        assert isinstance(row, (NativeAuthorizationCodeTable, NativeRefreshTokenTable))
        assert row.consumed_at is None
        assert await session.scalar(select(func.count()).select_from(NativeRefreshTokenTable)) == initial_refresh_count
