"""Narrow first-party OIDC issuer with persistent, single-use credentials.

Protocol helpers come from Authlib; PostgreSQL owns code consumption and refresh
families. This is intentionally not a general authorization-server framework.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NoReturn

from authlib.oauth2.rfc7636.challenge import create_s256_code_challenge
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.native_auth import (
    NativeAccountTable,
    NativeAuthEventTable,
    NativeAuthorizationCodeTable,
    NativeClientTable,
    NativeConsentTable,
    NativeLoginRequestTable,
    NativeRefreshTokenTable,
    NativeSessionTable,
)
from app.native_auth.config import NativeAuthConfig
from app.native_auth.crypto import digest_secret, random_secret
from app.native_auth.current_session import validate_browser_session, validate_native_session
from app.native_auth.errors import NativeCommitUnknown, NativeCredentialError
from app.native_auth.freshness import current_authentication_deadline
from app.native_auth.keys import KeyRing, NativeKeyError
from app.native_auth.protocol import (
    MFA_ACR,
    MFA_AMR,
    AuthorizationRequest,
    AuthorizationResponse,
    OAuthError,
    normalize_scope,
)
from app.native_auth.protocol import SUPPORTED_SCOPES as SUPPORTED_SCOPES
from app.native_auth.protocol import (
    valid_text as _text,
)
from app.native_auth.redirects import validate_issuer_redirect

if TYPE_CHECKING:
    from app.native_auth.credentials import NativeCredentials

_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}\Z", re.ASCII)


async def consent_satisfied(session: AsyncSession, request: NativeLoginRequestTable, client: NativeClientTable) -> bool:
    if request.consent_granted:
        return True
    if "consent" in request.prompt.split() or "offline_access" in request.scope.split():
        return False
    if not client.require_consent:
        return True
    consent = await session.get(NativeConsentTable, (request.account_ref, request.client_id), populate_existing=True)
    return bool(
        consent is not None
        and consent.client_version == client.version
        and set(request.scope.split()) <= set(consent.scopes)
    )


class NativeOAuth:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        config: NativeAuthConfig,
        keyring: KeyRing,
        *,
        credentials: NativeCredentials,
    ) -> None:
        self.sessions = sessions
        self.config = config
        self.keyring = keyring
        self.credentials = credentials

    async def prepare_authorization(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        response_type: str,
        scope: str,
        code_challenge: str,
        code_challenge_method: str,
        state: str | None = None,
        nonce: str | None = None,
        response_mode: str = "query",
        prompt: str = "",
        max_age: int | str | None = None,
        acr_values: str | None = None,
        login_hint: str | None = None,
    ) -> AuthorizationRequest:
        # Validate the registered recipient before producing any redirectable error.
        if not _text(client_id, 128) or not _text(redirect_uri, 2048):
            raise OAuthError("invalid_request")
        async with asyncio.timeout(self.config.request_timeout_seconds), self.sessions() as session:
            async with session.begin():
                client = await session.scalar(
                    select(NativeClientTable).where(NativeClientTable.client_id == client_id).with_for_update(read=True)
                )
                if client is None or not client.enabled:
                    raise OAuthError("invalid_client")
                if redirect_uri not in client.redirect_uris:
                    raise OAuthError("invalid_request")
                try:
                    validate_issuer_redirect(self.config.issuer, redirect_uri, self.config.environment)
                except ValueError:
                    raise OAuthError("invalid_request") from None
                # An untrusted state or unsupported response mode cannot safely be echoed.
                if state is not None and not _text(state, 1024):
                    raise OAuthError("invalid_request")
                safe_mode = response_mode if response_mode in {"query", "form_post"} else "query"

                def reject(error: str) -> NoReturn:
                    parameters = {"error": error, "iss": self.config.issuer}
                    if state:
                        parameters["state"] = state
                    raise OAuthError(error, response=AuthorizationResponse(redirect_uri, parameters, safe_mode))

                if response_type != "code":
                    reject("unsupported_response_type")
                if response_mode not in {"query", "form_post"}:
                    reject("invalid_request")
                try:
                    normalized_scope = normalize_scope(scope)
                except OAuthError:
                    reject("invalid_scope")
                if not set(normalized_scope.split()) <= set(client.allowed_scopes):
                    reject("invalid_scope")
                if (
                    (nonce is not None and not _text(nonce, 1024))
                    or not isinstance(code_challenge, str)
                    or code_challenge_method != "S256"
                    or not _CHALLENGE.fullmatch(code_challenge)
                    or not isinstance(prompt, str)
                    or (prompt and not _text(prompt, 128))
                    or len(prompt.split(" ")) != len(set(prompt.split(" ")))
                    or not set(prompt.split()) <= {"none", "login", "consent", "select_account"}
                    or ("none" in prompt.split() and prompt != "none")
                    or (login_hint is not None and not _text(login_hint, 128))
                ):
                    reject("invalid_request")
                if acr_values is not None and (not _text(acr_values, 512) or MFA_ACR not in acr_values.split()):
                    reject("unmet_authentication_requirements")
                age: int | None = None
                if max_age is not None:
                    if isinstance(max_age, str) and re.fullmatch(r"[0-9]{1,10}", max_age):
                        age = int(max_age)
                    elif isinstance(max_age, int) and not isinstance(max_age, bool):
                        age = max_age
                    if age is None or not 0 <= age <= 2147483647:
                        reject("invalid_request")
                now = datetime.now(UTC)
                request = AuthorizationRequest(
                    random_secret(),
                    random_secret(),
                    random_secret(),
                    now + timedelta(seconds=self.config.login_request_seconds),
                )
                session.add(
                    NativeLoginRequestTable(
                        request_ref=request.request_ref,
                        client_id=client_id,
                        client_version=client.version,
                        redirect_uri=redirect_uri,
                        state=state or "",
                        nonce=nonce,
                        code_challenge=code_challenge,
                        scope=normalized_scope,
                        response_mode=response_mode,
                        prompt=prompt,
                        max_age=age,
                        login_hint=login_hint,
                        browser_hash=digest_secret(request.browser_secret),
                        csrf_hash=digest_secret(request.csrf_token),
                        expires_at=request.expires_at,
                        status="password",
                    )
                )
            return request

    async def finish_authorization_response(
        self, *, request_ref: str, browser_secret: str, csrf_token: str
    ) -> AuthorizationResponse:
        if not all(_text(value, 128) for value in (request_ref, browser_secret, csrf_token)):
            raise OAuthError("invalid_request")
        async with asyncio.timeout(self.config.request_timeout_seconds), self.sessions() as session:
            # The first read only discovers the immutable account for lock order.
            hint = await session.get(NativeLoginRequestTable, request_ref)
            if hint is None or hint.account_ref is None:
                raise OAuthError("invalid_request")
            account = await self._lock_account(session, hint.account_ref, "invalid_request")
            request = await session.scalar(
                select(NativeLoginRequestTable)
                .where(NativeLoginRequestTable.request_ref == request_ref)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            now = datetime.now(UTC)
            if (
                request is None
                or request.status != "authenticated"
                or request.expires_at <= now
                or request.account_ref != account.account_ref
                or request.credential_version != account.credential_version
                or request.password_verified_at is None
                or request.password_verified_at > now
                or (
                    not request.authentication_reused
                    and request.password_verified_at + timedelta(seconds=self.config.login_request_seconds) <= now
                )
                or not hmac.compare_digest(request.browser_hash, digest_secret(browser_secret))
                or not hmac.compare_digest(request.csrf_hash, digest_secret(csrf_token))
                or account.status != "active"
                or account.totp_secret is None
            ):
                raise OAuthError("invalid_request")
            client = await self._client(session, request.client_id, request.redirect_uri)
            if request.client_version != client.version:
                raise OAuthError("invalid_request")
            if not await consent_satisfied(session, request, client):
                raise OAuthError("consent_required")
            if request.authentication_reused and (
                request.browser_session_hash is None or {"login", "select_account"} & set(request.prompt.split())
            ):
                raise OAuthError("login_required")
            expires_at = request.password_verified_at + timedelta(seconds=self.config.session_seconds)
            if request.browser_session_hash is not None:
                browser = await validate_browser_session(
                    session, request.browser_session_hash, account, request.password_verified_at, now=now
                )
                expires_at = min(expires_at, browser.expires_at)
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
                    authentication_deadline,
                    request.password_verified_at + timedelta(seconds=min(300, self.config.login_request_seconds)),
                )
            now = datetime.now(UTC)
            if request.expires_at <= now or expires_at <= now:
                raise OAuthError("invalid_request")
            if authentication_deadline <= now:
                raise OAuthError("login_required")
            login = NativeSessionTable(
                session_ref=random_secret(),
                account_ref=account.account_ref,
                client_id=request.client_id,
                client_version=client.version,
                scope=request.scope,
                browser_session_hash=request.browser_session_hash,
                credential_version=account.credential_version,
                auth_time=request.password_verified_at,
                expires_at=expires_at,
                acr=MFA_ACR,
                amr=list(MFA_AMR),
                nonce=request.nonce,
            )
            session.add(login)
            await session.flush()
            now = datetime.now(UTC)
            if request.expires_at <= now or expires_at <= now:
                raise OAuthError("invalid_request")
            if authentication_deadline <= now:
                raise OAuthError("login_required")
            code = random_secret()
            session.add(
                NativeAuthorizationCodeTable(
                    code_hash=digest_secret(code),
                    session_ref=login.session_ref,
                    redirect_uri=request.redirect_uri,
                    code_challenge=request.code_challenge,
                    expires_at=min(now + timedelta(seconds=self.config.authorization_code_seconds), login.expires_at),
                )
            )
            request.status = "done"
            request.pending_totp = None
            self._event(session, "authorization_completed", login)
            parameters = {"code": code, "iss": self.config.issuer}
            if request.state:
                parameters["state"] = request.state
            response = AuthorizationResponse(request.redirect_uri, parameters, request.response_mode, login.session_ref)
            await session.flush()
            checked_at = datetime.now(UTC)
            if request.expires_at <= checked_at or expires_at <= checked_at:
                raise OAuthError("invalid_request")
            if authentication_deadline <= checked_at:
                raise OAuthError("login_required")
            await self._commit(session)
            return response

    async def finish_authorization(self, *, request_ref: str, browser_secret: str, csrf_token: str) -> str:
        response = await self.finish_authorization_response(
            request_ref=request_ref, browser_secret=browser_secret, csrf_token=csrf_token
        )
        return response.location

    async def complete_authorization(self, *, request_ref: str, browser_secret: str, csrf_token: str) -> str:
        return await self.finish_authorization(
            request_ref=request_ref, browser_secret=browser_secret, csrf_token=csrf_token
        )

    async def exchange_code(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        code: str,
        code_verifier: str,
        client_secret: str | None = None,
        client_auth_method: str = "none",
    ) -> dict[str, Any]:
        if (
            not _text(client_id, 128)
            or not _text(redirect_uri, 2048)
            or not _text(code, 128)
            or not _VERIFIER.fullmatch(code_verifier)
        ):
            raise OAuthError("invalid_grant")
        async with asyncio.timeout(self.config.request_timeout_seconds), self.sessions() as session:
            await self.authenticate_client(session, client_id, client_secret, client_auth_method)
            row = await session.get(NativeAuthorizationCodeTable, digest_secret(code))
            if row is None:
                raise OAuthError("invalid_grant")
            login = await self._lock_session(session, row.session_ref)
            row = await session.get(NativeAuthorizationCodeTable, digest_secret(code), populate_existing=True)
            assert row is not None
            now = datetime.now(UTC)
            if (
                login.client_id != client_id
                or row.redirect_uri != redirect_uri
                or not hmac.compare_digest(row.code_challenge, create_s256_code_challenge(code_verifier))
            ):
                raise OAuthError("invalid_grant")
            client = await self.authenticate_client(session, client_id, client_secret, client_auth_method)
            if redirect_uri not in client.redirect_uris:
                raise OAuthError("invalid_grant")
            try:
                validate_issuer_redirect(self.config.issuer, redirect_uri, self.config.environment)
            except ValueError:
                raise OAuthError("invalid_grant") from None
            now = datetime.now(UTC)
            if row.consumed_at is not None:
                login.revoked_at = login.revoked_at or now
                self._event(session, "authorization_code_reused", login)
                await self._commit(session)
                raise OAuthError("invalid_grant")
            if row.expires_at <= now:
                raise OAuthError("invalid_grant")
            self._live_session(login, now)
            row.consumed_at = now
            tokens = self._issue_tokens(session, login, now, client)
            self._event(session, "tokens_issued", login)
            token_expires_at = int(now.timestamp()) + tokens["expires_in"]
            await session.flush()
            checked_at = datetime.now(UTC)
            if row.expires_at <= checked_at or token_expires_at <= checked_at.timestamp():
                raise OAuthError("invalid_grant")
            self._live_session(login, checked_at)
            tokens["expires_in"] = max(0, int(token_expires_at - checked_at.timestamp()))
            if tokens["expires_in"] <= 0:
                raise OAuthError("invalid_grant")
            await self._commit(session)
            return tokens

    async def exchange_refresh(
        self,
        *,
        client_id: str,
        refresh_token: str,
        client_secret: str | None = None,
        client_auth_method: str = "none",
        scope: str | None = None,
    ) -> dict[str, Any]:
        if not _text(client_id, 128) or not _text(refresh_token, 128):
            raise OAuthError("invalid_grant")
        async with asyncio.timeout(self.config.request_timeout_seconds), self.sessions() as session:
            await self.authenticate_client(session, client_id, client_secret, client_auth_method)
            row = await session.get(NativeRefreshTokenTable, digest_secret(refresh_token))
            if row is None:
                raise OAuthError("invalid_grant")
            login = await self._lock_session(session, row.session_ref)
            row = await session.get(NativeRefreshTokenTable, digest_secret(refresh_token), populate_existing=True)
            assert row is not None
            now = datetime.now(UTC)
            if login.client_id != client_id:
                raise OAuthError("invalid_grant")
            client = await self.authenticate_client(session, client_id, client_secret, client_auth_method)
            try:
                current_scope = normalize_scope(row.scope or login.scope)
            except OAuthError:
                raise OAuthError("invalid_grant") from None
            if not set(current_scope.split()) <= set(login.scope.split()):
                raise OAuthError("invalid_grant")
            requested_scope = normalize_scope(scope) if scope is not None else current_scope
            if not set(requested_scope.split()) <= set(current_scope.split()):
                raise OAuthError("invalid_scope")
            now = datetime.now(UTC)
            if row.consumed_at is not None:
                # Commit the family revocation before returning the protocol error.
                login.revoked_at = login.revoked_at or now
                self._event(session, "refresh_reused", login)
                await self._commit(session)
                raise OAuthError("invalid_grant")
            if row.expires_at <= now:
                raise OAuthError("invalid_grant")
            self._live_session(login, now)
            row.consumed_at = now
            tokens = self._issue_tokens(session, login, now, client, include_nonce=False, scope=requested_scope)
            self._event(session, "tokens_refreshed", login)
            token_expires_at = int(now.timestamp()) + tokens["expires_in"]
            await session.flush()
            checked_at = datetime.now(UTC)
            if row.expires_at <= checked_at or token_expires_at <= checked_at.timestamp():
                raise OAuthError("invalid_grant")
            self._live_session(login, checked_at)
            tokens["expires_in"] = max(0, int(token_expires_at - checked_at.timestamp()))
            if tokens["expires_in"] <= 0:
                raise OAuthError("invalid_grant")
            await self._commit(session)
            return tokens

    async def revoke(
        self,
        *,
        client_id: str,
        token: str,
        token_type_hint: str | None = None,
        client_secret: str | None = None,
        client_auth_method: str = "none",
    ) -> None:
        # RFC 7009 permits ignoring unknown hints. Token knowledge is the public
        # client's proof; client identity must still match the issued credential.
        if not _text(client_id, 128) or not _text(token, 16384):
            raise OAuthError("invalid_request")
        async with asyncio.timeout(self.config.request_timeout_seconds), self.sessions() as session:
            await self.authenticate_client(session, client_id, client_secret, client_auth_method)
            session_ref: str | None = None
            if len(token) <= 128:
                refresh = await session.get(NativeRefreshTokenTable, digest_secret(token))
                if refresh is not None:
                    session_ref = refresh.session_ref
            if session_ref is None:
                try:
                    claims = self.keyring.verify_access(token, self.config.issuer, ["stage-api", "stage-userinfo"])
                except NativeKeyError:
                    return
                if claims.get("client_id") != client_id or not _text(claims.get("sid"), 128):
                    return
                session_ref = claims["sid"]
            try:
                login = await self._lock_session(session, session_ref, allow_inactive=True)
            except OAuthError:
                return
            await self.authenticate_client(session, client_id, client_secret, client_auth_method)
            if login.client_id != client_id:
                return
            if login.revoked_at is None:
                login.revoked_at = datetime.now(UTC)
                self._event(session, "session_revoked", login)
                await self._commit(session)

    async def authenticate_account(
        self, token: str, *, max_age_seconds: int | None = 300, allow_userinfo_audience: bool = False
    ) -> dict[str, Any]:
        async with self._authenticated_account(
            token, max_age_seconds=max_age_seconds, allow_userinfo_audience=allow_userinfo_audience
        ) as (_account, claims):
            return claims

    @asynccontextmanager
    async def _authenticated_account(
        self, token: str, *, max_age_seconds: int | None, allow_userinfo_audience: bool
    ) -> AsyncIterator[tuple[NativeAccountTable, dict[str, Any]]]:
        """Keep current account reads in the same authority transaction as verification."""
        try:
            audience = ["stage-api", "stage-userinfo"] if allow_userinfo_audience else self.config.audience
            claims = self.keyring.verify_access(token, self.config.issuer, audience)
        except NativeKeyError:
            raise OAuthError("invalid_token", 401) from None
        if not _text(claims.get("sub"), 128):
            raise OAuthError("invalid_token", 401)
        async with asyncio.timeout(self.config.request_timeout_seconds), self.sessions() as session:
            await self._lock_account(session, claims["sub"], "invalid_token", status_code=401)
            now = datetime.now(UTC)
            account, login = await validate_native_session(session, claims, now=now)
            if max_age_seconds is not None:
                if max_age_seconds <= 0:
                    raise OAuthError("login_required", 401)
                deadline = await current_authentication_deadline(
                    session, self.config.organization_ref, login.auth_time, default_seconds=max_age_seconds
                )
                now = datetime.now(UTC)
                if claims["exp"] <= now.timestamp() or login.expires_at <= now:
                    raise OAuthError("invalid_token", 401)
                if deadline <= now:
                    raise OAuthError("login_required", 401)
            yield account, claims

    async def userinfo(self, token: str) -> dict[str, str]:
        async with self._authenticated_account(token, max_age_seconds=None, allow_userinfo_audience=True) as (
            account,
            claims,
        ):
            result = {"sub": claims["sub"]}
            if "profile" in claims["scope"].split():
                result["preferred_username"] = account.username
            return result

    async def authenticate_client(
        self, session: AsyncSession, client_id: str, client_secret: str | None = None, client_auth_method: str = "none"
    ) -> NativeClientTable:
        client = await session.get(NativeClientTable, client_id, populate_existing=True)
        if client is None or not client.enabled or client.token_endpoint_auth_method != client_auth_method:
            raise OAuthError("invalid_client", 401)
        if client_auth_method == "none":
            if client_secret is not None:
                raise OAuthError("invalid_client", 401)
        elif client_auth_method == "client_secret_basic":
            if (
                not isinstance(client_secret, str)
                or not _text(client_secret, 256)
                or not client.client_secret_hash
                or not hmac.compare_digest(client.client_secret_hash, digest_secret(client_secret))
            ):
                raise OAuthError("invalid_client", 401)
        else:
            raise OAuthError("invalid_client", 401)
        return client

    async def _lock_account(
        self, session: AsyncSession, account_ref: str, error: str = "invalid_grant", *, status_code: int = 400
    ) -> NativeAccountTable:
        try:
            return await self.credentials.lock_account(session, account_ref)
        except NativeCredentialError:
            raise OAuthError(error, status_code) from None

    async def _lock_session(
        self, session: AsyncSession, session_ref: str, *, allow_inactive: bool = False
    ) -> NativeSessionTable:
        hint = await session.get(NativeSessionTable, session_ref)
        if hint is None:
            raise OAuthError("invalid_grant")
        account = await self._lock_account(session, hint.account_ref)
        login = await session.scalar(
            select(NativeSessionTable)
            .where(NativeSessionTable.session_ref == session_ref)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if login is None or login.account_ref != account.account_ref:
            raise OAuthError("invalid_grant")
        if not allow_inactive and (
            account.status != "active"
            or account.totp_secret is None
            or account.credential_version != login.credential_version
        ):
            raise OAuthError("invalid_grant")
        client = await self._client(session, login.client_id)
        if login.client_version != client.version or not set(login.scope.split()) <= set(client.allowed_scopes):
            raise OAuthError("invalid_grant")
        if not allow_inactive and login.browser_session_hash is not None:
            try:
                browser = await validate_browser_session(
                    session, login.browser_session_hash, account, login.auth_time, now=datetime.now(UTC)
                )
            except OAuthError:
                raise OAuthError("invalid_grant") from None
            if login.expires_at > browser.expires_at:
                raise OAuthError("invalid_grant")
        return login

    async def _client(
        self, session: AsyncSession, client_id: str, redirect_uri: str | None = None
    ) -> NativeClientTable:
        client = await session.get(NativeClientTable, client_id, populate_existing=True)
        if client is None or not client.enabled:
            raise OAuthError("invalid_client")
        if redirect_uri is not None:
            if redirect_uri not in client.redirect_uris:
                raise OAuthError("invalid_grant")
            try:
                validate_issuer_redirect(self.config.issuer, redirect_uri, self.config.environment)
            except ValueError:
                raise OAuthError("invalid_grant") from None
        return client

    @staticmethod
    def _live_session(login: NativeSessionTable, now: datetime) -> None:
        if (
            login.revoked_at is not None
            or login.expires_at <= now
            or login.auth_time > now
            or login.acr != MFA_ACR
            or login.amr != MFA_AMR
        ):
            raise OAuthError("invalid_grant")

    def _issue_tokens(
        self,
        session: AsyncSession,
        login: NativeSessionTable,
        now: datetime,
        client: NativeClientTable,
        *,
        include_nonce: bool = True,
        scope: str | None = None,
    ) -> dict[str, Any]:
        granted_scope = normalize_scope(login.scope if scope is None else scope)
        if not set(granted_scope.split()) <= set(login.scope.split()) & set(client.allowed_scopes):
            raise OAuthError("invalid_scope")
        expires = min(
            int((now + timedelta(seconds=self.config.access_token_seconds)).timestamp()),
            int(login.expires_at.timestamp()),
        )
        issued = int(now.timestamp())
        if expires <= issued:
            raise OAuthError("invalid_grant")
        common: dict[str, Any] = {
            "iss": self.config.issuer,
            "sub": login.account_ref,
            "iat": issued,
            "exp": expires,
            "auth_time": int(login.auth_time.timestamp()),
            "acr": login.acr,
            "amr": login.amr,
            "sid": login.session_ref,
        }
        access = self.keyring.sign(
            {
                **common,
                "aud": client.resource_audience,
                "jti": random_secret(),
                "client_id": login.client_id,
                "credential_version": login.credential_version,
                "scope": granted_scope,
            },
            typ="at+jwt",
        )
        id_claims = {
            **common,
            "aud": login.client_id,
            "azp": login.client_id,
            "at_hash": base64.urlsafe_b64encode(hashlib.sha256(access.encode("ascii")).digest()[:16])
            .rstrip(b"=")
            .decode("ascii"),
        }
        if include_nonce and login.nonce is not None:
            id_claims["nonce"] = login.nonce
        identity = self.keyring.sign(id_claims, typ="JWT")
        refresh = random_secret()
        session.add(
            NativeRefreshTokenTable(
                token_hash=digest_secret(refresh),
                session_ref=login.session_ref,
                scope=granted_scope,
                expires_at=min(login.expires_at, now + timedelta(seconds=self.config.refresh_token_seconds)),
            )
        )
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": expires - issued,
            "refresh_token": refresh,
            "id_token": identity,
            "scope": granted_scope,
        }

    @staticmethod
    def _event(session: AsyncSession, event: str, login: NativeSessionTable) -> None:
        session.add(
            NativeAuthEventTable(
                event=event,
                account_ref=login.account_ref,
                client_id=login.client_id,
                detail={"session_ref": login.session_ref},
            )
        )

    @staticmethod
    async def _commit(session: AsyncSession) -> None:
        # A lost COMMIT acknowledgement must never return unconfirmed credentials
        # or invite a client to assume the authorization code remains unused.
        try:
            await session.commit()
        except BaseException:
            raise NativeCommitUnknown() from None
