"""Safe onboarding results are distinct from the once-only delivery envelope."""

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, AwareDatetime, Field, field_validator, model_validator

from app.administration.contracts import Contract, Reference, RevokeRequest, Version
from app.native_auth.crypto import normalize_username
from app.native_auth.errors import NativeCredentialError

Reason = Annotated[
    str, Field(strict=True, min_length=1, max_length=500), AfterValidator(RevokeRequest.meaningful_reason)
]


class EmployeeCreateRequest(Contract):
    username: str = Field(strict=True, min_length=1, max_length=128)
    expected_authority_version: Version
    expires_in_seconds: int = Field(default=3600, strict=True, ge=60, le=86400)
    reason: Reason

    @field_validator("username")
    @classmethod
    def exact_username(cls, value: str) -> str:
        try:
            return normalize_username(value)
        except NativeCredentialError:
            raise ValueError("exact_native_username_required") from None


class EmployeeActivationChange(Contract):
    expected_authority_version: Version
    expected_activation_version: Version
    reason: Reason


class EmployeeActivationReissue(EmployeeActivationChange):
    expires_in_seconds: int = Field(default=3600, strict=True, ge=60, le=86400)


class EmployeeActivationView(Contract):
    activation_ref: Reference
    principal_ref: Reference
    membership_ref: Reference
    account_ref: Reference
    username: str
    status: Literal["issued", "enrolling", "completed", "revoked"]
    version: Version
    expires_at: AwareDatetime
    completed_at: AwareDatetime | None


class EmployeeOperationResult(Contract):
    operation_ref: Reference
    operation_type: Literal["person.create", "person.activation.reissue", "person.activation.revoke"]
    status: Literal["succeeded"] = "succeeded"
    result: EmployeeActivationView
    replayed: bool = Field(strict=True)
    completed_at: AwareDatetime


class EmployeeDelivery(Contract):
    operation: EmployeeOperationResult
    activation_code: str | None = Field(
        default=None, repr=False, min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$"
    )
    activation_url: str

    @model_validator(mode="after")
    def once_only_delivery(self) -> Self:
        if self.activation_code is not None and (
            self.operation.replayed or self.operation.operation_type == "person.activation.revoke"
        ):
            raise ValueError("activation_secret_delivery_not_allowed")
        return self


class EmployeeAuditView(Contract):
    operation: EmployeeOperationResult
    actor_ref: Reference
    assignment_ref: Reference
    request_id: str
    reason: str


class EmployeeAuditList(Contract):
    items: tuple[EmployeeAuditView, ...]
    next_after: Reference | None
