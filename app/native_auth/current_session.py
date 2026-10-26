"""Current account and session facts shared by identity, credentials and issuer operations.

Callers establish signature/profile trust and acquire their authority locks before
these checks. This module reads current facts without acquiring or releasing locks.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import PrincipalTable
from app.models.native_auth import NativeAccountTable, NativeBrowserSessionTable, NativeClientTable, NativeSessionTable
from app.native_auth.protocol import MFA_ACR, MFA_AMR, OAuthError, normalize_scope, valid_text


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


async def validate_browser_session(
    session: AsyncSession, browser_hash: str, account: NativeAccountTable, auth_time: datetime, *, now: datetime
) -> NativeBrowserSessionTable:
    browser = await session.get(NativeBrowserSessionTable, browser_hash, populate_existing=True)
    if (
        browser is None
        or browser.account_ref != account.account_ref
        or browser.credential_version != account.credential_version
        or browser.auth_time != auth_time
        or browser.auth_time > now
        or browser.revoked_at is not None
        or browser.expires_at <= now
    ):
        raise OAuthError("login_required")
    return browser


async def validate_native_session(
    session: AsyncSession, claims: dict[str, Any], *, now: datetime
) -> tuple[NativeAccountTable, NativeSessionTable]:
    """Refresh current native facts after the caller acquired authority locks.

    Signature, issuer, audience, type and expiry must already have been verified.
    No lock is taken here: identity's trust/principal/organization ordering must
    remain the same for native and external credentials.
    """
    try:
        token_scope = normalize_scope(claims.get("scope"))
    except OAuthError:
        raise OAuthError("invalid_token", 401) from None
    if (
        not all(valid_text(claims.get(key), 128) for key in ("sub", "sid", "client_id"))
        or not _integer(claims.get("credential_version"))
        or not _integer(claims.get("auth_time"))
        or not _integer(claims.get("iat"))
        or not _integer(claims.get("exp"))
        or claims["credential_version"] <= 0
        or claims["auth_time"] > claims["iat"]
        or claims["iat"] > int(now.timestamp())
        or claims["exp"] <= now.timestamp()
        or claims["exp"] <= claims["iat"]
        or not isinstance(claims.get("scope"), str)
        or claims.get("acr") != MFA_ACR
        or claims.get("amr") != MFA_AMR
    ):
        raise OAuthError("invalid_token", 401)
    account = await session.get(NativeAccountTable, claims["sub"], populate_existing=True)
    login = await session.get(NativeSessionTable, claims["sid"], populate_existing=True)
    client = await session.get(NativeClientTable, claims["client_id"], populate_existing=True)
    if (
        account is None
        or login is None
        or client is None
        or not client.enabled
        or account.status != "active"
        or account.totp_secret is None
        or account.principal_type != "human"
        or account.credential_version != claims["credential_version"]
        or login.account_ref != account.account_ref
        or login.client_id != client.client_id
        or login.client_version != client.version
        or claims.get("scope") != token_scope
        or not set(token_scope.split()) <= set(login.scope.split())
        or not set(login.scope.split()) <= set(client.allowed_scopes)
        or ("aud" in claims and claims["aud"] != client.resource_audience)
        or login.credential_version != account.credential_version
        or login.revoked_at is not None
        or login.expires_at <= now
        or claims["exp"] > int(login.expires_at.timestamp())
        or int(login.auth_time.timestamp()) != claims["auth_time"]
        or login.acr != claims["acr"]
        or login.amr != claims["amr"]
    ):
        raise OAuthError("invalid_token", 401)
    principal = await session.scalar(
        select(PrincipalTable)
        .where(PrincipalTable.principal_ref == account.principal_ref)
        .execution_options(populate_existing=True)
    )
    if (
        principal is None
        or principal.status != "active"
        or principal.principal_type != "human"
        or principal.disabled_at is not None
    ):
        raise OAuthError("invalid_token", 401)
    if login.browser_session_hash is not None:
        try:
            browser = await validate_browser_session(
                session, login.browser_session_hash, account, login.auth_time, now=now
            )
        except OAuthError:
            raise OAuthError("invalid_token", 401) from None
        if login.expires_at > browser.expires_at:
            raise OAuthError("invalid_token", 401)
    checked_at = max(now, datetime.now(UTC))
    if claims["exp"] <= checked_at.timestamp() or login.expires_at <= checked_at:
        raise OAuthError("invalid_token", 401)
    return account, login
