"""Observed PostgreSQL lock races for human security-fact commands."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.administration.errors import AdminDenied
from app.identity.authentication import AuthenticationAssuranceRequired
from app.models.authorization import RoleAssignmentTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import IdentityControlOperationTable
from app.storage.coordination import LockMode, lock_organization
from tests.support.authorization import ORG
from tests.support.human_administration import BOB, administration_case
from tests.support.human_administration import signing_key as signing_key
from tests.support.postgres import finish_tasks, wait_until_blocked_by


@pytest.fixture(autouse=True)
def postgres_only(persistence_sessions: async_sessionmaker[AsyncSession]) -> None:
    if persistence_sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("authoritative lock ordering and cancellation require PostgreSQL")


async def test_same_key_competitors_commit_one_assignment_and_one_human_journal(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        request = case.grant_request()
        tasks: list[asyncio.Task[Any]] = []
        release = asyncio.Event()
        try:
            async with persistence_sessions() as blocker, blocker.begin():
                blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
                assert blocker_pid is not None
                await lock_organization(blocker, ORG, mode=LockMode.EXCLUSIVE)
                tasks.append(asyncio.create_task(case.grant(request=request)))
                await wait_until_blocked_by(persistence_sessions, blocker_pid)
                async with persistence_sessions() as observer:
                    first_pid = await observer.scalar(
                        text("SELECT pid FROM pg_stat_activity WHERE :blocker = ANY(pg_blocking_pids(pid)) LIMIT 1"),
                        {"blocker": blocker_pid},
                    )
                assert first_pid is not None
                tasks.extend(asyncio.create_task(case.grant(request=request)) for _ in range(7))
                await wait_until_blocked_by(persistence_sessions, first_pid)
                assert all(not task.done() for task in tasks)
            results = await asyncio.wait_for(asyncio.gather(*tasks), 15)
            assert len({result.operation_ref for result in results}) == 1
            assert sum(not result.replayed for result in results) == 1
            async with persistence_sessions() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(RoleAssignmentTable)
                        .where(RoleAssignmentTable.principal_ref == BOB)
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(IdentityControlOperationTable)
                        .where(
                            IdentityControlOperationTable.actor_kind == "human",
                            IdentityControlOperationTable.idempotency_key == "grant-bob",
                        )
                    )
                    == 1
                )
        finally:
            await finish_tasks(release, tasks)


async def test_cancelled_waiter_releases_command_key_and_rolls_back_before_retry(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        request = case.grant_request()
        tasks: list[asyncio.Task[Any]] = []
        try:
            async with persistence_sessions() as blocker, blocker.begin():
                blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
                assert blocker_pid is not None
                await lock_organization(blocker, ORG, mode=LockMode.EXCLUSIVE)
                task = asyncio.create_task(case.grant(request=request))
                tasks.append(task)
                await wait_until_blocked_by(persistence_sessions, blocker_pid)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                async with persistence_sessions() as observer:
                    assert (
                        await observer.scalar(
                            select(func.count())
                            .select_from(RoleAssignmentTable)
                            .where(RoleAssignmentTable.principal_ref == BOB)
                        )
                        == 0
                    )
            result = await asyncio.wait_for(case.grant(request=request), 10)
            assert result.result.status == "active" and not result.replayed
        finally:
            await finish_tasks(asyncio.Event(), tasks)


async def test_original_key_recovery_waits_for_the_inflight_command(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        tasks: list[asyncio.Task[Any]] = []
        try:
            async with persistence_sessions() as blocker, blocker.begin():
                blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
                assert blocker_pid is not None
                await lock_organization(blocker, ORG, mode=LockMode.EXCLUSIVE)
                command = asyncio.create_task(case.grant())
                tasks.append(command)
                await wait_until_blocked_by(persistence_sessions, blocker_pid)
                async with persistence_sessions() as observer:
                    command_pid = await observer.scalar(
                        text("SELECT pid FROM pg_stat_activity WHERE :blocker = ANY(pg_blocking_pids(pid)) LIMIT 1"),
                        {"blocker": blocker_pid},
                    )
                assert command_pid is not None
                recovery = asyncio.create_task(
                    case.service.operation(
                        case.evidence, assignment_ref=case.assignment_ref, idempotency_key="grant-bob"
                    )
                )
                tasks.append(recovery)
                await wait_until_blocked_by(persistence_sessions, command_pid)
                assert not recovery.done()
            committed, recovered = await asyncio.wait_for(asyncio.gather(command, recovery), 10)
            assert committed.operation_ref == recovered.operation_ref and recovered.replayed
        finally:
            await finish_tasks(asyncio.Event(), tasks)


async def test_management_revocation_winning_the_lock_prevents_a_waiting_grant(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        tasks: list[asyncio.Task[Any]] = []
        try:
            async with persistence_sessions() as writer, writer.begin():
                writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
                assert writer_pid is not None
                await lock_organization(writer, ORG, mode=LockMode.EXCLUSIVE)
                assignment = await writer.scalar(
                    select(RoleAssignmentTable).where(RoleAssignmentTable.assignment_ref == case.assignment_ref)
                )
                assert assignment is not None
                assignment.status, assignment.revoked_at = "revoked", datetime.now(UTC)
                assignment.version += 1
                task = asyncio.create_task(case.grant())
                tasks.append(task)
                await wait_until_blocked_by(persistence_sessions, writer_pid)
            with pytest.raises(AdminDenied):
                await asyncio.wait_for(task, 10)
            async with persistence_sessions() as observer:
                assert (
                    await observer.scalar(
                        select(func.count())
                        .select_from(RoleAssignmentTable)
                        .where(RoleAssignmentTable.principal_ref == BOB)
                    )
                    == 0
                )
        finally:
            await finish_tasks(asyncio.Event(), tasks)


async def test_target_principal_revocation_is_locked_before_organization_and_rechecked(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        tasks: list[asyncio.Task[Any]] = []
        try:
            async with persistence_sessions() as writer, writer.begin():
                writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
                assert writer_pid is not None
                principal = await writer.scalar(
                    select(PrincipalTable).where(PrincipalTable.principal_ref == BOB).with_for_update()
                )
                assert principal is not None
                task = asyncio.create_task(case.grant())
                tasks.append(task)
                await wait_until_blocked_by(persistence_sessions, writer_pid)
                # A correct administrator is waiting on this principal without
                # owning organization X, so the credential writer can still
                # acquire its ordinary organization S protection.
                await asyncio.wait_for(lock_organization(writer, ORG, mode=LockMode.SHARED), 5)
                principal.status, principal.disabled_at = "revoked", datetime.now(UTC)
                principal.identity_epoch += 1
                principal.version += 1
            with pytest.raises(AdminDenied):
                await asyncio.wait_for(task, 10)
        finally:
            await finish_tasks(asyncio.Event(), tasks)


async def test_waiting_for_organization_does_not_extend_recent_mfa_deadline(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        case.evidence = await case.authenticate(auth_time=int((datetime.now(UTC) - timedelta(seconds=299)).timestamp()))
        assert case.evidence.credential.authenticated_at is not None
        deadline = case.evidence.credential.authenticated_at + timedelta(seconds=300)
        tasks: list[asyncio.Task[Any]] = []
        try:
            async with persistence_sessions() as blocker, blocker.begin():
                blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
                assert blocker_pid is not None
                await lock_organization(blocker, ORG, mode=LockMode.EXCLUSIVE)
                task = asyncio.create_task(case.grant())
                tasks.append(task)
                await wait_until_blocked_by(persistence_sessions, blocker_pid)
                await asyncio.sleep(max(0, (deadline - datetime.now(UTC)).total_seconds()) + 0.02)
            with pytest.raises(AuthenticationAssuranceRequired):
                await asyncio.wait_for(task, 10)
        finally:
            await finish_tasks(asyncio.Event(), tasks)
