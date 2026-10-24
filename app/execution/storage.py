"""Short command transactions with one commit owner and tenant serialization."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from app.execution.errors import ExecutionCleanupFailed, TenantNotProvisioned, TransactionOwnershipError
from app.models.execution import TenantGuardTable

logger = logging.getLogger(__name__)


@asynccontextmanager
async def managed_uow(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator["UnitOfWork"]:
    """Always attempt rollback and close, without hiding an uncertain commit."""
    session = sessions()
    uow: UnitOfWork | None = None
    primary_failed = False
    try:
        await session.begin()
        connection = await session.connection()
        sync_connection = connection.sync_connection
        assert sync_connection is not None
        uow = UnitOfWork(session, sync_connection)
        yield uow
    except BaseException:
        primary_failed = True
        raise
    finally:
        cleanup_failed = False
        if uow is not None:
            uow.detach()
        if session.in_transaction():
            try:
                await session.rollback()
            except BaseException as exc:
                cleanup_failed = True
                logger.warning("execution rollback cleanup failed type=%s", type(exc).__name__)
        try:
            await session.close()
        except BaseException as exc:
            cleanup_failed = True
            logger.warning("execution session close failed type=%s", type(exc).__name__)
        if cleanup_failed and not primary_failed:
            raise ExecutionCleanupFailed()


async def lock_tenant(session: AsyncSession, tenant_ref: str) -> None:
    """Lock an explicitly provisioned tenant; never create one from caller input.

    SQLite needs a write reservation because SELECT FOR UPDATE is ignored.
    MySQL/PostgreSQL use row locking. All relevant trusted writers must use this
    same guard; this helper cannot protect against arbitrary out-of-band SQL.
    """
    if session.get_bind().dialect.name == "sqlite":
        result = await session.execute(
            update(TenantGuardTable)
            .where(TenantGuardTable.tenant_ref == tenant_ref)
            .values(epoch=TenantGuardTable.epoch)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            raise TenantNotProvisioned()
    else:
        row = await session.scalar(
            select(TenantGuardTable).where(TenantGuardTable.tenant_ref == tenant_ref).with_for_update()
        )
        if row is None:
            raise TenantNotProvisioned()


class UnitOfWork:
    """A trusted handler may use the session and flush, but cannot end the root transaction.

    This is an accidental misuse guard, not a sandbox against hostile Python.
    Never expose this object or its session to WASM or untrusted plugins.
    """

    def __init__(self, session: AsyncSession, connection: Connection) -> None:
        self.session = session
        self._connection = connection
        self._owner_committing = False
        self._violated = False
        self._transaction = session.sync_session.get_transaction()
        self._commit_guard = self._before_commit
        self._rollback_guard = self._after_rollback
        self._connection_commit_guard = self._before_connection_commit
        self._connection_rollback_guard = self._before_connection_rollback
        event.listen(session.sync_session, "before_commit", self._commit_guard)
        event.listen(session.sync_session, "after_rollback", self._rollback_guard)
        event.listen(connection, "commit", self._connection_commit_guard)
        event.listen(connection, "rollback", self._connection_rollback_guard)

    def _before_commit(self, _session: Session) -> None:
        if not self._owner_committing:
            self._violated = True
            raise TransactionOwnershipError()

    def _after_rollback(self, _session: Session) -> None:
        self._violated = True

    def _before_connection_commit(self, _connection: Connection) -> None:
        if not self._owner_committing:
            self._violated = True
            raise TransactionOwnershipError()

    def _before_connection_rollback(self, _connection: Connection) -> None:
        if not self._owner_committing:
            self._violated = True
            # A failed ORM flush also needs to roll back. Allow safe rollback,
            # but reject any handler that catches the error and keeps working.

    def assert_owned(self) -> None:
        if self._violated or self.session.sync_session.get_transaction() is not self._transaction:
            raise TransactionOwnershipError()
        if not self.session.in_transaction() or not self.session.is_active:
            raise TransactionOwnershipError()

    async def flush(self) -> None:
        self.assert_owned()
        await self.session.flush()

    async def commit(self) -> None:
        self._violated = True
        raise TransactionOwnershipError()

    async def _commit_by_owner(self) -> None:
        self.assert_owned()
        self._owner_committing = True
        try:
            await self.session.commit()
        finally:
            self._owner_committing = False

    def detach(self) -> None:
        event.remove(self.session.sync_session, "before_commit", self._commit_guard)
        event.remove(self.session.sync_session, "after_rollback", self._rollback_guard)
        event.remove(self._connection, "commit", self._connection_commit_guard)
        event.remove(self._connection, "rollback", self._connection_rollback_guard)

    def add(self, instance: Any) -> None:
        self.assert_owned()
        self.session.add(instance)
