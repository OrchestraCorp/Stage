"""Current-user session and application consent management under identity locks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.native_auth import (
    NativeAccountTable,
    NativeAuthEventTable,
    NativeBrowserSessionTable,
    NativeClientTable,
    NativeConsentTable,
    NativeLoginRequestTable,
    NativeSessionTable,
)
from app.native_auth.config import NativeAuthConfig
from app.native_auth.credentials import NativeCredentials, commit_authentication
from app.native_auth.current_session import validate_native_session
from app.native_auth.errors import NativeCredentialError
from app.native_auth.freshness import current_authentication_deadline
from app.native_auth.keys import NativeKeyError
from app.native_auth.oauth import NativeOAuth
from app.native_auth.protocol import OAuthError


def _reference(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value.encode("utf-8", errors="replace")) <= 128
        and all(ord(char) > 32 and ord(char) != 127 and not 0xD800 <= ord(char) <= 0xDFFF for char in value)
    )


def _page(limit: int, after: str | None) -> None:
    if type(limit) is not int or not 1 <= limit <= 50 or (after is not None and not _reference(after)):
        raise OAuthError("invalid_request")


class AccountSessions:
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

    @asynccontextmanager
    async def _actor(
        self, access_token: str, *, fresh: bool, write: bool
    ) -> AsyncIterator[tuple[AsyncSession, NativeAccountTable, NativeSessionTable, dict[str, Any], datetime | None]]:
        try:
            claims = self.oauth.keyring.verify_access(access_token, self.config.issuer, self.config.audience)
        except NativeKeyError:
            raise OAuthError("invalid_token", 401) from None
        if not _reference(claims.get("sub")):
            raise OAuthError("invalid_token", 401)
        async with asyncio.timeout(self.config.request_timeout_seconds), self.sessions() as session:
            try:
                account = (
                    await self.credentials.lock_account(session, claims["sub"])
                    if write
                    else await self.credentials.read_account(session, claims["sub"])
                )
            except NativeCredentialError:
                raise OAuthError("invalid_token", 401) from None
            _, login = await validate_native_session(session, claims, now=datetime.now(UTC))
            authentication_deadline = (
                await current_authentication_deadline(
                    session, self.config.organization_ref, login.auth_time, default_seconds=300
                )
                if fresh
                else None
            )
            self._deadline(claims, login, authentication_deadline=authentication_deadline)
            yield session, account, login, claims, authentication_deadline

    @staticmethod
    def _deadline(
        claims: dict[str, Any], login: NativeSessionTable, *, authentication_deadline: datetime | None
    ) -> None:
        now = datetime.now(UTC)
        if claims["exp"] <= now.timestamp() or login.expires_at <= now:
            raise OAuthError("invalid_token", 401)
        if authentication_deadline is not None and authentication_deadline <= now:
            raise OAuthError("login_required", 401)

    @staticmethod
    def _event(
        session: AsyncSession,
        event: str,
        account: NativeAccountTable,
        *,
        client_id: str | None = None,
        target_session: str | None = None,
    ) -> None:
        detail: dict[str, Any] = {"credential_version": account.credential_version}
        if target_session is not None:
            detail["session_ref"] = target_session
        session.add(
            NativeAuthEventTable(event=event, account_ref=account.account_ref, client_id=client_id, detail=detail)
        )

    async def list_sessions(self, access_token: str, *, limit: int = 20, after: str | None = None) -> dict[str, Any]:
        _page(limit, after)
        async with self._actor(access_token, fresh=False, write=False) as (
            session,
            account,
            actor_login,
            claims,
            authentication_deadline,
        ):
            query = (
                select(NativeSessionTable, NativeClientTable, NativeBrowserSessionTable)
                .join(NativeClientTable, NativeClientTable.client_id == NativeSessionTable.client_id)
                .outerjoin(
                    NativeBrowserSessionTable,
                    NativeBrowserSessionTable.session_hash == NativeSessionTable.browser_session_hash,
                )
                .where(NativeSessionTable.account_ref == account.account_ref)
                .order_by(NativeSessionTable.session_ref)
                .limit(limit + 1)
            )
            if after is not None:
                query = query.where(NativeSessionTable.session_ref > after)
            rows = (await session.execute(query)).all()
            now = datetime.now(UTC)
            items = []
            for login, client, browser in rows[:limit]:
                active = bool(
                    login.revoked_at is None
                    and login.expires_at > now
                    and login.credential_version == account.credential_version
                    and client.enabled
                    and login.client_version == client.version
                    and (
                        login.browser_session_hash is None
                        or (
                            browser is not None
                            and browser.revoked_at is None
                            and browser.expires_at > now
                            and browser.credential_version == account.credential_version
                        )
                    )
                )
                items.append(
                    {
                        "session_ref": login.session_ref,
                        "client_id": client.client_id,
                        "client_display_name": client.display_name or client.client_id,
                        "auth_time": login.auth_time.isoformat(),
                        "expires_at": login.expires_at.isoformat(),
                        "revoked_at": login.revoked_at.isoformat() if login.revoked_at is not None else None,
                        "current": login.session_ref == actor_login.session_ref,
                        "active": active,
                    }
                )
            self._deadline(claims, actor_login, authentication_deadline=authentication_deadline)
            return {"items": items, "next_after": items[-1]["session_ref"] if len(rows) > limit else None}

    async def revoke_session(self, access_token: str, session_ref: str) -> None:
        if not _reference(session_ref):
            raise OAuthError("invalid_request")
        async with self._actor(access_token, fresh=True, write=True) as (
            session,
            account,
            actor_login,
            claims,
            authentication_deadline,
        ):
            target = await session.scalar(
                select(NativeSessionTable)
                .where(
                    NativeSessionTable.session_ref == session_ref, NativeSessionTable.account_ref == account.account_ref
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if target is None:
                raise OAuthError("not_found", 404)
            if target.revoked_at is None:
                target.revoked_at = datetime.now(UTC)
                self._event(
                    session, "session.self_revoked", account, client_id=target.client_id, target_session=session_ref
                )
                await session.flush()
                self._deadline(claims, actor_login, authentication_deadline=authentication_deadline)
                await commit_authentication(session)
            else:
                self._deadline(claims, actor_login, authentication_deadline=authentication_deadline)

    async def revoke_all(self, access_token: str) -> dict[str, int]:
        async with self._actor(access_token, fresh=True, write=True) as (
            session,
            account,
            actor_login,
            claims,
            authentication_deadline,
        ):
            now = datetime.now(UTC)
            account.credential_version += 1
            for table in (NativeSessionTable, NativeBrowserSessionTable):
                await session.execute(
                    update(table)
                    .where(table.account_ref == account.account_ref, table.revoked_at.is_(None))
                    .values(revoked_at=now)
                )
            await session.execute(
                update(NativeLoginRequestTable)
                .where(
                    NativeLoginRequestTable.account_ref == account.account_ref, NativeLoginRequestTable.status != "done"
                )
                .values(status="done", pending_totp=None, consent_granted=False)
            )
            self._event(session, "sessions.all_revoked", account)
            await session.flush()
            self._deadline(claims, actor_login, authentication_deadline=authentication_deadline)
            result = {"credential_version": account.credential_version}
            await commit_authentication(session)
            return result

    async def list_consents(self, access_token: str, *, limit: int = 20, after: str | None = None) -> dict[str, Any]:
        _page(limit, after)
        async with self._actor(access_token, fresh=False, write=False) as (
            session,
            account,
            actor_login,
            claims,
            authentication_deadline,
        ):
            query = (
                select(NativeConsentTable, NativeClientTable)
                .join(NativeClientTable, NativeClientTable.client_id == NativeConsentTable.client_id)
                .where(NativeConsentTable.account_ref == account.account_ref)
                .order_by(NativeConsentTable.client_id)
                .limit(limit + 1)
            )
            if after is not None:
                query = query.where(NativeConsentTable.client_id > after)
            rows = (await session.execute(query)).all()
            items = [
                {
                    "client_id": client.client_id,
                    "client_display_name": client.display_name or client.client_id,
                    "client_version": grant.client_version,
                    "scopes": list(grant.scopes),
                    "granted_at": grant.granted_at.isoformat(),
                    "current": bool(client.enabled and grant.client_version == client.version),
                }
                for grant, client in rows[:limit]
            ]
            self._deadline(claims, actor_login, authentication_deadline=authentication_deadline)
            return {"items": items, "next_after": items[-1]["client_id"] if len(rows) > limit else None}

    async def revoke_consent(self, access_token: str, client_id: str) -> None:
        if not _reference(client_id):
            raise OAuthError("invalid_request")
        async with self._actor(access_token, fresh=True, write=True) as (
            session,
            account,
            actor_login,
            claims,
            authentication_deadline,
        ):
            grant = await session.get(NativeConsentTable, (account.account_ref, client_id))
            active_login = await session.scalar(
                select(NativeSessionTable.session_ref)
                .where(
                    NativeSessionTable.account_ref == account.account_ref,
                    NativeSessionTable.client_id == client_id,
                    NativeSessionTable.revoked_at.is_(None),
                )
                .limit(1)
            )
            pending = await session.scalar(
                select(NativeLoginRequestTable.request_ref)
                .where(
                    NativeLoginRequestTable.account_ref == account.account_ref,
                    NativeLoginRequestTable.client_id == client_id,
                    NativeLoginRequestTable.status != "done",
                )
                .limit(1)
            )
            if grant is None and active_login is None and pending is None:
                self._deadline(claims, actor_login, authentication_deadline=authentication_deadline)
                return
            await session.execute(
                delete(NativeConsentTable).where(
                    NativeConsentTable.account_ref == account.account_ref, NativeConsentTable.client_id == client_id
                )
            )
            await session.execute(
                update(NativeSessionTable)
                .where(
                    NativeSessionTable.account_ref == account.account_ref,
                    NativeSessionTable.client_id == client_id,
                    NativeSessionTable.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(UTC))
            )
            # A confirmed but unfinished request must not carry consent past its revocation.
            await session.execute(
                update(NativeLoginRequestTable)
                .where(
                    NativeLoginRequestTable.account_ref == account.account_ref,
                    NativeLoginRequestTable.client_id == client_id,
                    NativeLoginRequestTable.status != "done",
                )
                .values(status="done", pending_totp=None, consent_granted=False)
            )
            self._event(session, "consent.revoked", account, client_id=client_id)
            await session.flush()
            self._deadline(claims, actor_login, authentication_deadline=authentication_deadline)
            await commit_authentication(session)
