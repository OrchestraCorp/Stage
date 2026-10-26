"""Versioned administration contracts contain explicit actors' selected scope, never credentials."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.authz.management import GrantRule


def _valid_utf8(value: str) -> str:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ValueError("reference_must_be_valid_unicode") from None
    return value


Reference = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=128, pattern=r"^[^\s\x00-\x1f\x7f]+$"),
    AfterValidator(_valid_utf8),
]
ScopeReference = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=256, pattern=r"^[^\s\x00-\x1f\x7f]+$"),
    AfterValidator(_valid_utf8),
]
Version = Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
OperationType = Literal["activation", "assignment.grant", "assignment.revoke"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthorityView(Contract):
    authority_ref: Reference
    status: Literal["pending", "active", "revoked"]
    version: Version
    assignment_ref: Reference
    nomination_expires_at: datetime | None
    accepted_at: datetime | None
    grant_rules: tuple[GrantRule, ...]


class AdministrationMe(Contract):
    principal_ref: Reference
    membership_ref: Reference
    authority: AuthorityView | None


class ActivationRequest(Contract):
    expected_authority_version: Version


class GrantRequest(Contract):
    membership_ref: Reference
    expected_membership_version: Version
    expected_authority_version: Version
    role_ref: Reference
    role_revision: Version
    scope_ref: ScopeReference
    expires_at: AwareDatetime
    reason: str = Field(strict=True, min_length=1, max_length=500)

    @field_validator("expires_at", mode="before")
    @classmethod
    def explicit_datetime(cls, value: object) -> object:
        if not isinstance(value, (str, datetime)):
            raise ValueError("explicit_aware_datetime_required")
        return value

    @field_validator("expires_at")
    @classmethod
    def utc_expiration(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        if not value.strip() or any(ord(char) == 0 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("reason_must_be_nonempty_text")
        return value.strip()


class RevokeRequest(Contract):
    expected_assignment_version: Version
    expected_authority_version: Version
    reason: str = Field(strict=True, min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        if not value.strip() or any(ord(char) == 0 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("reason_must_be_nonempty_text")
        return value.strip()


class PersonView(Contract):
    principal_ref: Reference
    principal_status: str
    principal_version: Version
    membership_ref: Reference
    membership_status: str
    membership_version: Version
    valid_from: datetime
    expires_at: datetime | None


class PeopleList(Contract):
    items: tuple[PersonView, ...]
    next_after: Reference | None


class AdminAssignmentView(Contract):
    principal_ref: Reference
    membership_ref: Reference
    assignment_ref: Reference
    role_ref: Reference
    role_revision: Version
    role_code: str
    role_display_name: str
    scope_ref: ScopeReference
    valid_from: datetime
    expires_at: datetime | None
    status: str
    version: Version


class AdminAssignmentList(Contract):
    items: tuple[AdminAssignmentView, ...]
    next_after: Reference | None


class OperationResult(Contract):
    operation_ref: Reference
    status: Literal["succeeded"]
    operation_type: OperationType
    result: AdminAssignmentView
    replayed: bool = Field(strict=True)
    completed_at: datetime


class AuditView(Contract):
    operation_ref: Reference
    operation_type: OperationType
    actor_ref: Reference
    assignment_ref: Reference
    request_id: str
    target_ref: Reference
    reason: str | None
    completed_at: datetime
    result: AdminAssignmentView


class AuditList(Contract):
    items: tuple[AuditView, ...]
    next_after: Reference | None
