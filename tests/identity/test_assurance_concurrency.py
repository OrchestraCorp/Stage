"""MFA policy changes and commands share an observed PostgreSQL commit order."""

import asyncio
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.execution.contracts import CommandEffect, CommandEnvelope, ExecutionContext
from app.execution.storage import UnitOfWork
from app.features.work.details import update_work_details
from app.identity import assurance_administration
from app.identity.authentication import AuthenticationRejected
from app.models.execution import TenantGuardTable
from app.models.identity_foundation import TrustStateTable
from app.storage.coordination import LockMode
from tests.support.authenticated_commands import (
    authenticated_context,
    authenticated_kernel,
    identity_service,
    jwt_client,
)
from tests.support.authorization import ORG
from tests.support.database_commands import counts
from tests.support.human_concurrency import prepare
from tests.support.identity_assurance import configure
from tests.support.identity_tokens import token
from tests.support.postgres import finish_tasks, wait_until_blocked_by
from tests.support.work_details import assert_unchanged, command


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


async def test_mfa_policy_first_blocks_and_rejects_an_already_authenticated_weak_command(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await prepare(persistence_sessions)
    locked = asyncio.Event()
    release = asyncio.Event()
    writer_pid: int | None = None
    original_lock = assurance_administration.lock_organization

    async def hold_guard(session: AsyncSession, organization_ref: str, *, mode: LockMode) -> None:
        nonlocal writer_pid
        await original_lock(session, organization_ref, mode=mode)
        writer_pid = await session.scalar(text("SELECT pg_backend_pid()"))
        locked.set()
        await release.wait()

    monkeypatch.setattr(assurance_administration, "lock_organization", hold_guard)
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        context = await authenticated_context(identity, token(signing_key))
        engine = authenticated_kernel(persistence_sessions, identity)
        writer = asyncio.create_task(configure(persistence_sessions))
        tasks: list[asyncio.Task[Any]] = [writer]
        try:
            await asyncio.wait_for(locked.wait(), 10)
            assert writer_pid is not None
            execution = asyncio.create_task(engine.execute(context, command()))
            tasks.append(execution)
            await wait_until_blocked_by(persistence_sessions, writer_pid)
            assert not execution.done()
            release.set()
            await asyncio.wait_for(writer, 10)
            with pytest.raises(AuthenticationRejected, match="authentication_policy_changed"):
                await asyncio.wait_for(execution, 10)
        finally:
            await finish_tasks(release, tasks)
    await assert_unchanged(persistence_sessions)


async def test_command_first_commits_before_mfa_policy_change_then_old_context_cannot_recover(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    await prepare(persistence_sessions)
    locked = asyncio.Event()
    release = asyncio.Event()
    command_pid: int | None = None

    async def hold_command(
        uow: UnitOfWork, context: ExecutionContext, envelope: CommandEnvelope, payload: BaseModel
    ) -> CommandEffect:
        nonlocal command_pid
        effect = await update_work_details(uow, context, envelope, payload)
        command_pid = await uow.session.scalar(text("SELECT pg_backend_pid()"))
        locked.set()
        await release.wait()
        return effect

    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        context = await authenticated_context(identity, token(signing_key))
        engine = authenticated_kernel(persistence_sessions, identity, handler=hold_command)
        execution = asyncio.create_task(engine.execute(context, command()))
        tasks: list[asyncio.Task[Any]] = [execution]
        try:
            await asyncio.wait_for(locked.wait(), 10)
            assert command_pid is not None
            writer = asyncio.create_task(configure(persistence_sessions))
            tasks.append(writer)
            await wait_until_blocked_by(persistence_sessions, command_pid)
            assert not writer.done()
            release.set()
            committed = await asyncio.wait_for(execution, 10)
            assert committed.result["version"] == 2
            await asyncio.wait_for(writer, 10)
            with pytest.raises(AuthenticationRejected, match="authentication_policy_changed"):
                await engine.get_result(context, committed.execution_id)
            with pytest.raises(AuthenticationRejected, match="authentication_policy_changed"):
                await engine.execute(context, command())
        finally:
            await finish_tasks(release, tasks)
    assert await counts(persistence_sessions) == (2, 1, 1, 0)


async def test_policy_configuration_waits_for_trust_before_locking_organization(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await prepare(persistence_sessions)
    writer: asyncio.Task[None] | None = None
    try:
        async with persistence_sessions.begin() as blocker:
            await blocker.scalar(select(TrustStateTable).where(TrustStateTable.trust_ref == "trust").with_for_update())
            blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
            assert blocker_pid is not None
            writer = asyncio.create_task(configure(persistence_sessions))
            await wait_until_blocked_by(persistence_sessions, blocker_pid)
            async with persistence_sessions.begin() as observer:
                assert (
                    await observer.scalar(
                        select(TenantGuardTable).where(TenantGuardTable.tenant_ref == ORG).with_for_update(nowait=True)
                    )
                    is not None
                )
            assert not writer.done()
        await asyncio.wait_for(writer, 10)
    finally:
        if writer is not None:
            if not writer.done():
                writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
