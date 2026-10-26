"""Real PostgreSQL overlap and revocation order through native service entrypoints."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution import TenantGuardTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import IdentityProviderTable
from app.models.native_auth import NativeAccountTable, NativeClientTable, NativeLoginRequestTable, NativeSessionTable
from app.native_auth import client_administration
from app.native_auth.oauth import NativeOAuth
from app.native_auth.protocol import OAuthError
from app.native_auth.sessions import AccountSessions
from tests.support.native_clients import configuration
from tests.support.native_oauth import CLIENT, REDIRECT, VERIFIER, authenticated_request, tokens
from tests.support.native_oauth import native_keyring as native_keyring
from tests.support.native_oauth import oauth as oauth
from tests.support.native_sessions import manager as manager
from tests.support.postgres import finish_tasks, wait_until_blocked_by


@pytest.fixture
async def pg_oauth(oauth: NativeOAuth) -> NativeOAuth:
    if oauth.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("native shared/exclusive coordination requires actual PostgreSQL")
    return oauth


async def _two_accounts(oauth: NativeOAuth) -> tuple[dict[str, Any], dict[str, Any]]:
    first = await tokens(oauth)
    async with oauth.sessions.begin() as session:
        session.add(PrincipalTable(principal_ref="person:other", principal_type="human"))
        await session.flush()
        session.add(
            NativeAccountTable(
                account_ref="account:other",
                principal_ref="person:other",
                username="other-test",
                password_hash="not-used-by-coordination-tests",
                totp_secret="encrypted-test-placeholder",
                credential_version=1,
            )
        )
    request = await authenticated_request(oauth)
    async with oauth.sessions.begin() as session:
        row = await session.get(NativeLoginRequestTable, request.request_ref)
        assert row is not None
        row.account_ref = "account:other"
    redirect = await oauth.finish_authorization(
        request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
    )
    second = await oauth.exchange_code(
        client_id=CLIENT,
        redirect_uri=REDIRECT,
        code=parse_qs(urlsplit(redirect).query)["code"][0],
        code_verifier=VERIFIER,
    )
    return first, second


@pytest.mark.parametrize("other_read", ["userinfo", "sessions", "consents"])
async def test_same_person_pure_reads_reach_protected_area_together(
    pg_oauth: NativeOAuth, manager: AccountSessions, monkeypatch: pytest.MonkeyPatch, other_read: str
) -> None:
    issued = await tokens(pg_oauth)
    release, overlapping = asyncio.Event(), asyncio.Event()
    holders: set[int] = set()
    original = pg_oauth.credentials.read_account

    async def hold_read(session: AsyncSession, account_ref: str) -> NativeAccountTable:
        account = await original(session, account_ref)
        pid = await session.scalar(text("SELECT pg_backend_pid()"))
        assert pid is not None
        holders.add(pid)
        if len(holders) == 2:
            overlapping.set()
        await release.wait()
        return account

    monkeypatch.setattr(pg_oauth.credentials, "read_account", hold_read)
    operations: dict[str, Callable[[str], Coroutine[Any, Any, dict[str, Any]]]] = {
        "userinfo": pg_oauth.userinfo,
        "sessions": manager.list_sessions,
        "consents": manager.list_consents,
    }
    tasks = [
        asyncio.create_task(pg_oauth.userinfo(issued["access_token"])),
        asyncio.create_task(operations[other_read](issued["access_token"])),
    ]
    try:
        # Both database lock chains completed while neither transaction ended.
        await asyncio.wait_for(overlapping.wait(), 5)
        assert len(holders) == 2 and all(not task.done() for task in tasks)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert results[0]["sub"] == "account:native"
        if other_read == "sessions":
            assert len(results[1]["items"]) == 1
        elif other_read == "consents":
            assert results[1]["items"] == []
        else:
            assert results[1] == results[0]
    finally:
        await finish_tasks(release, tasks)


async def test_different_accounts_refresh_inside_write_sections_concurrently(
    pg_oauth: NativeOAuth, monkeypatch: pytest.MonkeyPatch
) -> None:
    issued = await _two_accounts(pg_oauth)
    release, overlapping = asyncio.Event(), asyncio.Event()
    holders: dict[str, int] = {}
    original = pg_oauth.credentials.lock_account

    async def hold_write(session: AsyncSession, account_ref: str) -> NativeAccountTable:
        account = await original(session, account_ref)
        pid = await session.scalar(text("SELECT pg_backend_pid()"))
        assert pid is not None
        holders[account_ref] = pid
        if len(holders) == 2:
            overlapping.set()
        await release.wait()
        return account

    monkeypatch.setattr(pg_oauth.credentials, "lock_account", hold_write)
    tasks = [
        asyncio.create_task(pg_oauth.exchange_refresh(client_id=CLIENT, refresh_token=item["refresh_token"]))
        for item in issued
    ]
    try:
        await asyncio.wait_for(overlapping.wait(), 5)
        assert set(holders) == {"account:native", "account:other"}
        assert len(set(holders.values())) == 2 and all(not task.done() for task in tasks)
        release.set()
        refreshed = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        for before, after in zip(issued, refreshed, strict=True):
            assert after["refresh_token"] != before["refresh_token"]
        assert {
            result["sub"] for result in await asyncio.gather(*(pg_oauth.userinfo(t["access_token"]) for t in refreshed))
        } == {"account:native", "account:other"}
    finally:
        await finish_tasks(release, tasks)


@pytest.mark.parametrize("reader_first", [True, False])
@pytest.mark.parametrize("reading", ["userinfo", "sessions"])
async def test_account_revocation_and_pure_read_have_observed_commit_order(
    pg_oauth: NativeOAuth,
    manager: AccountSessions,
    monkeypatch: pytest.MonkeyPatch,
    reader_first: bool,
    reading: str,
) -> None:
    issued = await tokens(pg_oauth)
    release, entered = asyncio.Event(), asyncio.Event()
    owner_pid: int | None = None
    method = "read_account" if reader_first else "lock_account"
    original = getattr(pg_oauth.credentials, method)

    async def hold_owner(session: AsyncSession, account_ref: str) -> NativeAccountTable:
        nonlocal owner_pid
        account = await original(session, account_ref)
        owner_pid = await session.scalar(text("SELECT pg_backend_pid()"))
        entered.set()
        await release.wait()
        return account

    monkeypatch.setattr(pg_oauth.credentials, method, hold_owner)
    read = pg_oauth.userinfo if reading == "userinfo" else manager.list_sessions
    owner = asyncio.create_task(
        read(issued["access_token"]) if reader_first else manager.revoke_all(issued["access_token"])
    )
    tasks: list[asyncio.Task[Any]] = [owner]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert owner_pid is not None
        waiter = asyncio.create_task(
            manager.revoke_all(issued["access_token"]) if reader_first else read(issued["access_token"])
        )
        tasks.append(waiter)
        await wait_until_blocked_by(pg_oauth.sessions, owner_pid)
        assert not waiter.done()
        release.set()
        result = await asyncio.wait_for(owner, 5)
        if reader_first:
            assert result["sub"] == "account:native" if reading == "userinfo" else len(result["items"]) == 1
            assert await asyncio.wait_for(waiter, 5) == {"credential_version": 2}
        else:
            assert result == {"credential_version": 2}
            with pytest.raises(OAuthError, match="invalid_token"):
                await asyncio.wait_for(waiter, 5)
        with pytest.raises(OAuthError, match="invalid_token"):
            await read(issued["access_token"])
    finally:
        await finish_tasks(release, tasks)


@pytest.mark.parametrize("reader_first", [True, False])
async def test_client_configuration_trust_barrier_coordinates_reads_across_accounts(
    pg_oauth: NativeOAuth, monkeypatch: pytest.MonkeyPatch, reader_first: bool
) -> None:
    issued = await _two_accounts(pg_oauth)
    async with pg_oauth.sessions.begin() as session:
        session.add(
            IdentityProviderTable(
                provider_ref=client_administration.PROVIDER,
                trust_ref=client_administration.TRUST,
                issuer_exact=pg_oauth.config.issuer,
            )
        )
    release, entered = asyncio.Event(), asyncio.Event()
    owner_pid: int | None = None
    read_original = pg_oauth.credentials.read_account
    invalidate_original = client_administration._invalidate_client

    async def hold_read(session: AsyncSession, account_ref: str) -> NativeAccountTable:
        nonlocal owner_pid
        account = await read_original(session, account_ref)
        if reader_first:
            owner_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            entered.set()
            await release.wait()
        return account

    async def hold_mutation(session: AsyncSession, client: NativeClientTable, guard: TenantGuardTable) -> None:
        nonlocal owner_pid
        await invalidate_original(session, client, guard)
        if not reader_first:
            await session.flush()
            owner_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            entered.set()
            await release.wait()

    monkeypatch.setattr(pg_oauth.credentials, "read_account", hold_read)
    monkeypatch.setattr(client_administration, "_invalidate_client", hold_mutation)

    async def disable() -> Any:
        return await client_administration.configure_client(
            pg_oauth.sessions,
            configuration(
                organization_ref=pg_oauth.config.organization_ref,
                client_id=CLIENT,
                expected_version=1,
                redirect_uris=[REDIRECT],
                enabled=False,
            ),
            actor_ref="operator:concurrency",
            idempotency_key="disable-client",
        )

    owner = asyncio.create_task(pg_oauth.userinfo(issued[0]["access_token"]) if reader_first else disable())
    tasks: list[asyncio.Task[Any]] = [owner]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert owner_pid is not None
        waiters = (
            [asyncio.create_task(disable())]
            if reader_first
            else [asyncio.create_task(pg_oauth.userinfo(item["access_token"])) for item in issued]
        )
        tasks.extend(waiters)
        await wait_until_blocked_by(pg_oauth.sessions, owner_pid)
        # It is the trust barrier that blocks, before organization/account locks.
        async with pg_oauth.sessions() as observer:
            async with asyncio.timeout(5):
                while True:
                    await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
                    blocked = (
                        await observer.scalars(
                            text("SELECT query FROM pg_stat_activity WHERE :owner = ANY(pg_blocking_pids(pid))"),
                            {"owner": owner_pid},
                        )
                    ).all()
                    if len(blocked) == len(waiters):
                        assert all("trust_state" in query for query in blocked)
                        break
                    await asyncio.sleep(0.01)
        assert all(not task.done() for task in waiters)
        release.set()
        first = await asyncio.wait_for(owner, 5)
        if reader_first:
            assert first["sub"] == "account:native"
            assert (await asyncio.wait_for(waiters[0], 5)).result["enabled"] is False
        else:
            assert first.result["enabled"] is False
            for task in waiters:
                with pytest.raises(OAuthError, match="invalid_token"):
                    await asyncio.wait_for(task, 5)
        for item in issued:
            with pytest.raises(OAuthError, match="invalid_token"):
                await pg_oauth.userinfo(item["access_token"])
        async with pg_oauth.sessions() as session:
            rows = (await session.scalars(select(NativeSessionTable))).all()
            assert len(rows) == 2 and all(row.revoked_at is not None for row in rows)
    finally:
        await finish_tasks(release, tasks)
