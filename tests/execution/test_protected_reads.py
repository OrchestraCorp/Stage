"""The public read entry owns authorization, time checks, and its transaction."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.context import AuthorizationContext
from app.authz.permissions import Permission
from app.execution.contracts import ExecutionContext
from app.execution.errors import (
    AuthorizationDenied,
    ContextExpired,
    RequestDeadlineExceeded,
    TransactionOwnershipError,
)
from app.execution.kernel import CommandKernel
from app.models.work import WorkItemTable
from tests.support.command_kernel import TestFactsLoader as FixtureFactsLoader
from tests.support.command_kernel import context, counts, seed


async def test_public_read_returns_materialized_data_without_execution_or_audit(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(persistence_sessions)
    kernel = CommandKernel(persistence_sessions, loader=FixtureFactsLoader(), commands=())

    async def read_title(session: AsyncSession) -> str:
        item = await session.get(WorkItemTable, 1)
        assert item is not None
        return item.title

    assert await kernel.read(context(), action=Permission.TASK_VIEW, target_ref="1", reader=read_title) == "Fixture"
    assert await counts(persistence_sessions) == (1, 0, 0, 0)


async def test_denied_read_never_calls_business_reader(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(persistence_sessions)
    kernel = CommandKernel(persistence_sessions, loader=FixtureFactsLoader(), commands=())

    async def must_not_read(session: AsyncSession) -> None:
        pytest.fail("Unauthorized requests cannot load the business result")

    with pytest.raises(AuthorizationDenied):
        await kernel.read(
            replace(context(), actor_ref="missing-person", effective_subject_ref="missing-person"),
            action=Permission.TASK_VIEW,
            target_ref="1",
            reader=must_not_read,
        )


@pytest.mark.parametrize("expiry", ["credential", "deadline", "assignment", "relation"])
async def test_public_read_rechecks_temporal_facts_after_loading(
    persistence_sessions: async_sessionmaker[AsyncSession], expiry: str
) -> None:
    await seed(persistence_sessions)
    now = [datetime.now(UTC)]
    expires_at = now[0] + timedelta(seconds=1)
    request = context()
    if expiry == "credential":
        request = replace(request, credential_expires_at=expires_at)
    elif expiry == "deadline":
        request = replace(request, deadline=expires_at)

    class ExpiringLoader(FixtureFactsLoader):
        async def load(self, session: AsyncSession, context: ExecutionContext, **kwargs: Any) -> AuthorizationContext:
            facts = await super().load(session, context, **kwargs)
            if expiry == "assignment":
                assert facts.assignment is not None
                return replace(facts, assignment=replace(facts.assignment, expires_at=expires_at))
            if expiry == "relation":
                return replace(facts, relations=tuple(replace(r, expires_at=expires_at) for r in facts.relations))
            return facts

    async def delayed_read(session: AsyncSession) -> str:
        item = await session.get(WorkItemTable, 1)
        assert item is not None
        now[0] = expires_at
        return item.title

    kernel = CommandKernel(persistence_sessions, loader=ExpiringLoader(), commands=(), clock=lambda: now[0])
    error = {"credential": ContextExpired, "deadline": RequestDeadlineExceeded}.get(expiry, AuthorizationDenied)
    with pytest.raises(error):
        await kernel.read(request, action=Permission.TASK_VIEW, target_ref="1", reader=delayed_read)
    assert await counts(persistence_sessions) == (1, 0, 0, 0)


@pytest.mark.parametrize("finish", ["commit", "rollback"])
async def test_reader_cannot_end_transaction_even_if_it_swallows_the_error(
    persistence_sessions: async_sessionmaker[AsyncSession], finish: str
) -> None:
    await seed(persistence_sessions)

    async def invalid_reader(session: AsyncSession) -> str:
        item = await session.get(WorkItemTable, 1)
        assert item is not None
        item.title = "Must not persist"
        await session.flush()
        try:
            await getattr(session, finish)()
        except TransactionOwnershipError:
            pass
        return "Must not disclose"

    kernel = CommandKernel(persistence_sessions, loader=FixtureFactsLoader(), commands=())
    with pytest.raises(TransactionOwnershipError):
        await kernel.read(context(), action=Permission.TASK_VIEW, target_ref="1", reader=invalid_reader)
    async with persistence_sessions() as session:
        item = await session.get(WorkItemTable, 1)
        assert item is not None and item.title == "Fixture"


async def test_reader_fault_rolls_back_and_releases_transaction(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(persistence_sessions)

    async def broken_reader(session: AsyncSession) -> str:
        raise RuntimeError("fixture read failure")

    kernel = CommandKernel(persistence_sessions, loader=FixtureFactsLoader(), commands=())
    with pytest.raises(RuntimeError, match="fixture read failure"):
        await kernel.read(context(), action=Permission.TASK_VIEW, target_ref="1", reader=broken_reader)
    assert await counts(persistence_sessions) == (1, 0, 0, 0)
