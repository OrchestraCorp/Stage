"""One explicitly admitted Keycloak access-token profile, outside database transactions."""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey


class AuthenticationRejected(Exception):
    """The credential cannot establish authentication. Never include token contents."""

    def __init__(self, code: str = "invalid_credential") -> None:
        self.code = code
        super().__init__(code)


class AuthenticationUnavailable(Exception):
    """The configured verification source could not be consulted safely."""

    def __init__(self, code: str = "authentication_unavailable") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class KeycloakProfile:
    profile_ref: str
    profile_version: int
    issuer: str
    jwks_url: str
    audience: str
    credential_kind: Literal["human_oidc", "workload_oauth2"]
    allowed_clients: tuple[str, ...]
    subject_namespace: str
    environment: str
    evidence_ttl_seconds: int = 60
    clock_skew_seconds: int = 5
    allow_loopback_http: bool = False

    def __post_init__(self) -> None:
        if not self.profile_ref or self.profile_version < 1 or not self.audience or not self.subject_namespace:
            raise ValueError("profile_identity_required")
        if self.credential_kind not in {"human_oidc", "workload_oauth2"}:
            raise ValueError("unsupported_credential_kind")
        if not self.allowed_clients or any(not isinstance(value, str) or not value for value in self.allowed_clients):
            raise ValueError("allowed_clients_required")
        if not 1 <= self.evidence_ttl_seconds <= 300 or not 0 <= self.clock_skew_seconds <= 30:
            raise ValueError("invalid_evidence_lifetime")
        for value in (self.issuer, self.jwks_url):
            url = urlsplit(value)
            local_http = (
                self.allow_loopback_http
                and self.environment in {"dev", "test"}
                and url.scheme == "http"
                and url.hostname in {"127.0.0.1", "::1", "localhost"}
            )
            if not url.hostname or url.username or url.password or url.fragment or url.query:
                raise ValueError("invalid_trusted_endpoint")
            if url.scheme != "https" and not local_http:
                raise ValueError("trusted_endpoint_requires_https")


@dataclass(frozen=True, slots=True)
class VerifiedCredential:
    evidence_ref: str
    profile_ref: str
    profile_version: int
    credential_kind: str
    subject_namespace: str
    environment: str
    issuer: str
    subject: str
    subject_kind: Literal["human", "service"]
    client_ref: str
    audiences: tuple[str, ...]
    scopes: tuple[str, ...]
    validated_at: datetime
    credential_expires_at: datetime
    evidence_valid_until: datetime
    authenticated_at: datetime | None
    acr: str | None
    amr: tuple[str, ...]
    key_id: str


