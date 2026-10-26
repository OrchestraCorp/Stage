"""Native account/session revocation and human business commits share one lock order."""

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.execution.contracts import CommandEffect, CommandEnvelope, ExecutionContext
from app.execution.storage import UnitOfWork
from app.features.work.details import update_work_details
from app.identity.authentication import AuthenticationRejected
from app.identity.service import IdentityDenied
from app.models.native_auth import NativeAccountTable
from app.native_auth.keys import KeyRing
from app.native_auth.oauth import NativeOAuth
from tests.support import native_oauth as oauth_support
from tests.support.authenticated_commands import authenticated_kernel
from tests.support.database_commands import counts
from tests.support.native_identity import native_context
from tests.support.postgres import finish_tasks, wait_until_blocked_by
from tests.support.work_details import assert_unchanged, command

native_keyring = oauth_support.native_keyring


async def prepare(sessions: async_sessionmaker[AsyncSession]) -> None:
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("Native revocation races require PostgreSQL lock observation")


async def revoke(oauth: NativeOAuth, tokens: dict[str, Any], mutation: str) -> None:
    if mutation == "logout":
        await oauth.revoke(client_id="maestro", token=tokens["refresh_token"])
    elif mutation == "factor_reset":
        assert (await oauth.credentials.admin_reset("native:alice", 2, reset_totp=True)).credential_version == 3
    else:
        result = await oauth.credentials.admin_reset("native:alice", 2, new_password="native-password-after-reset")
        assert result.credential_version == 3


@pytest.mark.parametrize("mutation", ["logout", "password_reset", "factor_reset"])
async def test_native_revocation_first_blocks_then_rejects_previously_authenticated_command(
    mutation: str,
    persistence_sessions: async_sessionmaker[AsyncSession],
    native_keyring: KeyRing,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await prepare(persistence_sessions)
    identity, oauth, context, tokens = await native_context(persistence_sessions, native_keyring)
    locked = asyncio.Event()
    release = asyncio.Event()
    writer_pid: int | None = None
    original = oauth.credentials.lock_account

    async def hold_lock(session: AsyncSession, account_ref: str) -> NativeAccountTable:
        nonlocal writer_pid
        account = await original(session, account_ref)
        writer_pid = await session.scalar(text("SELECT pg_backend_pid()"))
        locked.set()
        await release.wait()
        return account

    monkeypatch.setattr(oauth.credentials, "lock_account", hold_lock)
    writer = asyncio.create_task(revoke(oauth, tokens, mutation))
    tasks: list[asyncio.Task[Any]] = [writer]
    try:
        await asyncio.wait_for(locked.wait(), 10)
        assert writer_pid is not None
        execution = asyncio.create_task(
            authenticated_kernel(persistence_sessions, identity).execute(context, command())
        )
        tasks.append(execution)
        await wait_until_blocked_by(persistence_sessions, writer_pid)
        assert not execution.done()
        release.set()
        await asyncio.wait_for(writer, 10)
        with pytest.raises((AuthenticationRejected, IdentityDenied)):
            await asyncio.wait_for(execution, 10)
        await assert_unchanged(persistence_sessions)
    finally:
        await finish_tasks(release, tasks)
        await identity.aclose()


@pytest.mark.parametrize("mutation", ["logout", "password_reset", "factor_reset"])
async def test_native_command_first_commits_before_revocation_and_cannot_replay_afterward(
    mutation: str,
    persistence_sessions: async_sessionmaker[AsyncSession],
    native_keyring: KeyRing,
) -> None:
    await prepare(persistence_sessions)
    identity, oauth, context, tokens = await native_context(persistence_sessions, native_keyring)
    locked = asyncio.Event()
    release = asyncio.Event()
    command_pid: int | None = None

    async def hold_command(
        uow: UnitOfWork,
        execution: ExecutionContext,
        envelope: CommandEnvelope,
        payload: BaseModel,
    ) -> CommandEffect:
        nonlocal command_pid
        effect = await update_work_details(uow, execution, envelope, payload)
        command_pid = await uow.session.scalar(text("SELECT pg_backend_pid()"))
        locked.set()
        await release.wait()
        return effect

    kernel = authenticated_kernel(persistence_sessions, identity, handler=hold_command)
    execution = asyncio.create_task(kernel.execute(context, command()))
    tasks: list[asyncio.Task[Any]] = [execution]
    try:
        await asyncio.wait_for(locked.wait(), 10)
        assert command_pid is not None
        writer = asyncio.create_task(revoke(oauth, tokens, mutation))
        tasks.append(writer)
        await wait_until_blocked_by(persistence_sessions, command_pid)
        assert not writer.done()
        release.set()
        committed = await asyncio.wait_for(execution, 10)
        assert committed.result["version"] == 2
        await asyncio.wait_for(writer, 10)
        with pytest.raises((AuthenticationRejected, IdentityDenied)):
            await kernel.get_result(context, committed.execution_id)
        with pytest.raises((AuthenticationRejected, IdentityDenied)):
            await kernel.execute(context, command())
        assert await counts(persistence_sessions) == (2, 1, 1, 0)
    finally:
        await finish_tasks(release, tasks)
        await identity.aclose()
