from dataclasses import dataclass
from datetime import datetime

from app.core.enums import PrincipalType


@dataclass(frozen=True)
class RelationTuple:
    subject_ref: str
    relation: str
    resource_ref: str
    valid_from: datetime | None = None
    expires_at: datetime | None = None

    def is_active_at(self, now: datetime) -> bool:
        if self.valid_from is not None and now < self.valid_from:
            return False
        if self.expires_at is not None and now >= self.expires_at:
            return False
        return True


@dataclass(frozen=True)
class AttributeContext:
    subject_active: bool = True
    delegation_active: bool = True
    resource_classification: str = "internal"
    classification_ceiling: str = "internal"
    object_version: int | None = None
    expected_object_version: int | None = None
    resource_state: str | None = None
    allowed_resource_states: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuthorizationContext:
    request_id: str
    subject_ref: str
    principal_type: PrincipalType
    action: str
    resource_ref: str
    role_codes: tuple[str, ...]
    relations: tuple[RelationTuple, ...] = ()
    now: datetime | None = None
    attributes: AttributeContext = AttributeContext()
    policy_release_id: str = "unreleased"
    agent_ref: str | None = None
    delegation_ref: str | None = None
