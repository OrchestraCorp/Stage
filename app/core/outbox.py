from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class OutboxMessage:
    """事务 outbox 的领域结构；数据库表和投递 worker 后续接入。"""

    message_id: str
    topic: str
    aggregate_ref: str
    payload: dict[str, Any]
    occurred_at: datetime
    idempotency_key: str
