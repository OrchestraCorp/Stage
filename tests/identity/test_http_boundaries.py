"""Identity privacy headers survive validation and the outer HTTP failure boundary."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.identity.authentication import AuthenticationRejected, AuthenticationUnavailable
from app.identity.service import IdentityDenied, SelfIdentity
from app.main import create_app
from app.providers.identity import IDENTITY
from app.runtime import ComponentContext, PluginSpec, RuntimeHost

IDENTITY_PATH = "/api/v1/identity/me?profile_ref=profile&profile_version=1"


@pytest.mark.parametrize(
    ("method", "path", "headers", "status"),
    [
        ("GET", "/api/v1/identity/me", {}, 422),
        ("GET", IDENTITY_PATH.replace("profile_version=1", "profile_version=invalid"), {}, 422),
        ("GET", IDENTITY_PATH.replace("profile_ref=profile", "profile_ref=profile%00"), {}, 422),
        ("GET", IDENTITY_PATH.replace("profile_ref=profile", "profile_ref=profile%7F"), {}, 422),
        ("GET", IDENTITY_PATH + "&tenant_ref=forged", {}, 422),
        ("GET", IDENTITY_PATH + "&organization_ref=forged", {}, 422),
        ("POST", IDENTITY_PATH, {}, 405),
        ("GET", "/api/v1/identity/missing", {}, 404),
        ("GET", IDENTITY_PATH, {}, 401),
        ("GET", IDENTITY_PATH, {"Authorization": "Bearer synthetic-token"}, 503),
    ],
)
async def test_identity_errors_before_service_invocation_are_not_stored(
    method: str, path: str, headers: dict[str, str], status: int
) -> None:
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://stage") as client:
        response = await client.request(method, path, headers=headers)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
    assert "synthetic-token" not in response.text
    if status == 401:
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (None, 200),
        (AuthenticationRejected(), 401),
        (IdentityDenied(), 403),
        (AuthenticationUnavailable(), 503),
        (TimeoutError(), 503),
        (RuntimeError("synthetic-private-exception"), 500),
    ],
)
async def test_identity_service_responses_and_unexpected_errors_are_not_stored(
    failure: Exception | None, status: int
) -> None:
    async def read_self(access_token: str, *, profile_ref: str, profile_version: int) -> SelfIdentity:
        assert access_token == "synthetic-token" and profile_ref == "profile" and profile_version == 1
        if failure is not None:
            raise failure
        return SelfIdentity(
            principal_ref="principal",
            principal_type="human",
            organization_ref="organization",
            membership_ref="membership",
            membership_version=1,
            authenticated_workload_ref=None,
            authentication_valid_until=datetime.now(UTC) + timedelta(seconds=60),
        )

    def setup(ctx: ComponentContext) -> None:
        ctx.provide(IDENTITY, SimpleNamespace(read_self=read_self))

    host = RuntimeHost()
    host.load(PluginSpec("test.identity", setup, provides=(IDENTITY,)))
    assert (await host.await_ready([IDENTITY], timeout=2)).ready
    app = create_app()
    app.state.runtime = SimpleNamespace(host=host, ready=True)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://stage") as client:
            response = await client.get(IDENTITY_PATH, headers={"Authorization": "Bearer synthetic-token"})
        assert response.status_code == status
        assert response.headers["cache-control"] == "no-store"
        assert "synthetic-private-exception" not in response.text
        assert "synthetic-token" not in response.text
        if status != 200:
            assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
        if status == 401:
            assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'
    finally:
        assert (await host.close(timeout=2)).successful


async def test_identity_cache_policy_does_not_change_unrelated_routes() -> None:
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://stage") as client:
        for path in ("/health", "/ready", "/api/v1/identity-unrelated"):
            response = await client.get(path)
            assert "cache-control" not in response.headers


@pytest.mark.parametrize("path", ("/api/identity/me", "/api/v2/identity/me"))
async def test_identity_only_exposes_the_supported_api_major_version(path: str) -> None:
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://stage") as client:
        response = await client.get(path, params={"profile_ref": "profile", "profile_version": 1})
    assert response.status_code == 404
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
