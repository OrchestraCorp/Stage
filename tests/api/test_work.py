"""HTTP boundaries and real identity-to-metadata commit/recovery integration."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.execution.errors import (
    AuthorizationDenied,
    CommitOutcomeUnknown,
    ContextExpired,
    ExecutionNotFound,
    IdempotencyConflict,
    InvalidCommand,
    ObjectVersionConflict,
    RequestDeadlineExceeded,
)
from app.execution.storage import UnitOfWork, lock_tenant
from app.features.work.details import WorkDetailsConflict
from app.identity.authentication import AuthenticationRejected, AuthenticationUnavailable
from app.identity.evidence import HumanAuthenticationEvidence
from app.identity.service import IdentityDenied
from app.main import create_app
from app.models.authorization import RelationTupleTable
from app.providers.identity import IDENTITY, IdentityFacade
from app.providers.storage import STORAGE, Storage
from app.providers.work import WORK, human_work_plugin
from app.runtime import ComponentContext, PluginSpec, RuntimeHost
from tests.support.authenticated_commands import identity_service, jwt_client, seed_authenticated_command
from tests.support.authorization import ORG
from tests.support.authorization_recovery import _install_reader
from tests.support.database_commands import counts
from tests.support.identity_tokens import token
from tests.support.work_http import (
    assert_private,
)

BASE = "/api/v1/work-items/1"
QUERY: dict[str, str | int] = {
    "profile_ref": "local-human",
    "profile_version": 1,
    "assignment_ref": "assignment:engineering",
}
PAYLOAD = {"title": "  Updated via HTTP  ", "description": "  Keep description whitespace.  ", "expected_version": 1}
HEADERS = {"Authorization": "Bearer synthetic-token", "Idempotency-Key": "http-edit-1", "X-Request-ID": "http-test.1"}


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
async def work_client(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    request: pytest.FixtureRequest,
) -> AsyncIterator[httpx.AsyncClient]:
    await seed_authenticated_command(persistence_sessions)
    async with jwt_client(signing_key) as verifier_client:
        identity = identity_service(persistence_sessions, verifier_client)

        def storage_setup(ctx: ComponentContext) -> None:
            ctx.provide(STORAGE, Storage(persistence_sessions.kw["bind"], persistence_sessions))

        def identity_setup(ctx: ComponentContext) -> None:
            ctx.effect(lambda: identity.aclose, label="test.identity")
            ctx.provide(IDENTITY, IdentityFacade(ctx, identity))

        config = Settings(
            env="test",
            database_url="sqlite+aiosqlite://",
            redis_url=None,
            command_request_timeout_seconds=getattr(request, "param", 10),
        )
        host = RuntimeHost()
        host.load(PluginSpec("test.storage", storage_setup, provides=(STORAGE,)))
        host.load(PluginSpec("test.identity", identity_setup, requires=(STORAGE,), provides=(IDENTITY,)))
        host.load(human_work_plugin(config))
        assert (await host.await_ready([WORK], timeout=2)).ready
        app = create_app(config)
        app.state.runtime = SimpleNamespace(host=host, ready=True)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://stage",
                params=QUERY,
                headers={**HEADERS, "Authorization": f"Bearer {token(signing_key)}"},
            ) as client:
                yield client
        finally:
            assert (await host.close(timeout=2)).successful


async def test_real_identity_provider_http_edit_replay_recovery_and_conflict(
    work_client: httpx.AsyncClient, persistence_sessions: async_sessionmaker[AsyncSession]
) -> None:
    read = await work_client.get(BASE)
    assert read.status_code == 200 and read.json()["version"] == 1
    updated = await work_client.put(BASE + "/details", json=PAYLOAD)
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert set(body) == {"execution_id", "status", "result", "replayed", "completed_at"}
    assert body["status"] == "succeeded" and not body["replayed"]
    assert body["result"] == {
        "resource_ref": "work_item:1",
        "title": "Updated via HTTP",
        "description": "  Keep description whitespace.  ",
        "lifecycle": "open",
        "classification": "internal",
        "version": 2,
    }
    replay = await work_client.put(BASE + "/details", json={**PAYLOAD, "title": "Updated via HTTP"})
    recovered = await work_client.get(BASE + "/operations/http-edit-1")
    for response in (replay, recovered):
        assert response.status_code == 200
        assert response.json() == {**body, "replayed": True}
    stale = await work_client.put(BASE + "/details", json=PAYLOAD, headers={"Idempotency-Key": "new-operation"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "OBJECT_VERSION_CONFLICT"
    conflicting = await work_client.put(BASE + "/details", json={**PAYLOAD, "title": "Different"})
    assert conflicting.status_code == 409 and conflicting.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    missing = await work_client.get(BASE + "/operations/not-submitted")
    assert missing.status_code == 404
    for response in (read, updated, replay, recovered, stale, conflicting, missing):
        assert_private(response)
    assert await counts(persistence_sessions) == (2, 1, 1, 0)


async def test_unknown_commit_returns_original_key_location_and_recovers_without_second_write(
    work_client: httpx.AsyncClient,
    persistence_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = UnitOfWork._commit_by_owner

    async def lose_response(self: UnitOfWork) -> None:
        await commit(self)
        raise OSError("synthetic-private lost commit acknowledgement")

    monkeypatch.setattr(UnitOfWork, "_commit_by_owner", lose_response)
    unknown = await work_client.put(BASE + "/details", json=PAYLOAD)
    assert unknown.status_code == 503
    assert unknown.json()["error"]["code"] == "COMMIT_OUTCOME_UNKNOWN"
    location = unknown.headers["location"]
    assert location.startswith(BASE + "/operations/http-edit-1?") and "token" not in location
    monkeypatch.setattr(UnitOfWork, "_commit_by_owner", commit)
    recovered = await work_client.get(location)
    assert recovered.status_code == 200 and recovered.json()["result"]["version"] == 2
    assert_private(unknown)
    assert_private(recovered)
    assert await counts(persistence_sessions) == (2, 1, 1, 0)


async def test_revoked_current_relation_blocks_http_read_replay_and_result_recovery(
    work_client: httpx.AsyncClient, persistence_sessions: async_sessionmaker[AsyncSession]
) -> None:
    assert (await work_client.put(BASE + "/details", json=PAYLOAD)).status_code == 200
    async with persistence_sessions.begin() as session:
        await lock_tenant(session, ORG)
        relation = await session.scalar(select(RelationTupleTable))
        assert relation is not None
        relation.status = "revoked"
        relation.revoked_at = datetime.now(UTC)
        relation.version += 1
    responses = (
        await work_client.get(BASE),
        await work_client.put(BASE + "/details", json=PAYLOAD),
        await work_client.get(BASE + "/operations/http-edit-1"),
    )
    for response in responses:
        assert response.status_code == 403
        assert_private(response)
    assert await counts(persistence_sessions) == (2, 1, 1, 0)


async def test_parallel_people_and_assignments_never_borrow_authority(
    work_client: httpx.AsyncClient,
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
) -> None:
    await _install_reader(persistence_sessions, another_person=True)
    assert (await work_client.put(BASE + "/details", json=PAYLOAD)).status_code == 200
    alice = token(signing_key)
    bob = token(signing_key, claims={"sub": "subject-2"})
    cases = (
        (alice, "assignment:engineering", "GET", BASE, 200),
        (bob, "assignment:reader", "GET", BASE, 200),
        (alice, "assignment:reader", "GET", BASE, 403),
        (bob, "assignment:engineering", "GET", BASE, 403),
        (bob, "assignment:reader", "PUT", BASE + "/details", 403),
        (bob, "assignment:reader", "GET", BASE + "/operations/http-edit-1", 404),
    )

    async def request_case(case: tuple[str, str, str, str, int]) -> None:
        credential, assignment, method, path, expected = case
        response = await work_client.request(
            method,
            path,
            params={**QUERY, "assignment_ref": assignment},
            headers={"Authorization": "Bearer " + credential},
            json=PAYLOAD if method == "PUT" else None,
        )
        assert response.status_code == expected
        assert_private(response)

    await asyncio.gather(*(request_case(case) for case in cases * 2))
    assert await counts(persistence_sessions) == (2, 1, 1, 0)


async def test_unicode_description_still_obeys_encoded_command_budget(
    work_client: httpx.AsyncClient, persistence_sessions: async_sessionmaker[AsyncSession]
) -> None:
    response = await work_client.put(BASE + "/details", json={**PAYLOAD, "description": "🧪" * 20_000})
    assert response.status_code == 422 and response.json()["error"]["code"] == "COMMAND_INVALID"
    assert_private(response)
    assert await counts(persistence_sessions) == (1, 0, 0, 0)


async def test_workload_credential_cannot_be_used_as_a_person(
    work_client: httpx.AsyncClient,
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
) -> None:
    response = await work_client.put(
        BASE + "/details",
        json=PAYLOAD,
        headers={"Authorization": f"Bearer {token(signing_key, claims={'stage_credential_kind': 'workload_oauth2'})}"},
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'
    assert_private(response)
    assert await counts(persistence_sessions) == (1, 0, 0, 0)


@pytest.mark.parametrize("work_client", [0.5], indirect=True)
async def test_real_service_timeout_cancels_work_and_releases_runtime_calls(
    work_client: httpx.AsyncClient,
    persistence_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_authenticate = IdentityFacade.authenticate_human
    cancelled = asyncio.Event()

    async def stall_after_authentication(
        self: IdentityFacade, access_token: str, *, profile_ref: str, profile_version: int
    ) -> HumanAuthenticationEvidence:
        evidence = await original_authenticate(
            self, access_token, profile_ref=profile_ref, profile_version=profile_version
        )
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return evidence

    monkeypatch.setattr(IdentityFacade, "authenticate_human", stall_after_authentication)
    response = await work_client.put(BASE + "/details", json=PAYLOAD)
    assert response.status_code == 503 and response.json()["error"]["code"] == "WORK_UNAVAILABLE"
    assert cancelled.is_set()
    assert_private(response)
    assert await counts(persistence_sessions) == (1, 0, 0, 0)
    monkeypatch.setattr(IdentityFacade, "authenticate_human", original_authenticate)
    assert (await work_client.get(BASE)).status_code == 200
    # The fixture also requires RuntimeHost.close to finish with no held calls.


@pytest.mark.parametrize(
    ("method", "path", "params", "payload", "headers", "status"),
    [
        ("GET", BASE, QUERY, None, {}, 401),
        ("GET", BASE, QUERY, None, HEADERS, 503),
        ("GET", BASE, {}, None, HEADERS, 422),
        ("GET", BASE, {**QUERY, "actor_ref": "forged"}, None, HEADERS, 422),
        ("GET", BASE, {**QUERY, "organization_ref": "forged"}, None, HEADERS, 422),
        ("GET", BASE, {**QUERY, "tenant_ref": "forged"}, None, HEADERS, 422),
        ("GET", BASE, {**QUERY, "profile_version": 0}, None, HEADERS, 422),
        ("GET", BASE, {**QUERY, "profile_ref": "local-human\x00"}, None, HEADERS, 422),
        ("GET", BASE, {**QUERY, "assignment_ref": "assignment:engineering\x00"}, None, HEADERS, 422),
        ("GET", BASE, {**QUERY, "assignment_ref": "assignment:\x1fengineering"}, None, HEADERS, 422),
        ("GET", BASE.replace("/1", "/0"), QUERY, None, HEADERS, 422),
        ("GET", BASE.replace("/1", "/2147483648"), QUERY, None, HEADERS, 422),
        ("PUT", BASE + "/details", QUERY, PAYLOAD, {"Authorization": "Bearer synthetic-token"}, 422),
        ("PUT", BASE + "/details", QUERY, {**PAYLOAD, "expected_version": True}, HEADERS, 422),
        ("PUT", BASE + "/details", QUERY, {**PAYLOAD, "expected_version": "1"}, HEADERS, 422),
        ("PUT", BASE + "/details", QUERY, {**PAYLOAD, "actor_ref": "forged"}, HEADERS, 422),
        ("PUT", BASE + "/details", QUERY, {**PAYLOAD, "lifecycle": "closed"}, HEADERS, 422),
        ("PUT", BASE + "/details", QUERY, {**PAYLOAD, "human_confirmed": True}, HEADERS, 422),
        ("PUT", BASE + "/details", QUERY, {"title": "Valid", "expected_version": 1}, HEADERS, 422),
        ("PUT", BASE + "/details", QUERY, PAYLOAD, {**HEADERS, "Idempotency-Key": "unsafe/key"}, 422),
        ("GET", BASE + "/operations/bad%20key", QUERY, None, HEADERS, 422),
        ("POST", BASE, QUERY, PAYLOAD, HEADERS, 405),
        ("GET", BASE + "/missing", QUERY, None, HEADERS, 404),
    ],
)
async def test_http_validation_identity_and_cache_boundaries(
    method: str,
    path: str,
    params: dict[str, Any],
    payload: dict[str, Any] | None,
    headers: dict[str, str],
    status: int,
) -> None:
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://stage") as client:
        response = await client.request(method, path, params=params, json=payload, headers=headers)
    assert response.status_code == status
    assert_private(response)


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (AuthenticationRejected(), 401, "INVALID_CREDENTIAL"),
        (ContextExpired(), 401, "INVALID_CREDENTIAL"),
        (IdentityDenied(), 403, "WORK_ACCESS_DENIED"),
        (AuthorizationDenied(), 403, "WORK_ACCESS_DENIED"),
        (ObjectVersionConflict(), 409, "OBJECT_VERSION_CONFLICT"),
        (WorkDetailsConflict(), 409, "WORK_DETAILS_CONFLICT"),
        (IdempotencyConflict(), 409, "IDEMPOTENCY_CONFLICT"),
        (ExecutionNotFound(), 404, "OPERATION_NOT_FOUND"),
        (InvalidCommand(), 422, "COMMAND_INVALID"),
        (CommitOutcomeUnknown("private-internal-execution"), 503, "COMMIT_OUTCOME_UNKNOWN"),
        (AuthenticationUnavailable(), 503, "WORK_UNAVAILABLE"),
        (SQLAlchemyError("synthetic-private SQL"), 503, "WORK_UNAVAILABLE"),
        (TimeoutError(), 503, "WORK_UNAVAILABLE"),
        (RequestDeadlineExceeded(), 503, "WORK_UNAVAILABLE"),
        (RuntimeError("synthetic-private exception"), 500, "INTERNAL_ERROR"),
    ],
)
async def test_safe_failure_mapping_preserves_request_id_and_cache_policy(
    failure: Exception, status: int, code: str
) -> None:
    async def fail(*args: Any, **kwargs: Any) -> None:
        raise failure

    def setup(ctx: ComponentContext) -> None:
        ctx.provide(WORK, SimpleNamespace(update_details=fail))

    host = RuntimeHost()
    host.load(PluginSpec("test.work", setup, provides=(WORK,)))
    assert (await host.await_ready([WORK], timeout=2)).ready
    app = create_app()
    app.state.runtime = SimpleNamespace(host=host, ready=True)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://stage") as client:
            response = await client.put(BASE + "/details", params=QUERY, json=PAYLOAD, headers=HEADERS)
        assert response.status_code == status and response.json()["error"]["code"] == code
        assert response.headers["x-request-id"] == HEADERS["X-Request-ID"]
        assert "private-internal-execution" not in response.text
        assert_private(response)
    finally:
        assert (await host.close(timeout=2)).successful


@pytest.mark.parametrize("prefix", ["/stage", "/nested/stage"])
async def test_proxy_root_path_keeps_identity_and_work_errors_private(prefix: str) -> None:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, root_path=prefix), base_url="http://stage"
    ) as client:
        for path in ("/api/v1/identity/me", BASE, BASE + "/missing"):
            response = await client.get(prefix + path, params=QUERY)
            assert response.status_code in {401, 404, 422}
            assert_private(response)
        response = await client.get(prefix + "/api/v1/work-items-unrelated")
        assert response.status_code == 404 and "cache-control" not in response.headers
