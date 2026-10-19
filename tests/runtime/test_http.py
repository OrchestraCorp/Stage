import logging

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from app.core.config import Settings
from app.main import create_app


class SyntheticInput(BaseModel):
    value: int


async def test_request_ids_are_preserved_or_safely_regenerated() -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        valid = await client.get("/health", headers={"X-Request-ID": "test-request.1"})
        invalid = await client.get("/health", headers={"X-Request-ID": "x" * 200})
    assert valid.headers["x-request-id"] == "test-request.1"
    assert len(invalid.headers["x-request-id"]) == 32
    assert invalid.headers["x-request-id"] != "x" * 200


async def test_validation_and_unexpected_errors_never_echo_private_values(caplog: pytest.LogCaptureFixture) -> None:
    app = create_app()

    @app.post("/_test/validate")
    async def validate(payload: SyntheticInput) -> dict[str, int]:
        return {"value": payload.value}

    @app.get("/_test/fail")
    async def fail() -> None:
        raise RuntimeError("synthetic-database-password")

    @app.get("/_test/forbidden")
    async def forbidden() -> None:
        raise HTTPException(403, detail="synthetic-database-password")

    caplog.set_level(logging.ERROR)
    # Default ASGITransport rethrows unhandled application exceptions, just as a server sees them.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        responses = (
            await client.post("/_test/validate", json={"value": "synthetic-database-password"}),
            await client.get("/_test/fail"),
            await client.get("/_test/forbidden"),
            await client.get("/_test/not-found"),
        )
    assert [response.status_code for response in responses] == [422, 500, 403, 404]
    for response in responses:
        assert "synthetic-database-password" not in response.text
        error = response.json()["error"]
        assert error["request_id"] == response.headers["x-request-id"]
        assert error["code"]
    assert "synthetic-database-password" not in caplog.text


async def test_http_errors_preserve_required_protocol_headers() -> None:
    app = create_app()

    @app.get("/_test/authenticate")
    async def authenticate() -> None:
        raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})

    @app.get("/_test/retry")
    async def retry() -> None:
        raise HTTPException(429, headers={"Retry-After": "30"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/health")).headers["allow"] == "GET"
        assert (await client.get("/_test/authenticate")).headers["www-authenticate"] == "Bearer"
        assert (await client.get("/_test/retry")).headers["retry-after"] == "30"


async def test_configured_application_isolation_and_production_docs() -> None:
    config = Settings(env="prod", app_name="custom-stage", database_url="postgresql+asyncpg://localhost/stage")
    app = create_app(config)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        health = await client.get("/health")
        assert health.json()["app"] == "custom-stage"
        assert health.json()["env"] == "prod"
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404
