"""Account mutations obey the stricter current organization MFA deadline."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Self

import pyotp
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.identity_foundation import OrganizationHumanAuthenticationPolicyTable
from app.models.native_auth import NativeAccountTable, NativeAuthEventTable, NativeConsentTable, NativeSessionTable
from app.native_auth.errors import NativeCredentialError
from app.native_auth.oauth import OAuthError
from app.native_auth.sessions import AccountSessions
from tests.support import native_oauth as oauth_support
from tests.support import native_sessions as session_support
from tests.support.native_credentials import PASSWORD, credentials, enroll, strong_session

native_keyring = oauth_support.native_keyring
oauth = oauth_support.oauth
manager = session_support.manager


async def require_recent_authentication(sessions: async_sessionmaker[AsyncSession], organization_ref: str) -> None:
    async with sessions.begin() as session:
        session.add(
            OrganizationHumanAuthenticationPolicyTable(
                organization_ref=organization_ref,
                version=1,
                mode="required",
                max_auth_age_seconds=60,
                rules_json=[
                    {
                        "profile_ref": "native",
                        "profile_version": 1,
                        "required_acr": "urn:orchestra:acr:pwd-totp",
                        "required_amr": ["pwd", "otp"],
                    }
                ],
            )
        )


async def aged_access(manager: AccountSessions, age: int) -> tuple[str, str]:
    issued = await oauth_support.tokens(manager.oauth)
    claims = manager.oauth.keyring.verify_access(issued["access_token"], manager.config.issuer, "stage-api")
    authenticated_at = datetime.now(UTC) - timedelta(seconds=age)
    async with manager.sessions.begin() as session:
        login = await session.get(NativeSessionTable, claims["sid"])
        assert login is not None
        login.auth_time = authenticated_at
    claims["auth_time"] = int(authenticated_at.timestamp())
    return manager.oauth.keyring.sign(claims), claims["sid"]


@pytest.mark.parametrize("operation", ["password", "totp"])
async def test_organization_policy_shorter_than_five_minutes_blocks_credential_changes(
    persistence_sessions: async_sessionmaker[AsyncSession],
    operation: str,
) -> None:
    service = await credentials(persistence_sessions)
    factor, _ = await enroll(service, persistence_sessions)
    login_ref = await strong_session(persistence_sessions, age=120)
    await require_recent_authentication(persistence_sessions, "org-a")
    with pytest.raises(NativeCredentialError):
        if operation == "password":
            await service.change_password(login_ref, PASSWORD, PASSWORD + "-changed")
        else:
            await service.reset_totp(
                login_ref, PASSWORD, pyotp.TOTP(factor).at(datetime.now(UTC) + timedelta(seconds=30))
            )
    async with persistence_sessions() as session:
        account = await session.get(NativeAccountTable, "account-a")
        login = await session.get(NativeSessionTable, login_ref)
        assert account is not None and account.credential_version == 2
        assert account.totp_secret is not None
        assert login is not None and login.revoked_at is None


@pytest.mark.parametrize("operation", ["session", "all", "consent"])
async def test_organization_policy_blocks_session_mutation_without_turning_userinfo_into_business_admission(
    manager: AccountSessions,
    operation: str,
) -> None:
    access, session_ref = await aged_access(manager, 120)
    await require_recent_authentication(manager.sessions, manager.config.organization_ref)
    assert await manager.oauth.userinfo(access) == {"sub": "account:native"}
    assert (await manager.list_sessions(access))["items"]
    with pytest.raises(OAuthError, match="login_required"):
        await manager.oauth.authenticate_account(access)
    with pytest.raises(OAuthError, match="login_required"):
        if operation == "session":
            await manager.revoke_session(access, session_ref)
        elif operation == "all":
            await manager.revoke_all(access)
        else:
            await manager.revoke_consent(access, oauth_support.CLIENT)
    assert await manager.oauth.userinfo(access) == {"sub": "account:native"}


@pytest.mark.parametrize("operation", ["password", "totp"])
async def test_credential_change_rolls_back_when_database_wait_crosses_organization_deadline(
    persistence_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    from app.native_auth import credentials as module

    service = await credentials(persistence_sessions)
    factor, _ = await enroll(service, persistence_sessions)
    login_ref = await strong_session(persistence_sessions, age=30)
    await require_recent_authentication(persistence_sessions, "org-a")
    original_revoke = service._revoke_sessions

    class Clock(datetime):
        offset = timedelta()

        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp((datetime.now(tz) + cls.offset).timestamp(), tz)

    async def delayed_revoke(session: AsyncSession, account_ref: str, now: datetime) -> None:
        await original_revoke(session, account_ref, now)
        Clock.offset = timedelta(seconds=31)

    with monkeypatch.context() as patch:
        patch.setattr(module, "datetime", Clock)
        patch.setattr(service, "_revoke_sessions", delayed_revoke)
        with pytest.raises(NativeCredentialError):
            if operation == "password":
                await service.change_password(login_ref, PASSWORD, PASSWORD + "-changed")
            else:
                await service.reset_totp(
                    login_ref, PASSWORD, pyotp.TOTP(factor).at(datetime.now(UTC) + timedelta(seconds=30))
                )
    assert Clock.offset == timedelta(seconds=31)
    async with persistence_sessions() as session:
        account = await session.get(NativeAccountTable, "account-a")
        login = await session.get(NativeSessionTable, login_ref)
        assert account is not None and account.credential_version == 2 and account.totp_secret is not None
        assert login is not None and login.revoked_at is None
        assert not (
            await session.scalars(
                select(NativeAuthEventTable).where(NativeAuthEventTable.event.in_(["password.changed", "factor.reset"]))
            )
        ).all()


@pytest.mark.parametrize("operation", ["session", "all", "consent"])
async def test_session_mutation_rolls_back_when_database_wait_crosses_organization_deadline(
    manager: AccountSessions,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    from app.native_auth import sessions as module

    access, session_ref = await aged_access(manager, 30)
    await require_recent_authentication(manager.sessions, manager.config.organization_ref)
    async with manager.sessions.begin() as session:
        session.add(
            NativeConsentTable(
                account_ref="account:native",
                client_id=oauth_support.CLIENT,
                client_version=1,
                scopes=["openid"],
                granted_at=datetime.now(UTC),
            )
        )
    original_flush = AsyncSession.flush

    class Clock(datetime):
        offset = timedelta()

        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp((datetime.now(tz) + cls.offset).timestamp(), tz)

    events = {"session.self_revoked", "sessions.all_revoked", "consent.revoked"}

    async def delayed_flush(session: AsyncSession, objects: Sequence[Any] | None = None) -> None:
        changing_security = any(isinstance(row, NativeAuthEventTable) and row.event in events for row in session.new)
        await original_flush(session, objects)
        if changing_security:
            Clock.offset = timedelta(seconds=31)

    with monkeypatch.context() as patch:
        patch.setattr(module, "datetime", Clock)
        patch.setattr(AsyncSession, "flush", delayed_flush)
        with pytest.raises(OAuthError, match="login_required"):
            if operation == "session":
                await manager.revoke_session(access, session_ref)
            elif operation == "all":
                await manager.revoke_all(access)
            else:
                await manager.revoke_consent(access, oauth_support.CLIENT)
    assert Clock.offset == timedelta(seconds=31)
    assert await manager.oauth.userinfo(access)
    async with manager.sessions() as session:
        account = await session.get(NativeAccountTable, "account:native")
        grant = await session.get(NativeConsentTable, ("account:native", oauth_support.CLIENT))
        assert account is not None and account.credential_version == 1 and grant is not None
        assert not (
            await session.scalars(select(NativeAuthEventTable).where(NativeAuthEventTable.event.in_(events)))
        ).all()


async def test_recent_authentication_still_allows_account_mutation_under_short_policy(manager: AccountSessions) -> None:
    access, _ = await aged_access(manager, 20)
    await require_recent_authentication(manager.sessions, manager.config.organization_ref)
    assert await manager.revoke_all(access) == {"credential_version": 2}


async def test_longer_organization_policy_never_extends_account_security_freshness(manager: AccountSessions) -> None:
    access, session_ref = await aged_access(manager, 360)
    await require_recent_authentication(manager.sessions, manager.config.organization_ref)
    async with manager.sessions.begin() as session:
        policy = await session.get(OrganizationHumanAuthenticationPolicyTable, manager.config.organization_ref)
        assert policy is not None
        policy.max_auth_age_seconds = 900
    with pytest.raises(OAuthError, match="login_required"):
        await manager.oauth.authenticate_account(access)
    with pytest.raises(OAuthError, match="login_required"):
        await manager.revoke_session(access, session_ref)
