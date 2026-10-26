"""Kernel boundaries reject invalid proofs and nested tasks without leaking context."""

import asyncio
from dataclasses import replace

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.permissions import Permission
from app.execution.contracts import CommandEffect, CommandEnvelope, ExecutionContext
from app.execution.errors import TransactionOwnershipError
from app.execution.kernel import CommandKernel
from app.execution.storage import UnitOfWork
from app.models.work import WorkItemTable
from tests.support.command_kernel import TestFactsLoader as FixtureFactsLoader
from tests.support.command_kernel import claim, command, context, counts, seed, spec


@pytest.mark.parametrize("through_connection", [False, True])
async def test_handler_cannot_commit_after_swallowing_rejected_savepoint(
    persistence_sessions: async_sessionmaker[AsyncSession], through_connection: bool
) -> None:
    await seed(persistence_sessions)

    async def swallowing_handler(
        uow: UnitOfWork, ctx: ExecutionContext, envelope: CommandEnvelope, payload: BaseModel
    ) -> CommandEffect:
        effect = await claim(uow, ctx, envelope, payload)
        owner = await uow.session.connection() if through_connection else uow.session
        with pytest.raises(TransactionOwnershipError):
            await owner.begin_nested()
        return effect

    kernel = CommandKernel(persistence_sessions, loader=FixtureFactsLoader(), commands=[spec(swallowing_handler)])
    with pytest.raises(TransactionOwnershipError):
        await kernel.execute(context(), command())
    assert await counts(persistence_sessions) == (1, 0, 0, 0)


@pytest.mark.parametrize("through_connection", [False, True])
async def test_reader_cannot_disclose_after_swallowing_rejected_savepoint(
    persistence_sessions: async_sessionmaker[AsyncSession], through_connection: bool
) -> None:
    await seed(persistence_sessions)

    async def swallowing_reader(session: AsyncSession) -> str:
        item = await session.get(WorkItemTable, 1)
        assert item is not None
        materialized = item.title
        owner = await session.connection() if through_connection else session
        with pytest.raises(TransactionOwnershipError):
            await owner.begin_nested()
        return materialized

    kernel = CommandKernel(persistence_sessions, loader=FixtureFactsLoader(), commands=[])
    with pytest.raises(TransactionOwnershipError):
        await kernel.read(context(), action=Permission.TASK_VIEW, target_ref="1", reader=swallowing_reader)
    assert await counts(persistence_sessions) == (1, 0, 0, 0)


async def test_handler_child_task_cannot_start_another_kernel_transaction(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(persistence_sessions)
    other_kernel = CommandKernel(persistence_sessions, loader=FixtureFactsLoader(), commands=[spec()])

    async def nested_handler(
        uow: UnitOfWork, ctx: ExecutionContext, envelope: CommandEnvelope, payload: BaseModel
    ) -> CommandEffect:
        child = asyncio.create_task(other_kernel.execute(ctx, replace(envelope, idempotency_key="nested-key")))
        # The independent session would otherwise wait on its own ancestor's
        # object lock (and on SQLite its write reservation).
        await asyncio.wait_for(child, 5)
        return await claim(uow, ctx, envelope, payload)

    kernel = CommandKernel(persistence_sessions, loader=FixtureFactsLoader(), commands=[spec(nested_handler)])
    with pytest.raises(TransactionOwnershipError):
        await kernel.execute(context(), command())
    assert await counts(persistence_sessions) == (1, 0, 0, 0)
    # A fresh operation in the outside task must remain usable after rejection.
    assert (await other_kernel.execute(context(), command())).result == {"version": 2}
    assert await counts(persistence_sessions) == (2, 1, 1, 1)


@pytest.mark.parametrize("cancel", [False, True])
async def test_same_task_can_start_fresh_operation_after_exception_or_cancellation(
    persistence_sessions: async_sessionmaker[AsyncSession], cancel: bool
) -> None:
    await seed(persistence_sessions)
    first = True
    owner_task = asyncio.current_task()
    assert owner_task is not None
    previous_cancellations = owner_task.cancelling()

    async def fail_once(
        uow: UnitOfWork, ctx: ExecutionContext, envelope: CommandEnvelope, payload: BaseModel
    ) -> CommandEffect:
        nonlocal first
        effect = await claim(uow, ctx, envelope, payload)
        if first:
            first = False
            if cancel:
                owner_task.cancel()
                await asyncio.sleep(0)
            raise RuntimeError("synthetic handler failure")
        return effect

    kernel = CommandKernel(persistence_sessions, loader=FixtureFactsLoader(), commands=[spec(fail_once)])
    try:
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await kernel.execute(context(), command())
    finally:
        if cancel and owner_task.cancelling() > previous_cancellations:
            owner_task.uncancel()
    assert await counts(persistence_sessions) == (1, 0, 0, 0)
    assert (await kernel.execute(context(), command())).result == {"version": 2}
    assert await counts(persistence_sessions) == (2, 1, 1, 1)


async def test_external_sibling_tasks_do_not_share_kernel_entry_state(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(persistence_sessions)
    kernel = CommandKernel(persistence_sessions, loader=FixtureFactsLoader(), commands=[])

    async def reader(session: AsyncSession) -> str:
        item = await session.get(WorkItemTable, 1)
        assert item is not None
        return item.title

    calls = [
        kernel.read(context(), action=Permission.TASK_VIEW, target_ref="1", reader=reader),
        kernel.read(context(), action=Permission.TASK_VIEW, target_ref="1", reader=reader),
    ]
    assert await asyncio.gather(*calls) == ["Fixture", "Fixture"]
    assert await counts(persistence_sessions) == (1, 0, 0, 0)
