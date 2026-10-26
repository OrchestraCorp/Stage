"""RP initiated logout requires browser possession and an explicit CSRF-bound confirmation."""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.identity import PrincipalTable
from app.models.identity_foundation import TrustStateTable
from app.models.native_auth import (
    NativeAccountTable,
    NativeAuthEventTable,
    NativeBrowserSessionTable,
    NativeClientTable,
    NativeLogoutRequestTable,
    NativeSessionTable,
)
from app.native_auth.config import NativeAuthConfig
from app.native_auth.credentials import NativeCredentials, commit_authentication
from app.native_auth.crypto import digest_secret, random_secret
from app.native_auth.keys import NativeKeyError
from app.native_auth.oauth import NativeOAuth
from app.native_auth.protocol import OAuthError
from app.native_auth.redirects import validate_issuer_redirect
from app.storage.coordination import LockMode, lock_organization


@dataclass(frozen=True)
class LogoutChallenge:
    request_ref: str
    browser_secret: str = field(repr=False)
    csrf_token: str = field(repr=False)
    client_name: str | None
    redirect_uri: str | None


@dataclass(frozen=True)
class LogoutResult:
    redirect_uri: str | None
    state: str | None
    logged_out: bool


def _text(value: Any, limit: int, *, empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and (0 if empty else 1) <= len(value) <= limit
        and all(ord(char) >= 32 and ord(char) != 127 and not 0xD800 <= ord(char) <= 0xDFFF for char in value)
    )


