"""Independent adversarial regressions from the native issuer architecture audit."""

from urllib.parse import urlsplit

import pytest
from starlette.requests import Request

from app.api.native_auth import _origin
from app.native_auth.config import NativeAuthConfig


@pytest.mark.parametrize(
    "issuer",
    [
        "https://login.example.test?",
        "https://login.example.test#",
        "https://login.example.test?#",
    ],
)
def test_native_issuer_rejects_empty_query_and_fragment_delimiters(issuer: str) -> None:
    """An origin cannot put appended protocol paths in a query or fragment."""
    with pytest.raises(ValueError):
        NativeAuthConfig(issuer=issuer, organization_ref="org:audit", environment="prod")


@pytest.mark.parametrize(
    ("issuer", "browser_origin"),
    [
        ("https://LOGIN.example.test", "https://login.example.test"),
        ("https://login.example.test:443", "https://login.example.test"),
        ("http://127.0.0.1:80", "http://127.0.0.1"),
        ("https://[0:0:0:0:0:0:0:1]:443", "https://[::1]"),
        ("https://127.1", "https://127.0.0.1"),
        ("https://0x7f000001", "https://127.0.0.1"),
        ("https://0177.0.0.1", "https://127.0.0.1"),
    ],
)
def test_accepted_native_issuer_must_equal_browser_serialized_origin(issuer: str, browser_origin: str) -> None:
    """Either reject a noncanonical origin at startup or accept its real browser POST."""
    try:
        config = NativeAuthConfig(issuer=issuer, organization_ref="org:audit", environment="test")
    except ValueError:
        return
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/oauth/login",
            "headers": [(b"origin", browser_origin.encode("ascii"))],
        }
    )
    _origin(request, config.issuer)
    assert urlsplit(config.issuer + "/oauth/login").path == "/oauth/login"


@pytest.mark.parametrize(
    "issuer",
    [
        "https://login.example.test",
        "https://login.example.test:8443",
        "https://[::1]",
        "http://[::1]:8080",
        "http://localhost:8080",
        "https://xn--fiqs8s.example",
    ],
)
def test_canonical_native_issuer_retains_its_exact_trust_identity(issuer: str) -> None:
    assert NativeAuthConfig(issuer=issuer, organization_ref="org:audit", environment="test").issuer == issuer


def test_scoped_ipv6_cannot_be_a_browser_issuer() -> None:
    with pytest.raises(ValueError):
        NativeAuthConfig(issuer="https://[fe80::1%25eth0]", organization_ref="org:audit", environment="prod")
