"""Exact callback registration rules shared by operators and protocol revalidation."""

import ipaddress
from urllib.parse import parse_qsl, urlsplit

from app.native_auth.config import NativeAuthConfig

_RESERVED_CALLBACK_PARAMETERS = {"code", "state", "error", "error_description", "error_uri", "iss"}


def validate_redirect_uri(uri: str) -> None:
    """Only exact, unambiguous HTTPS or explicit loopback callbacks are registered."""
    parsed = urlsplit(uri)
    port = parsed.port
    local = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
    if (
        not 1 <= len(uri) <= 2048
        or not uri.isascii()
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "#" in uri
        or "\\" in uri
        or not parsed.path
        or "*" in uri
        or any(ord(char) <= 32 or ord(char) == 127 for char in uri)
        or (parsed.scheme != "https" and not local)
        or (local and port is None)
        or any(key in _RESERVED_CALLBACK_PARAMETERS for key, _ in parse_qsl(parsed.query, keep_blank_values=True))
    ):
        raise ValueError("invalid_exact_redirect_uri")
    NativeAuthConfig(issuer=f"{parsed.scheme}://{parsed.netloc}", organization_ref="validation", environment="test")


def validate_issuer_redirect(issuer: str, uri: str, environment: str) -> None:
    """Keep host-only authentication cookies away from registered applications.

    Cookie scope does not contain a port. A different path, scheme or port on
    the same hostname therefore cannot isolate an RP from the issuer's cookies.
    Disposable dev/test loopback deployments deliberately retain local callbacks.
    """
    validate_redirect_uri(uri)
    issuer_host = urlsplit(issuer).hostname
    if issuer_host is None:
        raise ValueError("native_issuer_hostname_required")
    callback_host = urlsplit(uri).hostname
    try:
        loopback = ipaddress.ip_address(issuer_host).is_loopback
    except ValueError:
        loopback = issuer_host == "localhost"
    if environment in {"dev", "test"} and loopback:
        return
    if callback_host is not None and issuer_host.rstrip(".") == callback_host.rstrip("."):
        raise ValueError("native_issuer_requires_dedicated_callback_hostname")
