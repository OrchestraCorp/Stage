"""The authenticated person's current, separately selectable assignments."""

from typing import Annotated

from fastapi import APIRouter, Query, Request, Security
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse

from app.api.identity import IdentityQuery, bearer
from app.authz.assignments import AssignmentList
from app.core.errors import error_response
from app.core.schemas import ErrorResponse
from app.execution.errors import TenantNotProvisioned
from app.identity.authentication import (
    AuthenticationAssuranceRequired,
    AuthenticationRejected,
    AuthenticationUnavailable,
)
from app.identity.service import IdentityDenied
from app.providers.assignments import ASSIGNMENTS
from app.runtime import MissingService, RuntimeClosed

router = APIRouter(prefix="/identity", tags=["Identity"])


class AssignmentsQuery(IdentityQuery):
    limit: int = Field(default=20, ge=1, le=50)
    after: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[^\s\x00-\x1f\x7f]+$")


@router.get(
    "/assignments",
    operation_id="identity_list_self_assignments",
    response_model=AssignmentList,
    description=(
        "Lists the current human's assignment choices. Each business operation must select one assignment and "
        "revalidate current authorization. Pages are bounded candidate scans: items may be empty while next_after "
        "still supplies the next position. No cross-page snapshot or effective permissions are returned."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "A valid Stage human access token is required"},
        403: {"model": ErrorResponse, "description": "Current human identity or organization admission is unavailable"},
        503: {"model": ErrorResponse, "description": "Identity or assignment discovery is temporarily unavailable"},
    },
)
async def list_self_assignments(
    request: Request,
    query: Annotated[AssignmentsQuery, Query()],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> AssignmentList | JSONResponse:
    if credentials is None:
        return error_response(
            request, 401, "AUTHENTICATION_REQUIRED", "A Bearer access token is required", {"WWW-Authenticate": "Bearer"}
        )
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None or not runtime.ready:
        return error_response(request, 503, "IDENTITY_UNAVAILABLE", "Identity verification is unavailable")
    try:
        async with runtime.host.call(ASSIGNMENTS) as call:
            return await call.require(ASSIGNMENTS).list_self(
                credentials.credentials,
                profile_ref=query.profile_ref,
                profile_version=query.profile_version,
                limit=query.limit,
                after=query.after,
            )
    except AuthenticationAssuranceRequired:
        return error_response(
            request,
            401,
            "MFA_REQUIRED",
            "Fresh multi-factor authentication is required",
            {"WWW-Authenticate": 'Bearer error="insufficient_user_authentication"'},
        )
    except AuthenticationRejected:
        return error_response(
            request,
            401,
            "INVALID_CREDENTIAL",
            "The access token is not valid",
            {"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    except IdentityDenied:
        return error_response(request, 403, "IDENTITY_ACCESS_DENIED", "Identity access is not permitted")
    except (
        AuthenticationUnavailable,
        MissingService,
        RuntimeClosed,
        SQLAlchemyError,
        TimeoutError,
        TenantNotProvisioned,
    ):
        return error_response(request, 503, "IDENTITY_UNAVAILABLE", "Identity verification is unavailable")
