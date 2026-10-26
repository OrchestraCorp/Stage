"""PostgreSQL serialization between verified human commands and local revocation."""

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
from app.identity import administration
from app.identity.administration import RevocationRequest, revoke
from app.identity.service import IdentityDenied
from app.models.execution import TenantGuardTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import TrustStateTable
from tests.support.authenticated_commands import (
    authenticated_context,
    authenticated_kernel,
    identity_service,
    jwt_client,
)
from tests.support.authorization import ORG, PERSON
from tests.support.database_commands import counts
from tests.support.human_concurrency import prepare
from tests.support.identity_tokens import token
from tests.support.postgres import finish_tasks, wait_until_blocked_by
from tests.support.work_details import assert_unchanged, command


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def profile_revocation() -> RevocationRequest:
    return RevocationRequest(
        environment="test",
        kind="profile",
        target_ref="local-human",
        expected_version=1,
        profile_version=1,
    )


async def test_profile_revocation_first_blocks_and_rejects_already_authenticated_command(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await prepare(persistence_sessions)
    locked = asyncio.Event()
    release = asyncio.Event()
    writer_pid: int | None = None
    original_lock = administration._lock_target

    async def hold_profile_lock(session: AsyncSession, request: RevocationRequest) -> Any:
        nonlocal writer_pid
        row = await original_lock(session, request)
        writer_pid = await session.scalar(text("SELECT pg_backend_pid()"))
        locked.set()
        await release.wait()
        return row

    monkeypatch.setattr(administration, "_lock_target", hold_profile_lock)
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        context = await authenticated_context(identity, token(signing_key))
        engine = authenticated_kernel(persistence_sessions, identity)
        writer = asyncio.create_task(
            revoke(persistence_sessions, profile_revocation(), actor_ref="operator", idempotency_key="profile-first")
        )
        tasks: list[asyncio.Task[Any]] = [writer]
        try:
            await asyncio.wait_for(locked.wait(), 10)
            assert writer_pid is not None
            execution = asyncio.create_task(engine.execute(context, command()))
            tasks.append(execution)
            await wait_until_blocked_by(persistence_sessions, writer_pid)
            assert not execution.done()
            release.set()
            assert (await asyncio.wait_for(writer, 10)).result["status"] == "revoked"
            with pytest.raises(IdentityDenied):
                await asyncio.wait_for(execution, 10)
        finally:
            await finish_tasks(release, tasks)
    await assert_unchanged(persistence_sessions)


async def test_command_first_holds_authentication_lock_through_commit_then_revocation_blocks_recovery(
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
            writer = asyncio.create_task(
                revoke(
                    persistence_sessions, profile_revocation(), actor_ref="operator", idempotency_key="command-first"
                )
            )
            tasks.append(writer)
            await wait_until_blocked_by(persistence_sessions, command_pid)
            assert not writer.done()
            release.set()
            committed = await asyncio.wait_for(execution, 10)
            assert committed.result["version"] == 2
            assert (await asyncio.wait_for(writer, 10)).result["status"] == "revoked"
            with pytest.raises(IdentityDenied):
                await engine.get_result(context, committed.execution_id)
            with pytest.raises(IdentityDenied):
                await engine.execute(context, command())
        finally:
            await finish_tasks(release, tasks)
    assert await counts(persistence_sessions) == (2, 1, 1, 0)


async def test_waiting_for_trust_does_not_acquire_principal_or_organization_locks_early(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    await prepare(persistence_sessions)
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        context = await authenticated_context(identity, token(signing_key))
        engine = authenticated_kernel(persistence_sessions, identity)
        execution: asyncio.Task[Any] | None = None
        try:
            async with persistence_sessions.begin() as blocker:
                await blocker.scalar(
                    select(TrustStateTable).where(TrustStateTable.trust_ref == "trust").with_for_update()
                )
                blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
                assert blocker_pid is not None
                execution = asyncio.create_task(engine.execute(context, command()))
                await wait_until_blocked_by(persistence_sessions, blocker_pid)
                # If the command took either later lock before trust, NOWAIT
                # fails here. This checks real ordering without query mocking.
                async with persistence_sessions.begin() as observer:
                    assert (
                        await observer.scalar(
                            select(PrincipalTable)
                            .where(PrincipalTable.principal_ref == PERSON)
                            .with_for_update(nowait=True)
                        )
                        is not None
                    )
                    assert (
                        await observer.scalar(
                            select(TenantGuardTable)
                            .where(TenantGuardTable.tenant_ref == ORG)
                            .with_for_update(nowait=True)
                        )
                        is not None
                    )
                assert not execution.done()
            assert (await asyncio.wait_for(execution, 10)).result["version"] == 2
        finally:
            if execution is not None:
                if not execution.done():
                    execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
    assert await counts(persistence_sessions) == (2, 1, 1, 0)
