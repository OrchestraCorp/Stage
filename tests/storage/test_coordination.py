"""Lock protocol lifecycle plus PostgreSQL-observed blocking relationships."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.control.operations import acquire_admin_lock
from app.execution.errors import TenantNotProvisioned, TransactionOwnershipError
from app.models.execution import TenantGuardTable
from app.storage.coordination import (
    LockMode,
    _advisory_key,
    _organization_key,
    assert_organization_lock,
    lock_command_key,
    lock_organization,
)
from tests.support.postgres import wait_until_blocked_by

ORG = "organization:coordination"
OTHER_ORG = "organization:other"
KEY = {
    "organization_ref": ORG,
    "actor_ref": "human:one",
    "command_name": "work.update-details",
    "command_version": "1",
    "idempotency_key": "request:one",
}


@pytest.fixture
async def sessions(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with persistence_sessions.begin() as session:
        session.add_all([TenantGuardTable(tenant_ref=ORG), TenantGuardTable(tenant_ref=OTHER_ORG)])
    yield persistence_sessions


@pytest.fixture
async def pg_sessions(
    sessions: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    async with sessions() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires actual PostgreSQL row/advisory lock semantics")
    return sessions


@pytest.mark.parametrize("mode", list(LockMode))
async def test_explicit_modes_allow_repeated_reads_without_upgrading(
    sessions: async_sessionmaker[AsyncSession], mode: LockMode
) -> None:
    async with sessions.begin() as session:
        # A prior read may start SQLAlchemy's transaction before SQLite obtains
        # a physical write reservation; neither initial nor repeated calls fail.
        assert await session.scalar(select(TenantGuardTable.epoch).where(TenantGuardTable.tenant_ref == ORG)) == 0
        await lock_organization(session, ORG, mode=mode)
        await lock_organization(session, ORG, mode=mode)
        await lock_organization(session, ORG, mode=LockMode.SHARED)
        assert_organization_lock(session, ORG)
        if mode is LockMode.EXCLUSIVE:
            assert_organization_lock(session, ORG, mode=LockMode.EXCLUSIVE)
        else:
            with pytest.raises(TransactionOwnershipError):
                await lock_organization(session, ORG, mode=LockMode.EXCLUSIVE)
            with pytest.raises(TransactionOwnershipError):
                assert_organization_lock(session, ORG, mode=LockMode.EXCLUSIVE)
        assert await session.scalar(select(TenantGuardTable.epoch).where(TenantGuardTable.tenant_ref == ORG)) == 0


async def test_unprovisioned_organization_cannot_publish_proof(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions.begin() as session:
        with pytest.raises(TenantNotProvisioned):
            await lock_organization(session, "missing", mode=LockMode.SHARED)
        with pytest.raises(TransactionOwnershipError):
            assert_organization_lock(session, "missing")
        with pytest.raises(TransactionOwnershipError):
            await lock_organization(session, ORG, mode=LockMode.EXCLUSIVE)


async def test_marker_copy_cannot_grant_proof_or_change_organization(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions.begin() as session:
        session.info["organization_lock"] = {"organization_ref": ORG, "mode": LockMode.EXCLUSIVE}
        with pytest.raises(TransactionOwnershipError):
            assert_organization_lock(session, ORG)
        await lock_organization(session, ORG, mode=LockMode.SHARED)
        with pytest.raises(TransactionOwnershipError):
            assert_organization_lock(session, OTHER_ORG)
        with pytest.raises(TransactionOwnershipError):
            await lock_organization(session, OTHER_ORG, mode=LockMode.SHARED)


@pytest.mark.parametrize("finish", ["commit", "rollback", "close"])
async def test_proof_never_survives_root_transaction_reuse(
    sessions: async_sessionmaker[AsyncSession], finish: str
) -> None:
    async with sessions() as session:
        await session.begin()
        await lock_organization(session, ORG, mode=LockMode.SHARED)
        await getattr(session, finish)()
        with pytest.raises(TransactionOwnershipError):
            assert_organization_lock(session, ORG)
        await session.begin()
        with pytest.raises(TransactionOwnershipError):
            assert_organization_lock(session, ORG)
        await lock_organization(session, OTHER_ORG, mode=LockMode.EXCLUSIVE)
        assert_organization_lock(session, OTHER_ORG, mode=LockMode.EXCLUSIVE)


async def test_lock_and_assert_require_active_transaction_and_typed_mode(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        with pytest.raises(TransactionOwnershipError):
            await lock_organization(session, ORG, mode=LockMode.SHARED)
        with pytest.raises(TransactionOwnershipError):
            await lock_command_key(session, **KEY)
        await session.begin()
        with pytest.raises(TransactionOwnershipError):
            await lock_organization(session, ORG, mode=cast(LockMode, "shared"))


@pytest.mark.parametrize("through_connection", [False, True])
async def test_savepoint_after_lock_invalidates_proof(
    sessions: async_sessionmaker[AsyncSession], through_connection: bool
) -> None:
    async with sessions() as session:
        await session.begin()
        await lock_organization(session, ORG, mode=LockMode.SHARED)
        owner = await session.connection() if through_connection else session
        with pytest.raises(TransactionOwnershipError):
            await owner.begin_nested()
        with pytest.raises(TransactionOwnershipError):
            assert_organization_lock(session, ORG)
        with pytest.raises(TransactionOwnershipError):
            await lock_organization(session, ORG, mode=LockMode.SHARED)
        await session.rollback()


@pytest.mark.parametrize("through_connection", [False, True])
async def test_lock_cannot_be_acquired_inside_existing_savepoint(
    sessions: async_sessionmaker[AsyncSession], through_connection: bool
) -> None:
    async with sessions.begin() as session:
        owner = await session.connection() if through_connection else session
        nested = await owner.begin_nested()
        with pytest.raises(TransactionOwnershipError):
            await lock_organization(session, ORG, mode=LockMode.SHARED)
        with pytest.raises(TransactionOwnershipError):
            await lock_command_key(session, **KEY)
        await nested.rollback()
        await lock_organization(session, ORG, mode=LockMode.SHARED)
        assert_organization_lock(session, ORG)


async def test_direct_connection_rollback_invalidates_proof(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        await session.begin()
        await lock_organization(session, ORG, mode=LockMode.SHARED)
        connection = await session.connection()
        await connection.rollback()
        with pytest.raises(TransactionOwnershipError):
            assert_organization_lock(session, ORG)
        with pytest.raises(TransactionOwnershipError):
            await lock_organization(session, ORG, mode=LockMode.SHARED)
        await session.close()


async def test_command_key_precedes_organization_and_only_one_key_is_permitted(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions.begin() as session:
        await lock_command_key(session, **KEY)
        await lock_command_key(session, **KEY)
        with pytest.raises(TransactionOwnershipError):
            assert_organization_lock(session, ORG)
        with pytest.raises(TransactionOwnershipError):
            await lock_command_key(session, **{**KEY, "idempotency_key": "another"})
        with pytest.raises(TransactionOwnershipError):
            await lock_organization(session, OTHER_ORG, mode=LockMode.SHARED)
        await lock_organization(session, ORG, mode=LockMode.SHARED)
        await lock_command_key(session, **KEY)
        assert_organization_lock(session, ORG)
    async with sessions.begin() as session:
        await lock_organization(session, ORG, mode=LockMode.SHARED)
        with pytest.raises(TransactionOwnershipError):
            await lock_command_key(session, **KEY)


async def test_sqlite_reservation_is_compatible_with_explicit_begin_immediate(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions.begin() as session:
        if session.get_bind().dialect.name != "sqlite":
            pytest.skip("SQLite explicit write reservation compatibility")
        await session.execute(text("BEGIN IMMEDIATE"))
        await lock_command_key(session, **KEY)
        await lock_organization(session, ORG, mode=LockMode.SHARED)
        await lock_organization(session, ORG, mode=LockMode.SHARED)
        assert_organization_lock(session, ORG)


def test_command_key_encoding_is_frozen_and_tuple_boundaries_are_unambiguous() -> None:
    assert _advisory_key(("organization:coordination", "human:one", "work.update-details", "1", "request:one")) == (
        2119461308,
        -1676307389,
    )
    assert _advisory_key(("a|b", "c", "d", "e", "f")) != _advisory_key(("a", "b|c", "d", "e", "f"))
    assert _advisory_key(("a", "b", "c", "d", "é")) != _advisory_key(("a", "b", "c", "d", "e\u0301"))


async def test_postgresql_shared_holders_overlap_and_exclusive_waits_for_both(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    writer: asyncio.Task[None] | None = None

    async def acquire_writer() -> None:
        async with pg_sessions.begin() as session:
            await lock_organization(session, ORG, mode=LockMode.EXCLUSIVE)

    try:
        async with pg_sessions.begin() as first:
            await lock_organization(first, ORG, mode=LockMode.SHARED)
            first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
            assert first_pid is not None
            async with pg_sessions.begin() as second:
                async with asyncio.timeout(5):
                    await lock_organization(second, ORG, mode=LockMode.SHARED)
                second_pid = await second.scalar(text("SELECT pg_backend_pid()"))
                assert second_pid is not None and second_pid != first_pid
                assert_organization_lock(first, ORG)
                assert_organization_lock(second, ORG)
                writer = asyncio.create_task(acquire_writer())
                await wait_until_blocked_by(pg_sessions, first_pid)
                assert not writer.done()
            await wait_until_blocked_by(pg_sessions, first_pid)
            assert not writer.done()
        await asyncio.wait_for(writer, 5)
    finally:
        if writer is not None:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)


async def test_postgresql_exclusive_blocks_readers_but_different_organization_proceeds(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    reader: asyncio.Task[None] | None = None

    async def acquire_reader() -> None:
        async with pg_sessions.begin() as session:
            await lock_organization(session, ORG, mode=LockMode.SHARED)

    try:
        async with pg_sessions.begin() as writer:
            await lock_organization(writer, ORG, mode=LockMode.EXCLUSIVE)
            writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
            assert writer_pid is not None
            reader = asyncio.create_task(acquire_reader())
            await wait_until_blocked_by(pg_sessions, writer_pid)
            async with pg_sessions.begin() as unrelated:
                async with asyncio.timeout(5):
                    await lock_organization(unrelated, OTHER_ORG, mode=LockMode.EXCLUSIVE)
            assert not reader.done()
        await asyncio.wait_for(reader, 5)
    finally:
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


@pytest.mark.parametrize("kind", ["organization", "command"])
async def test_postgresql_wait_cancellation_never_publishes_proof_and_releases_locks(
    pg_sessions: async_sessionmaker[AsyncSession], kind: str
) -> None:
    waiter: asyncio.Task[None] | None = None

    async def acquire(session: AsyncSession) -> None:
        if kind == "command":
            await lock_command_key(session, **KEY)
        else:
            await lock_organization(session, ORG, mode=LockMode.SHARED)

    async with pg_sessions() as waiting_session:
        await waiting_session.begin()
        try:
            async with pg_sessions.begin() as blocker:
                if kind == "command":
                    await lock_command_key(blocker, **KEY)
                else:
                    await lock_organization(blocker, ORG, mode=LockMode.EXCLUSIVE)
                blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
                assert blocker_pid is not None
                waiter = asyncio.create_task(acquire(waiting_session))
                await wait_until_blocked_by(pg_sessions, blocker_pid)
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
                with pytest.raises(TransactionOwnershipError):
                    assert_organization_lock(waiting_session, ORG)
                with pytest.raises(TransactionOwnershipError):
                    await acquire(waiting_session)
                await waiting_session.rollback()
            async with waiting_session.begin():
                async with asyncio.timeout(5):
                    await lock_command_key(waiting_session, **KEY)
                    await lock_organization(waiting_session, ORG, mode=LockMode.SHARED)
                assert_organization_lock(waiting_session, ORG)
        finally:
            if waiter is not None:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)


async def test_postgresql_same_key_waits_while_other_keys_and_bigint_space_proceed(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    waiter: asyncio.Task[None] | None = None

    async def same_key() -> None:
        async with pg_sessions.begin() as session:
            await lock_command_key(session, **KEY)

    try:
        async with pg_sessions.begin() as owner:
            await lock_command_key(owner, **KEY)
            owner_pid = await owner.scalar(text("SELECT pg_backend_pid()"))
            assert owner_pid is not None
            waiter = asyncio.create_task(same_key())
            await wait_until_blocked_by(pg_sessions, owner_pid)
            async with pg_sessions.begin() as other:
                async with asyncio.timeout(5):
                    await lock_command_key(other, **{**KEY, "idempotency_key": "request:other"})
                    first, second = _advisory_key(cast(Any, tuple(KEY.values())))
                    bigint_key = ((first & 0xFFFFFFFF) << 32) | (second & 0xFFFFFFFF)
                    if bigint_key >= 1 << 63:
                        bigint_key -= 1 << 64
                    assert await other.scalar(
                        text("SELECT pg_try_advisory_xact_lock(CAST(:key AS bigint))"), {"key": bigint_key}
                    )
                    await lock_organization(other, ORG, mode=LockMode.SHARED)
            assert not waiter.done()
        await asyncio.wait_for(waiter, 5)
    finally:
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)


def test_organization_gate_encoding_is_stable_and_uses_reserved_bigint_prefix() -> None:
    key = _organization_key(ORG)
    assert key == 6004502608442815287
    assert key >> 32 == int.from_bytes(b"STGO", signed=False)
    assert 0 < key < 2**63
    assert _organization_key(OTHER_ORG) != key


async def test_postgresql_waiting_organization_writer_precedes_later_shared_reader(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    release_writer, writer_entered = asyncio.Event(), asyncio.Event()
    writer_pid: int | None = None
    writer: asyncio.Task[None] | None = None
    reader: asyncio.Task[None] | None = None

    async def writing() -> None:
        nonlocal writer_pid
        async with pg_sessions.begin() as session:
            writer_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            await lock_organization(session, ORG, mode=LockMode.EXCLUSIVE)
            writer_entered.set()
            await release_writer.wait()

    async def reading() -> None:
        async with pg_sessions.begin() as session:
            await lock_organization(session, ORG, mode=LockMode.SHARED)

    try:
        async with pg_sessions.begin() as first:
            await lock_organization(first, ORG, mode=LockMode.SHARED)
            first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
            assert first_pid is not None
            writer = asyncio.create_task(writing())
            await wait_until_blocked_by(pg_sessions, first_pid)
            assert writer_pid is not None
            reader = asyncio.create_task(reading())
            # PostgreSQL exposes a soft blocking edge from the later reader to
            # the already queued exclusive gate waiter, even though S/S is safe.
            await wait_until_blocked_by(pg_sessions, writer_pid)
            assert not reader.done() and not writer_entered.is_set()
        await asyncio.wait_for(writer_entered.wait(), 5)
        await wait_until_blocked_by(pg_sessions, writer_pid)
        assert not reader.done()
        release_writer.set()
        await asyncio.wait_for(asyncio.gather(writer, reader), 5)
    finally:
        release_writer.set()
        for task in (writer, reader):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (writer, reader) if task is not None), return_exceptions=True)


async def test_postgresql_cancelling_queued_writer_releases_later_gate_readers(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    writer: asyncio.Task[None] | None = None
    reader: asyncio.Task[None] | None = None
    async with pg_sessions.begin() as first, pg_sessions() as writer_session, pg_sessions() as reader_session:
        try:
            await lock_organization(first, ORG, mode=LockMode.SHARED)
            first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
            writer_pid = await writer_session.scalar(text("SELECT pg_backend_pid()"))
            assert first_pid is not None and writer_pid is not None
            writer = asyncio.create_task(lock_organization(writer_session, ORG, mode=LockMode.EXCLUSIVE))
            await wait_until_blocked_by(pg_sessions, first_pid)
            await reader_session.begin()
            reader = asyncio.create_task(lock_organization(reader_session, ORG, mode=LockMode.SHARED))
            await wait_until_blocked_by(pg_sessions, writer_pid)
            assert not reader.done()
            writer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await writer
            with pytest.raises(TransactionOwnershipError):
                assert_organization_lock(writer_session, ORG)
            await writer_session.rollback()
            # The original shared holder stays active: cancellation removed the
            # only incompatible waiter, so the later shared reader can enter.
            await asyncio.wait_for(reader, 5)
            assert_organization_lock(first, ORG)
            assert_organization_lock(reader_session, ORG)
        finally:
            for task in (writer, reader):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(task for task in (writer, reader) if task is not None), return_exceptions=True)


async def test_postgresql_cancel_after_gate_before_row_does_not_publish_proof(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    waiter: asyncio.Task[None] | None = None
    async with pg_sessions.begin() as blocker, pg_sessions() as waiting_session:
        try:
            # Deliberately model a row-only holder to exercise the narrow gap
            # between gate admission and row protection, not an allowed writer.
            await blocker.scalar(select(TenantGuardTable).where(TenantGuardTable.tenant_ref == ORG).with_for_update())
            blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
            assert blocker_pid is not None
            await waiting_session.begin()
            waiter = asyncio.create_task(lock_organization(waiting_session, ORG, mode=LockMode.SHARED))
            await wait_until_blocked_by(pg_sessions, blocker_pid)
            async with pg_sessions.begin() as observer:
                assert not await observer.scalar(
                    text("SELECT pg_try_advisory_xact_lock(CAST(:key AS bigint))"), {"key": _organization_key(ORG)}
                )
            with pytest.raises(TransactionOwnershipError):
                assert_organization_lock(waiting_session, ORG)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            await waiting_session.rollback()
            with pytest.raises(TransactionOwnershipError):
                assert_organization_lock(waiting_session, ORG)
            async with pg_sessions.begin() as observer:
                assert await observer.scalar(
                    text("SELECT pg_try_advisory_xact_lock(CAST(:key AS bigint))"), {"key": _organization_key(ORG)}
                )
        finally:
            if waiter is not None:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)


async def test_postgresql_organization_gate_namespace_is_separate_from_actual_control_mutex(
    pg_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with pg_sessions.begin() as control:
        await acquire_admin_lock(control)
        namespaces = (
            await control.scalars(
                text(
                    "SELECT classid::bigint FROM pg_locks "
                    "WHERE locktype='advisory' AND objsubid=1 AND pid=pg_backend_pid()"
                )
            )
        ).all()
        assert namespaces and int.from_bytes(b"STGO", signed=False) not in namespaces
        async with pg_sessions.begin() as reader:
            async with asyncio.timeout(5):
                await lock_organization(reader, ORG, mode=LockMode.SHARED)
            assert_organization_lock(reader, ORG)
