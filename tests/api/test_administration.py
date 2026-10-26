"""Administration HTTP contracts, error privacy and explicit authority selection."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from app.administration.contracts import (
    AdminAssignmentList,
    AdminAssignmentView,
    AdministrationMe,
    AuditList,
    AuthorityView,
    GrantRequest,
    OperationResult,
    OperationType,
    PeopleList,
    PersonView,
)
from app.administration.errors import AdminCommitUnknown, AdminConflict, AdminDenied, AdminNotFound
from app.api.administration import AdministrationSecurityMiddleware, router
from app.core.errors import register_exception_handlers
from app.core.middleware import RequestIdMiddleware
from app.identity.authentication import (
    AuthenticationAssuranceRequired,
    AuthenticationRejected,
    AuthenticationUnavailable,
)
from app.identity.service import IdentityDenied
from app.providers.administration import ADMINISTRATION, AdministrationFacade
from app.runtime import ComponentContext, PluginSpec, RuntimeHost

BASE = "/api/v1/administration"
PROFILE: dict[str, str | int] = {"profile_ref": "native-human", "profile_version": 1}
QUERY: dict[str, str | int] = {**PROFILE, "assignment_ref": "assignment:administrator"}
HEADERS = {
    "Authorization": "Bearer synthetic-admin-token",
    "Idempotency-Key": "admin-test:1",
    "X-Request-ID": "admin.http.1",
}
GRANT = {
    "membership_ref": "membership:target",
    "expected_membership_version": 1,
    "expected_authority_version": 2,
    "role_ref": "role:engineering",
    "role_revision": 1,
    "scope_ref": "scope:engineering",
    "expires_at": "2026-11-01T00:00:00+08:00",
    "reason": "Explicit onboarding",
}
REVOKE = {"expected_assignment_version": 1, "expected_authority_version": 2, "reason": "Assignment ended"}


def assignment_view() -> AdminAssignmentView:
    return AdminAssignmentView(
        principal_ref="person:target",
        membership_ref="membership:target",
        assignment_ref="assignment:target",
        role_ref="role:engineering",
        role_revision=1,
        role_code="engineering-member",
        role_display_name="Engineering",
        scope_ref="scope:engineering",
        valid_from=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=1),
        status="active",
        version=1,
    )


class RecordingFacade:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.error: Exception | None = None
        now = datetime.now(UTC)
        person = PersonView(
            principal_ref="person:target",
            principal_status="active",
            principal_version=1,
            membership_ref="membership:target",
            membership_status="active",
            membership_version=1,
            valid_from=now,
            expires_at=None,
        )
        self.responses: dict[str, BaseModel] = {
            "me": AdministrationMe(
                principal_ref="person:admin",
                membership_ref="membership:admin",
                authority=AuthorityView(
                    authority_ref="authority:bootstrap",
                    status="pending",
                    version=1,
                    assignment_ref="assignment:administrator",
                    nomination_expires_at=now + timedelta(days=1),
                    accepted_at=None,
                    grant_rules=(),
                ),
            ),
            "people": PeopleList(items=(person,), next_after=None),
            "person": person,
            "assignments": AdminAssignmentList(items=(assignment_view(),), next_after=None),
            "audit": AuditList(items=(), next_after=None),
        }
        for method, kind in (
            ("activate", "activation"),
            ("grant", "assignment.grant"),
            ("revoke", "assignment.revoke"),
            ("operation", "assignment.grant"),
        ):
            self.responses[method] = OperationResult(
                operation_ref="operation:1",
                operation_type=cast(OperationType, kind),
                status="succeeded",
                result=assignment_view(),
                replayed=method == "operation",
                completed_at=now,
            )

    def __getattr__(self, name: str) -> Any:
        async def call(token: str, **values: Any) -> BaseModel:
            self.calls.append((name, token, values))
            if self.error is not None:
                raise self.error
            return self.responses[name]

        return call


@pytest.fixture
async def admin_http() -> AsyncIterator[tuple[httpx.AsyncClient, RecordingFacade, FastAPI]]:
    facade = RecordingFacade()
    host = RuntimeHost()

    def provide(ctx: ComponentContext) -> None:
        ctx.provide(ADMINISTRATION, cast(AdministrationFacade, facade))

    host.load(PluginSpec("admin-http", provide, provides=(ADMINISTRATION,)))
    assert (await host.await_ready([ADMINISTRATION], timeout=2)).ready
    app = FastAPI()
    app.state.runtime = SimpleNamespace(host=host, ready=True)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(AdministrationSecurityMiddleware)
    register_exception_handlers(app)
    app.include_router(router, prefix="/api/v1")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://stage", headers=HEADERS
        ) as client:
            yield client, facade, app
    finally:
        assert (await host.close(timeout=2)).successful


def private(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert "synthetic-admin-token" not in response.text


@pytest.mark.parametrize(
    "method,path,query,payload,service_method",
    [
        ("GET", "/me", PROFILE, None, "me"),
        ("POST", "/activation", PROFILE, {"expected_authority_version": 1}, "activate"),
        ("GET", "/people", QUERY, None, "people"),
        ("GET", "/people/person:target", QUERY, None, "person"),
        ("GET", "/people/person:target/assignments", QUERY, None, "assignments"),
        ("POST", "/people/person:target/assignments", QUERY, GRANT, "grant"),
        ("POST", "/people/person:target/assignments/assignment:target/revoke", QUERY, REVOKE, "revoke"),
        ("GET", "/operations/admin-test:1", QUERY, None, "operation"),
        ("GET", "/audit", QUERY, None, "audit"),
    ],
)
async def test_each_endpoint_forwards_only_explicit_request_context(
    admin_http: tuple[httpx.AsyncClient, RecordingFacade, FastAPI],
    method: str,
    path: str,
    query: dict[str, Any],
    payload: dict[str, Any] | None,
    service_method: str,
) -> None:
    client, facade, _ = admin_http
    response = await client.request(method, BASE + path, params=query, json=payload)
    assert response.status_code == 200, response.text
    private(response)
    assert len(facade.calls) == 1
    name, token, values = facade.calls[0]
    assert name == service_method and token == "synthetic-admin-token"
    assert values["profile_ref"] == PROFILE["profile_ref"] and values["profile_version"] == 1
    if service_method not in {"me", "activate"}:
        assert values["assignment_ref"] == QUERY["assignment_ref"]
    if method == "POST":
        assert values["idempotency_key"] == "admin-test:1" and values["request_id"] == "admin.http.1"
        assert isinstance(values["request"], BaseModel)
    if service_method == "grant":
        assert isinstance(values["request"], GrantRequest)
        assert values["request"].expires_at == datetime(2026, 10, 31, 16, tzinfo=UTC)
    if service_method == "revoke":
        assert values["target_assignment_ref"] == "assignment:target"


@pytest.mark.parametrize(
    "path", ["/people", "/people/person:target", "/people/person:target/assignments", "/operations/key", "/audit"]
)
async def test_management_reads_require_selected_assignment(
    admin_http: tuple[httpx.AsyncClient, RecordingFacade, FastAPI],
    path: str,
) -> None:
    client, facade, _ = admin_http
    response = await client.get(BASE + path, params=PROFILE)
    assert response.status_code == 422 and facade.calls == []
    private(response)


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_membership_version", True),
        ("expected_membership_version", "1"),
        ("expected_authority_version", 0),
        ("role_revision", 2**31),
        ("expires_at", None),
        ("expires_at", "2026-11-01T00:00:00"),
        ("expires_at", 1793491200),
        ("reason", "   "),
        ("reason", "x\x00y"),
        ("reason", "x" * 501),
        ("principal_ref", "forged-actor"),
        ("organization_ref", "another-org"),
        ("scope_ref", "x" * 257),
    ],
)
async def test_grant_rejects_implicit_identity_invalid_versions_and_expiry(
    admin_http: tuple[httpx.AsyncClient, RecordingFacade, FastAPI],
    field: str,
    value: Any,
) -> None:
    client, facade, _ = admin_http
    response = await client.post(BASE + "/people/person:target/assignments", params=QUERY, json={**GRANT, field: value})
    assert response.status_code == 422 and facade.calls == []
    assert response.json()["error"]["code"] == "REQUEST_INVALID"
    private(response)


@pytest.mark.parametrize(
    "path,params",
    [
        ("/people", {**QUERY, "tenant_ref": "forged-tenant"}),
        ("/people", {**QUERY, "limit": 51}),
        ("/audit", {**QUERY, "after": ""}),
        ("/me", QUERY),
        ("/people", {**QUERY, "effective_permissions": "all"}),
    ],
)
async def test_query_extra_selection_and_page_bounds_are_rejected(
    admin_http: tuple[httpx.AsyncClient, RecordingFacade, FastAPI],
    path: str,
    params: dict[str, Any],
) -> None:
    client, facade, _ = admin_http
    response = await client.get(BASE + path, params=params)
    assert response.status_code == 422 and facade.calls == []
    private(response)


@pytest.mark.parametrize("kind,status", [("authorization", 401), ("query", 422), ("idempotency", 422), ("json", 422)])
async def test_duplicate_inputs_never_choose_an_ambiguous_authority(
    admin_http: tuple[httpx.AsyncClient, RecordingFacade, FastAPI],
    kind: str,
    status: int,
) -> None:
    client, facade, _ = admin_http
    if kind == "authorization":
        response = await client.get(
            BASE + "/me",
            params=PROFILE,
            headers=[("Authorization", "Bearer first"), ("Authorization", "Bearer second")],
        )
    elif kind == "query":
        response = await client.get(
            BASE + "/me", params=[("profile_ref", "first"), ("profile_ref", "second"), ("profile_version", "1")]
        )
    elif kind == "idempotency":
        response = await client.post(
            BASE + "/activation",
            params=PROFILE,
            json={"expected_authority_version": 1},
            headers=[("Idempotency-Key", "first"), ("Idempotency-Key", "second")],
        )
    else:
        response = await client.post(
            BASE + "/activation",
            params=PROFILE,
            content='{"expected_authority_version":1,"expected_authority_version":2}',
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == status and facade.calls == []
    private(response)


@pytest.mark.parametrize(
    "error,status,code",
    [
        (AuthenticationAssuranceRequired(), 401, "MFA_REQUIRED"),
        (AuthenticationRejected("private-token-value"), 401, "INVALID_CREDENTIAL"),
        (IdentityDenied(), 403, "ADMINISTRATION_ACCESS_DENIED"),
        (AdminDenied(), 403, "ADMINISTRATION_ACCESS_DENIED"),
        (AdminNotFound(), 404, "ADMINISTRATION_NOT_FOUND"),
        (AdminConflict("OBJECT_VERSION_CONFLICT"), 409, "OBJECT_VERSION_CONFLICT"),
        (AdminConflict("IDEMPOTENCY_CONFLICT"), 409, "IDEMPOTENCY_CONFLICT"),
        (AuthenticationUnavailable("private-source-value"), 503, "ADMINISTRATION_UNAVAILABLE"),
        (SQLAlchemyError("private-database-value"), 503, "ADMINISTRATION_UNAVAILABLE"),
        (TimeoutError("private-timeout-value"), 503, "ADMINISTRATION_UNAVAILABLE"),
    ],
)
async def test_public_error_mapping_keeps_underlying_details_private(
    admin_http: tuple[httpx.AsyncClient, RecordingFacade, FastAPI],
    error: Exception,
    status: int,
    code: str,
) -> None:
    client, facade, _ = admin_http
    facade.error = error
    response = await client.get(BASE + "/people", params=QUERY)
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert "private-" not in response.text
    if code == "MFA_REQUIRED":
        assert response.headers["www-authenticate"] == 'Bearer error="insufficient_user_authentication"'
    private(response)


@pytest.mark.parametrize(
    "path,payload,query",
    [
        ("/activation", {"expected_authority_version": 1}, PROFILE),
        ("/people/person:target/assignments", GRANT, QUERY),
        ("/people/person:target/assignments/assignment:target/revoke", REVOKE, QUERY),
    ],
)
async def test_unknown_commit_location_preserves_original_key_and_server_selected_management_assignment(
    admin_http: tuple[httpx.AsyncClient, RecordingFacade, FastAPI],
    path: str,
    payload: dict[str, Any],
    query: dict[str, Any],
) -> None:
    client, facade, _ = admin_http
    facade.error = AdminCommitUnknown("assignment:server-selected")
    response = await client.post(BASE + path, params=query, json=payload)
    assert response.status_code == 503 and response.json()["error"]["code"] == "COMMIT_OUTCOME_UNKNOWN"
    location = urlsplit(response.headers["location"])
    assert location.path == BASE + "/operations/admin-test%3A1"
    assert parse_qs(location.query) == {
        "profile_ref": ["native-human"],
        "profile_version": ["1"],
        "assignment_ref": ["assignment:server-selected"],
    }
    private(response)


async def test_missing_authentication_is_rejected_before_runtime_or_domain_access(
    admin_http: tuple[httpx.AsyncClient, RecordingFacade, FastAPI],
) -> None:
    client, facade, app = admin_http
    del client.headers["Authorization"]
    app.state.runtime.ready = False
    response = await client.get(BASE + "/me", params=PROFILE)
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"
    assert facade.calls == []
    private(response)


@pytest.mark.parametrize("method,path,status", [("GET", "/missing", 404), ("DELETE", "/me", 405)])
async def test_unmatched_admin_routes_still_receive_privacy_headers(
    admin_http: tuple[httpx.AsyncClient, RecordingFacade, FastAPI],
    method: str,
    path: str,
    status: int,
) -> None:
    client, facade, _ = admin_http
    response = await client.request(method, BASE + path)
    assert response.status_code == status and facade.calls == []
    private(response)


async def test_bounded_json_and_content_type_reject_before_decoding(
    admin_http: tuple[httpx.AsyncClient, RecordingFacade, FastAPI],
) -> None:
    client, facade, _ = admin_http
    too_large = await client.post(
        BASE + "/activation", params=PROFILE, content=" " * 8193, headers={"Content-Type": "application/json"}
    )
    wrong_type = await client.post(BASE + "/activation", params=PROFILE, data={"expected_authority_version": "1"})
    assert too_large.status_code == 413 and wrong_type.status_code == 415 and facade.calls == []
    private(too_large)
    private(wrong_type)


async def test_unicode_existing_people_and_long_scope_are_not_rejected_by_response_contracts(
    admin_http: tuple[httpx.AsyncClient, RecordingFacade, FastAPI],
) -> None:
    client, facade, _ = admin_http
    view = assignment_view().model_copy(update={"principal_ref": "人员:示例", "scope_ref": "scope:" + "x" * 230})
    facade.responses["assignments"] = AdminAssignmentList(items=(view,), next_after=None)
    response = await client.get(BASE + "/people/person:target/assignments", params=QUERY)
    assert response.status_code == 200 and response.json()["items"][0]["principal_ref"] == "人员:示例"
    private(response)


async def test_openapi_exposes_only_explicit_administration_contracts(
    admin_http: tuple[httpx.AsyncClient, RecordingFacade, FastAPI],
) -> None:
    _, _, app = admin_http
    contract = app.openapi()
    paths = contract["paths"]
    assert len(paths) == 8
    for path, operations in paths.items():
        for operation in operations.values():
            assert operation["security"] == [{"HTTPBearer": []}]
            params = {param["name"]: param for param in operation["parameters"]}
            assert params["profile_ref"]["required"] and params["profile_version"]["required"]
            if path not in {BASE + "/me", BASE + "/activation"}:
                assert params["assignment_ref"]["required"]
            assert not {"tenant_ref", "organization_ref", "actor_ref"} & params.keys()
    for name in (
        "ActivationRequest",
        "GrantRequest",
        "RevokeRequest",
        "OperationResult",
        "AuthorityView",
        "PersonView",
    ):
        assert contract["components"]["schemas"][name]["additionalProperties"] is False
    grant = contract["components"]["schemas"]["GrantRequest"]
    assert "expires_at" in grant["required"]
    assert "assignment_ref" not in grant["properties"]


@pytest.mark.parametrize("field", ["membership_ref", "role_ref", "scope_ref"])
async def test_escaped_surrogate_reference_is_a_private_validation_error_before_domain_dispatch(
    admin_http: tuple[httpx.AsyncClient, RecordingFacade, FastAPI],
    field: str,
) -> None:
    client, facade, _ = admin_http
    body = json.dumps({**GRANT, field: "invalid-\ud800"}, ensure_ascii=True)
    response = await client.post(
        BASE + "/people/person:target/assignments",
        params=QUERY,
        content=body.encode("ascii"),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422 and facade.calls == []
    assert response.json()["error"]["code"] == "REQUEST_INVALID"
    assert "invalid-" not in response.text
    private(response)


async def test_valid_unicode_reference_limits_continue_to_count_characters(
    admin_http: tuple[httpx.AsyncClient, RecordingFacade, FastAPI],
) -> None:
    client, facade, _ = admin_http
    response = await client.post(
        BASE + "/people/person:target/assignments",
        params=QUERY,
        json={**GRANT, "membership_ref": "人" * 128, "role_ref": "员" * 128, "scope_ref": "域" * 256},
    )
    assert response.status_code == 200
    request = facade.calls[0][2]["request"]
    assert len(request.membership_ref) == 128 and len(request.role_ref) == 128 and len(request.scope_ref) == 256
    private(response)
