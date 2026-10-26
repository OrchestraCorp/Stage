"""Public local-operator mutex, atomic journal, replay and uncertain-result recovery.

The existing identity_control_operations table is a shared control journal. Its
persisted operation identities and historical encodings remain unchanged.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.control.contracts import (
    BOOTSTRAP_TYPE,
    AdministrationError,
    AdministrationResult,
    OperationUnknown,
)
from app.models.identity_foundation import BootstrapRecordTable, IdentityControlOperationTable


def public_result(operation_ref: str, value: dict[str, Any]) -> AdministrationResult:
    # Adapt immutable historical journal fields at the operator boundary.
    result = dict(value)
    for old, new in (("tenant_ref", "organization_ref"), ("tenant_authz_epoch", "organization_authz_epoch")):
        if old in result:
            result[new] = result.pop(old)
    if result.get("kind") == "tenant":
        result["kind"] = "organization"
    return AdministrationResult(operation_ref=operation_ref, result=result)


def validate_operator(actor_ref: str, idempotency_key: str) -> None:
    if any(not value.strip() or len(value) > 128 for value in (actor_ref, idempotency_key)):
        raise AdministrationError("invalid_operation_identity")


async def acquire_admin_lock(session: AsyncSession) -> None:
    # A single offline installation/operator mutex also serializes journal lookup.
    # PostgreSQL reads still coordinate through the trust/principal/tenant row locks.
    if session.get_bind().dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(b"stage-development-identity-admin").digest()[:8], signed=True)
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    else:
        await session.execute(text("BEGIN IMMEDIATE"))


async def check_database_environment(session: AsyncSession, environment: str) -> None:
    # A Stage database belongs to one deployment environment. Check under the
    # installation mutex, before replay as well as before changing identity facts.
    environments = set(
        (
            await session.scalars(
                select(BootstrapRecordTable.environment)
                .where(BootstrapRecordTable.bootstrap_type == BOOTSTRAP_TYPE)
                .distinct()
            )
        ).all()
    )
    if len(environments) > 1:
        raise AdministrationError("identity_database_environment_conflict")
    if environments and environment not in environments:
        raise AdministrationError("identity_database_environment_mismatch")


@asynccontextmanager
async def operation_transaction(
    sessions: async_sessionmaker[AsyncSession],
    operation_type: str,
    idempotency_key: str,
) -> AsyncIterator[AsyncSession]:
    try:
        async with sessions() as session, session.begin():
            yield session
    except IntegrityError:
        # A rejected uniqueness/foreign-key statement cannot have committed this transaction.
        raise
    except SQLAlchemyError:
        # Connection loss may occur after COMMIT. Recover with the original operator/key.
        raise OperationUnknown(operation_type, idempotency_key) from None


async def find_replay(
    session: AsyncSession,
    actor_ref: str,
    operation_type: str,
    idempotency_key: str,
    digest: str,
) -> AdministrationResult | None:
    row = await session.scalar(
        select(IdentityControlOperationTable).where(
            IdentityControlOperationTable.actor_kind == "bootstrap_operator",
            IdentityControlOperationTable.actor_ref == actor_ref,
            IdentityControlOperationTable.operation_type == operation_type,
            IdentityControlOperationTable.idempotency_key == idempotency_key,
        )
    )
    if row is None:
        return None
    if row.input_digest != digest:
        raise AdministrationError("idempotency_conflict")
    return public_result(row.operation_ref, row.result_json)


def append_journal(
    session: AsyncSession,
    *,
    actor_ref: str,
    operation_type: str,
    idempotency_key: str,
    digest: str,
    environment: str,
    target_ref: str,
    result: dict[str, Any],
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    operation_ref: str | None = None,
) -> AdministrationResult:
    reference = operation_ref or str(uuid4())
    session.add(
        IdentityControlOperationTable(
            operation_ref=reference,
            operation_type=operation_type,
            idempotency_key=idempotency_key,
            input_digest=digest,
            actor_kind="bootstrap_operator",
            actor_ref=actor_ref,
            authentication_json={"source": "local_os_operator", "environment": environment},
            target_ref=target_ref,
            before_json=before,
            after_json=after,
            result_json=result,
            completed_at=datetime.now(UTC),
        )
    )
    return public_result(reference, result)


async def operation_result(
    sessions: async_sessionmaker[AsyncSession],
    *,
    environment: str,
    actor_ref: str,
    operation_type: str,
    idempotency_key: str,
) -> AdministrationResult:
    """Recover only the original operator's result in its recorded environment.

    This read remains available when mixed installation environments block all
    writes; a historical committed result must not disappear during recovery.
    """
    validate_operator(actor_ref, idempotency_key)
    async with sessions() as session:
        journal_type = "revoke.tenant" if operation_type == "revoke.organization" else operation_type
        row = await session.scalar(
            select(IdentityControlOperationTable).where(
                IdentityControlOperationTable.actor_kind == "bootstrap_operator",
                IdentityControlOperationTable.actor_ref == actor_ref,
                IdentityControlOperationTable.operation_type == journal_type,
                IdentityControlOperationTable.idempotency_key == idempotency_key,
            )
        )
        if row is None or row.authentication_json.get("environment") != environment:
            raise AdministrationError("operation_not_found")
        return public_result(row.operation_ref, row.result_json)
