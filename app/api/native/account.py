"""Authenticated account security, sessions and application authorizations."""

import json
import re
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Request, Security
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import Response

from app.api.native.common import (
    NativeHTTPError,
    NativeRoute,
    bearer,
    bearer_token,
    native_services,
    parse_pairs,
    read_body,
    require_fields,
    require_same_origin,
)
from app.core.schemas import ErrorResponse

account_router = APIRouter(
    prefix="/account",
    tags=["Account security"],
    route_class=NativeRoute,
    responses={status: {"model": ErrorResponse} for status in (400, 401, 403, 404, 413, 429, 503)},
)


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    old_password: str = Field(min_length=1, max_length=256, repr=False)
    new_password: str = Field(min_length=12, max_length=256, repr=False)


class TotpReset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    password: str = Field(min_length=1, max_length=256, repr=False)
    otp: str = Field(pattern=r"^[0-9]{6}$", repr=False)


class FactorResetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential_version: int = Field(gt=0)
    enrollment_code: str = Field(
        min_length=20,
        max_length=128,
        repr=False,
        description=(
            "Shown once. Save this code, sign in again with your password, "
            "and enter it in the recovery form to bind a new authenticator."
        ),
    )


class AccountSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_ref: str
    client_id: str
    client_display_name: str
    auth_time: datetime
    expires_at: datetime
    revoked_at: datetime | None
    current: bool
    active: bool


class AccountSessionPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[AccountSession]
    next_after: str | None


class AccountConsent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: str
    client_display_name: str
    client_version: int
    scopes: list[str]
    granted_at: datetime
    current: bool


class AccountConsentPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[AccountConsent]
    next_after: str | None


class CredentialVersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential_version: int = Field(gt=0)


_ACCOUNT_PAGE_CONTRACT = {
    "parameters": [
        {
            "name": "limit",
            "in": "query",
            "required": False,
            "schema": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        },
        {
            "name": "after",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "minLength": 1, "maxLength": 128},
        },
    ]
}


def _account_page(request: Request) -> dict[str, Any]:
    values = parse_pairs(request.scope.get("query_string", b""))
    require_fields(values, set(), {"limit", "after"})
    if "limit" in values and not re.fullmatch(r"[0-9]{1,2}", values["limit"]):
        raise NativeHTTPError()
    limit = int(values.get("limit", "20"))
    if not 1 <= limit <= 50:
        raise NativeHTTPError()
    return {"limit": limit, "after": values.get("after")}


async def _empty_account_operation(request: Request) -> None:
    if request.url.query or await read_body(request):
        raise NativeHTTPError()


@account_router.get(
    "/sessions",
    operation_id="account_list_sessions",
    response_model=AccountSessionPage,
    openapi_extra=_ACCOUNT_PAGE_CONTRACT,
)
async def account_sessions(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> dict[str, Any]:
    query = _account_page(request)
    async with native_services(request) as (_facade, services):
        return await services.sessions.list_sessions(bearer_token(credentials, request), **query)


@account_router.delete(
    "/sessions/{session_ref}", operation_id="account_revoke_session", status_code=204, response_class=Response
)
async def account_revoke_session(
    request: Request,
    session_ref: str,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> Response:
    await _empty_account_operation(request)
    async with native_services(request) as (facade, services):
        require_same_origin(request, facade.config.issuer)
        await services.sessions.revoke_session(bearer_token(credentials, request), session_ref)
    return Response(status_code=204)


@account_router.post(
    "/sessions/revoke-all",
    operation_id="account_revoke_all_sessions",
    response_model=CredentialVersionResponse,
)
async def account_revoke_all_sessions(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> dict[str, Any]:
    await _empty_account_operation(request)
    async with native_services(request) as (facade, services):
        require_same_origin(request, facade.config.issuer)
        return await services.sessions.revoke_all(bearer_token(credentials, request))


@account_router.get(
    "/consents",
    operation_id="account_list_consents",
    response_model=AccountConsentPage,
    openapi_extra=_ACCOUNT_PAGE_CONTRACT,
)
async def account_consents(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> dict[str, Any]:
    query = _account_page(request)
    async with native_services(request) as (_facade, services):
        return await services.sessions.list_consents(bearer_token(credentials, request), **query)


@account_router.delete(
    "/consents/{client_id}", operation_id="account_revoke_consent", status_code=204, response_class=Response
)
async def account_revoke_consent(
    request: Request,
    client_id: str,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> Response:
    await _empty_account_operation(request)
    async with native_services(request) as (facade, services):
        require_same_origin(request, facade.config.issuer)
        await services.sessions.revoke_consent(bearer_token(credentials, request), client_id)
    return Response(status_code=204)


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


async def _account_body(request: Request) -> Any:
    if (
        request.url.query
        or request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"
    ):
        raise NativeHTTPError()
    try:
        return json.loads(await read_body(request), object_pairs_hook=_json_pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise NativeHTTPError() from None


def _json_contract(model: type[BaseModel]) -> dict[str, Any]:
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": model.model_json_schema()}}}}


@account_router.post(
    "/password",
    operation_id="account_change_password",
    status_code=204,
    response_class=Response,
    openapi_extra=_json_contract(PasswordChange),
)
async def change_password(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> Response:
    try:
        values = PasswordChange.model_validate(await _account_body(request))
    except ValidationError:
        raise NativeHTTPError() from None
    async with native_services(request) as (facade, services):
        require_same_origin(request, facade.config.issuer)
        claims = await services.oauth.authenticate_account(bearer_token(credentials, request), max_age_seconds=300)
        await services.credentials.change_password(
            claims["sid"], values.old_password, values.new_password, access_claims=claims
        )
    return Response(status_code=204)


@account_router.post(
    "/totp/reset",
    operation_id="account_reset_totp",
    response_model=FactorResetResponse,
    openapi_extra=_json_contract(TotpReset),
)
async def reset_totp(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> FactorResetResponse:
    try:
        values = TotpReset.model_validate(await _account_body(request))
    except ValidationError:
        raise NativeHTTPError() from None
    async with native_services(request) as (facade, services):
        require_same_origin(request, facade.config.issuer)
        claims = await services.oauth.authenticate_account(bearer_token(credentials, request), max_age_seconds=300)
        result = await services.credentials.reset_totp(claims["sid"], values.password, values.otp, access_claims=claims)
    return FactorResetResponse(credential_version=result.credential_version, enrollment_code=result.enrollment_code)
