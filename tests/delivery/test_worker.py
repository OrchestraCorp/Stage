import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.context import AttributeContext, AuthorizationContext, RelationTuple, RoleAssignment
from app.authz.decision import AuthorizationDecision, AuthorizationEngine
from app.core.enums import PrincipalType
from app.delivery import (
    DeliveryDrainTimeout,
    DeliveryEnvelope,
    DeliveryReceipt,
    DeliveryWorkerClosed,
    ReconciliationResult,
    ReconciliationStatus,
    SinkCapabilities,
)
from app.delivery.worker import OutboxWorker
from app.execution.errors import TransactionOwnershipError
from app.execution.storage import UnitOfWork
from app.models.execution import CommandExecutionTable, DeliveryAttemptTable, OutboxRecordTable, TenantGuardTable

SERVICE = "service:delivery"
TENANT_A = "tenant:a"
TENANT_B = "tenant:b"


@dataclass
class Clock:
    now: datetime = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class MutableAuthorizer:
    """Explicit finite test facts evaluated by the real authorization engine."""

    clock: Clock
    allowed_resources: set[tuple[str, str]]
    active: bool = True
    corrupt_field: str | None = None
    sessions: list[AsyncSession] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)

    async def authorize(
        self,
        session: AsyncSession,
        *,
        service_ref: str,
        envelope: DeliveryEnvelope,
        operation: Literal["send", "reconcile"],
        request_id: str,
    ) -> AuthorizationDecision:
        assert session.in_transaction()
        self.sessions.append(session)
        self.operations.append(operation)
        has_relation = (envelope.tenant_ref, envelope.aggregate_ref) in self.allowed_resources
        decision = AuthorizationEngine().decide(
            AuthorizationContext(
                request_id=request_id,
                subject_ref=service_ref,
                principal_type=PrincipalType.SERVICE,
                action=envelope.read_action,
                resource_ref=envelope.aggregate_ref,
                organization_ref=envelope.tenant_ref,
                resource_organization_ref=envelope.tenant_ref,
                resource_scope_refs=(envelope.tenant_ref,),
                assignment=RoleAssignment(
                    assignment_ref="test-assignment:" + envelope.tenant_ref,
                    principal_ref=service_ref,
                    role_code="engineering-member",
                    organization_ref=envelope.tenant_ref,
                    scope_refs=(envelope.tenant_ref,),
                ),
                relations=(RelationTuple(service_ref, "viewer_of", envelope.aggregate_ref),) if has_relation else (),
                now=self.clock(),
                attributes=AttributeContext(
                    subject_active=self.active,
                    resource_classification="internal",
                    classification_ceiling="internal",
                    resource_state="open",
                ),
            )
        )
        if self.corrupt_field:
            changes: dict[str, Any] = {self.corrupt_field: "wrong-context"}
            decision = replace(decision, **changes)
        return decision


@dataclass
class ReceiverSink:
    capabilities: SinkCapabilities = SinkCapabilities(True, True)
    calls: int = 0
    reconciliations: int = 0
    effects: int = 0
    receipts: dict[tuple[str, str, str], DeliveryReceipt] = field(default_factory=dict)
    envelopes: list[DeliveryEnvelope] = field(default_factory=list)
    failure: Literal["before", "after"] | None = None
    block_first: bool = False
    ignore_cancellation: bool = False
    force_unknown_reconcile: bool = False
    invalid_receipt: bool = False
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    authorizer: MutableAuthorizer | None = None

    def accept(self, envelope: DeliveryEnvelope) -> DeliveryReceipt:
        key = (envelope.tenant_ref, envelope.destination, envelope.dedupe_key)
        if key not in self.receipts:
            self.effects += 1
            self.receipts[key] = DeliveryReceipt(envelope.message_id, envelope.dedupe_key, f"receipt:{self.effects}")
        return self.receipts[key]

    async def deliver(self, envelope: DeliveryEnvelope) -> DeliveryReceipt:
        if self.authorizer is not None:
            assert all(not session.in_transaction() for session in self.authorizer.sessions)
        self.calls += 1
        self.envelopes.append(envelope)
        self.started.set()
        if self.block_first and self.calls == 1:
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    if not self.ignore_cancellation:
                        raise
        if self.failure == "before":
            raise TimeoutError("private error details must never be persisted")
        receipt = self.accept(envelope)
        if self.failure == "after":
            raise TimeoutError("receiver accepted, but acknowledgement was lost")
        if self.invalid_receipt:
            return replace(receipt, message_id="another-message")
        return receipt

    async def reconcile(self, envelope: DeliveryEnvelope) -> ReconciliationResult:
        self.reconciliations += 1
        if self.authorizer is not None:
            assert all(not session.in_transaction() for session in self.authorizer.sessions)
        if self.force_unknown_reconcile:
            return ReconciliationResult(ReconciliationStatus.UNKNOWN)
        receipt = self.receipts.get((envelope.tenant_ref, envelope.destination, envelope.dedupe_key))
        if receipt:
            return ReconciliationResult(ReconciliationStatus.DELIVERED, receipt)
        return ReconciliationResult(ReconciliationStatus.NOT_FOUND)


