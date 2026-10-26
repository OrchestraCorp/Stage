"""Historical lightweight audit DTO; active persistence uses app.execution and app.models.execution.

Retained for compatibility, not an alternative write path for new domain commands.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AuditEntry:
    """历史最小结构；实际命令审计由 CommandKernel 在业务事务中持久化。"""

    request_id: str
    decision_id: str | None
    actor_ref: str
    action: str
    target_ref: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    reason: str | None = None
