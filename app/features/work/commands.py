"""Historical task DTO sketches, not registered commands or public request schemas.

Their subject/reviewer fields are not authenticated identity. The live human
metadata command is in details.py; future task commands require host-derived
identity, current authorization, idempotency, version checks and atomic audit.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ClaimWork:
    """直接领取命令的输入结构；事务实现将在 work service 中落地。"""

    request_id: str
    work_item_ref: str
    subject_ref: str
    expected_version: int
    idempotency_key: str


@dataclass(frozen=True)
class SubmitDelivery:
    request_id: str
    work_item_ref: str
    commitment_ref: str
    subject_ref: str
    object_version: int
    artifact_ref: str
    idempotency_key: str


@dataclass(frozen=True)
class AcceptDelivery:
    request_id: str
    delivery_ref: str
    reviewer_ref: str
    object_version: int
    accepted: bool
    idempotency_key: str
