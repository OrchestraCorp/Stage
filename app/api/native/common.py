"""Shared native HTTP security, bounded parsing, errors and response rendering."""

import asyncio
import ipaddress
import re
import secrets
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from html import escape
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Request
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError
from starlette._utils import get_route_path
from starlette.datastructures import MutableHeaders
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import error_response
from app.execution.errors import TenantNotProvisioned
from app.native_auth.errors import NativeCommitUnknown, NativeCredentialError
from app.native_auth.protocol import AuthorizationResponse, OAuthError
from app.providers.native_auth import NATIVE_AUTH, NativeAuthFacade, NativeAuthServices
from app.runtime import MissingService, RuntimeClosed

MAX_FORM_BYTES = 8192
STRICT_CSP = "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
bearer = HTTPBearer(auto_error=False)


class NativeHTTPError(Exception):
    def __init__(self, error: str = "invalid_request", status: int = 400) -> None:
        self.error = error
        self.status = status


def _oauth_error(error: str, status: int) -> JSONResponse:
    headers: dict[str, str] = {}
    if status == 401:
        headers["WWW-Authenticate"] = (
            'Basic realm="Stage OAuth"' if error == "invalid_client" else 'Bearer error="invalid_token"'
        )
    if status == 429:
        headers["Retry-After"] = "60"
    return JSONResponse({"error": error}, status_code=status, headers=headers)


def _native_error(request: Request, error: str, status: int) -> JSONResponse:
    if not get_route_path(request.scope).startswith("/api/v1/account/"):
        return _oauth_error(error, status)
    code, message = {
        "invalid_token": ("INVALID_CREDENTIAL", "The access token is not valid"),
        "login_required": ("FRESH_AUTHENTICATION_REQUIRED", "Recent multi-factor authentication is required"),
        "commit_outcome_unknown": ("COMMIT_OUTCOME_UNKNOWN", "The account change outcome is not yet known"),
        "temporarily_unavailable": ("ACCOUNT_SECURITY_UNAVAILABLE", "Account security is temporarily unavailable"),
        "access_denied": ("ACCOUNT_CREDENTIAL_REJECTED", "Account credential verification failed"),
        "not_found": ("HTTP_404", "Request could not be completed"),
    }.get(error, ("REQUEST_INVALID", "Request validation failed"))
    headers = {"WWW-Authenticate": 'Bearer error="invalid_token"'} if status == 401 else {}
    if error == "login_required":
        headers["WWW-Authenticate"] = 'Bearer error="insufficient_user_authentication"'
    if status == 429:
        headers["Retry-After"] = "60"
    return error_response(request, status, code, message, headers)


class NativeRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def protected(request: Request) -> Response:
            config = getattr(request.app.state, "settings", None)
            if config is None or not config.native_auth_enabled:
                return _native_error(request, "not_found", 404)
            try:
                async with asyncio.timeout(config.native_auth_request_timeout_seconds):
                    return await handler(request)
            except NativeHTTPError as exc:
                return _native_error(request, exc.error, exc.status)
            except OAuthError as exc:
                if exc.response is not None:
                    return authorization_response(exc.response)
                return _native_error(request, exc.error, exc.status_code)
            except NativeCredentialError as exc:
                if exc.code == "rate_limited":
                    return _native_error(request, "temporarily_unavailable", 429)
                if exc.code == "authentication_unavailable":
                    return _native_error(request, "temporarily_unavailable", 503)
                return _native_error(request, "access_denied", 400)
            except NativeCommitUnknown:
                # Distinguish an uncertain single-use consumption from a known rejection.
                return _native_error(request, "commit_outcome_unknown", 503)
            except (MissingService, RuntimeClosed, SQLAlchemyError, TimeoutError, TenantNotProvisioned):
                return _native_error(request, "temporarily_unavailable", 503)

        return protected


class OAuthFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error: str


