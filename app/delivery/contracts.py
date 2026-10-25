"""Trusted contracts for the durable delivery boundary.

Sink implementations must substantiate their declared deduplication and lookup
guarantees. A successful local ``deliver`` call is not an exactly-once promise.
Current authorization belongs to an injected server-side adapter; these types
neither authenticate callers nor supply a permissive policy implementation.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.decision import AuthorizationDecision


@dataclass(frozen=True)
class DeliveryEnvelope:
    message_id: str
    tenant_ref: str
    execution_id: str
    topic: str
    destination: str
    aggregate_ref: str
    payload: Mapping[str, Any]
    read_action: str
    dedupe_key: str


@dataclass(frozen=True)
class DeliveryReceipt:
    """An affirmative receiver acknowledgement bound to this delivery identity."""

    message_id: str
    dedupe_key: str
    receipt_ref: str


@dataclass(frozen=True)
class SinkCapabilities:
    """Both guarantees are required before an uncertain delivery may be retried.

    ``authoritative_not_found`` means lookup returning not_found guarantees that
    retrying the original dedupe identity is safe, including an earlier request
    that is still in flight. Eventual-consistency absence is insufficient.
    """

    idempotent_delivery: bool = False
    authoritative_not_found: bool = False


class ReconciliationStatus(StrEnum):
    DELIVERED = "delivered"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReconciliationResult:
    status: ReconciliationStatus
    receipt: DeliveryReceipt | None = None


class DeliverySink(Protocol):
    capabilities: SinkCapabilities

    async def deliver(self, envelope: DeliveryEnvelope) -> DeliveryReceipt:
        """Acknowledge delivery, or raise; ambiguous errors remain unknown."""
        ...

    async def reconcile(self, envelope: DeliveryEnvelope) -> ReconciliationResult:
        """Query the original stable delivery identity without sending again."""
        ...


class CurrentDeliveryAuthorizer(Protocol):
    async def authorize(
        self,
        session: AsyncSession,
        *,
        service_ref: str,
        envelope: DeliveryEnvelope,
        operation: Literal["send", "reconcile"],
        request_id: str,
    ) -> AuthorizationDecision:
        """Load current trusted facts and call app.authz inside this transaction.

        The adapter must enforce tenant, reader and destination boundaries. It
        must not reuse a saved positive decision or perform remote network I/O
        while holding the tenant guard. No implementation is provided here.
        """
        ...


@dataclass(frozen=True)
class DeliveryRunSummary:
    claimed: int = 0
    sent: int = 0
    unknown: int = 0
    blocked: int = 0
    expired_leases: int = 0
    lost_leases: int = 0


@dataclass(frozen=True)
class DeliveryWorkerStatus:
    state: Literal["idle", "running", "stopping", "stopped", "failed"]
    exception_type: str | None = None


class DeliveryDrainTimeout(RuntimeError):
    """Owned tasks remain live; callers must not report completed teardown."""

    def __init__(self, pending_operations: int) -> None:
        self.pending_operations = pending_operations
        super().__init__("delivery worker still has unfinished operations")


class DeliveryWorkerClosed(RuntimeError):
    pass
