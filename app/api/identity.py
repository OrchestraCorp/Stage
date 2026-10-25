"""The caller's identity under this deployment's single organization authorization root."""

from typing import Annotated

from fastapi import APIRouter, Query, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import error_response
from app.core.schemas import ErrorResponse
from app.execution.errors import TenantNotProvisioned
from app.identity.authentication import AuthenticationRejected, AuthenticationUnavailable
from app.identity.service import IdentityDenied, SelfIdentity
from app.providers.identity import IDENTITY
from app.runtime import MissingService, RuntimeClosed

router = APIRouter(prefix="/identity", tags=["Identity"])
bearer = HTTPBearer(auto_error=False)


class IdentityQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_ref: str = Field(min_length=1, max_length=128)
    profile_version: int = Field(ge=1, le=2**63 - 1)


class IdentityCacheControlMiddleware:
    """Keep every identity response private, including validation and failure responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/api/v1/identity" or path.startswith("/api/v1/identity/")):
            await self.app(scope, receive, send)
            return

        async def private_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, private_send)


@router.get(
    "/me",
    operation_id="identity_get_self",
    response_model=SelfIdentity,
    responses={
        401: {"model": ErrorResponse, "description": "A valid Stage access token is required"},
        403: {"model": ErrorResponse, "description": "Current identity or organization admission is unavailable"},
        503: {"model": ErrorResponse, "description": "Identity verification is temporarily unavailable"},
    },
)
async def read_self_identity(
    request: Request,
    query: Annotated[IdentityQuery, Query()],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> SelfIdentity | JSONResponse:
    if credentials is None:
        result = error_response(
            request, 401, "AUTHENTICATION_REQUIRED", "A Bearer access token is required", {"WWW-Authenticate": "Bearer"}
        )
    else:
        runtime = getattr(request.app.state, "runtime", None)
        if runtime is None or not runtime.ready:
            result = error_response(request, 503, "IDENTITY_UNAVAILABLE", "Identity verification is unavailable")
        else:
            try:
                async with runtime.host.call(IDENTITY) as call:
                    return await call.require(IDENTITY).read_self(
                        credentials.credentials,
                        profile_ref=query.profile_ref,
                        profile_version=query.profile_version,
                    )
            except AuthenticationRejected:
                result = error_response(
                    request,
                    401,
                    "INVALID_CREDENTIAL",
                    "The access token is not valid",
                    {"WWW-Authenticate": 'Bearer error="invalid_token"'},
                )
            except IdentityDenied:
                result = error_response(request, 403, "IDENTITY_ACCESS_DENIED", "Identity access is not permitted")
            except (
                AuthenticationUnavailable,
                MissingService,
                RuntimeClosed,
                SQLAlchemyError,
                TimeoutError,
                TenantNotProvisioned,
            ):
                result = error_response(request, 503, "IDENTITY_UNAVAILABLE", "Identity verification is unavailable")
    return result
