"""Activation forms preserve issuer/browser isolation and one-time secret rendering."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI

from app.api.native import activation
from app.api.native_auth import NativeAuthSecurityMiddleware, router
from app.native_auth.activation import ActivationProgress
from app.native_auth.config import NativeAuthConfig
from app.native_auth.errors import NativeCommitUnknown, NativeCredentialError
from tests.support.native_http import fields

ISSUER = "https://activation.example.test"
ACTIVATION_REF = "activation:employee-http"
ACTIVATION_CODE = "synthetic-private-activation-code"
PASSWORD = "synthetic-employee-http-password"
TOTP_SECRET = "SYNTHETICTOTPSECRET"
RECOVERY_CODES = ("synthetic-recovery-first", "synthetic-recovery-second")


@dataclass
class ActivationStub:
    """Record calls at the HTTP seam; credential and transaction behavior has its own tests."""

    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    bound_secret: str | None = None
    value: ActivationProgress = field(
        default_factory=lambda: ActivationProgress(
            activation_ref=ACTIVATION_REF,
            status="enrolling",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            totp_secret=TOTP_SECRET,
            totp_uri="otpauth://totp/Stage:synthetic?secret=" + TOTP_SECRET,
        )
    )
    begin_failure: Exception | None = None
    complete_failure: Exception | None = None

    async def begin(self, **values: str) -> ActivationProgress:
        self.calls.append(("begin", values))
        if self.begin_failure is not None:
            raise self.begin_failure
        self.bound_secret = values["browser_secret"]
        return self.value

    async def progress(self, **values: str) -> ActivationProgress:
        self.calls.append(("progress", values))
        if values["activation_ref"] != ACTIVATION_REF or values["browser_secret"] != self.bound_secret:
            raise NativeCredentialError()
        return self.value

    async def complete(self, **values: str) -> ActivationProgress:
        self.calls.append(("complete", values))
        if self.complete_failure is not None:
            raise self.complete_failure
        if values["activation_ref"] != ACTIVATION_REF or values["browser_secret"] != self.bound_secret:
            raise NativeCredentialError()
        self.value = replace(self.value, status="completed", totp_secret=None, totp_uri=None)
        return replace(self.value, recovery_codes=RECOVERY_CODES)


@dataclass
class PeerBudget:
    peers: list[str] = field(default_factory=list)
    failure: Exception | None = None

    async def throttle_activation(self, peer: str) -> None:
        self.peers.append(peer)
        if self.failure is not None:
            raise self.failure


@dataclass
class Harness:
    client: httpx.AsyncClient
    app: FastAPI
    service: ActivationStub
    budget: PeerBudget


@pytest.fixture
async def activation_http(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    service = ActivationStub()
    budget = PeerBudget()
    config = NativeAuthConfig(issuer=ISSUER, organization_ref="org:activation-http", environment="test")

    @asynccontextmanager
    async def services(_request: Any) -> AsyncIterator[tuple[Any, Any]]:
        yield SimpleNamespace(config=config), SimpleNamespace(activation=service, credentials=budget)

    monkeypatch.setattr(activation, "native_services", services)
    app = FastAPI()
    app.state.settings = SimpleNamespace(native_auth_enabled=True, native_auth_request_timeout_seconds=5)
    app.include_router(router)
    app.add_middleware(NativeAuthSecurityMiddleware)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER) as client:
        yield Harness(client, app, service, budget)


async def entry(harness: Harness) -> dict[str, str]:
    response = await harness.client.get("/oauth/activate")
    assert response.status_code == 200
    return {**fields(response), "activation_ref": ACTIVATION_REF, "code": ACTIVATION_CODE, "password": PASSWORD}


async def enrolling(harness: Harness) -> dict[str, str]:
    form = await entry(harness)
    response = await harness.client.post("/oauth/activate", data=form, headers={"origin": ISSUER})
    assert response.status_code == 200
    return {**fields(response), "otp": "123456"}


def assert_private(response: httpx.Response, *, html: bool = False) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == ("same-origin" if html else "no-referrer")
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    )


async def test_activation_entry_uses_dedicated_cookie_and_configured_issuer(activation_http: Harness) -> None:
    response = await activation_http.client.get(
        "/oauth/activate", headers={"host": "attacker.invalid", "x-forwarded-host": "attacker.invalid"}
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-stage-activation=")
    assert all(value in cookie for value in ("HttpOnly", "Secure", "Path=/", "SameSite=lax"))
    assert "Domain=" not in cookie
    assert f'action="{ISSUER}/oauth/activate"' in response.text
    assert f'action="{ISSUER}/oauth/activate/status"' in response.text
    assert "attacker.invalid" not in response.text
    assert "stage_sso" not in cookie and "stage-native-login" not in cookie
    assert activation_http.service.calls == []
    assert_private(response, html=True)


async def test_activation_form_progress_and_completion_never_issue_login_or_tokens(activation_http: Harness) -> None:
    form = await entry(activation_http)
    cookie = activation_http.client.cookies.get("__Host-stage-activation")
    repeated = await activation_http.client.get("/oauth/activate")
    assert fields(repeated)["csrf_token"] == form["csrf_token"]
    assert activation_http.client.cookies.get("__Host-stage-activation") == cookie
    response = await activation_http.client.post("/oauth/activate", data=form, headers={"origin": ISSUER})
    assert response.status_code == 200
    assert TOTP_SECRET in response.text
    assert ACTIVATION_CODE not in response.text and PASSWORD not in response.text
    assert f'action="{ISSUER}/oauth/activate/complete"' in response.text
    assert activation_http.service.calls[-1] == (
        "begin",
        {
            "activation_ref": ACTIVATION_REF,
            "code": ACTIVATION_CODE,
            "password": PASSWORD,
            "browser_secret": cookie,
            "csrf_token": form["csrf_token"],
            "peer_key": "127.0.0.1",
        },
    )
    status = await activation_http.client.get("/oauth/activate/status", params={"activation_ref": ACTIVATION_REF})
    assert status.status_code == 200 and TOTP_SECRET in status.text
    assert activation_http.service.calls[-1] == (
        "progress",
        {"activation_ref": ACTIVATION_REF, "browser_secret": cookie},
    )
    completed = await activation_http.client.post(
        "/oauth/activate/complete", data={**fields(status), "otp": "123456"}, headers={"origin": ISSUER}
    )
    assert completed.status_code == 200 and "账号激活完成" in completed.text
    assert all(code in completed.text for code in RECOVERY_CODES)
    assert (
        TOTP_SECRET not in completed.text and ACTIVATION_CODE not in completed.text and PASSWORD not in completed.text
    )
    assert "location" not in completed.headers and "set-cookie" not in completed.headers
    assert set(activation_http.client.cookies.keys()) == {"__Host-stage-activation"}
    for item in (response, status, completed):
        assert_private(item, html=True)
        assert not any(value in item.text for value in ("access_token", "refresh_token", "id_token"))
    # Suppress secrets on GET even if a service accidentally reuses a completion DTO.
    activation_http.service.value = replace(activation_http.service.value, recovery_codes=RECOVERY_CODES)
    refreshed = await activation_http.client.get("/oauth/activate/status", params={"activation_ref": ACTIVATION_REF})
    assert refreshed.status_code == 200 and "账号激活完成" in refreshed.text
    assert all(code not in refreshed.text for code in RECOVERY_CODES)


@pytest.mark.parametrize("stage", ["begin", "complete"])
@pytest.mark.parametrize(
    "invalid", ["csrf", "unicode_csrf", "cookie", "duplicate_cookie", "duplicate_cookie_header", "origin", "fetch_site"]
)
async def test_activation_rejects_cross_browser_or_cross_origin_writes_before_service(
    activation_http: Harness, stage: str, invalid: str
) -> None:
    form = await entry(activation_http) if stage == "begin" else await enrolling(activation_http)
    path = "/oauth/activate" if stage == "begin" else "/oauth/activate/complete"
    headers: list[tuple[str, str]] = []
    if invalid in {"csrf", "unicode_csrf"}:
        form["csrf_token"] = "wrong" if invalid == "csrf" else "错误"
    elif invalid == "cookie":
        activation_http.client.cookies.clear()
    elif invalid.startswith("duplicate_cookie"):
        value = activation_http.client.cookies.get("__Host-stage-activation")
        if invalid == "duplicate_cookie":
            headers.append(("cookie", f"__Host-stage-activation={value}; __Host-stage-activation={value}"))
        else:
            headers.extend([("cookie", f"__Host-stage-activation={value}")] * 2)
    elif invalid == "origin":
        headers.append(("origin", "https://attacker.invalid"))
    else:
        headers.append(("sec-fetch-site", "cross-site"))
    calls = len(activation_http.service.calls)
    response = await activation_http.client.post(path, data=form, headers=headers)
    assert response.status_code in {400, 403}
    assert len(activation_http.service.calls) == calls
    assert all(value not in response.text for value in (ACTIVATION_CODE, PASSWORD, TOTP_SECRET))
    assert_private(response)


@pytest.mark.parametrize(
    ("path", "body", "content_type", "status"),
    [
        ("/oauth/activate", "activation_ref=a&activation_ref=b", "application/x-www-form-urlencoded", 400),
        ("/oauth/activate", "activation_ref=%zz", "application/x-www-form-urlencoded", 400),
        ("/oauth/activate", "password=" + "x" * 8192, "application/x-www-form-urlencoded", 413),
        ("/oauth/activate", '{"password":"private"}', "application/json", 415),
        ("/oauth/activate?code=private", "activation_ref=a", "application/x-www-form-urlencoded", 400),
        ("/oauth/activate/complete", "activation_ref=a&otp=x&otp=y", "application/x-www-form-urlencoded", 400),
        (
            "/oauth/activate/complete",
            "activation_ref=a&csrf_token=x&otp=123456&password=private",
            "application/x-www-form-urlencoded",
            400,
        ),
    ],
)
async def test_activation_forms_reject_ambiguous_oversized_and_query_secrets(
    activation_http: Harness, path: str, body: str, content_type: str, status: int
) -> None:
    response = await activation_http.client.post(path, content=body, headers={"content-type": content_type})
    assert response.status_code == status
    assert activation_http.service.calls == []
    assert "private" not in response.text
    assert_private(response)


@pytest.mark.parametrize(
    "path",
    [
        "/oauth/activate?code=synthetic-private-code",
        "/oauth/activate?activation_ref=anything",
        "/oauth/activate/status",
        "/oauth/activate/status?activation_ref=a&activation_ref=b",
        "/oauth/activate/status?activation_ref=a&password=synthetic-private-password",
        "/oauth/activate/status?activation_ref=a&code=synthetic-private-code",
    ],
)
async def test_activation_gets_accept_only_the_nonsecret_status_reference(activation_http: Harness, path: str) -> None:
    response = await activation_http.client.get(path)
    assert response.status_code == 400
    assert activation_http.service.calls == []
    assert "synthetic-private" not in response.text
    assert_private(response)


@pytest.mark.parametrize("failure", [NativeCredentialError(), NativeCredentialError("invalid_password")])
async def test_activation_invalid_initial_credentials_show_safe_retry_form(
    activation_http: Harness, failure: NativeCredentialError
) -> None:
    activation_http.service.begin_failure = failure
    response = await activation_http.client.post("/oauth/activate", data=await entry(activation_http))
    assert response.status_code == 400 and 'role="alert"' in response.text
    assert 'name="password"' in response.text
    assert ACTIVATION_CODE not in response.text and PASSWORD not in response.text and TOTP_SECRET not in response.text
    assert_private(response, html=True)


async def test_activation_bad_otp_preserves_bound_enrollment_without_replaying_initial_secrets(
    activation_http: Harness,
) -> None:
    form = await enrolling(activation_http)
    activation_http.service.complete_failure = NativeCredentialError()
    response = await activation_http.client.post("/oauth/activate/complete", data=form)
    assert response.status_code == 400 and TOTP_SECRET in response.text
    assert 'role="alert"' in response.text and 'name="otp"' in response.text
    assert ACTIVATION_CODE not in response.text and PASSWORD not in response.text
    assert_private(response, html=True)


@pytest.mark.parametrize("stage", ["begin", "complete"])
async def test_activation_unknown_commit_offers_browser_bound_status_without_reposting_secrets(
    activation_http: Harness, stage: str
) -> None:
    if stage == "begin":
        form = await entry(activation_http)
        activation_http.service.begin_failure = NativeCommitUnknown()
        path = "/oauth/activate"
    else:
        form = await enrolling(activation_http)
        activation_http.service.complete_failure = NativeCommitUnknown()
        path = "/oauth/activate/complete"
    browser_secret = activation_http.client.cookies.get("__Host-stage-activation")
    deadline = activation_http.service.value.expires_at
    response = await activation_http.client.post(path, data=form)
    assert response.status_code == 503 and "正在确认激活结果" in response.text
    assert "/oauth/activate/status?" + urlencode({"activation_ref": ACTIVATION_REF}) in response.text
    assert "<form" not in response.text
    assert not any(value in response.text for value in (ACTIVATION_CODE, PASSWORD, TOTP_SECRET, *RECOVERY_CODES))
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"__Host-stage-activation={browser_secret};") and "Max-Age=300" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert set(activation_http.client.cookies.keys()) == {"__Host-stage-activation"}
    assert activation_http.client.cookies.get("__Host-stage-activation") == browser_secret
    # Model the committed branch of an uncertain response. The status request
    # must reuse that original proof and never replay completion secrets.
    activation_http.service.bound_secret = browser_secret
    if stage == "complete":
        activation_http.service.value = replace(
            activation_http.service.value,
            status="completed",
            totp_secret=None,
            totp_uri=None,
            recovery_codes=RECOVERY_CODES,
        )
    recovered = await activation_http.client.get("/oauth/activate/status", params={"activation_ref": ACTIVATION_REF})
    assert recovered.status_code == 200
    assert all(value not in recovered.text for value in (ACTIVATION_CODE, PASSWORD, *RECOVERY_CODES))
    assert activation_http.service.value.expires_at == deadline
    assert activation_http.service.calls[-1] == (
        "progress",
        {"activation_ref": ACTIVATION_REF, "browser_secret": browser_secret},
    )
    assert_private(response, html=True)


async def test_activation_status_exhausted_peer_budget_never_enters_exclusive_domain_locks(
    activation_http: Harness,
) -> None:
    await enrolling(activation_http)
    count = len(activation_http.service.calls)
    activation_http.budget.failure = NativeCredentialError("rate_limited")
    response = await activation_http.client.get("/oauth/activate/status", params={"activation_ref": ACTIVATION_REF})
    assert response.status_code == 429 and response.headers["retry-after"] == "60"
    assert activation_http.budget.peers == ["127.0.0.1"]
    assert len(activation_http.service.calls) == count
    assert TOTP_SECRET not in response.text
    assert_private(response)


async def test_activation_status_budget_is_shared_by_peer_across_activation_references(
    activation_http: Harness,
) -> None:
    await enrolling(activation_http)
    for reference in (ACTIVATION_REF, "another-activation-reference"):
        await activation_http.client.get("/oauth/activate/status", params={"activation_ref": reference})
    assert activation_http.budget.peers == ["127.0.0.1", "127.0.0.1"]


@pytest.mark.parametrize("failure,status", [(NativeCredentialError("rate_limited"), 429), (TimeoutError(), 503)])
async def test_activation_throttle_and_unavailability_do_not_reflect_secrets(
    activation_http: Harness, failure: Exception, status: int
) -> None:
    activation_http.service.begin_failure = failure
    response = await activation_http.client.post("/oauth/activate", data=await entry(activation_http))
    assert response.status_code == status
    assert ACTIVATION_CODE not in response.text and PASSWORD not in response.text
    if status == 429:
        assert response.headers["retry-after"] == "60"
    assert_private(response)


async def test_activation_status_rejects_a_different_browser_cookie(activation_http: Harness) -> None:
    await enrolling(activation_http)
    activation_http.client.cookies.clear()
    await activation_http.client.get("/oauth/activate")
    response = await activation_http.client.get("/oauth/activate/status", params={"activation_ref": ACTIVATION_REF})
    assert response.status_code == 400
    assert TOTP_SECRET not in response.text
    assert_private(response)


@pytest.mark.parametrize("prefix", ["/stage", "/nested/stage"])
async def test_activation_proxy_root_path_preserves_security_headers_and_canonical_issuer_actions(
    activation_http: Harness, prefix: str
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=activation_http.app, root_path=prefix), base_url=ISSUER
    ) as client:
        response = await client.get(prefix + "/oauth/activate")
        assert response.status_code == 200
        assert f'action="{ISSUER}/oauth/activate"' in response.text
        assert prefix + "/oauth/activate" not in response.text
        assert_private(response, html=True)
        for path in ("/oauth/activate/status", "/oauth/activate/missing"):
            response = await client.get(prefix + path)
            assert response.status_code in {400, 404}
            assert_private(response)
        response = await client.get(prefix + "/oauth-unrelated")
        assert response.status_code == 404 and "cache-control" not in response.headers


async def test_disabled_native_auth_hides_every_activation_step(activation_http: Harness) -> None:
    activation_http.app.state.settings.native_auth_enabled = False
    for method, path in (
        ("GET", "/oauth/activate"),
        ("POST", "/oauth/activate"),
        ("GET", "/oauth/activate/status"),
        ("POST", "/oauth/activate/complete"),
    ):
        response = await activation_http.client.request(method, path)
        assert response.status_code == 404
        assert_private(response)
    assert activation_http.service.calls == []


def test_activation_openapi_has_no_bearer_grant_and_keeps_secrets_in_form_bodies(activation_http: Harness) -> None:
    paths = activation_http.app.openapi()["paths"]
    assert not paths["/oauth/activate"]["get"].get("parameters")
    for path, fields_expected in (
        ("/oauth/activate", {"activation_ref", "code", "password", "csrf_token"}),
        ("/oauth/activate/complete", {"activation_ref", "csrf_token", "otp"}),
    ):
        operation = paths[path]["post"]
        body = operation["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]
        assert set(body["required"]) == fields_expected
        assert body["additionalProperties"] is False
        assert not operation.get("parameters") and not operation.get("security")
        assert "text/html" in operation["responses"]["200"]["content"]
    assert {item["name"] for item in paths["/oauth/activate/status"]["get"]["parameters"]} == {"activation_ref"}
