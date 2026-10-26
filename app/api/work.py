"""Versioned human work metadata API; callers never supply execution identity."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Header, Path, Query, Request, Security
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse

from app.api.identity import IdentityQuery, bearer
from app.core.errors import error_response
from app.core.schemas import ErrorResponse
from app.execution.contracts import CommandResult
from app.execution.errors import (
    AuthorizationDenied,
    CommitOutcomeUnknown,
    ContextExpired,
    ExecutionCleanupFailed,
    ExecutionNotFound,
    IdempotencyConflict,
    InvalidCommand,
    ObjectVersionConflict,
    RequestDeadlineExceeded,
    TenantNotProvisioned,
)
from app.features.work.details import WorkDetailsConflict, WorkDetailsInput, WorkDetailsResult
from app.identity.authentication import (
    AuthenticationAssuranceRequired,
    AuthenticationRejected,
    AuthenticationUnavailable,
)
from app.identity.service import IdentityDenied
from app.providers.work import WORK, HumanWorkService, WorkItemView
from app.runtime import MissingService, RuntimeClosed

router = APIRouter(prefix="/work-items", tags=["Work"])
IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
WorkItemID = Annotated[int, Path(ge=1, le=2**31 - 1)]
Credentials = Annotated[HTTPAuthorizationCredentials | None, Security(bearer)]


class WorkQuery(IdentityQuery):
    assignment_ref: str = Field(min_length=1, max_length=128, pattern=r"^[^\s\x00-\x1f\x7f]+$")


class UpdateWorkDetailsRequest(WorkDetailsInput):
    expected_version: int = Field(ge=1, le=2**31 - 1)


class WorkOperationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    execution_id: str
    status: Literal["succeeded"]
    result: WorkDetailsResult
    replayed: bool
    completed_at: datetime

    @classmethod
    def from_command(cls, result: CommandResult) -> "WorkOperationResult":
        return cls(
            execution_id=result.execution_id,
            status="succeeded",
            result=WorkDetailsResult.model_validate(result.result),
            replayed=result.replayed,
            completed_at=result.completed_at,
        )


_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "A current human access token is required"},
    403: {"model": ErrorResponse, "description": "Current identity, assignment or resource access is not permitted"},
    409: {"model": ErrorResponse, "description": "Object version, editable state or idempotency input conflicts"},
    503: {
        "model": ErrorResponse,
        "description": "Temporarily unavailable or commit outcome unknown; retain the original idempotency key",
        "headers": {
            "Location": {
                "description": "For an unknown commit outcome, query this URL with current credentials",
                "schema": {"type": "string"},
            }
        },
    },
}


def _recovery_location(request: Request, query: WorkQuery, work_item_id: int, idempotency_key: str) -> str:
    root = request.scope.get("root_path", "")
    path = f"{root}/api/v1/work-items/{work_item_id}/operations/{quote(idempotency_key, safe='')}"
    return f"{path}?{urlencode(query.model_dump())}"


async def _call_work[Model: BaseModel](
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    operation: Callable[[HumanWorkService, str], Awaitable[Model]],
    *,
    recovery_location: str | None = None,
) -> Model | JSONResponse:
    if credentials is None:
        return error_response(
            request, 401, "AUTHENTICATION_REQUIRED", "A Bearer access token is required", {"WWW-Authenticate": "Bearer"}
        )
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None or not runtime.ready:
        return error_response(request, 503, "WORK_UNAVAILABLE", "Work operations are unavailable")
    try:
        async with runtime.host.call(WORK) as call:
            return await operation(call.require(WORK), credentials.credentials)
    except AuthenticationAssuranceRequired:
        return error_response(
            request,
            401,
            "MFA_REQUIRED",
            "Fresh multi-factor authentication is required",
            {"WWW-Authenticate": 'Bearer error="insufficient_user_authentication"'},
        )
    except (AuthenticationRejected, ContextExpired):
        return error_response(
            request,
            401,
            "INVALID_CREDENTIAL",
            "The access token or current authentication is not valid",
            {"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    except (ObjectVersionConflict, WorkDetailsConflict, IdempotencyConflict) as error:
        return error_response(request, 409, error.code, "The operation conflicts with the current object or input")
    except (IdentityDenied, AuthorizationDenied):
        return error_response(request, 403, "WORK_ACCESS_DENIED", "Work access is not permitted")
    except ExecutionNotFound:
        return error_response(request, 404, "OPERATION_NOT_FOUND", "The operation is not available")
    except InvalidCommand:
        return error_response(request, 422, "COMMAND_INVALID", "Command validation failed")
    except CommitOutcomeUnknown:
        response = error_response(
            request, 503, "COMMIT_OUTCOME_UNKNOWN", "Query the operation with its original idempotency key"
        )
        if recovery_location is not None:
            response.headers["Location"] = recovery_location
        return response
    except (
        AuthenticationUnavailable,
        MissingService,
        RuntimeClosed,
        SQLAlchemyError,
        TimeoutError,
        RequestDeadlineExceeded,
        TenantNotProvisioned,
        ExecutionCleanupFailed,
    ):
        return error_response(request, 503, "WORK_UNAVAILABLE", "Work operations are unavailable")


@router.get(
    "/{work_item_id}",
    operation_id="work_get_item",
    response_model=WorkItemView,
    responses=_ERROR_RESPONSES,
)
async def read_work_item(
    request: Request,
    work_item_id: WorkItemID,
    query: Annotated[WorkQuery, Query()],
    credentials: Credentials,
) -> WorkItemView | JSONResponse:
    async def read(service: HumanWorkService, token: str) -> WorkItemView:
        return await service.get_item(
            token,
            work_item_id=work_item_id,
            **query.model_dump(),
            request_id=request.state.request_id,
        )

    return await _call_work(request, credentials, read)


@router.put(
    "/{work_item_id}/details",
    operation_id="work_update_details",
    response_model=WorkOperationResult,
    responses=_ERROR_RESPONSES,
    description=(
        "Replace the title and description of an existing draft/open work item. "
        "Requires current task.metadata.update authority and owner_of relation. "
        "The description must be explicit text or null. "
        "Retain the same idempotency key and input when recovering an uncertain result. "
        "This operation does not claim, deliver, approve or change the item's lifecycle."
    ),
)
async def update_work_details(
    request: Request,
    work_item_id: WorkItemID,
    query: Annotated[WorkQuery, Query()],
    credentials: Credentials,
    payload: UpdateWorkDetailsRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", pattern=IDEMPOTENCY_PATTERN, max_length=128)],
) -> WorkOperationResult | JSONResponse:
    async def update(service: HumanWorkService, token: str) -> WorkOperationResult:
        result = await service.update_details(
            token,
            work_item_id=work_item_id,
            **query.model_dump(),
            request_id=request.state.request_id,
            idempotency_key=idempotency_key,
            expected_version=payload.expected_version,
            details=WorkDetailsInput(title=payload.title, description=payload.description),
        )
        return WorkOperationResult.from_command(result)

    return await _call_work(
        request,
        credentials,
        update,
        recovery_location=_recovery_location(request, query, work_item_id, idempotency_key),
    )


@router.get(
    "/{work_item_id}/operations/{idempotency_key}",
    operation_id="work_get_operation",
    response_model=WorkOperationResult,
    responses={
        **_ERROR_RESPONSES,
        404: {"model": ErrorResponse, "description": "Operation not found or not available"},
    },
    description=(
        "Recover this caller's work.details.update result by its original idempotency key "
        "with a currently authorized selected assignment."
    ),
)
async def read_work_operation(
    request: Request,
    work_item_id: WorkItemID,
    idempotency_key: Annotated[str, Path(pattern=IDEMPOTENCY_PATTERN, max_length=128)],
    query: Annotated[WorkQuery, Query()],
    credentials: Credentials,
) -> WorkOperationResult | JSONResponse:
    async def recover(service: HumanWorkService, token: str) -> WorkOperationResult:
        result = await service.get_operation(
            token,
            work_item_id=work_item_id,
            **query.model_dump(),
            request_id=request.state.request_id,
            idempotency_key=idempotency_key,
        )
        return WorkOperationResult.from_command(result)

    return await _call_work(
        request,
        credentials,
        recover,
        recovery_location=_recovery_location(request, query, work_item_id, idempotency_key),
    )
