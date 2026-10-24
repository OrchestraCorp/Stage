"""Short-transaction outbox ownership, external I/O, and fenced receipts.

This worker is deliberately not wired into an application or HTTP endpoint.
The composition root must supply a real current-authorization adapter and
reviewed sinks. The clock/leases assume the current single-host deployment;
cross-host clock and fencing protocols require separate verification.
"""

import asyncio
import logging
import math
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.decision import AuthorizationDecision
from app.core.enums import DecisionOutcome
from app.delivery.contracts import (
    CurrentDeliveryAuthorizer,
    DeliveryDrainTimeout,
    DeliveryEnvelope,
    DeliveryReceipt,
    DeliveryRunSummary,
    DeliverySink,
    DeliveryWorkerClosed,
    ReconciliationResult,
    ReconciliationStatus,
)
from app.execution.storage import UnitOfWork, lock_tenant, managed_uow
from app.models.execution import DeliveryAttemptTable, OutboxRecordTable

type Operation = Literal["send", "reconcile"]
logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Claim:
    envelope: DeliveryEnvelope
    token: str
    attempt_id: str
    operation: Operation
    lease_until: datetime


@dataclass(frozen=True)
class _Selection:
    claim: _Claim | None = None
    disposition: str | None = None


class OutboxWorker:
    """Own delivery tasks until close completes, with no implicit authorization.

    ``run_once`` only sends pending messages. Unknown messages require explicit
    ``reconcile_one``; it never sends. Reconciliation appends a separate attempt
    and preserves the original uncertain attempt. An authoritative absence is
    recorded as a failed reconciliation attempt with ``reconcile.not_found``;
    only a sink promising both deduplication and safe absence can requeue it.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        authorizer: CurrentDeliveryAuthorizer,
        sinks: Mapping[str, DeliverySink],
        service_ref: str,
        worker_ref: str,
        clock: Callable[[], datetime] = _utc_now,
        lease_duration: timedelta = timedelta(seconds=30),
        delivery_timeout: float = 10,
        close_timeout: float = 10,
        poll_interval: float = 1,
        retry_delay: timedelta = timedelta(seconds=1),
        per_tenant_limit: int = 10,
        max_send_attempts: int = 3,
    ) -> None:
        if authorizer is None:
            raise ValueError("a current delivery authorizer is required")
        for identity_name, identity_value in (("service_ref", service_ref), ("worker_ref", worker_ref)):
            if not identity_value or not identity_value.strip() or len(identity_value) > 128:
                raise ValueError(identity_name + " must be a nonempty bounded identity")
        for timeout_name, timeout_value in (
            ("delivery_timeout", delivery_timeout),
            ("close_timeout", close_timeout),
            ("poll_interval", poll_interval),
        ):
            if not math.isfinite(timeout_value) or timeout_value <= 0:
                raise ValueError(timeout_name + " must be finite and positive")
        if lease_duration.total_seconds() <= delivery_timeout:
            raise ValueError("lease duration must exceed the external I/O timeout")
        if retry_delay < timedelta(0):
            raise ValueError("retry delay must not be negative")
        if type(per_tenant_limit) is not int or not 1 <= per_tenant_limit <= 100:
            raise ValueError("per tenant limit must be 1..100")
        if type(max_send_attempts) is not int or not 1 <= max_send_attempts <= 100:
            raise ValueError("max send attempts must be 1..100")
        self._sessions = session_factory
        self._authorizer = authorizer
        self._sinks = dict(sinks)
        self._service_ref = service_ref
        self._worker_ref = worker_ref
        self._clock = clock
        self._lease_duration = lease_duration
        self._delivery_timeout = delivery_timeout
        self._close_timeout = close_timeout
        self._poll_interval = poll_interval
        self._retry_delay = retry_delay
        self._limit = per_tenant_limit
        self._max_sends = max_send_attempts
        self._stopping = False
        self._operations: set[asyncio.Task[Any]] = set()
        self._loop_task: asyncio.Task[None] | None = None

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("delivery clock must return an aware timestamp")
        return value.astimezone(UTC)

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        if self._stopping:
            raise DeliveryWorkerClosed("delivery worker is stopping")
        task = asyncio.current_task()
        assert task is not None
        self._operations.add(task)
        try:
            yield
        finally:
            self._operations.discard(task)

    @staticmethod
    def _envelope(row: OutboxRecordTable) -> DeliveryEnvelope:
        return DeliveryEnvelope(
            message_id=row.message_id,
            tenant_ref=row.tenant_ref,
            execution_id=row.execution_id,
            topic=row.topic,
            destination=row.destination,
            aggregate_ref=row.aggregate_ref,
            payload=deepcopy(row.payload_json),
            read_action=row.read_action,
            dedupe_key=row.dedupe_key,
        )

    def _decision_matches(self, decision: AuthorizationDecision, envelope: DeliveryEnvelope, request_id: str) -> bool:
        return (
            isinstance(decision, AuthorizationDecision)
            and decision.outcome == DecisionOutcome.ALLOWED
            and decision.subject_ref == self._service_ref
            and decision.action == envelope.read_action
            and decision.resource_ref == envelope.aggregate_ref
            and decision.request_id == request_id
        )

    async def _authorization_error(
        self, uow: UnitOfWork, envelope: DeliveryEnvelope, operation: Operation, request_id: str
    ) -> str | None:
        if envelope.destination not in self._sinks:
            return "sink.unconfigured"
        try:
            decision = await self._authorizer.authorize(
                uow.session,
                service_ref=self._service_ref,
                envelope=envelope,
                operation=operation,
                request_id=request_id,
            )
        except Exception:
            # A callback cannot end/replace the transaction even when it catches
            # its own commit violation, or throws an unrelated policy error.
            uow.assert_owned()
            return "authorization.unavailable"
        uow.assert_owned()
        return None if self._decision_matches(decision, envelope, request_id) else "authorization.denied"

    async def _send_count(self, session: AsyncSession, envelope: DeliveryEnvelope) -> int:
        return (
            await session.scalar(
                select(func.count())
                .select_from(DeliveryAttemptTable)
                .where(
                    DeliveryAttemptTable.tenant_ref == envelope.tenant_ref,
                    DeliveryAttemptTable.message_id == envelope.message_id,
                    DeliveryAttemptTable.operation == "send",
                    DeliveryAttemptTable.status != "blocked",
                )
            )
        ) or 0

    async def _recover_expired(self, tenant_ref: str) -> int:
        async with managed_uow(self._sessions) as uow:
            session = uow.session
            await lock_tenant(session, tenant_ref)
            now = self._now()
            rows = list(
                (
                    await session.scalars(
                        select(OutboxRecordTable)
                        .where(
                            OutboxRecordTable.tenant_ref == tenant_ref,
                            OutboxRecordTable.status == "delivering",
                            or_(OutboxRecordTable.lease_until <= now, OutboxRecordTable.lease_until.is_(None)),
                        )
                        .order_by(OutboxRecordTable.created_at, OutboxRecordTable.message_id)
                        .limit(self._limit)
                        .with_for_update()
                    )
                ).all()
            )
            for row in rows:
                await self._expire(session, row, now)
            await uow._commit_by_owner()
            return len(rows)

    async def _expire(self, session: AsyncSession, row: OutboxRecordTable, now: datetime) -> None:
        await session.execute(
            update(DeliveryAttemptTable)
            .where(
                DeliveryAttemptTable.tenant_ref == row.tenant_ref,
                DeliveryAttemptTable.message_id == row.message_id,
                DeliveryAttemptTable.lease_token == row.lease_token,
                DeliveryAttemptTable.status == "delivering",
            )
            .values(status="unknown", completed_at=now, error_code="lease.expired")
        )
        row.status = "unknown"
        row.last_error_code = "lease.expired"
        row.lease_token = None
        row.lease_owner = None
        row.lease_until = None

    async def _claim(
        self, tenant_ref: str, *, operation: Operation, message_id: str | None = None
    ) -> _Selection | None:
        async with managed_uow(self._sessions) as uow:
            session = uow.session
            await lock_tenant(session, tenant_ref)
            now = self._now()
            expected_status = "pending" if operation == "send" else "unknown"
            query = select(OutboxRecordTable).where(
                OutboxRecordTable.tenant_ref == tenant_ref,
                OutboxRecordTable.status == expected_status,
            )
            if operation == "send":
                query = query.where(OutboxRecordTable.available_at <= now)
            if message_id is not None:
                query = query.where(OutboxRecordTable.message_id == message_id)
            row = await session.scalar(
                query.order_by(OutboxRecordTable.created_at, OutboxRecordTable.message_id).limit(1).with_for_update()
            )
            if row is None or self._stopping:
                return None
            envelope = self._envelope(row)
            token, attempt_id = str(uuid4()), str(uuid4())
            error_code = await self._authorization_error(uow, envelope, operation, attempt_id)
            if self._stopping:
                return None
            if operation == "send" and error_code is None:
                if await self._send_count(session, envelope) >= self._max_sends:
                    error_code = "attempts.exhausted"
            blocked = error_code is not None
            new_status = (
                expected_status if blocked and operation == "reconcile" else "blocked" if blocked else "delivering"
            )
            now = self._now()
            lease_until = now + self._lease_duration
            updated = await session.execute(
                update(OutboxRecordTable)
                .where(
                    OutboxRecordTable.tenant_ref == tenant_ref,
                    OutboxRecordTable.message_id == row.message_id,
                    OutboxRecordTable.status == expected_status,
                )
                .values(
                    status=new_status,
                    attempt_count=row.attempt_count + 1,
                    lease_token=None if blocked else token,
                    lease_owner=None if blocked else self._worker_ref,
                    lease_until=None if blocked else lease_until,
                    last_error_code=error_code,
                )
                .execution_options(synchronize_session=False)
            )
            assert isinstance(updated, CursorResult)
            if updated.rowcount != 1:
                return None
            session.add(
                DeliveryAttemptTable(
                    attempt_id=attempt_id,
                    tenant_ref=tenant_ref,
                    message_id=row.message_id,
                    lease_token=token,
                    worker_ref=self._worker_ref,
                    operation=operation,
                    status="blocked" if blocked else "delivering",
                    started_at=now,
                    completed_at=now if blocked else None,
                    error_code=error_code,
                )
            )
            await uow._commit_by_owner()
            if blocked:
                return _Selection(disposition="blocked")
            return _Selection(claim=_Claim(envelope, token, attempt_id, operation, lease_until))

    async def _finish(
        self,
        claim: _Claim,
        *,
        status: Literal["sent", "unknown", "pending"],
        error_code: str | None = None,
        receipt: DeliveryReceipt | None = None,
        attempt_status: str | None = None,
    ) -> bool:
        async with managed_uow(self._sessions) as uow:
            session = uow.session
            await lock_tenant(session, claim.envelope.tenant_ref)
            now = self._now()
            row = await session.scalar(
                select(OutboxRecordTable)
                .where(
                    OutboxRecordTable.tenant_ref == claim.envelope.tenant_ref,
                    OutboxRecordTable.message_id == claim.envelope.message_id,
                    OutboxRecordTable.status == "delivering",
                    OutboxRecordTable.lease_token == claim.token,
                    OutboxRecordTable.lease_owner == self._worker_ref,
                )
                .with_for_update()
            )
            if row is None:
                return False
            if row.lease_until is None or row.lease_until <= now:
                await self._expire(session, row, now)
                await uow._commit_by_owner()
                return False
            changed = await session.execute(
                update(OutboxRecordTable)
                .where(
                    OutboxRecordTable.tenant_ref == claim.envelope.tenant_ref,
                    OutboxRecordTable.message_id == claim.envelope.message_id,
                    OutboxRecordTable.status == "delivering",
                    OutboxRecordTable.lease_token == claim.token,
                    OutboxRecordTable.lease_owner == self._worker_ref,
                    OutboxRecordTable.lease_until > now,
                )
                .values(
                    status=status,
                    lease_token=None,
                    lease_owner=None,
                    lease_until=None,
                    available_at=now + self._retry_delay if status == "pending" else row.available_at,
                    sent_at=now if status == "sent" else None,
                    receipt_ref=receipt.receipt_ref if receipt else None,
                    last_error_code=error_code,
                )
                .execution_options(synchronize_session=False)
            )
            assert isinstance(changed, CursorResult)
            if changed.rowcount != 1:
                return False
            attempt_changed = await session.execute(
                update(DeliveryAttemptTable)
                .where(
                    DeliveryAttemptTable.attempt_id == claim.attempt_id,
                    DeliveryAttemptTable.tenant_ref == claim.envelope.tenant_ref,
                    DeliveryAttemptTable.lease_token == claim.token,
                    DeliveryAttemptTable.status == "delivering",
                )
                .values(
                    status=attempt_status or status,
                    completed_at=now,
                    error_code=error_code,
                    receipt_ref=receipt.receipt_ref if receipt else None,
                )
            )
            assert isinstance(attempt_changed, CursorResult)
            if attempt_changed.rowcount != 1:
                raise RuntimeError("delivery receipt has no matching owned attempt")
            await uow._commit_by_owner()
            return True

    @staticmethod
    def _receipt_matches(receipt: DeliveryReceipt, envelope: DeliveryEnvelope) -> bool:
        return (
            isinstance(receipt, DeliveryReceipt)
            and receipt.message_id == envelope.message_id
            and receipt.dedupe_key == envelope.dedupe_key
            and bool(receipt.receipt_ref and receipt.receipt_ref.strip())
            and len(receipt.receipt_ref) <= 256
        )

    async def _send(self, claim: _Claim) -> str:
        if self._stopping or claim.lease_until <= self._now():
            applied = await self._finish(claim, status="unknown", error_code="delivery.not_dispatched")
            return "unknown" if applied else "lost_lease"
        try:
            async with asyncio.timeout(self._delivery_timeout):
                receipt = await self._sinks[claim.envelope.destination].deliver(claim.envelope)
        except asyncio.CancelledError as cancelled:
            await self._finish_cancelled(claim, "delivery.cancelled", cancelled)
            raise
        except Exception as error:
            code = "delivery.timeout" if isinstance(error, TimeoutError) else "delivery.unknown"
            applied = await self._finish(claim, status="unknown", error_code=code)
            return "unknown" if applied else "lost_lease"
        if not self._receipt_matches(receipt, claim.envelope):
            applied = await self._finish(claim, status="unknown", error_code="receipt.invalid")
            return "unknown" if applied else "lost_lease"
        # Receipt bookkeeping preserves an external fact even if permission was
        # revoked after dispatch. It cannot be turned into a new send authority.
        applied = await self._finish(claim, status="sent", receipt=receipt)
        return "sent" if applied else "lost_lease"

    async def _finish_cancelled(self, claim: _Claim, code: str, cancelled: asyncio.CancelledError) -> None:
        try:
            await self._finish(claim, status="unknown", error_code=code)
        except BaseException as error:
            # Preserve cancellation. No local success or resend is inferred:
            # the persisted delivering lease must later expire to unknown.
            logger.warning("cancelled delivery receipt persistence failed type=%s", type(error).__name__)
            cancelled.add_note("Delivery bookkeeping failed; the durable lease requires expiry reconciliation.")

    async def run_once(self, tenant_ref: str) -> DeliveryRunSummary:
        """Process at most the configured number of this tenant's pending rows."""
        async with self._operation():
            expired = await self._recover_expired(tenant_ref)
            counts = {"claimed": 0, "sent": 0, "unknown": 0, "blocked": 0, "lost_leases": 0}
            for _ in range(self._limit):
                if self._stopping:
                    break
                selection = await self._claim(tenant_ref, operation="send")
                if selection is None:
                    break
                if selection.claim is None:
                    counts["blocked"] += 1
                    continue
                counts["claimed"] += 1
                outcome = await self._send(selection.claim)
                counts["lost_leases" if outcome == "lost_lease" else outcome] += 1
            return DeliveryRunSummary(**counts, expired_leases=expired)

    async def reconcile_one(self, tenant_ref: str, message_id: str) -> str:
        """Explicitly query an unknown message; only proven safe absence requeues."""
        async with self._operation():
            await self._recover_expired(tenant_ref)
            selection = await self._claim(tenant_ref, operation="reconcile", message_id=message_id)
            if selection is None:
                return "not_applicable"
            if selection.claim is None:
                return "blocked"
            claim = selection.claim
            sink = self._sinks[claim.envelope.destination]
            try:
                async with asyncio.timeout(self._delivery_timeout):
                    result = await sink.reconcile(claim.envelope)
            except asyncio.CancelledError as cancelled:
                await self._finish_cancelled(claim, "reconcile.cancelled", cancelled)
                raise
            except Exception as error:
                code = "reconcile.timeout" if isinstance(error, TimeoutError) else "reconcile.unknown"
                applied = await self._finish(claim, status="unknown", error_code=code)
                return "unknown" if applied else "lost_lease"
            if not isinstance(result, ReconciliationResult):
                applied = await self._finish(claim, status="unknown", error_code="reconcile.invalid")
                return "unknown" if applied else "lost_lease"
            if result.status == ReconciliationStatus.DELIVERED:
                if result.receipt is None or not self._receipt_matches(result.receipt, claim.envelope):
                    applied = await self._finish(claim, status="unknown", error_code="receipt.invalid")
                    return "unknown" if applied else "lost_lease"
                applied = await self._finish(claim, status="sent", receipt=result.receipt)
                return "sent" if applied else "lost_lease"
            if result.status == ReconciliationStatus.NOT_FOUND and result.receipt is None:
                safe = (
                    sink.capabilities.idempotent_delivery is True and sink.capabilities.authoritative_not_found is True
                )
                status: Literal["pending", "unknown"] = "pending" if safe else "unknown"
                applied = await self._finish(
                    claim, status=status, error_code="reconcile.not_found", attempt_status="failed"
                )
                return status if applied else "lost_lease"
            applied = await self._finish(claim, status="unknown", error_code="reconcile.inconclusive")
            return "unknown" if applied else "lost_lease"

    async def retry_blocked(self, tenant_ref: str, message_id: str) -> str:
        """Explicitly reopen a never-dispatched block after current authorization.

        This is not an uncertain-delivery retry: unknown requires reconciliation.
        The existing blocked attempt and real send budget are retained. A later
        send performs its own fresh authorization check before external I/O.
        """
        async with self._operation(), managed_uow(self._sessions) as uow:
            session = uow.session
            await lock_tenant(session, tenant_ref)
            row = await session.scalar(
                select(OutboxRecordTable)
                .where(
                    OutboxRecordTable.tenant_ref == tenant_ref,
                    OutboxRecordTable.message_id == message_id,
                    OutboxRecordTable.status == "blocked",
                )
                .with_for_update()
            )
            if row is None or self._stopping:
                return "not_applicable"
            envelope = self._envelope(row)
            error = await self._authorization_error(uow, envelope, "send", str(uuid4()))
            if error is None and await self._send_count(session, envelope) >= self._max_sends:
                error = "attempts.exhausted"
            if self._stopping:
                return "blocked"
            if error is not None:
                # Repeated rejected retry requests do not append unbounded
                # attempts or consume/reset the delivery send budget.
                if row.last_error_code != error:
                    row.last_error_code = error
                    await uow._commit_by_owner()
                return "blocked"
            row.status = "pending"
            row.available_at = self._now()
            row.last_error_code = None
            row.lease_token = None
            row.lease_owner = None
            row.lease_until = None
            await uow._commit_by_owner()
            return "pending"

    def start(self, tenant_refs: Sequence[str]) -> None:
        if self._stopping:
            raise DeliveryWorkerClosed("delivery worker is stopping")
        if self._loop_task is not None:
            raise RuntimeError("delivery worker already started")
        tenants = tuple(dict.fromkeys(tenant_refs))
        if not tenants:
            raise ValueError("explicit tenant references are required")

        async def poll() -> None:
            while not self._stopping:
                for tenant_ref in tenants:
                    if self._stopping:
                        return
                    await self.run_once(tenant_ref)
                await asyncio.sleep(self._poll_interval)

        self._loop_task = asyncio.create_task(poll(), name="stage-outbox-worker")

    async def close(self) -> None:
        """Stop intake and await owned I/O and durable cancellation bookkeeping.

        A noncooperative sink cannot be forcibly terminated inside Python. If
        tasks remain live this raises, retaining their ownership; the host must
        not dispose shared dependencies or claim a completed drain.
        """
        self._stopping = True
        current = asyncio.current_task()
        tasks = {task for task in self._operations if task is not current and not task.done()}
        if self._loop_task is not None and self._loop_task is not current and not self._loop_task.done():
            tasks.add(self._loop_task)
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=self._close_timeout)
            if pending:
                raise DeliveryDrainTimeout(len(pending))
            failures = [error for task in done if not task.cancelled() if (error := task.exception()) is not None]
            if failures:
                raise BaseExceptionGroup("delivery operations failed while stopping", failures)
        if self._loop_task is not None and self._loop_task.done():
            # Retrieve supervised failures; operational errors must remain
            # visible instead of becoming an unobserved background exception.
            if not self._loop_task.cancelled():
                error = self._loop_task.exception()
                if error is not None:
                    raise error
