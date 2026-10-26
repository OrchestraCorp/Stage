"""Explicit native issuer configuration; requests cannot choose authority or endpoints."""

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class NativeAuthConfig:
    issuer: str
    organization_ref: str
    trust_ref: str = "trust:native-auth"
    audience: str = "stage-api"
    environment: str = "dev"
    access_token_seconds: int = 300
    session_seconds: int = 28800
    refresh_token_seconds: int = 28800
    authorization_code_seconds: int = 60
    login_request_seconds: int = 300
    request_timeout_seconds: float = 15
    peer_attempt_limit: int = 300
    authorization_attempt_limit: int = 300
    rate_window_seconds: int = 600

    def __post_init__(self) -> None:
        parsed = urlsplit(self.issuer)
        try:
            _ = parsed.port
        except ValueError:
            raise ValueError("native_issuer_port_invalid") from None
        local = self.environment in {"dev", "test"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or "?" in self.issuer
            or "#" in self.issuer
            or parsed.path
            or parsed.netloc.endswith(":")
            or "\\" in self.issuer
            or parsed.scheme not in {"http", "https"}
            or (parsed.scheme == "http" and not local)
            or any(ord(char) <= 32 or ord(char) == 127 for char in self.issuer)
        ):
            raise ValueError("native_issuer_requires_fixed_https_origin_or_development_loopback")
        # Browsers serialize Origin independently of the configured OIDC issuer.
        # Require the same spelling at deployment time rather than weakening the
        # exact Origin check or silently changing the issuer's trust identity.
        hostname = parsed.hostname
        assert hostname is not None
        if "%" in hostname:
            raise ValueError("native_issuer_requires_canonical_browser_origin")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if not re.fullmatch(r"[a-z0-9.-]+", hostname) or re.fullmatch(
                r"(?:[0-9]+|0x[0-9a-f]+)", hostname.rstrip(".").rsplit(".", 1)[-1]
            ):
                # Browsers interpret numeric hosts as IPv4, including short,
                # hexadecimal and octal spellings that urlsplit leaves intact.
                raise ValueError("native_issuer_requires_canonical_browser_origin") from None
            canonical_host = hostname
        else:
            canonical_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
        port = parsed.port
        default_port = 443 if parsed.scheme == "https" else 80
        canonical_port = "" if port is None or port == default_port else f":{port}"
        if self.issuer != f"{parsed.scheme}://{canonical_host}{canonical_port}":
            raise ValueError("native_issuer_requires_canonical_browser_origin")
        if (
            any(
                not isinstance(value, str)
                or not 1 <= len(value) <= 128
                or any(ord(char) <= 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value)
                for value in (self.organization_ref, self.trust_ref)
            )
            or self.audience != "stage-api"
        ):
            raise ValueError("native_issuer_authority_required")
        if not 30 <= self.access_token_seconds <= 600 or not 300 <= self.session_seconds <= 86400:
            raise ValueError("native_token_lifetime_invalid")
        if not 60 <= self.refresh_token_seconds <= self.session_seconds:
            raise ValueError("native_refresh_lifetime_invalid")
        if not 10 <= self.authorization_code_seconds <= 120 or not 60 <= self.login_request_seconds <= 600:
            raise ValueError("native_authorization_lifetime_invalid")
        if not 0 < self.request_timeout_seconds <= 60:
            raise ValueError("native_request_timeout_invalid")
        if (
            any(
                type(value) is not int or not 1 <= value <= 10000
                for value in (self.peer_attempt_limit, self.authorization_attempt_limit)
            )
            or type(self.rate_window_seconds) is not int
            or not 60 <= self.rate_window_seconds <= 3600
        ):
            raise ValueError("native_rate_limits_invalid")