class KeycloakAuthenticator:
    """Bounded JWKS retrieval; cached keys never extend their configured lifetime.

    Keycloak clients must be provisioned with mutually exclusive flows and a signed
    ``stage_credential_kind`` claim. Roles, email and preferred_username are ignored.
    Database trust/mapping revisions are checked by the caller after verification.
    """

    def __init__(
        self,
        profile: KeycloakProfile,
        *,
        http_client: httpx.AsyncClient | None = None,
        jwks_cache_seconds: float = 60,
        unknown_kid_refresh_seconds: float = 5,
    ) -> None:
        if not 1 <= jwks_cache_seconds <= 300 or not 1 <= unknown_kid_refresh_seconds <= 60:
            raise ValueError("invalid_jwks_cache_lifetime")
        self.profile = profile
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(3.0),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )
        self._owns_client = http_client is None
        self._keys: dict[str, RSAPublicKey] = {}
        self._expires_at = 0.0
        self._last_attempt = -math.inf
        self._cache_seconds = jwks_cache_seconds
        self._refresh_seconds = unknown_kid_refresh_seconds
        self._refresh_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def authenticate(self, access_token: str) -> VerifiedCredential:
        if not isinstance(access_token, str) or not 1 <= len(access_token) <= 16384:
            raise AuthenticationRejected()
        try:
            header = jwt.get_unverified_header(access_token)
        except (jwt.PyJWTError, ValueError, TypeError, RecursionError):
            raise AuthenticationRejected() from None
        kid = header.get("kid")
        if (
            header.get("alg") != "RS256"
            or header.get("typ") != "JWT"
            or not isinstance(kid, str)
            or not 1 <= len(kid) <= 256
            or any(name in header for name in ("jku", "x5u", "jwk", "crit"))
        ):
            raise AuthenticationRejected()
        key = await self._get_key(kid)
        try:
            claims: dict[str, Any] = jwt.decode(
                access_token,
                key=key,
                algorithms=["RS256"],
                audience=self.profile.audience,
                issuer=self.profile.issuer,
                leeway=self.profile.clock_skew_seconds,
                options={"require": ["exp", "iat", "iss", "sub", "aud", "azp", "typ", "stage_credential_kind"]},
            )
        except (jwt.PyJWTError, ValueError, TypeError, RecursionError):
            raise AuthenticationRejected() from None
        now = datetime.now(UTC)
        try:
            expires = _numeric_date(claims["exp"])
            issued = _numeric_date(claims["iat"])
            if expires <= now or expires <= issued:
                raise ValueError("expired")
            if "nbf" in claims:
                _numeric_date(claims["nbf"])
            subject = _bounded_string(claims["sub"], 256)
            client = _bounded_string(claims["azp"], 256)
            if (
                claims["typ"] != "Bearer"
                or claims["stage_credential_kind"] != self.profile.credential_kind
                or client not in self.profile.allowed_clients
            ):
                raise ValueError("wrong_token_profile")
            raw_audiences = claims["aud"]
            audiences = (raw_audiences,) if isinstance(raw_audiences, str) else tuple(raw_audiences)
            if not audiences or len(audiences) > 32:
                raise ValueError("invalid_audience")
            audiences = tuple(_bounded_string(value, 256) for value in audiences)
            raw_scope = claims.get("scope", "")
            if not isinstance(raw_scope, str) or len(raw_scope) > 4096:
                raise ValueError("invalid_scope")
            scopes = tuple(sorted(set(raw_scope.split())))
            authenticated_at = _numeric_date(claims["auth_time"]) if "auth_time" in claims else None
            if authenticated_at is not None and authenticated_at > now + timedelta(
                seconds=self.profile.clock_skew_seconds
            ):
                raise ValueError("future_authentication")
            acr = _bounded_string(claims["acr"], 128) if "acr" in claims else None
            raw_amr = claims.get("amr", [])
            if not isinstance(raw_amr, list) or len(raw_amr) > 32:
                raise ValueError("invalid_authentication_methods")
            amr = tuple(_bounded_string(value, 128) for value in raw_amr)
        except (TypeError, ValueError, OverflowError, OSError):
            raise AuthenticationRejected() from None
        return VerifiedCredential(
            evidence_ref=str(uuid4()),
            profile_ref=self.profile.profile_ref,
            profile_version=self.profile.profile_version,
            credential_kind=self.profile.credential_kind,
            subject_namespace=self.profile.subject_namespace,
            environment=self.profile.environment,
            issuer=self.profile.issuer,
            subject=subject,
            subject_kind="human" if self.profile.credential_kind == "human_oidc" else "service",
            client_ref=client,
            audiences=audiences,
            scopes=scopes,
            validated_at=now,
            credential_expires_at=expires,
            evidence_valid_until=min(expires, now + timedelta(seconds=self.profile.evidence_ttl_seconds)),
            authenticated_at=authenticated_at,
            acr=acr,
            amr=amr,
            key_id=kid,
        )

    async def _get_key(self, kid: str) -> RSAPublicKey:
        now = time.monotonic()
        if now < self._expires_at and kid in self._keys:
            return self._keys[kid]
        async with self._refresh_lock:
            now = time.monotonic()
            if now < self._expires_at and kid in self._keys:
                return self._keys[kid]
            if now - self._last_attempt < self._refresh_seconds:
                if now >= self._expires_at:
                    raise AuthenticationUnavailable()
                raise AuthenticationRejected()
            self._last_attempt = now
            keys = await self._fetch_keys()
            self._keys = keys
            self._expires_at = time.monotonic() + self._cache_seconds
            if kid not in keys:
                raise AuthenticationRejected()
            return keys[kid]

    async def _fetch_keys(self) -> dict[str, RSAPublicKey]:
        try:
            # A total deadline also bounds servers that drip data before each socket timeout.
            async with asyncio.timeout(5):
                async with self._client.stream("GET", self.profile.jwks_url, follow_redirects=False) as response:
                    if response.status_code != 200:
                        raise AuthenticationUnavailable()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 131072:
                            raise AuthenticationUnavailable()
            document = json.loads(body)
            raw_keys = document.get("keys") if isinstance(document, dict) else None
            if not isinstance(raw_keys, list) or not 1 <= len(raw_keys) <= 32:
                raise ValueError("invalid_jwks")
            keys: dict[str, RSAPublicKey] = {}
            for raw in raw_keys:
                if not isinstance(raw, dict):
                    raise ValueError("invalid_jwk")
                if raw.get("kty") != "RSA" or raw.get("use", "sig") != "sig" or raw.get("alg", "RS256") != "RS256":
                    continue
                if "key_ops" in raw and raw["key_ops"] != ["verify"]:
                    continue
                kid = _bounded_string(raw.get("kid"), 256)
                if kid in keys:
                    raise ValueError("duplicate_key_id")
                key = jwt.PyJWK.from_dict(raw, algorithm="RS256").key
                if not isinstance(key, RSAPublicKey) or not 2048 <= key.key_size <= 8192:
                    raise ValueError("invalid_rsa_key")
                keys[kid] = key
            if not keys:
                raise ValueError("missing_signing_keys")
            return keys
        except AuthenticationUnavailable:
            raise
        except (TimeoutError, httpx.HTTPError, ValueError, TypeError, RecursionError, jwt.PyJWTError):
            raise AuthenticationUnavailable() from None


def _bounded_string(value: Any, limit: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= limit:
        raise ValueError("invalid_string_claim")
    return value


def _numeric_date(value: Any) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError("invalid_numeric_date")
    return datetime.fromtimestamp(value, tz=UTC)
