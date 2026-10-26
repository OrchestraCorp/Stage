"""Session ownership, bounded discovery, revocation and application-consent races."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Self

import pytest
from authlib.oauth2.rfc7636.challenge import create_s256_code_challenge
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import PrincipalTable
from app.models.native_auth import (
    NativeAccountTable,
    NativeAuthEventTable,
    NativeBrowserSessionTable,
    NativeClientTable,
    NativeConsentTable,
    NativeLoginRequestTable,
    NativeSessionTable,
)
from app.native_auth.crypto import digest_secret
from app.native_auth.oauth import MFA_ACR, OAuthError
from app.native_auth.sessions import AccountSessions
from tests.support.native_oauth import CLIENT, REDIRECT, VERIFIER, authenticated_request, native_keyring, oauth, tokens  # noqa: F401 -- shared fixture
from tests.support.native_sessions import (
    manager as manager,
)


async def test_session_discovery_is_bounded_and_contains_no_credentials(manager: AccountSessions) -> None:
    issued = [await tokens(manager.oauth) for _ in range(3)]
    first = await manager.list_sessions(issued[0]["access_token"], limit=2)
    second = await manager.list_sessions(issued[0]["access_token"], limit=2, after=first["next_after"])
    items = first["items"] + second["items"]
    assert len(items) == 3 and second["next_after"] is None
    assert len({item["session_ref"] for item in items}) == 3
    assert sum(item["current"] for item in items) == 1
    assert all(item["active"] for item in items)
    assert all(item["client_display_name"] == CLIENT for item in items)
    payload = json.dumps(items)
    assert "hash" not in payload and "secret" not in payload and "password" not in payload
    assert all(token["access_token"] not in payload and token["refresh_token"] not in payload for token in issued)


@pytest.mark.parametrize("limit,after", [(0, None), (51, None), (True, None), (20, ""), (20, "\x00"), (20, "x" * 129)])
async def test_session_and_consent_discovery_reject_unbounded_input(
    manager: AccountSessions, limit: int, after: str | None
) -> None:
    for operation in (manager.list_sessions, manager.list_consents):
        with pytest.raises(OAuthError, match="invalid_request"):
            await operation("unused-token", limit=limit, after=after)


async def add_other_account_session(manager: AccountSessions) -> None:
    now = datetime.now(UTC)
    async with manager.sessions.begin() as session:
        session.add(PrincipalTable(principal_ref="person:other", principal_type="human"))
        await session.flush()
        session.add(
            NativeAccountTable(
                account_ref="account:other",
                principal_ref="person:other",
                username="other",
                password_hash="not-used",
                totp_secret="not-used",
            )
        )
        await session.flush()
        session.add(
            NativeSessionTable(
                session_ref="other-session",
                account_ref="account:other",
                client_id=CLIENT,
                credential_version=1,
                auth_time=now,
                expires_at=now + timedelta(hours=1),
                acr=MFA_ACR,
                amr=["pwd", "otp"],
            )
        )


async def test_session_management_never_discovers_or_revokes_another_person(manager: AccountSessions) -> None:
    issued = await tokens(manager.oauth)
    await add_other_account_session(manager)
    result = await manager.list_sessions(issued["access_token"])
    assert len(result["items"]) == 1
    for target in ("other-session", "unknown-session"):
        with pytest.raises(OAuthError) as error:
            await manager.revoke_session(issued["access_token"], target)
        assert error.value.error == "not_found" and error.value.status_code == 404
    async with manager.sessions() as session:
        other = await session.get(NativeSessionTable, "other-session")
        assert other is not None and other.revoked_at is None


async def test_one_session_revocation_is_idempotent_and_does_not_revoke_another_login(manager: AccountSessions) -> None:
    actor = await tokens(manager.oauth)
    target = await tokens(manager.oauth)
    target_claims = manager.oauth.keyring.verify_access(target["access_token"], manager.config.issuer, "stage-api")
    await manager.revoke_session(actor["access_token"], target_claims["sid"])
    await manager.revoke_session(actor["access_token"], target_claims["sid"])
    with pytest.raises(OAuthError):
        await manager.oauth.userinfo(target["access_token"])
    assert await manager.oauth.userinfo(actor["access_token"])
    async with manager.sessions() as session:
        events = (
            await session.scalars(
                select(NativeAuthEventTable).where(NativeAuthEventTable.event == "session.self_revoked")
            )
        ).all()
        assert len(events) == 1 and events[0].detail["session_ref"] == target_claims["sid"]


async def test_current_session_can_be_revoked_and_cannot_authorize_later_requests(manager: AccountSessions) -> None:
    actor = await tokens(manager.oauth)
    listing = await manager.list_sessions(actor["access_token"])
    await manager.revoke_session(actor["access_token"], listing["items"][0]["session_ref"])
    with pytest.raises(OAuthError, match="invalid_token"):
        await manager.list_sessions(actor["access_token"])


async def test_revoke_all_versions_credentials_and_invalidates_sessions_browser_and_pending_requests(
    manager: AccountSessions,
) -> None:
    actor = await tokens(manager.oauth)
    other = await tokens(manager.oauth)
    pending = await authenticated_request(manager.oauth)
    async with manager.sessions.begin() as session:
        session.add(
            NativeBrowserSessionTable(
                session_hash=digest_secret("browser-for-revoke-all"),
                account_ref="account:native",
                credential_version=1,
                auth_time=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    result = await manager.revoke_all(actor["access_token"])
    assert result == {"credential_version": 2}
    for issued in (actor, other):
        with pytest.raises(OAuthError):
            await manager.oauth.userinfo(issued["access_token"])
        with pytest.raises(OAuthError):
            await manager.oauth.exchange_refresh(client_id=CLIENT, refresh_token=issued["refresh_token"])
    with pytest.raises(OAuthError):
        await manager.oauth.finish_authorization(
            request_ref=pending.request_ref, browser_secret=pending.browser_secret, csrf_token=pending.csrf_token
        )
    async with manager.sessions() as session:
        browser = await session.scalar(select(NativeBrowserSessionTable))
        assert browser is not None and browser.revoked_at is not None
        request = await session.get(NativeLoginRequestTable, pending.request_ref)
        assert request is not None and request.status == "done" and request.pending_totp is None


async def test_stale_authentication_can_read_but_cannot_revoke(manager: AccountSessions) -> None:
    issued = await tokens(manager.oauth)
    claims = manager.oauth.keyring.verify_access(issued["access_token"], manager.config.issuer, "stage-api")
    old = datetime.now(UTC) - timedelta(minutes=10)
    async with manager.sessions.begin() as session:
        login = await session.get(NativeSessionTable, claims["sid"])
        assert login is not None
        login.auth_time = old
    claims["auth_time"] = int(old.timestamp())
    stale = manager.oauth.keyring.sign(claims)
    assert (await manager.list_sessions(stale))["items"]
    operations = [
        manager.revoke_session(stale, claims["sid"]),
        manager.revoke_all(stale),
        manager.revoke_consent(stale, CLIENT),
    ]
    for operation in operations:
        with pytest.raises(OAuthError, match="login_required"):
            await operation


async def test_revocation_deadline_after_database_work_rolls_back(
    manager: AccountSessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.native_auth import sessions as module

    actor = await tokens(manager.oauth)
    target = await tokens(manager.oauth)
    claims = manager.oauth.keyring.verify_access(target["access_token"], manager.config.issuer, "stage-api")
    original_flush = AsyncSession.flush
    advance = False

    class AfterFlush(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            value = datetime.now(tz) + (timedelta(minutes=10) if advance else timedelta())
            return cls.fromtimestamp(value.timestamp(), tz)

    async def flush(session: AsyncSession, objects: Sequence[Any] | None = None) -> None:
        nonlocal advance
        await original_flush(session, objects)
        if any(
            isinstance(obj, NativeAuthEventTable) and obj.event == "session.self_revoked"
            for obj in session.identity_map.values()
        ):
            advance = True
        # Explicit flush follows the pending event; autoflush before reads has none.
        if any(
            isinstance(obj, NativeSessionTable) and obj.session_ref == claims["sid"] and obj.revoked_at is not None
            for obj in session.identity_map.values()
        ):
            advance = True

    with monkeypatch.context() as patch:
        patch.setattr(module, "datetime", AfterFlush)
        patch.setattr(AsyncSession, "flush", flush)
        with pytest.raises(OAuthError, match="invalid_token"):
            await manager.revoke_session(actor["access_token"], claims["sid"])
    assert await manager.oauth.userinfo(target["access_token"])


async def test_consent_discovery_and_revocation_invalidate_pending_grants_only_for_that_app(
    manager: AccountSessions,
) -> None:
    actor = await tokens(manager.oauth)
    now = datetime.now(UTC)
    async with manager.sessions.begin() as session:
        session.add(NativeClientTable(client_id="external-app", redirect_uris=[REDIRECT], require_consent=True))
        await session.flush()
        session.add(
            NativeConsentTable(
                account_ref="account:native",
                client_id="external-app",
                client_version=1,
                scopes=["openid"],
                granted_at=now,
            )
        )
        session.add(
            NativeSessionTable(
                session_ref="external-app-session",
                account_ref="account:native",
                client_id="external-app",
                credential_version=1,
                auth_time=now,
                expires_at=now + timedelta(hours=1),
                acr=MFA_ACR,
                amr=["pwd", "otp"],
            )
        )
        session.add(
            NativeBrowserSessionTable(
                session_hash=digest_secret("shared-browser"),
                account_ref="account:native",
                credential_version=1,
                auth_time=now,
                expires_at=now + timedelta(hours=1),
            )
        )
    request = await manager.oauth.prepare_authorization(
        client_id="external-app",
        redirect_uri=REDIRECT,
        response_type="code",
        scope="openid",
        state="state",
        nonce="nonce",
        code_challenge_method="S256",
        code_challenge=create_s256_code_challenge(VERIFIER),
    )
    async with manager.sessions.begin() as session:
        row = await session.get(NativeLoginRequestTable, request.request_ref)
        assert row is not None
        row.account_ref, row.credential_version = "account:native", 1
        row.password_verified_at, row.status, row.consent_granted = now, "authenticated", True
    listing = await manager.list_consents(actor["access_token"])
    assert listing["items"][0]["client_id"] == "external-app" and listing["items"][0]["current"]
    await manager.revoke_consent(actor["access_token"], "external-app")
    await manager.revoke_consent(actor["access_token"], "external-app")
    assert await manager.oauth.userinfo(actor["access_token"])
    assert (await manager.list_consents(actor["access_token"]))["items"] == []
    with pytest.raises(OAuthError):
        await manager.oauth.finish_authorization(
            request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
        )
    async with manager.sessions() as session:
        login = await session.get(NativeSessionTable, "external-app-session")
        browser = await session.get(NativeBrowserSessionTable, digest_secret("shared-browser"))
        assert login is not None and login.revoked_at is not None
        assert browser is not None and browser.revoked_at is None


@pytest.mark.parametrize("first", ["revoke", "refresh"])
async def test_postgresql_session_revocation_orders_with_refresh_on_the_same_identity_lock(
    manager: AccountSessions, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    import asyncio

    async with manager.sessions() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires PostgreSQL authority row locks")
    issued = await tokens(manager.oauth)
    original_lock = manager.credentials.lock_account
    leader_locked, follower_attempted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def gate(session: AsyncSession, account_ref: str) -> NativeAccountTable:
        task = asyncio.current_task()
        assert task is not None
        if task.get_name() != first:
            follower_attempted.set()
        account = await original_lock(session, account_ref)
        if task.get_name() == first:
            leader_locked.set()
            await release.wait()
        return account

    async def operation(name: str) -> object:
        if name == "revoke":
            return await manager.revoke_all(issued["access_token"])
        try:
            return await manager.oauth.exchange_refresh(client_id=CLIENT, refresh_token=issued["refresh_token"])
        except OAuthError as error:
            return error

    with monkeypatch.context() as patch:
        patch.setattr(manager.credentials, "lock_account", gate)
        async with asyncio.timeout(15):
            leader = asyncio.create_task(operation(first), name=first)
            await leader_locked.wait()
            second = "refresh" if first == "revoke" else "revoke"
            follower = asyncio.create_task(operation(second), name=second)
            await follower_attempted.wait()
            assert not follower.done()
            release.set()
            results = dict(zip([first, second], await asyncio.gather(leader, follower), strict=True))
    assert results["revoke"] == {"credential_version": 2}
    if first == "revoke":
        assert isinstance(results["refresh"], OAuthError)
    else:
        refreshed = results["refresh"]
        assert isinstance(refreshed, dict)
        with pytest.raises(OAuthError):
            await manager.oauth.userinfo(refreshed["access_token"])
    with pytest.raises(OAuthError):
        await manager.oauth.userinfo(issued["access_token"])
