"""Read-only smoke tests for a running Stage development service.

The suite is intentionally opt-in.  Set ``STAGE_SMOKE_BASE_URL`` to run the
HTTP checks and set the database/Redis variables separately to run their
direct, non-mutating connectivity checks.  A normal repository test run has
no external-service dependency because each live test skips when its target
was not explicitly selected.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text

from app.core.config import Settings
from app.core.db import create_engine
from app.core.redis import create_redis_client

try:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncEngine
except ImportError:  # pragma: no cover - dependencies are part of the project
    Redis = Any  # type: ignore[misc, assignment]
    AsyncEngine = Any  # type: ignore[misc, assignment]


AllowedComponentState = Literal["ready", "unavailable", "disabled"]


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    pytest.fail(f"{name} must be a boolean value")


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        pytest.fail(f"{name} must be an integer")
    if not minimum <= parsed <= maximum:
        pytest.fail(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _timeout_seconds() -> float:
    value = os.environ.get("STAGE_SMOKE_TIMEOUT_SECONDS", "5")
    try:
        timeout = float(value)
    except ValueError:
        pytest.fail("STAGE_SMOKE_TIMEOUT_SECONDS must be a number")
    if not 0 < timeout <= 60:
        pytest.fail("STAGE_SMOKE_TIMEOUT_SECONDS must be between 0 and 60")
    return timeout


def _validated_base_url() -> str:
    value = os.environ.get("STAGE_SMOKE_BASE_URL")
    if not value:
        pytest.skip("set STAGE_SMOKE_BASE_URL to select a live Stage service")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        pytest.fail("STAGE_SMOKE_BASE_URL must be an HTTP(S) URL without user information")
    if parsed.query or parsed.fragment:
        pytest.fail("STAGE_SMOKE_BASE_URL must not include a query or fragment")
    return value.rstrip("/")


@pytest.fixture(scope="module")
def smoke_base_url() -> str:
    return _validated_base_url()


@pytest.fixture
async def http_client(smoke_base_url: str) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(base_url=smoke_base_url, timeout=_timeout_seconds()) as client:
        yield client


async def _get(client: AsyncClient, path: str) -> Response:
    try:
        return await client.get(path)
    except Exception as error:
        pytest.fail(f"GET {path} failed ({type(error).__name__})")


def _json_object(response: Response, endpoint: str) -> Mapping[str, Any]:
    try:
        value = response.json()
    except ValueError:
        pytest.fail(f"{endpoint} did not return JSON")
    if not isinstance(value, Mapping):
        pytest.fail(f"{endpoint} returned a non-object JSON value")
    return value


def _assert_no_config_material(responses: list[Response]) -> None:
    """Reject configured URLs/passwords without printing the sensitive value."""

    configured = {
        value
        for name in (
            "STAGE_DATABASE_URL",
            "STAGE_REDIS_URL",
            "STAGE_SMOKE_DATABASE_URL",
            "STAGE_SMOKE_REDIS_URL",
        )
        if (value := os.environ.get(name))
    }
    candidates = set(configured)
    for value in configured:
        # A username such as ``stage`` is intentionally not checked by itself:
        # it is a harmless product name and appears in ``orchestra-stage``.
        # Passwords are credential material and must never occur in a response.
        parsed = urlsplit(value)
        if parsed.password:
            candidates.add(parsed.password)
    for response in responses:
        body = response.content
        if any(candidate.encode() in body for candidate in candidates if candidate):
            pytest.fail("a live response contained configured connection material")


def _assert_health(response: Response) -> None:
    if response.status_code != 200:
        pytest.fail(f"/health returned HTTP {response.status_code}")
    payload = _json_object(response, "/health")
    if (
        payload.get("status") != "ok"
        or not isinstance(payload.get("app"), str)
        or not isinstance(payload.get("env"), str)
    ):
        pytest.fail("/health response did not match the liveness shape")


def _expected_ready() -> bool:
    return _env_bool("STAGE_SMOKE_EXPECT_READY", True)


def _assert_ready(response: Response) -> Mapping[str, Any]:
    expected_ready = _expected_ready()
    expected_status = 200 if expected_ready else 503
    if response.status_code != expected_status:
        pytest.fail(f"/ready returned HTTP {response.status_code}, expected {expected_status}")
    payload = _json_object(response, "/ready")
    expected_payload_status = "ready" if expected_ready else "not_ready"
    if payload.get("status") != expected_payload_status:
        pytest.fail("/ready status did not match its HTTP status")
    components = payload.get("components")
    if not isinstance(components, Mapping) or set(components) != {"database", "redis"}:
        pytest.fail("/ready did not report database and redis component states")
    allowed: set[AllowedComponentState] = {"ready", "unavailable", "disabled"}
    if any(state not in allowed for state in components.values()):
        pytest.fail("/ready reported an unknown component state")
    if expected_ready and components.get("database") != "ready":
        pytest.fail("/ready reported a ready service without a ready database")

    expected_redis = os.environ.get("STAGE_SMOKE_EXPECT_REDIS")
    if expected_redis is None and os.environ.get("STAGE_SMOKE_REDIS_URL"):
        expected_redis = "ready"
    if expected_redis is not None and components.get("redis") != expected_redis:
        pytest.fail("/ready Redis state did not match STAGE_SMOKE_EXPECT_REDIS")
    return payload


async def test_live_health_and_readiness(http_client: AsyncClient) -> None:
    health, ready = await asyncio.gather(_get(http_client, "/health"), _get(http_client, "/ready"))
    _assert_health(health)
    _assert_ready(ready)
    _assert_no_config_material([health, ready])


async def test_live_probe_concurrency(http_client: AsyncClient) -> None:
    concurrency = _env_int("STAGE_SMOKE_CONCURRENCY", 8, minimum=1, maximum=64)
    rounds = _env_int("STAGE_SMOKE_ROUNDS", 2, minimum=1, maximum=10)
    for _ in range(rounds):
        responses = await asyncio.gather(
            *(_get(http_client, "/health") for _ in range(concurrency)),
            *(_get(http_client, "/ready") for _ in range(concurrency)),
        )
        health_responses = responses[:concurrency]
        ready_responses = responses[concurrency:]
        for response in health_responses:
            _assert_health(response)
        for response in ready_responses:
            _assert_ready(response)
        request_ids = [response.headers.get("x-request-id") for response in responses]
        if any(not request_id for request_id in request_ids) or len(set(request_ids)) != len(request_ids):
            pytest.fail("concurrent probes did not receive unique request IDs")
        _assert_no_config_material(responses)


async def test_live_openapi_contract_is_safe(http_client: AsyncClient) -> None:
    response = await _get(http_client, "/openapi.json")
    expected = os.environ.get("STAGE_SMOKE_EXPECT_OPENAPI", "auto").strip().lower()
    if expected not in {"auto", "0", "1"}:
        pytest.fail("STAGE_SMOKE_EXPECT_OPENAPI must be auto, 0, or 1")
    if expected == "1" and response.status_code != 200:
        pytest.fail(f"/openapi.json returned HTTP {response.status_code}, expected 200")
    if expected == "0" and response.status_code != 404:
        pytest.fail(f"/openapi.json returned HTTP {response.status_code}, expected 404")
    if response.status_code == 200:
        payload = _json_object(response, "/openapi.json")
        paths = payload.get("paths")
        if not isinstance(paths, Mapping) or not {"/health", "/ready"}.issubset(paths):
            pytest.fail("OpenAPI did not describe the system probes")
    elif response.status_code != 404:
        pytest.fail(f"/openapi.json returned unexpected HTTP {response.status_code}")
    _assert_no_config_material([response])


@pytest.fixture
def smoke_database_url() -> str:
    value = os.environ.get("STAGE_SMOKE_DATABASE_URL")
    if not value:
        pytest.skip("set STAGE_SMOKE_DATABASE_URL for a direct read-only database probe")
    return value


@pytest.fixture
def smoke_redis_url() -> str:
    value = os.environ.get("STAGE_SMOKE_REDIS_URL")
    if not value:
        pytest.skip("set STAGE_SMOKE_REDIS_URL for a direct read-only Redis probe")
    return value


async def test_database_probe_is_read_only(smoke_database_url: str) -> None:
    engine: AsyncEngine | None = None
    try:
        engine = create_engine(smoke_database_url, pool_size=1, max_overflow=0, pool_timeout=_timeout_seconds())
        async with asyncio.timeout(_timeout_seconds()), engine.connect() as connection:
            result = await connection.execute(text("SELECT 1"))
            if result.scalar_one() != 1:
                pytest.fail("database SELECT 1 returned an unexpected result")
    except pytest.fail.Exception:
        raise
    except Exception as error:
        pytest.fail(f"database probe failed ({type(error).__name__})")
    finally:
        if engine is not None:
            await engine.dispose()


async def test_redis_probe_is_read_only(smoke_redis_url: str) -> None:
    client: Redis | None = None
    try:
        config = Settings(_env_file=None, env="test", redis_url=smoke_redis_url)  # type: ignore[call-arg]
        client = create_redis_client(config)
        async with asyncio.timeout(_timeout_seconds()):
            if not await client.ping():
                pytest.fail("Redis PING returned false")
    except pytest.fail.Exception:
        raise
    except Exception as error:
        pytest.fail(f"Redis probe failed ({type(error).__name__})")
    finally:
        if client is not None:
            await client.aclose()
