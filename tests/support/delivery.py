"""Shared synthetic builders for delivery."""

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.context import AttributeContext, AuthorizationContext, RelationTuple, RoleAssignment
from app.authz.decision import AuthorizationDecision, AuthorizationEngine
from app.core.enums import PrincipalType
from app.delivery import (
    DeliveryEnvelope,
    DeliveryReceipt,
    ReconciliationResult,
    ReconciliationStatus,
    SinkCapabilities,
)
from app.models.execution import CommandExecutionTable, DeliveryAttemptTable, OutboxRecordTable, TenantGuardTable

SERVICE = "service:delivery"


TENANT_A = "tenant:a"


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
