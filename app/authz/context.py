"""Trusted server-side authorization inputs; these are not request DTOs.

The authenticated principal and selected assignment must be loaded by a service.
Resource organization, scope ancestors, relations, attributes and delegation are
read from authoritative state, never accepted as claims from a client or Agent.
References are globally namespaced; loaders must additionally filter by tenant.
Frozen inputs prevent accidental reassignment, not forged or stale evidence.
Write commands must recheck critical facts and versions inside their transaction.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from app.authz.permissions import BUILTIN_POLICY_RELEASE_ID
from app.core.enums import PrincipalType


def is_aware(value: datetime | None) -> bool:
    return value is not None and value.tzinfo is not None and value.utcoffset() is not None


def utc_or_none(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if not is_aware(value):
        return value
    return value.astimezone(UTC)


def validity_failure(
    now: datetime,
    *,
    valid_from: datetime | None,
    expires_at: datetime | None,
    status: str,
    revoked_at: datetime | None = None,
) -> str | None:
    """Describe a validity failure without conflating revocation with expiry."""
    if revoked_at is not None or status == "revoked":
        return "revoked"
    if status != "active":
        return "inactive"
    if not is_aware(now) or any(value is not None and not is_aware(value) for value in (valid_from, expires_at)):
        return "time.invalid"
    start, end = utc_or_none(valid_from), utc_or_none(expires_at)
    if start is not None and end is not None and start >= end:
        return "time.invalid_window"
    if start is not None and now < start:
        return "not_yet_valid"
    if end is not None and now >= end:
        return "expired"
    return None


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

    def inactive_reason_at(self, now: datetime) -> str | None:
        return validity_failure(now, valid_from=self.valid_from, expires_at=self.expires_at, status=self.status)

    def is_active_at(self, now: datetime) -> bool:
        return self.inactive_reason_at(now) is None

    def covers(self, *, organization_ref: str, resource_scopes: tuple[str, ...]) -> bool:
        """Match an explicitly granted scope against authoritative ancestors.

        ``organization_ref`` establishes tenant identity; it is not itself a
        grant. An organization-wide scope must be explicitly assigned rather
        than appended automatically to every department assignment.
        """
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

    def inactive_reason_at(self, now: datetime) -> str | None:
        return validity_failure(
            now,
            valid_from=self.valid_from,
            expires_at=self.expires_at,
            status=self.status,
            revoked_at=self.revoked_at,
        )

    def is_active_at(self, now: datetime) -> bool:
        return self.inactive_reason_at(now) is None

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
        return validity_failure(now, valid_from=self.valid_from, expires_at=self.expires_at, status=self.status) is None


@dataclass(frozen=True)
class AttributeContext:
    subject_active: bool | None = None
    resource_classification: str | None = None
    classification_ceiling: str | None = None
    object_version: int | None = None
    expected_object_version: int | None = None
    resource_state: str | None = None
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
    policy_release_id: str = BUILTIN_POLICY_RELEASE_ID
    delegation: Delegation | None = None

    @property
    def agent_ref(self) -> str | None:
        return self.delegation.agent_ref if self.delegation is not None else None

    @property
    def delegation_ref(self) -> str | None:
        return self.delegation.delegation_ref if self.delegation is not None else None
