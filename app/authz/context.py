"""Trusted, immutable authorization input objects."""

from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.enums import PrincipalType


def is_aware(value: datetime | None) -> bool:
    return value is not None and value.tzinfo is not None and value.utcoffset() is not None


def utc_or_none(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if not is_aware(value):
        return value
    return value.astimezone(UTC)


@dataclass(frozen=True)
class RoleAssignment:
    assignment_ref: str
    principal_ref: str
    role_code: str
    organization_ref: str
    scope_refs: tuple[str, ...]
    valid_from: datetime | None = None
    expires_at: datetime | None = None
    status: str = "active"

    def is_active_at(self, now: datetime) -> bool:
        if not is_aware(now) or self.status != "active":
            return False
        now_utc = now.astimezone(UTC)
        start = utc_or_none(self.valid_from)
        end = utc_or_none(self.expires_at)
        if self.valid_from is not None and not is_aware(self.valid_from):
            return False
        if self.expires_at is not None and not is_aware(self.expires_at):
            return False
        return not (start is not None and now_utc < start) and not (end is not None and now_utc >= end)

    def covers(self, *, organization_ref: str, resource_scopes: tuple[str, ...]) -> bool:
        if not organization_ref or organization_ref != self.organization_ref:
            return False
        if not self.scope_refs or not resource_scopes:
            return False
        return bool(set(self.scope_refs).intersection(resource_scopes))


@dataclass(frozen=True)
class Delegation:
    delegation_ref: str
    agent_ref: str
    delegator_ref: str
    assignment_ref: str
    organization_ref: str
    allowed_actions: tuple[str, ...]
    resource_refs: tuple[str, ...]
    valid_from: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    status: str = "active"

    def is_active_at(self, now: datetime) -> bool:
        if not is_aware(now) or not is_aware(self.valid_from) or not is_aware(self.expires_at):
            return False
        now_utc = now.astimezone(UTC)
        if self.status != "active" or self.revoked_at is not None:
            return False
        return self.valid_from.astimezone(UTC) <= now_utc < self.expires_at.astimezone(UTC)

    def permits(self, *, action: str, resource_ref: str, organization_ref: str) -> bool:
        return (
            organization_ref == self.organization_ref
            and action in self.allowed_actions
            and resource_ref in self.resource_refs
        )


@dataclass(frozen=True)
class RelationTuple:
    subject_ref: str
    relation: str
    resource_ref: str
    valid_from: datetime | None = None
    expires_at: datetime | None = None
    status: str = "active"

    def is_active_at(self, now: datetime) -> bool:
        if not is_aware(now) or self.status != "active":
            return False
        now_utc = now.astimezone(UTC)
        start = utc_or_none(self.valid_from)
        end = utc_or_none(self.expires_at)
        if self.valid_from is not None and not is_aware(self.valid_from):
            return False
        if self.expires_at is not None and not is_aware(self.expires_at):
            return False
        return not (start is not None and now_utc < start) and not (end is not None and now_utc >= end)


@dataclass(frozen=True)
class AttributeContext:
    subject_active: bool | None = None
    resource_classification: str | None = None
    classification_ceiling: str | None = None
    object_version: int | None = None
    expected_object_version: int | None = None
    resource_state: str | None = None
    allowed_resource_states: tuple[str, ...] = ()
    delegation_active: bool | None = None
    delegator_active: bool | None = None


@dataclass(frozen=True)
class AuthorizationContext:
    request_id: str
    subject_ref: str
    principal_type: PrincipalType
    action: str
    resource_ref: str
    organization_ref: str | None
    resource_organization_ref: str | None
    resource_scope_refs: tuple[str, ...]
    assignment: RoleAssignment | None
    relations: tuple[RelationTuple, ...] = ()
    now: datetime | None = None
    attributes: AttributeContext = AttributeContext()
    policy_release_id: str = "unreleased"
    delegation: Delegation | None = None

    @property
    def agent_ref(self) -> str | None:
        return self.delegation.agent_ref if self.delegation is not None else None

    @property
    def delegation_ref(self) -> str | None:
        return self.delegation.delegation_ref if self.delegation is not None else None
