"""Browser SSO and consent, bound to the same account and transaction authority as tokens."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.native_auth import (
    NativeBrowserSessionTable,
    NativeClientTable,
    NativeConsentTable,
    NativeLoginRequestTable,
)
from app.native_auth.config import NativeAuthConfig
from app.native_auth.credentials import NativeCredentials, authentication_event, commit_authentication
from app.native_auth.crypto import digest_secret, random_secret
from app.native_auth.current_session import validate_browser_session
from app.native_auth.errors import NativeCredentialError
from app.native_auth.freshness import current_authentication_deadline
from app.native_auth.oauth import NativeOAuth, consent_satisfied
from app.native_auth.protocol import AuthorizationResponse, OAuthError
from app.native_auth.redirects import validate_issuer_redirect


@dataclass(frozen=True)
class BrowserOutcome:
    kind: str
    client_name: str
    scopes: tuple[str, ...]
    redirect_uri: str
    cookie_secret: str | None = field(default=None, repr=False)
    cookie_max_age: int = 0


class BrowserAuthorization:
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

    async def _client(self, session: AsyncSession, request: NativeLoginRequestTable) -> NativeClientTable:
        try:
            validate_issuer_redirect(self.config.issuer, request.redirect_uri, self.config.environment)
        except ValueError:
            raise OAuthError("invalid_request") from None
        client = await session.get(NativeClientTable, request.client_id, populate_existing=True)
        if (
            client is None
            or not client.enabled
            or client.version != request.client_version
            or request.redirect_uri not in client.redirect_uris
            or not set(request.scope.split()) <= set(client.allowed_scopes)
        ):
            raise OAuthError("invalid_request")
        return client

    def _error(self, request: NativeLoginRequestTable, error: str) -> OAuthError:
        parameters = {"error": error, "iss": self.config.issuer}
        if request.state:
            parameters["state"] = request.state
        return OAuthError(
            error, response=AuthorizationResponse(request.redirect_uri, parameters, request.response_mode)
        )

    async def resume(
        self, request_ref: str, browser_secret: str, csrf_token: str, sso_cookie: str | None = None
    ) -> bool:
        """Only a live opaque browser cookie can reuse the original MFA time."""
        if not sso_cookie or not 20 <= len(sso_cookie) <= 256:
            return False
        async with self.sessions() as session:
            hint = await session.get(NativeBrowserSessionTable, digest_secret(sso_cookie))
            if hint is None:
                return False
            try:
                account = await self.credentials.lock_account(session, hint.account_ref)
            except NativeCredentialError:
                return False
            request = await self.credentials.validate_browser_request(
                session, request_ref, browser_secret, csrf_token, lock=True
            )
            await self._client(session, request)
            if request.status != "password" or {"login", "select_account"} & set(request.prompt.split()):
                return False
            now = datetime.now(UTC)
            if (
                account.status != "active"
                or account.totp_secret is None
                or hint.credential_version != account.credential_version
            ):
                return False
            try:
                browser = await validate_browser_session(session, hint.session_hash, account, hint.auth_time, now=now)
            except OAuthError:
                return False
            # Freeze the current policy deadline under the organization guard,
            # then check it again after consent reads and the final write wait.
            authentication_deadline = await current_authentication_deadline(
                session,
                self.config.organization_ref,
                browser.auth_time,
                default_seconds=self.config.session_seconds,
                max_age_seconds=request.max_age,
            )
            if authentication_deadline <= datetime.now(UTC):
                return False
            if request.login_hint is not None and request.login_hint != account.username:
                return False
            request.account_ref = account.account_ref
            request.credential_version = account.credential_version
            request.password_verified_at = browser.auth_time
            request.authentication_reused = True
            request.max_age = int((authentication_deadline - browser.auth_time).total_seconds())
            request.browser_session_hash = browser.session_hash
            request.status = "authenticated"
            consent = await session.get(
                NativeConsentTable, (account.account_ref, request.client_id), populate_existing=True
            )
            request.consent_granted = bool(
                "consent" not in request.prompt.split()
                and consent is not None
                and consent.client_version == request.client_version
                and set(request.scope.split()) <= set(consent.scopes)
            )
            await session.flush()
            deadline = datetime.now(UTC)
            if request.expires_at <= deadline or browser.expires_at <= deadline or authentication_deadline <= deadline:
                raise OAuthError("login_required")
            await commit_authentication(session)
            return True

    async def advance(
        self, request_ref: str, browser_secret: str, csrf_token: str, consent: bool | None = None
    ) -> BrowserOutcome:
        async with self.sessions() as session:
            hint = await session.get(NativeLoginRequestTable, request_ref)
            if hint is None or hint.account_ref is None:
                raise OAuthError("invalid_request")
            account = await self.credentials.lock_account(session, hint.account_ref)
            request = await self.credentials.validate_browser_request(
                session, request_ref, browser_secret, csrf_token, lock=True
            )
            client = await self._client(session, request)
            now = datetime.now(UTC)
            if (
                request.status != "authenticated"
                or request.account_ref != account.account_ref
                or request.credential_version != account.credential_version
                or account.status != "active"
                or account.totp_secret is None
                or request.password_verified_at is None
                or request.password_verified_at > now
            ):
                raise OAuthError("login_required")
            if not request.authentication_reused:
                self.credentials._factor_deadline(request)
            authentication_deadline = await current_authentication_deadline(
                session,
                self.config.organization_ref,
                request.password_verified_at,
                default_seconds=self.config.session_seconds,
                max_age_seconds=(
                    None if not request.authentication_reused and request.max_age == 0 else request.max_age
                ),
            )
            if not request.authentication_reused:
                authentication_deadline = min(
                    authentication_deadline, request.password_verified_at + timedelta(minutes=5)
                )
            if authentication_deadline <= datetime.now(UTC):
                raise self._error(request, "login_required")
            cookie_secret = None
            if request.browser_session_hash:
                browser = await validate_browser_session(
                    session, request.browser_session_hash, account, request.password_verified_at, now=now
                )
            else:
                cookie_secret = random_secret()
                browser = NativeBrowserSessionTable(
                    session_hash=digest_secret(cookie_secret),
                    account_ref=account.account_ref,
                    credential_version=account.credential_version,
                    auth_time=request.password_verified_at,
                    expires_at=request.password_verified_at + timedelta(seconds=self.config.session_seconds),
                )
                session.add(browser)
                await session.flush()
                request.browser_session_hash = browser.session_hash
                authentication_event(
                    session, "browser_session.created", account_ref=account.account_ref, client_id=client.client_id
                )
            if consent is False:
                request.status = "done"
                authentication_event(
                    session, "consent.denied", account_ref=account.account_ref, client_id=client.client_id
                )
                await session.flush()
                denied_at = datetime.now(UTC)
                if (
                    request.expires_at <= denied_at
                    or browser.expires_at <= denied_at
                    or authentication_deadline <= denied_at
                ):
                    raise self._error(request, "login_required")
                await commit_authentication(session)
                raise self._error(request, "access_denied")
            if consent is True:
                existing = await session.get(
                    NativeConsentTable, (account.account_ref, client.client_id), populate_existing=True
                )
                scopes = set(request.scope.split())
                if existing is not None and existing.client_version == client.version:
                    scopes.update(existing.scopes)
                if existing is None:
                    existing = NativeConsentTable(
                        account_ref=account.account_ref,
                        client_id=client.client_id,
                        client_version=client.version,
                        scopes=sorted(scopes),
                        granted_at=now,
                    )
                    session.add(existing)
                else:
                    existing.client_version, existing.scopes, existing.granted_at = client.version, sorted(scopes), now
                request.consent_granted = True
                authentication_event(
                    session, "consent.granted", account_ref=account.account_ref, client_id=client.client_id
                )
            satisfied = await consent_satisfied(session, request, client)
            if not satisfied and "none" in request.prompt.split():
                raise self._error(request, "consent_required")
            await session.flush()
            deadline = datetime.now(UTC)
            if request.expires_at <= deadline or browser.expires_at <= deadline or authentication_deadline <= deadline:
                raise self._error(request, "login_required")
            await commit_authentication(session)
            return BrowserOutcome(
                "complete" if satisfied else "consent",
                client.display_name or client.client_id,
                tuple(request.scope.split()),
                request.redirect_uri,
                cookie_secret,
                max(1, int((browser.expires_at - deadline).total_seconds())),
            )
