"""Real PostgreSQL overlap and command-key recovery without a global writer queue."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.execution.contracts import CommandEffect, CommandEnvelope, ExecutionContext
from app.execution.errors import IdempotencyConflict, ObjectVersionConflict, TransactionOwnershipError
from app.execution.kernel import CommandKernel
from app.execution.storage import UnitOfWork
from app.features.work.details import WORK_DETAILS_COMMAND, update_work_details
from app.identity.authentication import AuthenticationRejected
from app.identity.service import IdentityDenied, IdentityService
from app.models.authorization import AuthorizationResourceTable, RelationTupleTable
from app.models.identity import PrincipalTable
from app.models.work import WorkItemTable
from app.storage.coordination import LockMode, lock_command_key, lock_organization
from tests.support.authenticated_commands import (
    authenticated_context,
    authenticated_kernel,
    identity_service,
    jwt_client,
    seed_authenticated_command,
)
from tests.support.authorization import ORG, PERSON, TARGET
from tests.support.database_commands import counts
from tests.support.identity_tokens import token
from tests.support.postgres import finish_tasks, wait_until_blocked_by
from tests.support.work_details import command


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
async def human(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> AsyncIterator[tuple[IdentityService, ExecutionContext]]:
    if persistence_sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("transaction overlap and blocking order require PostgreSQL")
    await seed_authenticated_command(persistence_sessions)
    async with persistence_sessions.begin() as session:
        session.add(WorkItemTable(id=2, title="Second", lifecycle="open", scope_ref="scope:engineering", version=1))
        await session.flush()
        session.add(
            AuthorizationResourceTable(
                organization_ref=ORG,
                resource_ref="work_item:2",
                resource_kind="work_item",
                scope_ref="scope:engineering",
            )
        )
        await session.flush()
        session.add(
            RelationTupleTable(
                subject_ref=PERSON,
                subject_type="human",
                relation="owner_of",
                resource_ref="work_item:2",
                organization_ref=ORG,
                relation_ref="relation:second",
                membership_ref="membership:alice",
                relation_type="owner_of",
                source_kind="direct_acl",
                source_ref="work_item:2",
                source_version=1,
                valid_from=datetime.now(UTC) - timedelta(days=1),
            )
        )
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        try:
            yield identity, await authenticated_context(identity, token(signing_key))
        finally:
            await identity.aclose()


@pytest.mark.parametrize("reading", [False, True])
async def test_independent_writes_and_same_object_reads_really_overlap(
    persistence_sessions: async_sessionmaker[AsyncSession],
    human: tuple[IdentityService, ExecutionContext],
    reading: bool,
) -> None:
    identity, context = human
    entered = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()

    async def handler(
        uow: UnitOfWork, ctx: ExecutionContext, cmd: CommandEnvelope, payload: BaseModel
    ) -> CommandEffect:
        effect = await update_work_details(uow, ctx, cmd, payload)
        entered[int(cmd.target_ref[-1]) - 1].set()
        await release.wait()
        return effect

    kernel = authenticated_kernel(persistence_sessions, identity, handler=handler)

    async def read(index: int) -> str:
        async def reader(session: AsyncSession) -> str:
            item = await session.get(WorkItemTable, 1)
            assert item is not None
            entered[index].set()
            await release.wait()
            return item.title

        return await kernel.read(context, action="task.view", target_ref=TARGET, reader=reader)

    tasks: list[asyncio.Task[Any]] = [
        asyncio.create_task(
            read(i)
            if reading
            else kernel.execute(context, command(target_ref=f"work_item:{i + 1}", idempotency_key=f"independent-{i}"))
        )
        for i in range(2)
    ]
    try:
        async with asyncio.timeout(10):
            await asyncio.gather(*(event.wait() for event in entered))
        assert all(not task.done() for task in tasks)
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
        assert (await counts(persistence_sessions))[1:3] == ((0, 0) if reading else (2, 2))
    finally:
        await finish_tasks(release, tasks)


@pytest.mark.parametrize(
    "second", ["replay", "payload", "target", "recover", "rollback", "cancel", "object-conflict", "uuid"]
)
async def test_same_key_retries_and_recovery_observe_the_previous_transaction(
    persistence_sessions: async_sessionmaker[AsyncSession], human: tuple[IdentityService, ExecutionContext], second: str
) -> None:
    identity, context = human
    entered, release = asyncio.Event(), asyncio.Event()
    pid: int | None = None
    calls = 0

    async def handler(
        uow: UnitOfWork, ctx: ExecutionContext, cmd: CommandEnvelope, payload: BaseModel
    ) -> CommandEffect:
        nonlocal pid, calls
        calls += 1
        effect = await update_work_details(uow, ctx, cmd, payload)
        if calls == 1:
            pid = await uow.session.scalar(text("SELECT pg_backend_pid()"))
            entered.set()
            await release.wait()
            if second == "rollback":
                raise RuntimeError("synthetic rollback")
        return effect

    kernel = authenticated_kernel(persistence_sessions, identity, handler=handler)
    first = asyncio.create_task(kernel.execute(context, command()))
    tasks: list[asyncio.Task[Any]] = [first]
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert pid is not None
        if second == "recover":
            operation = kernel.get_result_by_key(
                context,
                command_name=WORK_DETAILS_COMMAND.name,
                command_version=WORK_DETAILS_COMMAND.version,
                idempotency_key=command().idempotency_key,
                target_ref=TARGET,
            )
        elif second == "uuid":
            # The UUID path retains its existing organization-X wait contract.
            operation = kernel.get_result(context, "not-yet-known")
        else:
            changes: dict[str, Any] = {}
            if second == "payload":
                changes["payload"] = {"title": "different", "description": None}
            if second == "target":
                changes["target_ref"] = "work_item:2"
            if second == "object-conflict":
                changes["idempotency_key"] = "another-key"
            operation = kernel.execute(context, command(**changes))
        follower = asyncio.create_task(operation)
        tasks.append(follower)
        await wait_until_blocked_by(persistence_sessions, pid)
        assert not follower.done()
        if second == "cancel":
            follower.cancel()
            with pytest.raises(asyncio.CancelledError):
                await follower
        release.set()
        if second == "rollback":
            with pytest.raises(RuntimeError, match="synthetic rollback"):
                await first
            result = await asyncio.wait_for(follower, 10)
            assert not result.replayed
        else:
            result = await asyncio.wait_for(first, 10)
            if second in {"payload", "target"}:
                with pytest.raises(IdempotencyConflict):
                    await follower
            elif second == "object-conflict":
                with pytest.raises(ObjectVersionConflict):
                    await follower
            elif second == "uuid":
                from app.execution.errors import ExecutionNotFound

                with pytest.raises(ExecutionNotFound):
                    await follower
            elif second == "cancel":
                replay = await kernel.execute(context, command())
                assert replay.replayed and replay.execution_id == result.execution_id
            else:
                replay = await asyncio.wait_for(follower, 10)
                assert replay.replayed and replay.execution_id == result.execution_id
        assert await counts(persistence_sessions) == (2, 1, 1, 0)
        assert calls == (2 if second == "rollback" else 1)
    finally:
        await finish_tasks(release, tasks)


async def test_key_waiter_does_not_pin_organization_and_revalidates_after_revocation(
    persistence_sessions: async_sessionmaker[AsyncSession], human: tuple[IdentityService, ExecutionContext]
) -> None:
    identity, context = human
    kernel = authenticated_kernel(persistence_sessions, identity)
    release = asyncio.Event()
    tasks: list[asyncio.Task[Any]] = []
    try:
        async with persistence_sessions.begin() as holder:
            await lock_command_key(
                holder,
                organization_ref=ORG,
                actor_ref=PERSON,
                command_name=command().name,
                command_version=command().version,
                idempotency_key=command().idempotency_key,
            )
            pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            assert pid is not None
            follower = asyncio.create_task(kernel.execute(context, command()))
            tasks.append(follower)
            await wait_until_blocked_by(persistence_sessions, pid)
            # This would deadlock if the waiter held principal/org protection.
            async with asyncio.timeout(5), persistence_sessions.begin() as writer:
                person = await writer.scalar(
                    select(PrincipalTable).where(PrincipalTable.principal_ref == PERSON).with_for_update()
                )
                assert person is not None
                await lock_organization(writer, ORG, mode=LockMode.EXCLUSIVE)
                person.status, person.disabled_at = "revoked", datetime.now(UTC)
                person.version += 1
                person.identity_epoch += 1
        with pytest.raises((IdentityDenied, AuthenticationRejected)):
            await asyncio.wait_for(follower, 10)
        assert await counts(persistence_sessions) == (1, 0, 0, 0)
    finally:
        await finish_tasks(release, tasks)


async def test_nested_kernel_entry_is_rejected_before_waiting_on_its_own_locks(
    persistence_sessions: async_sessionmaker[AsyncSession], human: tuple[IdentityService, ExecutionContext]
) -> None:
    identity, context = human
    kernel: CommandKernel

    async def nested(uow: UnitOfWork, ctx: ExecutionContext, cmd: CommandEnvelope, payload: BaseModel) -> CommandEffect:
        await kernel.execute(ctx, command(idempotency_key="nested"))
        return await update_work_details(uow, ctx, cmd, payload)

    kernel = authenticated_kernel(persistence_sessions, identity, handler=nested)
    with pytest.raises(TransactionOwnershipError):
        await kernel.execute(context, command())
    assert await counts(persistence_sessions) == (1, 0, 0, 0)


@pytest.mark.parametrize(
    "kind,target",
    [
        ("assignment", "assignment:engineering"),
        ("relation", "relation:eligible"),
        ("role_version", "role:engineering"),
        ("scope", "scope:engineering"),
        ("resource", TARGET),
    ],
)
@pytest.mark.parametrize("writer_first", [False, True])
async def test_every_authorization_writer_remains_ordered_with_business_commit(
    persistence_sessions: async_sessionmaker[AsyncSession],
    human: tuple[IdentityService, ExecutionContext],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    target: str,
    writer_first: bool,
) -> None:
    from app.authz import administration
    from app.execution.errors import AuthorizationDenied

    identity, context = human
    entered, release = asyncio.Event(), asyncio.Event()
    pid: int | None = None
    original = administration.lock_organization

    async def hold_writer(session: AsyncSession, organization_ref: str, *, mode: LockMode) -> None:
        nonlocal pid
        await original(session, organization_ref, mode=mode)
        assert mode is LockMode.EXCLUSIVE
        if writer_first:
            pid = await session.scalar(text("SELECT pg_backend_pid()"))
            entered.set()
            await release.wait()

    async def handler(
        uow: UnitOfWork, ctx: ExecutionContext, cmd: CommandEnvelope, payload: BaseModel
    ) -> CommandEffect:
        nonlocal pid
        effect = await update_work_details(uow, ctx, cmd, payload)
        if not writer_first:
            pid = await uow.session.scalar(text("SELECT pg_backend_pid()"))
            entered.set()
            await release.wait()
        return effect

    monkeypatch.setattr(administration, "lock_organization", hold_writer)
    request = administration.AuthorizationRevocationRequest.model_validate(
        {
            "environment": "test",
            "organization_ref": ORG,
            "kind": kind,
            "target_ref": target,
            "expected_version": 1,
            "role_revision": 1 if kind == "role_version" else None,
        }
    )
    kernel = authenticated_kernel(persistence_sessions, identity, handler=handler)

    async def change() -> Any:
        return await administration.revoke_authorization(
            persistence_sessions, request, actor_ref="operator:concurrency", idempotency_key="revoke"
        )

    first = asyncio.create_task(change() if writer_first else kernel.execute(context, command()))
    tasks: list[asyncio.Task[Any]] = [first]
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert pid is not None
        following = asyncio.create_task(kernel.execute(context, command()) if writer_first else change())
        tasks.append(following)
        await wait_until_blocked_by(persistence_sessions, pid)
        release.set()
        await asyncio.wait_for(first, 10)
        if writer_first:
            with pytest.raises(AuthorizationDenied):
                await asyncio.wait_for(following, 10)
            assert await counts(persistence_sessions) == (1, 0, 0, 0)
        else:
            await asyncio.wait_for(following, 10)
            assert await counts(persistence_sessions) == (2, 1, 1, 0)
            with pytest.raises(AuthorizationDenied):
                await kernel.execute(context, command())
    finally:
        await finish_tasks(release, tasks)


@pytest.mark.parametrize("by_key", [False, True])
async def test_result_wait_expiry_is_not_reported_as_missing(
    persistence_sessions: async_sessionmaker[AsyncSession],
    human: tuple[IdentityService, ExecutionContext],
    by_key: bool,
) -> None:
    from app.execution.errors import RequestDeadlineExceeded

    identity, original = human
    now = [datetime.now(UTC)]
    context = replace(original, deadline=now[0] + timedelta(seconds=1))
    kernel = authenticated_kernel(persistence_sessions, identity, clock=lambda: now[0])
    release = asyncio.Event()
    tasks: list[asyncio.Task[Any]] = []
    try:
        async with persistence_sessions.begin() as holder:
            await lock_organization(holder, ORG, mode=LockMode.EXCLUSIVE)
            pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            assert pid is not None
            operation = (
                kernel.get_result_by_key(
                    context,
                    command_name=command().name,
                    command_version=command().version,
                    idempotency_key="missing",
                    target_ref=TARGET,
                )
                if by_key
                else kernel.get_result(context, "missing")
            )
            task = asyncio.create_task(operation)
            tasks.append(task)
            await wait_until_blocked_by(persistence_sessions, pid)
            now[0] += timedelta(seconds=2)
        with pytest.raises(RequestDeadlineExceeded):
            await asyncio.wait_for(task, 10)
    finally:
        await finish_tasks(release, tasks)
