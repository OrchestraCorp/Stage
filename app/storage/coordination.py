"""Transaction-bound authorization barriers and short-command key mutexes.

Organization admission uses a PostgreSQL transaction advisory gate before the
existing shared/exclusive guard row. Waiting writers stop later gate readers
from repeatedly joining a row MultiXact ahead of them. This does not promise
FIFO scheduling or change principal/trust row-lock admission. Command keys
precede the trust/principal/organization chain; neither lock is a durable lease.
Organization bigint keys reserve the STGO prefix, separate from the existing
control mutex; command keys use PostgreSQL's disjoint two-integer namespace.
SQLite deliberately retains a database write reservation for both modes.

These checks guard accidental misuse in trusted Python; they are not a sandbox.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from weakref import WeakKeyDictionary

from sqlalchemy import event, select, text, update
from sqlalchemy.engine import Connection, Transaction
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from app.execution.errors import TenantNotProvisioned, TransactionOwnershipError
from app.models.execution import TenantGuardTable


class LockMode(StrEnum):
    SHARED = "shared"
    EXCLUSIVE = "exclusive"


CommandKey = tuple[str, str, str, str, str]


@dataclass
class _Protection:
    connection: Connection
    connection_transaction: Transaction
    organization_ref: str | None = None
    mode: LockMode | None = None
    command_key: CommandKey | None = None
    violated: bool = False

    def reject_savepoint(self, _connection: Connection, _name: str | None) -> None:
        self.violated = True
        raise TransactionOwnershipError()


# Root transaction identity is the authority. Session.info is intentionally not
# used: copying an application marker cannot grant a lock or revive old proof.
_protections: WeakKeyDictionary[SessionTransaction, _Protection] = WeakKeyDictionary()


@event.listens_for(Session, "after_transaction_create")
def _reject_nested_transaction(_session: Session, transaction: SessionTransaction) -> None:
    if not transaction.nested:
        return
    root = transaction
    while root.parent is not None:
        root = root.parent
    protection = _protections.get(root)
    if protection is not None:
        protection.violated = True
        raise TransactionOwnershipError()


@event.listens_for(Session, "after_transaction_end")
def _release_proof(_session: Session, transaction: SessionTransaction) -> None:
    protection = _protections.pop(transaction, None)
    if protection is not None:
        event.remove(protection.connection, "savepoint", protection.reject_savepoint)


def _root_transaction(session: AsyncSession) -> SessionTransaction:
    root = session.sync_session.get_transaction()
    if root is None or not root.is_active or not session.is_active or session.in_nested_transaction():
        raise TransactionOwnershipError()
    return root


def _assert_connection(protection: _Protection) -> None:
    if (
        protection.violated
        or protection.connection.closed
        or protection.connection.get_transaction() is not protection.connection_transaction
        or protection.connection.in_nested_transaction()
    ):
        raise TransactionOwnershipError()


async def _protection(session: AsyncSession) -> _Protection:
    root = _root_transaction(session)
    protection = _protections.get(root)
    if protection is None:
        connection = (await session.connection()).sync_connection
        if connection is None:
            raise TransactionOwnershipError()
        transaction = connection.get_transaction()
        if transaction is None or not transaction.is_active or connection.in_nested_transaction():
            raise TransactionOwnershipError()
        protection = _Protection(connection, transaction)
        event.listen(connection, "savepoint", protection.reject_savepoint)
        _protections[root] = protection
    _assert_connection(protection)
    return protection


def _check_mode(mode: LockMode) -> None:
    if not isinstance(mode, LockMode):
        raise TransactionOwnershipError()


async def _sqlite_reservation(session: AsyncSession, organization_ref: str) -> None:
    # An UPDATE starts the physical write transaction even if a prior SELECT
    # only started SQLAlchemy's logical transaction. It is safe after BEGIN
    # IMMEDIATE and on repeated calls, unlike another explicit BEGIN.
    result = await session.execute(
        update(TenantGuardTable)
        .where(TenantGuardTable.tenant_ref == organization_ref)
        .values(epoch=TenantGuardTable.epoch)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise TenantNotProvisioned()


async def lock_organization(session: AsyncSession, organization_ref: str, *, mode: LockMode) -> None:
    """Protect an existing organization; never upgrade SHARED within a transaction."""
    _check_mode(mode)
    protection = await _protection(session)
    if protection.organization_ref not in (None, organization_ref):
        raise TransactionOwnershipError()
    if protection.mode is not None:
        assert_organization_lock(session, organization_ref, mode=mode)
        return
    protection.organization_ref = organization_ref
    try:
        if session.get_bind().dialect.name == "sqlite":
            await _sqlite_reservation(session, organization_ref)
        else:
            gate = (
                "SELECT pg_advisory_xact_lock_shared(CAST(:organization_key AS bigint))"
                if mode is LockMode.SHARED
                else "SELECT pg_advisory_xact_lock(CAST(:organization_key AS bigint))"
            )
            await session.execute(text(gate), {"organization_key": _organization_key(organization_ref)})
            row = await session.scalar(
                select(TenantGuardTable.tenant_ref)
                .where(TenantGuardTable.tenant_ref == organization_ref)
                .with_for_update(read=mode is LockMode.SHARED)
            )
            if row is None:
                raise TenantNotProvisioned()
        _assert_connection(protection)
        protection.mode = mode
    except BaseException:
        # In particular, cancellation while waiting must never publish proof.
        protection.violated = True
        raise


def assert_organization_lock(session: AsyncSession, organization_ref: str, *, mode: LockMode = LockMode.SHARED) -> None:
    """Require current transaction proof; an exclusive lock also permits reads."""
    _check_mode(mode)
    protection = _protections.get(_root_transaction(session))
    if (
        protection is None
        or protection.organization_ref != organization_ref
        or protection.mode is None
        or (mode is LockMode.EXCLUSIVE and protection.mode is not LockMode.EXCLUSIVE)
    ):
        raise TransactionOwnershipError()
    _assert_connection(protection)


def _organization_key(organization_ref: str) -> int:
    # Protocol v1 reserves bigint high bits ASCII STGO (0x5354474f). The
    # existing control mutex has another high word; regression tests verify
    # the actual mutex acquisition remains outside this organization space.
    # Stage has one organization per database. Digest collisions between
    # hypothetical extra organization guards only add waiting; each transaction
    # can acquire one organization and the row check remains authoritative.
    encoded = json.dumps(
        ["stage.organization-guard", 1, organization_ref], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    suffix = int.from_bytes(hashlib.sha256(encoded).digest()[:4], signed=False)
    return (int.from_bytes(b"STGO", signed=False) << 32) | suffix


def _advisory_key(key: CommandKey) -> tuple[int, int]:
    # Protocol v1: compact JSON array, UTF-8, no Unicode normalization. Every
    # field is a string and the persistent idempotency tuple remains decisive.
    encoded = json.dumps(["stage.command-key", 1, *key], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).digest()
    return int.from_bytes(digest[:4], signed=True), int.from_bytes(digest[4:8], signed=True)


async def lock_command_key(
    session: AsyncSession,
    *,
    organization_ref: str,
    actor_ref: str,
    command_name: str,
    command_version: str,
    idempotency_key: str,
) -> None:
    """Acquire one stable logical command key before organization protection.

    Hash collisions only add waiting: uniqueness and full input comparison are
    still enforced by the command journal. A repeated identical key is a no-op;
    acquiring another key or acquiring a first key after the organization fails.
    """
    key = (organization_ref, actor_ref, command_name, command_version, idempotency_key)
    protection = await _protection(session)
    if protection.command_key is not None:
        if protection.command_key != key:
            raise TransactionOwnershipError()
        return
    if protection.mode is not None or protection.organization_ref not in (None, organization_ref):
        raise TransactionOwnershipError()
    protection.organization_ref = organization_ref
    try:
        if session.get_bind().dialect.name == "sqlite":
            await _sqlite_reservation(session, organization_ref)
        else:
            first, second = _advisory_key(key)
            await session.execute(
                text("SELECT pg_advisory_xact_lock(CAST(:first AS integer), CAST(:second AS integer))"),
                {"first": first, "second": second},
            )
        _assert_connection(protection)
        protection.command_key = key
    except BaseException:
        protection.violated = True
        raise
