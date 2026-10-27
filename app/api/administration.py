"""Versioned administration HTTP boundary; every authority choice is explicit."""

import json
from collections.abc import Awaitable, Callable, Coroutine
from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Header, Path, Query, Request, Security
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette._utils import get_route_path
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.administration.contracts import (
    ActivationRequest,
    AdminAssignmentList,
    AdministrationMe,
    AuditList,
    GrantRequest,
    OperationResult,
    PeopleList,
    PersonView,
    RevokeRequest,
)
from app.administration.errors import AdminCommitUnknown, AdminConflict, AdminDenied, AdminNotFound
from app.api.identity import IdentityQuery, bearer
from app.core.errors import error_response
from app.core.schemas import ErrorResponse
from app.execution.errors import ContextExpired, ExecutionCleanupFailed, RequestDeadlineExceeded, TenantNotProvisioned
from app.identity.authentication import (
    AuthenticationAssuranceRequired,
    AuthenticationRejected,
    AuthenticationUnavailable,
)
from app.identity.service import IdentityDenied
from app.native_auth.errors import NativeCredentialError
from app.providers.administration import ADMINISTRATION, AdministrationFacade
from app.runtime import MissingService, RuntimeClosed

IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
REFERENCE_PATTERN = r"^[^\s\x00-\x1f\x7f]+$"
Credentials = Annotated[HTTPAuthorizationCredentials | None, Security(bearer)]
PrincipalRef = Annotated[str, Path(min_length=1, max_length=128, pattern=REFERENCE_PATTERN)]
TargetAssignmentRef = Annotated[str, Path(min_length=1, max_length=128, pattern=REFERENCE_PATTERN)]
IdempotencyHeader = Annotated[str, Header(alias="Idempotency-Key", pattern=IDEMPOTENCY_PATTERN, max_length=128)]
IdempotencyPath = Annotated[str, Path(pattern=IDEMPOTENCY_PATTERN, max_length=128)]
MAX_BODY_BYTES = 8192


class AdministrationQuery(IdentityQuery):
    assignment_ref: str = Field(min_length=1, max_length=128, pattern=REFERENCE_PATTERN)


class AdministrationPageQuery(AdministrationQuery):
    limit: int = Field(default=20, ge=1, le=50)
    after: str | None = Field(default=None, min_length=1, max_length=128, pattern=REFERENCE_PATTERN)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_member")
        result[key] = value
    return result


class AdministrationRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def checked(request: Request) -> Response:
            query_keys = [key for key, _ in request.query_params.multi_items()]
            if len(query_keys) != len(set(query_keys)) or len(request.headers.getlist("idempotency-key")) > 1:
                return error_response(request, 422, "REQUEST_INVALID", "Request validation failed")
            if len(request.headers.getlist("authorization")) > 1:
                return error_response(
                    request,
                    401,
                    "INVALID_CREDENTIAL",
                    "The access token is not valid",
                    {"WWW-Authenticate": 'Bearer error="invalid_token"'},
                )
            if request.method == "POST":
                if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    return error_response(request, 415, "REQUEST_INVALID", "A JSON request body is required")
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > MAX_BODY_BYTES:
                        return error_response(request, 413, "REQUEST_INVALID", "Request body is too large")
                # Starlette's Request caches its consumed body here for FastAPI's
                # schema decoder; bounded input is parsed once more for duplicate keys.
                request._body = bytes(body)
                try:
                    json.loads(request._body, object_pairs_hook=_unique_json_object)
                except (ValueError, UnicodeError, RecursionError):
                    return error_response(request, 422, "REQUEST_INVALID", "Request validation failed")
            return await handler(request)

        return checked


_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "A current human token and fresh MFA are required"},
    403: {"model": ErrorResponse, "description": "The selected current administration assignment is not permitted"},
    404: {"model": ErrorResponse, "description": "The requested managed object or operation is unavailable"},
    409: {"model": ErrorResponse, "description": "Current versions, nomination state or original input conflict"},
    413: {"model": ErrorResponse, "description": "The request exceeds the bounded JSON body size"},
    415: {"model": ErrorResponse, "description": "A JSON request body is required"},
    422: {"model": ErrorResponse, "description": "Request validation failed"},
    503: {
        "model": ErrorResponse,
        "description": "Unavailable or commit outcome unknown; preserve the original key",
        "headers": {
            "Location": {
                "schema": {"type": "string"},
                "description": "Recover with current credentials and the original idempotency key",
            }
        },
    },
}
router = APIRouter(
    prefix="/administration", tags=["Administration"], route_class=AdministrationRoute, responses=_ERROR_RESPONSES
)


