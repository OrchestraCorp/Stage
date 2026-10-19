from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AuditEntry:
    """审计事件的最小结构；持久化由后续 Stage 事务层接入。"""

    request_id: str
    decision_id: str | None
    actor_ref: str
    action: str
    target_ref: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    reason: str | None = None
