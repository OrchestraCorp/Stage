"""Historical outbox DTO; new commands emit app.execution.contracts.OutboxIntent.

Durable records and delivery live in app.models.execution and app.delivery.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class OutboxMessage:
    """历史结构；不替代已实现的事务 OutboxIntent、持久记录和投递 Worker。"""

    message_id: str
    topic: str
    aggregate_ref: str
    payload: dict[str, Any]
    occurred_at: datetime
    idempotency_key: str
