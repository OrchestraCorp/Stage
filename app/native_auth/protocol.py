"""Shared OAuth/OIDC values and errors, independent of issuer services and persistence."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MFA_ACR = "urn:orchestra:acr:pwd-totp"
MFA_AMR = ["pwd", "otp"]


@dataclass(frozen=True)
class AuthorizationResponse:
    redirect_uri: str
    parameters: dict[str, str] = field(repr=False)
    response_mode: str = "query"
    session_ref: str | None = None

    @property
    def location(self) -> str:
        parts = urlsplit(self.redirect_uri)
        query = parse_qsl(parts.query, keep_blank_values=True)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode([*query, *self.parameters.items()]), ""))


class OAuthError(Exception):
    """Only stable protocol error names may cross the HTTP boundary."""

    def __init__(self, error: str, status_code: int = 400, *, response: AuthorizationResponse | None = None) -> None:
        self.error = error
        self.status_code = status_code
        self.response = response
        super().__init__(error)


@dataclass(frozen=True)
class AuthorizationRequest:
    request_ref: str
    browser_secret: str = field(repr=False)
    csrf_token: str = field(repr=False)
    expires_at: datetime


def valid_text(value: Any, limit: int, *, minimum: int = 1) -> bool:
    return (
        isinstance(value, str)
        and minimum <= len(value) <= limit
        and all(ord(char) >= 32 and ord(char) != 127 and not 0xD800 <= ord(char) <= 0xDFFF for char in value)
    )


SUPPORTED_SCOPES = frozenset({"openid", "profile", "offline_access"})


def normalize_scope(value: Any) -> str:
    if not valid_text(value, 128):
        raise OAuthError("invalid_scope")
    scopes = value.split(" ")
    if len(scopes) != len(set(scopes)) or "openid" not in scopes or not set(scopes) <= SUPPORTED_SCOPES:
        raise OAuthError("invalid_scope")
    return " ".join(scope for scope in ("openid", "profile", "offline_access") if scope in scopes)
