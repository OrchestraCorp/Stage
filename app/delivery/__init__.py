"""Durable, explicitly authorized outbox delivery primitives."""

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
    SinkCapabilities,
)

__all__ = [
    "CurrentDeliveryAuthorizer",
    "DeliveryDrainTimeout",
    "DeliveryEnvelope",
    "DeliveryReceipt",
    "DeliveryRunSummary",
    "DeliverySink",
    "DeliveryWorkerClosed",
    "ReconciliationResult",
    "ReconciliationStatus",
    "SinkCapabilities",
]