async def seed_message(
    sessions: async_sessionmaker[AsyncSession], clock: Clock, *, tenant_ref: str = TENANT_A
) -> tuple[str, str]:
    execution_id, message_id = str(uuid4()), str(uuid4())
    aggregate = "work:" + tenant_ref
    async with sessions() as session, session.begin():
        if await session.get(TenantGuardTable, tenant_ref) is None:
            session.add(TenantGuardTable(tenant_ref=tenant_ref, epoch=0))
        session.add(
            CommandExecutionTable(
                execution_id=execution_id,
                tenant_ref=tenant_ref,
                actor_ref="person:test",
                effective_subject_ref="person:test",
                command_name="test.create",
                command_version="1",
                idempotency_key=str(uuid4()),
                payload_digest="a" * 64,
                context_digest="b" * 64,
                target_ref=aggregate,
                expected_version=1,
                request_id=str(uuid4()),
                status="succeeded",
                result_json={"ok": True},
                created_at=clock(),
                completed_at=clock(),
            )
        )
        await session.flush()
        session.add(
            OutboxRecordTable(
                message_id=message_id,
                tenant_ref=tenant_ref,
                execution_id=execution_id,
                topic="test.created",
                destination="test-sink",
                aggregate_ref=aggregate,
                payload_json={"public_fact": "created"},
                read_action="task.view",
                dedupe_key="message:" + message_id,
                status="pending",
                available_at=clock(),
                created_at=clock(),
                attempt_count=0,
            )
        )
    return message_id, aggregate


async def message_state(
    sessions: async_sessionmaker[AsyncSession], message_id: str
) -> tuple[OutboxRecordTable, list[DeliveryAttemptTable]]:
    async with sessions() as session:
        message = await session.get(OutboxRecordTable, message_id)
        assert message is not None
        attempts = list(
            (
                await session.scalars(
                    select(DeliveryAttemptTable)
                    .where(DeliveryAttemptTable.message_id == message_id)
                    .order_by(DeliveryAttemptTable.started_at, DeliveryAttemptTable.attempt_id)
                )
            ).all()
        )
        return message, attempts


def make_worker(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    authorizer: MutableAuthorizer,
    sink: ReceiverSink,
    **options: Any,
) -> OutboxWorker:
    sink.authorizer = authorizer
    return OutboxWorker(
        sessions,
        authorizer=authorizer,
        sinks={"test-sink": sink},
        service_ref=SERVICE,
        worker_ref="worker:test",
        clock=clock,
        **options,
    )


async def test_delivery_requires_current_authorizer(persistence_sessions: async_sessionmaker[AsyncSession]) -> None:
    with pytest.raises(ValueError, match="authorizer"):
        OutboxWorker(
            persistence_sessions,
            authorizer=None,  # type: ignore[arg-type]
            sinks={},
            service_ref=SERVICE,
            worker_ref="worker:test",
        )


