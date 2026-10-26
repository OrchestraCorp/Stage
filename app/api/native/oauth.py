"""OAuth/OIDC metadata, client authentication, tokens, revocation and UserInfo."""

import base64
import binascii
import re
from typing import Annotated, Any, Literal
from urllib.parse import unquote_to_bytes

from fastapi import Request, Security
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response

from app.api.native.common import (
    MAX_FORM_BYTES,
    NativeHTTPError,
    bearer,
    bearer_token,
    form_contract,
    make_oauth_router,
    native_services,
    read_body,
    read_form,
    require_fields,
)
from app.native_auth.protocol import MFA_ACR

router = make_oauth_router()


class TokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_token: str = Field(repr=False)
    token_type: Literal["Bearer"]
    expires_in: int = Field(gt=0, le=600)
    refresh_token: str = Field(repr=False)
    id_token: str = Field(repr=False)
    scope: str


class UserInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sub: str
    preferred_username: str | None = None


def client_authentication(request: Request, values: dict[str, str]) -> dict[str, Any]:
    """RFC 6749 Basic credentials have form-encoded components before base64."""
    headers = request.headers.getlist("authorization")
    if not headers:
        client_id = values.get("client_id")
        if not client_id:
            raise NativeHTTPError("invalid_client", 401)
        return {"client_id": client_id, "client_auth_method": "none"}
    if len(headers) != 1 or len(headers[0]) > MAX_FORM_BYTES:
        raise NativeHTTPError("invalid_client", 401)
    scheme, separator, encoded = headers[0].partition(" ")
    if not separator or scheme.lower() != "basic":
        raise NativeHTTPError("invalid_client", 401)
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8", "strict")
        raw_client, separator, raw_secret = decoded.partition(":")
        if not separator or re.search(r"%(?![0-9a-fA-F]{2})", decoded):
            raise ValueError
        client_id, client_secret = (
            unquote_to_bytes(value.replace("+", " ")).decode("utf-8", "strict") for value in (raw_client, raw_secret)
        )
        if not client_id or not client_secret or values.get("client_id", client_id) != client_id:
            raise ValueError
    except (ValueError, UnicodeError, binascii.Error):
        raise NativeHTTPError("invalid_client", 401) from None
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_auth_method": "client_secret_basic",
    }


@router.get("/.well-known/openid-configuration", operation_id="native_oidc_discovery")
async def discovery(request: Request) -> dict[str, Any]:
    async with native_services(request) as (facade, _services):
        issuer = facade.config.issuer
        return {
            "issuer": issuer,
            "authorization_endpoint": issuer + "/oauth/authorize",
            "token_endpoint": issuer + "/oauth/token",
            "userinfo_endpoint": issuer + "/oauth/userinfo",
            "revocation_endpoint": issuer + "/oauth/revoke",
            "end_session_endpoint": issuer + "/oauth/logout",
            "jwks_uri": issuer + "/oauth/jwks",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query", "form_post"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
            "revocation_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["openid", "profile", "offline_access"],
            "claims_supported": [
                "iss",
                "sub",
                "aud",
                "exp",
                "iat",
                "auth_time",
                "acr",
                "amr",
                "nonce",
                "sid",
                "azp",
                "at_hash",
                "preferred_username",
            ],
            "acr_values_supported": [MFA_ACR],
            "authorization_response_iss_parameter_supported": True,
            "claims_parameter_supported": False,
            "request_parameter_supported": False,
            "request_uri_parameter_supported": False,
        }


@router.get("/.well-known/oauth-authorization-server", operation_id="native_oauth_metadata")
async def oauth_metadata(request: Request) -> dict[str, Any]:
    metadata = await discovery(request)
    oauth_fields = {
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "revocation_endpoint",
        "jwks_uri",
        "response_types_supported",
        "response_modes_supported",
        "grant_types_supported",
        "token_endpoint_auth_methods_supported",
        "revocation_endpoint_auth_methods_supported",
        "code_challenge_methods_supported",
        "scopes_supported",
        "authorization_response_iss_parameter_supported",
    }
    return {name: value for name, value in metadata.items() if name in oauth_fields}


@router.get("/oauth/jwks", operation_id="native_jwks")
async def jwks(request: Request) -> dict[str, Any]:
    async with native_services(request) as (facade, _services):
        return facade.keyring.jwks()


@router.post(
    "/oauth/token",
    operation_id="native_token",
    description=(
        "Public clients send client_id in the form. Confidential clients authenticate with HTTP Basic, "
        "using form-encoded client_id and client_secret components before base64 encoding. "
        "PKCE S256 is required for both client types; client_secret in the request body is rejected."
    ),
    response_model=TokenResponse,
    openapi_extra=form_contract(
        ["grant_type"], ["client_id", "code", "redirect_uri", "code_verifier", "refresh_token", "scope"]
    ),
)
async def token(request: Request) -> dict[str, Any]:
    values = await read_form(request)
    grant = values.get("grant_type")
    async with native_services(request) as (_facade, services):
        if grant == "authorization_code":
            require_fields(values, {"grant_type", "code", "redirect_uri", "code_verifier"}, {"client_id"})
            return await services.oauth.exchange_code(
                **client_authentication(request, values),
                code=values["code"],
                redirect_uri=values["redirect_uri"],
                code_verifier=values["code_verifier"],
            )
        if grant == "refresh_token":
            require_fields(values, {"grant_type", "refresh_token"}, {"client_id", "scope"})
            return await services.oauth.exchange_refresh(
                **client_authentication(request, values),
                refresh_token=values["refresh_token"],
                scope=values.get("scope"),
            )
        raise NativeHTTPError("unsupported_grant_type")


@router.post(
    "/oauth/revoke",
    operation_id="native_revoke",
    description=(
        "Revoke one client session family. Public clients send client_id in the form; confidential clients "
        "use HTTP Basic. Unknown tokens return 200 after successful client authentication."
    ),
    openapi_extra=form_contract(["token"], ["client_id", "token_type_hint"]),
)
async def revoke(request: Request) -> Response:
    values = await read_form(request)
    require_fields(values, {"token"}, {"client_id", "token_type_hint"})
    async with native_services(request) as (_facade, services):
        await services.oauth.revoke(
            **client_authentication(request, values),
            token=values["token"],
            token_type_hint=values.get("token_type_hint"),
        )
    return Response(status_code=200)


@router.get(
    "/oauth/userinfo", operation_id="native_userinfo", response_model=UserInfo, response_model_exclude_none=True
)
@router.post(
    "/oauth/userinfo", operation_id="native_userinfo_post", response_model=UserInfo, response_model_exclude_none=True
)
async def userinfo(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> dict[str, Any]:
    if request.url.query or (request.method == "POST" and await read_body(request)):
        raise NativeHTTPError()
    async with native_services(request) as (_facade, services):
        return await services.oauth.userinfo(bearer_token(credentials, request))