class NativeAuthSecurityMiddleware:
    """Also protect validation, method errors and redirects containing authorization codes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = get_route_path(scope)
        private = (
            path == "/oauth"
            or path.startswith("/oauth/")
            or path in {"/.well-known/openid-configuration", "/.well-known/oauth-authorization-server"}
            or path == "/api/v1/account"
            or path.startswith("/api/v1/account/")
        )
        if not private:
            await self.app(scope, receive, send)
            return

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["Pragma"] = "no-cache"
                login_document = (
                    path
                    in {
                        "/oauth/authorize",
                        "/oauth/login",
                        "/oauth/logout",
                        "/oauth/activate",
                        "/oauth/activate/status",
                        "/oauth/activate/complete",
                    }
                    and headers.get("content-type", "").split(";", 1)[0] == "text/html"
                )
                # Chromium gives form POSTs an opaque Origin under no-referrer.
                # Preserve the same-origin POST proof without disclosing a
                # referrer to the cross-origin registered callback.
                headers["Referrer-Policy"] = (
                    headers.get("Referrer-Policy", "same-origin") if login_document else "no-referrer"
                )
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Content-Security-Policy"] = (
                    headers.get("Content-Security-Policy", STRICT_CSP) if login_document else STRICT_CSP
                )
            await send(message)

        await self.app(scope, receive, secure_send)


@asynccontextmanager
async def native_services(request: Request) -> AsyncIterator[tuple[NativeAuthFacade, NativeAuthServices]]:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None or not runtime.ready:
        raise NativeHTTPError("temporarily_unavailable", 503)
    async with runtime.host.call(NATIVE_AUTH) as call:
        facade = call.require(NATIVE_AUTH)
        async with facade.services() as services:
            yield facade, services


def parse_pairs(raw: bytes) -> dict[str, str]:
    if len(raw) > MAX_FORM_BYTES:
        raise NativeHTTPError(status=413)
    try:
        text = raw.decode("utf-8", "strict")
        if re.search(r"%(?![0-9a-fA-F]{2})", text):
            raise ValueError
        pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=24)
    except (ValueError, UnicodeError):
        raise NativeHTTPError() from None
    result: dict[str, str] = {}
    for key, value in pairs:
        if key in result or not key or "\x00" in key or "\x00" in value:
            raise NativeHTTPError()
        result[key] = value
    return result


async def read_body(request: Request) -> bytes:
    value = bytearray()
    async for chunk in request.stream():
        value.extend(chunk)
        if len(value) > MAX_FORM_BYTES:
            raise NativeHTTPError(status=413)
    return bytes(value)


async def read_form(request: Request) -> dict[str, str]:
    if request.url.query:
        raise NativeHTTPError()
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise NativeHTTPError(status=415)
    return parse_pairs(await read_body(request))


def require_fields(values: dict[str, str], required: set[str], optional: set[str] | None = None) -> None:
    if not required <= values.keys() or values.keys() - required - (optional or set()):
        raise NativeHTTPError()
    if any(not values[key] for key in required):
        raise NativeHTTPError()


def require_same_origin(request: Request, issuer: str) -> None:
    origins = request.headers.getlist("origin")
    if len(origins) > 1 or (origins and origins[0] != issuer):
        raise NativeHTTPError("access_denied", 403)
    # Browser-secret + CSRF remain mandatory when a native HTTP client omits Origin.
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise NativeHTTPError("access_denied", 403)


def login_cookie_name(issuer: str) -> str:
    return "__Host-stage-native-login" if issuer.startswith("https://") else "stage_native_login"


def sso_cookie_name(issuer: str) -> str:
    return "__Host-stage-sso" if issuer.startswith("https://") else "stage_sso"


def logout_cookie_name(issuer: str) -> str:
    return "__Host-stage-logout" if issuer.startswith("https://") else "stage_logout"


def cookie_secret(request: Request, name: str, *, required: bool) -> str | None:
    # Reject ambiguous cookie tossing rather than choosing the framework's last value.
    matches = [
        item.partition("=")[2].strip()
        for header in request.headers.getlist("cookie")
        for item in header.split(";")
        if item.partition("=")[0].strip() == name
    ]
    if not matches and not required:
        return None
    if len(matches) != 1 or not 20 <= len(matches[0]) <= 256:
        raise NativeHTTPError("access_denied", 400)
    return matches[0]


def get_browser_secret(request: Request, issuer: str) -> str:
    value = cookie_secret(request, login_cookie_name(issuer), required=True)
    assert value is not None
    return value


def set_browser_cookie(response: Response, name: str, secret: str, issuer: str, max_age: int) -> None:
    response.set_cookie(
        name, secret, max_age=max_age, path="/", secure=issuer.startswith("https://"), httponly=True, samesite="lax"
    )


def clear_browser_cookie(response: Response, name: str, issuer: str) -> None:
    response.delete_cookie(name, path="/", secure=issuer.startswith("https://"), httponly=True, samesite="lax")


def hidden_input(name: str, value: str) -> str:
    return f'<input type="hidden" name="{escape(name, quote=True)}" value="{escape(value, quote=True)}">'


def callback_origin(redirect_uri: str | None) -> str:
    """Only consume the server-validated registration; never insert path/query in CSP."""
    if redirect_uri is None:
        raise NativeHTTPError()
    try:
        parsed = urlsplit(redirect_uri)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        hostname = parsed.hostname.encode("idna").decode("ascii")
        if ":" in hostname:
            hostname = "[" + str(ipaddress.IPv6Address(hostname)) + "]"
        elif not re.fullmatch(r"[A-Za-z0-9.-]+", hostname):
            raise ValueError
        port = "" if parsed.port is None else f":{parsed.port}"
        return f"{parsed.scheme}://{hostname}{port}"
    except (ValueError, UnicodeError):
        raise NativeHTTPError() from None


def authorization_response(result: AuthorizationResponse) -> Response:
    """Render only the callback already validated by the protocol service."""
    if result.response_mode == "query":
        return RedirectResponse(result.location, status_code=303)
    if result.response_mode != "form_post":
        raise NativeHTTPError()
    nonce = secrets.token_urlsafe(24)
    target = escape(result.redirect_uri, quote=True)
    controls = "".join(hidden_input(name, value) for name, value in result.parameters.items())
    return HTMLResponse(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>返回应用</title></head><body>"
        f'<form id="callback" method="post" action="{target}">{controls}'
        '<button type="submit">返回应用</button></form>'
        f'<script nonce="{nonce}">document.getElementById("callback").submit();</script>'
        "</body></html>",
        headers={
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; "
                f"form-action {callback_origin(result.redirect_uri)}; frame-ancestors 'none'; base-uri 'none'"
            ),
        },
    )


def form_contract(required: list[str], optional: list[str] | None = None) -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/x-www-form-urlencoded": {
                    "schema": {
                        "type": "object",
                        "required": required,
                        "properties": {key: {"type": "string"} for key in required + (optional or [])},
                        "additionalProperties": False,
                    }
                }
            },
        }
    }


def bearer_token(credentials: HTTPAuthorizationCredentials | None, request: Request) -> str:
    if credentials is None or len(request.headers.getlist("authorization")) != 1:
        raise NativeHTTPError("invalid_token", 401)
    return credentials.credentials


def make_oauth_router() -> APIRouter:
    """Keep protocol routes on the same error and native-service availability boundary."""
    return APIRouter(
        tags=["Native authentication"],
        route_class=NativeRoute,
        responses={
            status: {"content": {"application/json": {"schema": OAuthFailure.model_json_schema()}}}
            for status in (400, 401, 403, 404, 413, 415, 429, 503)
        },
    )