class NativeLogout:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        config: NativeAuthConfig,
        *,
        credentials: NativeCredentials,
        oauth: NativeOAuth,
    ) -> None:
        self.sessions = sessions
        self.config = config
        self.credentials = credentials
        self.oauth = oauth

    async def _client(
        self, session: AsyncSession, client_id: str | None, redirect_uri: str | None
    ) -> NativeClientTable | None:
        if client_id is None:
            if redirect_uri is not None:
                raise OAuthError("invalid_request")
            return None
        if redirect_uri is not None:
            try:
                validate_issuer_redirect(self.config.issuer, redirect_uri, self.config.environment)
            except ValueError:
                raise OAuthError("invalid_request") from None
        client = await session.get(NativeClientTable, client_id, populate_existing=True)
        if (
            client is None
            or not client.enabled
            or (redirect_uri is not None and redirect_uri not in client.post_logout_redirect_uris)
        ):
            raise OAuthError("invalid_request")
        return client

    async def prepare(
        self,
        *,
        id_token_hint: str | None = None,
        client_id: str | None = None,
        post_logout_redirect_uri: str | None = None,
        state: str | None = None,
        sso_cookie: str | None = None,
    ) -> LogoutChallenge:
        if any(
            value is not None and not _text(value, limit)
            for value, limit in (
                (client_id, 128),
                (post_logout_redirect_uri, 2048),
                (sso_cookie, 128),
            )
        ) or (state is not None and not _text(state, 1024, empty=True)):
            raise OAuthError("invalid_request")
        hint = None
        if id_token_hint is not None:
            try:
                hint = self.oauth.keyring.verify_id_token_hint(id_token_hint, self.config.issuer, client_id)
            except NativeKeyError:
                raise OAuthError("invalid_request") from None
            client_id = hint["aud"]
        async with self.sessions() as session:
            client = await self._client(session, client_id, post_logout_redirect_uri)
            browser = await session.get(NativeBrowserSessionTable, digest_secret(sso_cookie)) if sso_cookie else None
            if hint is not None:
                login = await session.get(NativeSessionTable, hint["sid"])
                if (
                    login is None
                    or login.client_id != client_id
                    or login.account_ref != hint["sub"]
                    or int(login.auth_time.timestamp()) != hint["auth_time"]
                    or (browser is not None and browser.account_ref != hint["sub"])
                ):
                    raise OAuthError("invalid_request")
            request_ref, browser_secret, csrf_token = random_secret(), random_secret(), random_secret()
            session.add(
                NativeLogoutRequestTable(
                    request_ref=request_ref,
                    browser_hash=digest_secret(browser_secret),
                    csrf_hash=digest_secret(csrf_token),
                    browser_session_hash=browser.session_hash if browser is not None else None,
                    client_id=client_id,
                    post_logout_redirect_uri=post_logout_redirect_uri,
                    state=state,
                    expires_at=datetime.now(UTC) + timedelta(seconds=self.config.login_request_seconds),
                )
            )
            await commit_authentication(session)
        return LogoutChallenge(
            request_ref,
            browser_secret,
            csrf_token,
            client.display_name if client is not None else None,
            post_logout_redirect_uri,
        )

    async def _lock_browser(self, session: AsyncSession, session_hash: str) -> NativeBrowserSessionTable:
        browser = await session.get(NativeBrowserSessionTable, session_hash)
        account = await session.get(NativeAccountTable, browser.account_ref) if browser is not None else None
        if browser is None or account is None:
            raise OAuthError("invalid_request")
        # Logout only removes authority. Even a suspended principal can close a
        # browser it still possesses, while retaining normal mutation lock order.
        trust = await session.scalar(
            select(TrustStateTable).where(TrustStateTable.trust_ref == self.config.trust_ref).with_for_update(read=True)
        )
        principal = await session.scalar(
            select(PrincipalTable).where(PrincipalTable.principal_ref == account.principal_ref).with_for_update()
        )
        if trust is None or trust.purpose != "authentication" or principal is None:
            raise OAuthError("invalid_request")
        await lock_organization(session, self.config.organization_ref, mode=LockMode.SHARED)
        browser = await session.scalar(
            select(NativeBrowserSessionTable)
            .where(NativeBrowserSessionTable.session_hash == session_hash)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if browser is None or browser.account_ref != account.account_ref:
            raise OAuthError("invalid_request")
        return browser

    @staticmethod
    def _proof(request: NativeLogoutRequestTable | None, browser_secret: str, csrf_token: str, now: datetime) -> None:
        if (
            request is None
            or request.consumed_at is not None
            or request.expires_at <= now
            or not hmac.compare_digest(request.browser_hash, digest_secret(browser_secret))
            or not hmac.compare_digest(request.csrf_hash, digest_secret(csrf_token))
        ):
            raise OAuthError("invalid_request")

    async def confirm(
        self, *, request_ref: str, browser_secret: str, csrf_token: str, sso_cookie: str | None = None, confirm: bool
    ) -> LogoutResult:
        if (
            any(not _text(value, 128) for value in (request_ref, browser_secret, csrf_token))
            or (sso_cookie is not None and not _text(sso_cookie, 128))
            or type(confirm) is not bool
        ):
            raise OAuthError("invalid_request")
        async with self.sessions() as session:
            if session.get_bind().dialect.name == "sqlite":
                # SQLite ignores FOR UPDATE; reserve this short mutation once.
                from sqlalchemy import text

                await session.execute(text("BEGIN IMMEDIATE"))
            hint = await session.get(NativeLogoutRequestTable, request_ref)
            self._proof(hint, browser_secret, csrf_token, datetime.now(UTC))
            assert hint is not None
            browser = None
            if hint.browser_session_hash is not None:
                if sso_cookie is None or not hmac.compare_digest(hint.browser_session_hash, digest_secret(sso_cookie)):
                    raise OAuthError("invalid_request")
                browser = await self._lock_browser(session, hint.browser_session_hash)
            else:
                # A user may have signed in after opening the confirmation page.
                # A challenge that never held that browser cannot clear its login.
                if sso_cookie and await session.get(NativeBrowserSessionTable, digest_secret(sso_cookie)) is not None:
                    raise OAuthError("invalid_request")
                trust = await session.scalar(
                    select(TrustStateTable)
                    .where(TrustStateTable.trust_ref == self.config.trust_ref)
                    .with_for_update(read=True)
                )
                if trust is None or trust.purpose != "authentication":
                    raise OAuthError("invalid_request")
            request = await session.scalar(
                select(NativeLogoutRequestTable)
                .where(NativeLogoutRequestTable.request_ref == request_ref)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            self._proof(request, browser_secret, csrf_token, datetime.now(UTC))
            assert request is not None
            if confirm:
                await self._client(session, request.client_id, request.post_logout_redirect_uri)
            now = datetime.now(UTC)
            self._proof(request, browser_secret, csrf_token, now)
            if confirm and browser is not None:
                browser.revoked_at = browser.revoked_at or now
                await session.execute(
                    update(NativeSessionTable)
                    .where(
                        NativeSessionTable.browser_session_hash == browser.session_hash,
                        NativeSessionTable.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
            request.consumed_at = now
            session.add(
                NativeAuthEventTable(
                    event="browser.logout_confirmed" if confirm else "browser.logout_declined",
                    account_ref=browser.account_ref if browser is not None else None,
                    client_id=request.client_id,
                    detail={
                        "browser_session_hash": browser.session_hash if browser is not None else None,
                        "sessions_revoked": bool(confirm and browser is not None),
                    },
                )
            )
            await session.flush()
            # DB waits can cross the challenge deadline; roll back all revocation
            # and consumption if it has expired before the commit attempt.
            if request.expires_at <= datetime.now(UTC):
                raise OAuthError("invalid_request")
            await commit_authentication(session)
            return LogoutResult(
                request.post_logout_redirect_uri if confirm else None,
                request.state if confirm and request.post_logout_redirect_uri is not None else None,
                bool(confirm and browser is not None),
            )
