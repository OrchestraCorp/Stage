"""Onboarding contracts keep privilege inputs and once-only secrets out of recovery."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from app.administration.onboarding_contracts import (
    EmployeeActivationChange,
    EmployeeActivationReissue,
    EmployeeCreateRequest,
    EmployeeDelivery,
    EmployeeOperationResult,
)


def operation(**changes: Any) -> EmployeeOperationResult:
    now = datetime.now(UTC)
    return EmployeeOperationResult.model_validate(
        {
            "operation_ref": "operation:example",
            "operation_type": "person.create",
            "status": "succeeded",
            "result": {
                "activation_ref": "activation:example",
                "principal_ref": "person:example",
                "membership_ref": "membership:example",
                "account_ref": "account:example",
                "username": "employee.example",
                "status": "issued",
                "version": 1,
                "expires_at": now + timedelta(hours=1),
                "completed_at": None,
            },
            "replayed": False,
            "completed_at": now,
            **changes,
        }
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"role_ref": "role:stage-administrator"},
        {"assignment_ref": "assignment:admin"},
        {"principal_ref": "person:existing"},
        {"password": "preconfigured-secret"},
        {"totp_secret": "preconfigured-factor"},
        {"status": "active"},
    ],
)
def test_employee_creation_cannot_choose_identity_rights_or_credentials(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        EmployeeCreateRequest.model_validate(
            {"username": "employee.example", "expected_authority_version": 3, "reason": "New employee", **extra}
        )


@pytest.mark.parametrize("username", ["Employee", " employee", "employee ", "员工", "employee\x00"])
def test_username_requires_exact_native_login_spelling(username: str) -> None:
    with pytest.raises(ValidationError):
        EmployeeCreateRequest(username=username, expected_authority_version=3, reason="New employee")


@pytest.mark.parametrize("request_type", [EmployeeCreateRequest, EmployeeActivationChange, EmployeeActivationReissue])
def test_request_reasons_share_explicit_text_validation(request_type: Any) -> None:
    value = (
        {"username": "employee.example"}
        if request_type is EmployeeCreateRequest
        else {"expected_activation_version": 1}
    )
    for reason in (" ", "bad\x00reason", "bad\ud800reason"):
        with pytest.raises(ValidationError):
            request_type.model_validate({"expected_authority_version": 3, "reason": reason, **value})


@pytest.mark.parametrize("changes", [{"replayed": True}, {"operation_type": "person.activation.revoke"}])
def test_replay_or_revoke_can_never_serialize_activation_secret(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        EmployeeDelivery(
            operation=operation(**changes), activation_code="x" * 43, activation_url="https://id.example/oauth/activate"
        )
    recovered = EmployeeDelivery(operation=operation(**changes), activation_url="https://id.example/oauth/activate")
    assert recovered.activation_code is None


def test_delivery_keeps_secret_out_of_safe_result_and_python_representation() -> None:
    code = "x" * 43
    delivered = EmployeeDelivery(
        operation=operation(), activation_code=code, activation_url="https://id.example/oauth/activate"
    )
    assert code not in repr(delivered)
    assert code not in delivered.operation.model_dump_json()
    assert delivered.model_dump()["activation_code"] == code
    with pytest.raises(ValidationError):
        EmployeeOperationResult.model_validate({**operation().model_dump(), "activation_code": code})


@pytest.mark.parametrize("value", [1, "true", None])
def test_replay_marker_is_explicit_boolean(value: object) -> None:
    with pytest.raises(ValidationError):
        operation(replayed=value)