async def test_send_records_acknowledgement_and_short_transactions(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock, sink = Clock(), ReceiverSink()
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    authorizer = MutableAuthorizer(clock, {(TENANT_A, aggregate)})
    worker = make_worker(persistence_sessions, clock, authorizer, sink)
    summary = await worker.run_once(TENANT_A)
    row, attempts = await message_state(persistence_sessions, message_id)
    assert (summary.claimed, summary.sent) == (1, 1)
    assert row.status == "sent" and row.receipt_ref == "receipt:1"
    assert row.sent_at == clock() and row.lease_token is None
    assert row.attempt_count == 1 and len(attempts) == 1
    assert attempts[0].status == "sent" and attempts[0].operation == "send"
    assert authorizer.operations == ["send"] and sink.effects == 1
    await worker.close()


async def test_denied_current_relation_blocks_without_sending(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock, sink = Clock(), ReceiverSink()
    message_id, _ = await seed_message(persistence_sessions, clock)
    worker = make_worker(persistence_sessions, clock, MutableAuthorizer(clock, set()), sink)
    summary = await worker.run_once(TENANT_A)
    row, attempts = await message_state(persistence_sessions, message_id)
    assert summary.blocked == 1 and sink.calls == 0
    assert row.status == "blocked" and row.sent_at is None
    assert attempts[0].status == "blocked" and row.last_error_code == "authorization.denied"


@pytest.mark.parametrize("field_name", ["subject_ref", "action", "resource_ref", "request_id"])
async def test_allowed_decision_must_match_delivery_context(
    persistence_sessions: async_sessionmaker[AsyncSession], field_name: str
) -> None:
    clock, sink = Clock(), ReceiverSink()
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    authorizer = MutableAuthorizer(clock, {(TENANT_A, aggregate)}, corrupt_field=field_name)
    worker = make_worker(persistence_sessions, clock, authorizer, sink)
    assert (await worker.run_once(TENANT_A)).blocked == 1
    assert sink.calls == 0
    assert (await message_state(persistence_sessions, message_id))[0].status == "blocked"


async def test_lost_ack_is_unknown_until_explicit_receiver_reconciliation(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock, sink = Clock(), ReceiverSink(failure="after")
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    authorizer = MutableAuthorizer(clock, {(TENANT_A, aggregate)})
    worker = make_worker(persistence_sessions, clock, authorizer, sink)
    assert (await worker.run_once(TENANT_A)).unknown == 1
    assert (await worker.run_once(TENANT_A)).claimed == 0
    assert sink.calls == sink.effects == 1
    assert await worker.reconcile_one(TENANT_A, message_id) == "sent"
    row, attempts = await message_state(persistence_sessions, message_id)
    assert row.status == "sent" and row.attempt_count == 2
    assert {(a.operation, a.status) for a in attempts} == {("send", "unknown"), ("reconcile", "sent")}
    assert next(a for a in attempts if a.operation == "send").error_code == "delivery.timeout"
    assert sink.calls == 1 and authorizer.operations == ["send", "reconcile"]


@pytest.mark.parametrize(
    "capabilities", [SinkCapabilities(), SinkCapabilities(True, False), SinkCapabilities(False, True)]
)
async def test_absence_without_both_sink_guarantees_cannot_requeue(
    persistence_sessions: async_sessionmaker[AsyncSession], capabilities: SinkCapabilities
) -> None:
    clock, sink = Clock(), ReceiverSink(capabilities=capabilities, failure="before")
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    worker = make_worker(persistence_sessions, clock, MutableAuthorizer(clock, {(TENANT_A, aggregate)}), sink)
    await worker.run_once(TENANT_A)
    assert await worker.reconcile_one(TENANT_A, message_id) == "unknown"
    assert (await worker.run_once(TENANT_A)).claimed == 0
    assert sink.calls == 1 and sink.effects == 0


async def test_safe_absence_requeues_original_dedupe_identity_with_fresh_authorization(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock, sink = Clock(), ReceiverSink(failure="before")
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    authorizer = MutableAuthorizer(clock, {(TENANT_A, aggregate)})
    worker = make_worker(persistence_sessions, clock, authorizer, sink)
    await worker.run_once(TENANT_A)
    assert await worker.reconcile_one(TENANT_A, message_id) == "pending"
    assert (await worker.run_once(TENANT_A)).claimed == 0
    clock.advance(1)
    sink.failure = None
    assert (await worker.run_once(TENANT_A)).sent == 1
    assert sink.calls == 2 and sink.effects == 1
    assert sink.envelopes[0].dedupe_key == sink.envelopes[1].dedupe_key
    # Model the original transport arriving late: the receiver deduplicates it.
    sink.accept(sink.envelopes[0])
    assert sink.effects == 1
    assert authorizer.operations == ["send", "reconcile", "send"]


async def test_reconciliation_itself_requires_current_authorization(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock, sink = Clock(), ReceiverSink(failure="after")
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    authorizer = MutableAuthorizer(clock, {(TENANT_A, aggregate)})
    worker = make_worker(persistence_sessions, clock, authorizer, sink)
    await worker.run_once(TENANT_A)
    authorizer.active = False
    assert await worker.reconcile_one(TENANT_A, message_id) == "blocked"
    row, attempts = await message_state(persistence_sessions, message_id)
    assert row.status == "unknown" and sink.reconciliations == 0
    assert sorted(a.status for a in attempts) == ["blocked", "unknown"]


async def test_expired_owner_cannot_finalize_after_a_new_attempt(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock, sink = Clock(), ReceiverSink(block_first=True)
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    authorizer = MutableAuthorizer(clock, {(TENANT_A, aggregate)})
    first = make_worker(persistence_sessions, clock, authorizer, sink)
    second = make_worker(persistence_sessions, clock, authorizer, sink)
    original = asyncio.create_task(first.run_once(TENANT_A))
    await sink.started.wait()
    clock.advance(30)  # Equality already expires the lease.
    recovery = await second.run_once(TENANT_A)
    assert recovery.expired_leases == 1 and recovery.claimed == 0
    assert await second.reconcile_one(TENANT_A, message_id) == "pending"
    clock.advance(1)
    assert (await second.run_once(TENANT_A)).sent == 1
    sink.release.set()
    assert (await original).lost_leases == 1
    row, attempts = await message_state(persistence_sessions, message_id)
    assert row.status == "sent" and row.receipt_ref == "receipt:1"
    assert sink.effects == 1 and sink.calls == 2
    assert any(a.status == "unknown" and a.error_code == "lease.expired" for a in attempts)


async def test_another_worker_cannot_claim_active_lease(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock, sink = Clock(), ReceiverSink(block_first=True)
    _, aggregate = await seed_message(persistence_sessions, clock)
    authorizer = MutableAuthorizer(clock, {(TENANT_A, aggregate)})
    first = make_worker(persistence_sessions, clock, authorizer, sink)
    second = make_worker(persistence_sessions, clock, authorizer, sink)
    running = asyncio.create_task(first.run_once(TENANT_A))
    await sink.started.wait()
    assert (await second.run_once(TENANT_A)).claimed == 0
    sink.release.set()
    assert (await running).sent == 1 and sink.calls == 1


async def test_tenant_batch_limit_and_reader_boundaries(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock, sink = Clock(), ReceiverSink()
    first_id, aggregate_a = await seed_message(persistence_sessions, clock)
    second_id, _ = await seed_message(persistence_sessions, clock)
    other_id, aggregate_b = await seed_message(persistence_sessions, clock, tenant_ref=TENANT_B)
    authorizer = MutableAuthorizer(clock, {(TENANT_A, aggregate_a), (TENANT_B, aggregate_b)})
    worker = make_worker(persistence_sessions, clock, authorizer, sink, per_tenant_limit=1)
    assert (await worker.run_once(TENANT_A)).sent == 1
    a_states = [(await message_state(persistence_sessions, message))[0].status for message in (first_id, second_id)]
    assert sorted(a_states) == ["pending", "sent"]
    assert (await message_state(persistence_sessions, other_id))[0].status == "pending"
    assert await worker.reconcile_one(TENANT_B, first_id) == "not_applicable"


async def test_revocation_after_dispatch_does_not_erase_external_receipt(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock, sink = Clock(), ReceiverSink(block_first=True)
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    authorizer = MutableAuthorizer(clock, {(TENANT_A, aggregate)})
    worker = make_worker(persistence_sessions, clock, authorizer, sink)
    running = asyncio.create_task(worker.run_once(TENANT_A))
    await sink.started.wait()
    authorizer.active = False
    sink.release.set()
    assert (await running).sent == 1
    assert (await message_state(persistence_sessions, message_id))[0].status == "sent"
    next_id, _ = await seed_message(persistence_sessions, clock)
    assert (await worker.run_once(TENANT_A)).blocked == 1
    assert (await message_state(persistence_sessions, next_id))[0].status == "blocked"
    assert sink.calls == 1


async def test_wrong_receipt_does_not_become_sent(persistence_sessions: async_sessionmaker[AsyncSession]) -> None:
    clock, sink = Clock(), ReceiverSink(invalid_receipt=True)
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    worker = make_worker(persistence_sessions, clock, MutableAuthorizer(clock, {(TENANT_A, aggregate)}), sink)
    assert (await worker.run_once(TENANT_A)).unknown == 1
    row, _ = await message_state(persistence_sessions, message_id)
    assert row.status == "unknown" and row.receipt_ref is None


async def test_close_awaits_cancellation_and_persists_unknown(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock, sink = Clock(), ReceiverSink(block_first=True)
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    worker = make_worker(persistence_sessions, clock, MutableAuthorizer(clock, {(TENANT_A, aggregate)}), sink)
    running = asyncio.create_task(worker.run_once(TENANT_A))
    await sink.started.wait()
    await worker.close()
    with pytest.raises(asyncio.CancelledError):
        await running
    row, attempts = await message_state(persistence_sessions, message_id)
    assert row.status == "unknown" and row.lease_token is None
    assert attempts[0].error_code == "delivery.cancelled"
    with pytest.raises(DeliveryWorkerClosed):
        await worker.run_once(TENANT_A)
    await worker.close()


async def test_close_timeout_does_not_claim_noncooperative_io_was_drained(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock, sink = Clock(), ReceiverSink(block_first=True, ignore_cancellation=True)
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    worker = make_worker(
        persistence_sessions, clock, MutableAuthorizer(clock, {(TENANT_A, aggregate)}), sink, close_timeout=0.01
    )
    running = asyncio.create_task(worker.run_once(TENANT_A))
    await sink.started.wait()
    try:
        with pytest.raises(DeliveryDrainTimeout) as failure:
            await worker.close()
        assert failure.value.pending_operations == 1 and not running.done()
        assert (await message_state(persistence_sessions, message_id))[0].status == "delivering"
    finally:
        sink.release.set()
        await running
        await worker.close()


async def test_io_deadline_is_unknown_and_does_not_automatically_retry(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock, sink = Clock(), ReceiverSink(block_first=True)
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    worker = make_worker(
        persistence_sessions, clock, MutableAuthorizer(clock, {(TENANT_A, aggregate)}), sink, delivery_timeout=0.01
    )
    assert (await worker.run_once(TENANT_A)).unknown == 1
    row, attempts = await message_state(persistence_sessions, message_id)
    assert row.last_error_code == attempts[0].error_code == "delivery.timeout"
    assert (await worker.run_once(TENANT_A)).claimed == 0
    assert sink.calls == 1


async def test_background_loop_is_awaitably_closed(persistence_sessions: async_sessionmaker[AsyncSession]) -> None:
    clock, sink = Clock(), ReceiverSink(block_first=True)
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    worker = make_worker(persistence_sessions, clock, MutableAuthorizer(clock, {(TENANT_A, aggregate)}), sink)
    worker.start([TENANT_A])
    await sink.started.wait()
    await worker.close()
    assert (await message_state(persistence_sessions, message_id))[0].status == "unknown"
    with pytest.raises(DeliveryWorkerClosed):
        worker.start([TENANT_A])


async def test_safe_retries_have_a_finite_send_budget(persistence_sessions: async_sessionmaker[AsyncSession]) -> None:
    clock, sink = Clock(), ReceiverSink(failure="before")
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    worker = make_worker(
        persistence_sessions, clock, MutableAuthorizer(clock, {(TENANT_A, aggregate)}), sink, max_send_attempts=2
    )
    for _ in range(2):
        assert (await worker.run_once(TENANT_A)).unknown == 1
        assert await worker.reconcile_one(TENANT_A, message_id) == "pending"
        clock.advance(1)
    assert (await worker.run_once(TENANT_A)).blocked == 1
    row, _ = await message_state(persistence_sessions, message_id)
    assert row.last_error_code == "attempts.exhausted" and sink.calls == 2


async def test_inconclusive_reconciliation_keeps_unknown_and_never_sends(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock, sink = Clock(), ReceiverSink(failure="before", force_unknown_reconcile=True)
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    worker = make_worker(persistence_sessions, clock, MutableAuthorizer(clock, {(TENANT_A, aggregate)}), sink)
    await worker.run_once(TENANT_A)
    assert await worker.reconcile_one(TENANT_A, message_id) == "unknown"
    assert (await worker.run_once(TENANT_A)).claimed == 0
    assert sink.calls == 1 and sink.effects == 0


async def test_scheduled_message_and_naive_clock_cannot_dispatch(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock, sink = Clock(), ReceiverSink()
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    async with persistence_sessions() as session, session.begin():
        row = await session.get(OutboxRecordTable, message_id)
        assert row is not None
        row.available_at = clock() + timedelta(seconds=10)
    worker = make_worker(persistence_sessions, clock, MutableAuthorizer(clock, {(TENANT_A, aggregate)}), sink)
    assert (await worker.run_once(TENANT_A)).claimed == 0 and sink.calls == 0
    clock.now = clock.now.replace(tzinfo=None)
    with pytest.raises(ValueError, match="aware"):
        await worker.run_once(TENANT_A)
    assert (await message_state(persistence_sessions, message_id))[0].status == "pending"


async def test_blocked_message_can_be_explicitly_reopened_but_dispatch_rechecks_authorization(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock, sink = Clock(), ReceiverSink()
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    authorizer = MutableAuthorizer(clock, set())
    worker = make_worker(persistence_sessions, clock, authorizer, sink)
    await worker.run_once(TENANT_A)
    for _ in range(3):
        assert await worker.retry_blocked(TENANT_A, message_id) == "blocked"
    row, attempts = await message_state(persistence_sessions, message_id)
    assert row.attempt_count == len(attempts) == 1 and sink.calls == 0
    async with persistence_sessions() as session, session.begin():
        session.add(TenantGuardTable(tenant_ref=TENANT_B, epoch=0))
    assert await worker.retry_blocked(TENANT_B, message_id) == "not_applicable"
    authorizer.allowed_resources.add((TENANT_A, aggregate))
    assert await worker.retry_blocked(TENANT_A, message_id) == "pending"
    assert len((await message_state(persistence_sessions, message_id))[1]) == 1
    authorizer.active = False
    assert (await worker.run_once(TENANT_A)).blocked == 1
    assert sink.calls == 0
    authorizer.active = True
    assert await worker.retry_blocked(TENANT_A, message_id) == "pending"
    assert (await worker.run_once(TENANT_A)).sent == 1
    row, attempts = await message_state(persistence_sessions, message_id)
    assert row.status == "sent" and row.attempt_count == len(attempts) == 3
    assert [a.status for a in attempts].count("blocked") == 2 and sink.calls == 1


async def test_retry_blocked_cannot_reset_real_send_attempt_budget(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock, sink = Clock(), ReceiverSink(failure="before")
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    authorizer = MutableAuthorizer(clock, {(TENANT_A, aggregate)})
    worker = make_worker(persistence_sessions, clock, authorizer, sink, max_send_attempts=1)
    await worker.run_once(TENANT_A)
    assert await worker.reconcile_one(TENANT_A, message_id) == "pending"
    clock.advance(1)
    await worker.run_once(TENANT_A)
    before, attempts = await message_state(persistence_sessions, message_id)
    assert before.status == "blocked" and before.last_error_code == "attempts.exhausted"
    for _ in range(3):
        assert await worker.retry_blocked(TENANT_A, message_id) == "blocked"
    after, after_attempts = await message_state(persistence_sessions, message_id)
    assert after.attempt_count == before.attempt_count and len(after_attempts) == len(attempts)
    assert sink.calls == 1


async def test_retry_blocked_preserves_unconfigured_destination_block(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = Clock()
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    authorizer = MutableAuthorizer(clock, {(TENANT_A, aggregate)})
    worker = OutboxWorker(
        persistence_sessions,
        authorizer=authorizer,
        sinks={},
        service_ref=SERVICE,
        worker_ref="worker:test",
        clock=clock,
    )
    assert (await worker.run_once(TENANT_A)).blocked == 1
    assert await worker.retry_blocked(TENANT_A, message_id) == "blocked"
    assert authorizer.operations == []
    assert (await message_state(persistence_sessions, message_id))[0].last_error_code == "sink.unconfigured"


@pytest.mark.parametrize("operation", ["send", "reconcile"])
async def test_receipt_commit_failure_retains_lease_and_does_not_retry_finalize_or_send(
    persistence_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    clock, sink = Clock(), ReceiverSink(failure="after" if operation == "reconcile" else None)
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    authorizer = MutableAuthorizer(clock, {(TENANT_A, aggregate)})
    worker = make_worker(persistence_sessions, clock, authorizer, sink)
    if operation == "reconcile":
        await worker.run_once(TENANT_A)
    original_commit = UnitOfWork._commit_by_owner
    commit_calls = 0

    async def fail_receipt_commit(uow: UnitOfWork) -> None:
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 3:
            # Both operations first recover leases, then claim, then finalize.
            # Fail before the final commit, exercising actual rollback cleanup.
            raise OSError("fixture receipt persistence failure")
        await original_commit(uow)

    with monkeypatch.context() as patch:
        patch.setattr(UnitOfWork, "_commit_by_owner", fail_receipt_commit)
        with pytest.raises(OSError, match="receipt persistence"):
            if operation == "reconcile":
                await worker.reconcile_one(TENANT_A, message_id)
            else:
                await worker.run_once(TENANT_A)
    assert commit_calls == 3  # A DB error must not trigger a second _finish.
    row, attempts = await message_state(persistence_sessions, message_id)
    assert row.status == "delivering" and row.lease_token is not None
    assert any(a.status == "delivering" for a in attempts)
    assert (await worker.run_once(TENANT_A)).claimed == 0
    assert sink.calls == sink.effects == 1
    clock.advance(30)
    assert (await worker.run_once(TENANT_A)).expired_leases == 1
    assert (await message_state(persistence_sessions, message_id))[0].status == "unknown"
    assert await worker.reconcile_one(TENANT_A, message_id) == "sent"
    assert sink.calls == sink.effects == 1


async def test_cancelled_io_preserves_cancellation_when_receipt_persistence_fails(
    persistence_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock, sink = Clock(), ReceiverSink(block_first=True)
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    worker = make_worker(persistence_sessions, clock, MutableAuthorizer(clock, {(TENANT_A, aggregate)}), sink)
    running = asyncio.create_task(worker.run_once(TENANT_A))
    await sink.started.wait()
    original_commit = UnitOfWork._commit_by_owner

    async def fail_commit(_uow: UnitOfWork) -> None:
        raise OSError("fixture database unavailable during cancellation")

    with monkeypatch.context() as patch:
        patch.setattr(UnitOfWork, "_commit_by_owner", fail_commit)
        await worker.close()
        with pytest.raises(asyncio.CancelledError) as failure:
            await running
    assert any("expiry reconciliation" in note for note in failure.value.__notes__)
    assert (await message_state(persistence_sessions, message_id))[0].status == "delivering"
    clock.advance(30)
    recovering = make_worker(persistence_sessions, clock, MutableAuthorizer(clock, {(TENANT_A, aggregate)}), sink)
    assert UnitOfWork._commit_by_owner is original_commit
    assert (await recovering.run_once(TENANT_A)).expired_leases == 1
    assert (await message_state(persistence_sessions, message_id))[0].status == "unknown"


@dataclass
class TransactionBreakingAuthorizer(MutableAuthorizer):
    violation: str = "commit"
    swallow: bool = False

    async def authorize(
        self,
        session: AsyncSession,
        *,
        service_ref: str,
        envelope: DeliveryEnvelope,
        operation: Literal["send", "reconcile"],
        request_id: str,
    ) -> AuthorizationDecision:
        try:
            if self.violation == "connection_commit":
                connection = await session.connection()
                await connection.commit()
            elif self.violation == "rollback":
                await session.rollback()
            else:
                await session.commit()
        except TransactionOwnershipError:
            if not self.swallow:
                raise
        return await super().authorize(
            session,
            service_ref=service_ref,
            envelope=envelope,
            operation=operation,
            request_id=request_id,
        )


@pytest.mark.parametrize("violation", ["commit", "connection_commit", "rollback"])
@pytest.mark.parametrize("swallow", [False, True])
async def test_authorizer_cannot_commit_or_replace_owned_claim_transaction(
    persistence_sessions: async_sessionmaker[AsyncSession], violation: str, swallow: bool
) -> None:
    clock, sink = Clock(), ReceiverSink()
    message_id, aggregate = await seed_message(persistence_sessions, clock)
    authorizer = TransactionBreakingAuthorizer(clock, {(TENANT_A, aggregate)}, violation=violation, swallow=swallow)
    worker = make_worker(persistence_sessions, clock, authorizer, sink)
    with pytest.raises(TransactionOwnershipError):
        await worker.run_once(TENANT_A)
    row, attempts = await message_state(persistence_sessions, message_id)
    assert row.status == "pending" and row.attempt_count == 0
    assert attempts == [] and sink.calls == 0