class AdministrationSecurityMiddleware:
    """Keep successful, rejected, unknown and missing administration responses private."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = get_route_path(scope) if scope["type"] == "http" else ""
        if path != "/api/v1/administration" and not path.startswith("/api/v1/administration/"):
            await self.app(scope, receive, send)
            return

        async def private_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["Pragma"] = "no-cache"
                headers["X-Content-Type-Options"] = "nosniff"
            await send(message)

        await self.app(scope, receive, private_send)


def _recovery_location(
    request: Request, query: IdentityQuery, assignment_ref: str, key: str, collection: str = "operations"
) -> str:
    values = {
        "profile_ref": query.profile_ref,
        "profile_version": query.profile_version,
        "assignment_ref": assignment_ref,
    }
    root = request.scope.get("root_path", "")
    return f"{root}/api/v1/administration/{collection}/{quote(key, safe='')}?{urlencode(values)}"


async def _call[Model: BaseModel](
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    operation: Callable[[AdministrationFacade, str], Awaitable[Model]],
    *,
    query: IdentityQuery,
    idempotency_key: str | None = None,
    recovery_collection: str = "operations",
) -> Model | JSONResponse:
    if credentials is None:
        return error_response(
            request, 401, "AUTHENTICATION_REQUIRED", "A Bearer access token is required", {"WWW-Authenticate": "Bearer"}
        )
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None or not runtime.ready:
        return error_response(request, 503, "ADMINISTRATION_UNAVAILABLE", "Administration is unavailable")
    try:
        async with runtime.host.call(ADMINISTRATION) as call:
            return await operation(call.require(ADMINISTRATION), credentials.credentials)
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
    except (AdminDenied, IdentityDenied):
        return error_response(request, 403, "ADMINISTRATION_ACCESS_DENIED", "Administration access is not permitted")
    except AdminConflict as error:
        return error_response(request, 409, error.code, "The operation conflicts with current state or input")
    except NativeCredentialError:
        return error_response(
            request, 409, "ACTIVATION_UNAVAILABLE", "The current enrollment configuration or state is unavailable"
        )
    except AdminNotFound:
        return error_response(
            request, 404, "ADMINISTRATION_NOT_FOUND", "The managed object or operation is not available"
        )
    except AdminCommitUnknown as error:
        response = error_response(
            request, 503, "COMMIT_OUTCOME_UNKNOWN", "Query the operation with its original idempotency key"
        )
        if idempotency_key is not None:
            response.headers["Location"] = _recovery_location(
                request, query, error.assignment_ref, idempotency_key, recovery_collection
            )
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
        return error_response(request, 503, "ADMINISTRATION_UNAVAILABLE", "Administration is unavailable")


@router.get("/me", operation_id="administration_get_self", response_model=AdministrationMe)
async def me(
    request: Request, query: Annotated[IdentityQuery, Query()], credentials: Credentials
) -> AdministrationMe | JSONResponse:
    async def read(service: AdministrationFacade, token: str) -> AdministrationMe:
        return await service.me(token, **query.model_dump())

    return await _call(request, credentials, read, query=query)


@router.post("/activation", operation_id="administration_activate", response_model=OperationResult)
async def activate(
    request: Request,
    query: Annotated[IdentityQuery, Query()],
    credentials: Credentials,
    payload: ActivationRequest,
    idempotency_key: IdempotencyHeader,
) -> OperationResult | JSONResponse:
    async def update(service: AdministrationFacade, token: str) -> OperationResult:
        return await service.activate(
            token,
            **query.model_dump(),
            request=payload,
            request_id=request.state.request_id,
            idempotency_key=idempotency_key,
        )

    return await _call(request, credentials, update, query=query, idempotency_key=idempotency_key)


@router.get("/people", operation_id="administration_list_people", response_model=PeopleList)
async def people(
    request: Request, query: Annotated[AdministrationPageQuery, Query()], credentials: Credentials
) -> PeopleList | JSONResponse:
    async def read(service: AdministrationFacade, token: str) -> PeopleList:
        return await service.people(token, **query.model_dump())

    return await _call(request, credentials, read, query=query)


@router.get("/people/{principal_ref}", operation_id="administration_get_person", response_model=PersonView)
async def person(
    request: Request,
    principal_ref: PrincipalRef,
    query: Annotated[AdministrationQuery, Query()],
    credentials: Credentials,
) -> PersonView | JSONResponse:
    async def read(service: AdministrationFacade, token: str) -> PersonView:
        return await service.person(token, **query.model_dump(), principal_ref=principal_ref)

    return await _call(request, credentials, read, query=query)


@router.get(
    "/people/{principal_ref}/assignments",
    operation_id="administration_list_assignments",
    response_model=AdminAssignmentList,
)
async def assignments(
    request: Request,
    principal_ref: PrincipalRef,
    query: Annotated[AdministrationPageQuery, Query()],
    credentials: Credentials,
) -> AdminAssignmentList | JSONResponse:
    async def read(service: AdministrationFacade, token: str) -> AdminAssignmentList:
        return await service.assignments(token, **query.model_dump(), principal_ref=principal_ref)

    return await _call(request, credentials, read, query=query)


@router.post(
    "/people/{principal_ref}/assignments",
    operation_id="administration_grant_assignment",
    response_model=OperationResult,
)
async def grant(
    request: Request,
    principal_ref: PrincipalRef,
    query: Annotated[AdministrationQuery, Query()],
    credentials: Credentials,
    payload: GrantRequest,
    idempotency_key: IdempotencyHeader,
) -> OperationResult | JSONResponse:
    async def update(service: AdministrationFacade, token: str) -> OperationResult:
        return await service.grant(
            token,
            **query.model_dump(),
            principal_ref=principal_ref,
            request=payload,
            request_id=request.state.request_id,
            idempotency_key=idempotency_key,
        )

    return await _call(request, credentials, update, query=query, idempotency_key=idempotency_key)


@router.post(
    "/people/{principal_ref}/assignments/{target_assignment_ref}/revoke",
    operation_id="administration_revoke_assignment",
    response_model=OperationResult,
)
async def revoke(
    request: Request,
    principal_ref: PrincipalRef,
    target_assignment_ref: TargetAssignmentRef,
    query: Annotated[AdministrationQuery, Query()],
    credentials: Credentials,
    payload: RevokeRequest,
    idempotency_key: IdempotencyHeader,
) -> OperationResult | JSONResponse:
    async def update(service: AdministrationFacade, token: str) -> OperationResult:
        return await service.revoke(
            token,
            **query.model_dump(),
            principal_ref=principal_ref,
            target_assignment_ref=target_assignment_ref,
            request=payload,
            request_id=request.state.request_id,
            idempotency_key=idempotency_key,
        )

    return await _call(request, credentials, update, query=query, idempotency_key=idempotency_key)


@router.get(
    "/operations/{idempotency_key}", operation_id="administration_get_operation", response_model=OperationResult
)
async def operation(
    request: Request,
    idempotency_key: IdempotencyPath,
    query: Annotated[AdministrationQuery, Query()],
    credentials: Credentials,
) -> OperationResult | JSONResponse:
    async def read(service: AdministrationFacade, token: str) -> OperationResult:
        return await service.operation(token, **query.model_dump(), idempotency_key=idempotency_key)

    return await _call(request, credentials, read, query=query, idempotency_key=idempotency_key)


@router.get("/audit", operation_id="administration_list_audit", response_model=AuditList)
async def audit(
    request: Request, query: Annotated[AdministrationPageQuery, Query()], credentials: Credentials
) -> AuditList | JSONResponse:
    async def read(service: AdministrationFacade, token: str) -> AuditList:
        return await service.audit(token, **query.model_dump())

    return await _call(request, credentials, read, query=query)
