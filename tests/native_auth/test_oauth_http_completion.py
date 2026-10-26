"""Browser protocol boundaries: callbacks, client credentials and session ceremonies."""

import base64
import re
from datetime import UTC, datetime
from html import unescape
from urllib.parse import parse_qs, quote_plus, urlsplit

import pyotp
import pytest
from sqlalchemy import func, select
from starlette.requests import Request

from app.api.native_auth import NativeHTTPError, _authorization_response, _client_authentication
from app.models.native_auth import NativeClientTable, NativeLoginThrottleTable, NativeLogoutRequestTable
from app.native_auth.crypto import digest_secret
from app.native_auth.oauth import AuthorizationResponse
from tests.support.native_http import (
    CALLBACK,
    CLIENT,
    ISSUER,
    PASSWORD,
    VERIFIER,
    Harness,
    begin,
    enroll,
    exchange,
    fields,
    parameters,
    password,
)
from tests.support.native_http import http_keyring as http_keyring  # noqa: F401
from tests.support.native_http import native_http as native_http  # noqa: F401


def request(headers: list[tuple[bytes, bytes]]) -> Request:
    return Request({"type": "http", "headers": headers, "method": "POST", "path": "/oauth/token"})


def basic(client: str, secret: str) -> bytes:
    return b"Basic " + base64.b64encode(f"{quote_plus(client)}:{quote_plus(secret)}".encode())


def test_basic_authentication_decodes_form_components_and_rejects_body_conflict() -> None:
    header = basic("client:with+delimiter", "secret:with plus+and%percent")
    actual = _client_authentication(request([(b"authorization", header)]), {})
    assert actual == {
        "client_id": "client:with+delimiter",
        "client_secret": "secret:with plus+and%percent",
        "client_auth_method": "client_secret_basic",
    }
    assert _client_authentication(request([]), {"client_id": "public"}) == {
        "client_id": "public",
        "client_auth_method": "none",
    }
    with pytest.raises(NativeHTTPError) as conflict:
        _client_authentication(request([(b"authorization", header)]), {"client_id": "other"})
    assert conflict.value.status == 401 and conflict.value.error == "invalid_client"


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"authorization", b"Basic not-base64")],
        [(b"authorization", b"Bearer token")],
        [(b"authorization", b"Basic " + base64.b64encode(b"missing-delimiter"))],
        [(b"authorization", b"Basic " + base64.b64encode(b"client:%zz"))],
        [(b"authorization", b"Basic " + base64.b64encode(b"client:%ff"))],
        [(b"authorization", b"Basic " + base64.b64encode(b"client:"))],
        [(b"authorization", basic("client", "secret")), (b"authorization", basic("client", "other"))],
    ],
)
def test_malformed_or_ambiguous_client_authentication_is_a_protocol_failure(
    headers: list[tuple[bytes, bytes]],
) -> None:
    with pytest.raises(NativeHTTPError) as failure:
        _client_authentication(request(headers), {})
    assert failure.value.status == 401 and failure.value.error == "invalid_client"


def test_form_post_escapes_values_uses_nonce_and_registered_origin_only() -> None:
    callback = "https://client.example/callback?label=%22;script-src%20https://attacker.invalid"
    parameters = {
        "code": '<script>alert("unsafe")</script>',
        "state": '" onfocus="unsafe',
        "iss": "https://issuer.test",
    }
    result = AuthorizationResponse(redirect_uri=callback, parameters=parameters, response_mode="form_post")
    response = _authorization_response(result)
    document = bytes(response.body).decode()
    assert response.status_code == 200 and response.headers["referrer-policy"] == "no-referrer"
    assert '<script>alert("unsafe")</script>' not in document
    controls = {
        name: unescape(value)
        for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', document)
    }
    assert controls == parameters
    nonce = re.findall(r'<script nonce="([^"]+)">', document)[0]
    policy = response.headers["content-security-policy"]
    assert f"script-src 'nonce-{nonce}'" in policy
    assert "form-action https://client.example;" in policy
    assert "attacker.invalid" not in policy and "unsafe-inline" not in policy
    assert 'document.getElementById("callback").submit()' in document
    assert '<button type="submit">' in document


def test_query_authorization_response_preserves_protocol_values_without_rendering_html() -> None:
    result = AuthorizationResponse(
        redirect_uri="https://client.example/callback?registered=1",
        parameters={"error": "login_required", "state": "opaque state", "iss": "https://issuer.test"},
        response_mode="query",
    )
    response = _authorization_response(result)
    assert response.status_code == 303 and response.headers["location"] == result.location
    assert response.body == b""


async def test_prompt_none_requires_a_current_browser_session_and_returns_registered_error(
    native_http: Harness,
) -> None:
    response = await native_http.client.get("/oauth/authorize", params={**parameters(), "prompt": "none"})
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query == {"error": ["login_required"], "state": ["http-state"], "iss": [ISSUER]}
    assert "set-cookie" not in response.headers
    assert response.headers["referrer-policy"] == "no-referrer"
    attack = await native_http.client.get(
        "/oauth/authorize", params={**parameters(), "prompt": "none", "redirect_uri": "https://attacker.invalid"}
    )
    assert attack.status_code == 400 and "location" not in attack.headers


async def test_browser_sso_reuses_authentication_but_prompt_login_and_zero_max_age_do_not(native_http: Harness) -> None:
    await enroll(native_http)
    assert native_http.client.cookies.get("stage_sso")
    assert not native_http.client.cookies.get("stage_native_login")
    silent = await native_http.client.get("/oauth/authorize", params={**parameters(), "prompt": "none"})
    assert silent.status_code == 303
    query = parse_qs(urlsplit(silent.headers["location"]).query)
    assert "code" in query and query["iss"] == [ISSUER]
    for extra in ({"prompt": "login"}, {"max_age": "0"}, {"prompt": "select_account"}):
        forced = await native_http.client.get("/oauth/authorize", params={**parameters(), **extra})
        assert forced.status_code == 200 and 'name="password"' in forced.text
    expired = await native_http.client.get(
        "/oauth/authorize", params={**parameters(), "prompt": "none", "max_age": "0"}
    )
    assert parse_qs(urlsplit(expired.headers["location"]).query)["error"] == ["login_required"]


async def test_optional_state_nonce_and_unsupported_parameters_are_explicit(native_http: Harness) -> None:
    values = {key: value for key, value in parameters().items() if key not in {"state", "nonce"}}
    response = await native_http.client.get("/oauth/authorize", params=values)
    assert response.status_code == 200 and 'name="password"' in response.text
    for extra in ({"request_uri": "https://attacker.invalid"}, {"claims": "{}"}, {"prompt": "none login"}):
        failure = await native_http.client.get("/oauth/authorize", params={**parameters(), **extra})
        if "location" in failure.headers:
            assert parse_qs(urlsplit(failure.headers["location"]).query)["error"] == ["invalid_request"]
        else:
            assert failure.status_code == 400


async def test_sso_form_post_returns_escaped_controls_and_callback_issuer(native_http: Harness) -> None:
    await enroll(native_http)
    response = await native_http.client.get(
        "/oauth/authorize", params={**parameters(), "prompt": "none", "response_mode": "form_post"}
    )
    assert response.status_code == 200 and "location" not in response.headers
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "script-src 'nonce-" in response.headers["content-security-policy"]
    controls = fields(response)
    assert controls["state"] == "http-state" and controls["iss"] == ISSUER and controls["code"]
    exchanged = await native_http.client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT,
            "redirect_uri": CALLBACK,
            "code": controls["code"],
            "code_verifier": VERIFIER,
        },
    )
    assert exchanged.status_code == 200


async def test_consent_required_silent_failure_and_explicit_approval_then_reuse(native_http: Harness) -> None:
    await enroll(native_http)
    async with native_http.sessions.begin() as session:
        session.add(
            NativeClientTable(
                client_id="third-party",
                display_name="External <client>",
                redirect_uris=[CALLBACK],
                require_consent=True,
                allowed_scopes=["openid", "profile"],
                resource_audience="external-api",
            )
        )
    values = {**parameters(), "client_id": "third-party", "scope": "openid profile"}
    silent = await native_http.client.get("/oauth/authorize", params={**values, "prompt": "none"})
    assert parse_qs(urlsplit(silent.headers["location"]).query)["error"] == ["consent_required"]
    page = await native_http.client.get("/oauth/authorize", params=values)
    assert page.status_code == 200 and "External &lt;client&gt;" in page.text and 'name="password"' not in page.text
    assert page.headers["referrer-policy"] == "same-origin"
    assert "http://127.0.0.1:49152" in page.headers["content-security-policy"]
    approved = await native_http.client.post("/oauth/login", data={**fields(page), "decision": "allow"})
    assert approved.status_code == 303
    reused = await native_http.client.get("/oauth/authorize", params={**values, "prompt": "none"})
    assert reused.status_code == 303 and "code" in parse_qs(urlsplit(reused.headers["location"]).query)
    forced = await native_http.client.get("/oauth/authorize", params={**values, "prompt": "consent"})
    assert forced.status_code == 200 and 'name="decision"' in forced.text


async def test_consent_denial_never_issues_a_code_and_cannot_be_replayed(native_http: Harness) -> None:
    await enroll(native_http)
    page = await native_http.client.get("/oauth/authorize", params={**parameters(), "prompt": "consent"})
    denied = await native_http.client.post("/oauth/login", data={**fields(page), "decision": "deny"})
    query = parse_qs(urlsplit(denied.headers["location"]).query)
    assert query == {"error": ["access_denied"], "state": ["http-state"], "iss": [ISSUER]}
    replay = await native_http.client.post("/oauth/login", data={**fields(page), "decision": "allow"})
    assert replay.status_code == 400 and "location" not in replay.headers


async def test_fresh_authentication_preserves_new_sso_cookie_across_consent_page(native_http: Harness) -> None:
    async with native_http.sessions.begin() as session:
        client = await session.get(NativeClientTable, CLIENT)
        assert client is not None
        client.require_consent = True
    form = await begin(native_http)
    page = await password(native_http, form)
    secret = re.findall(r'<code id="enrollment-secret">([^<]+)</code>', page.text)[0]
    enrolled = await native_http.client.post(
        "/oauth/login", data={**form, "stage": "otp", "code": pyotp.TOTP(secret).now()}
    )
    assert 'id="recovery-codes"' in enrolled.text
    consent = await native_http.client.post("/oauth/login", data={**form, "stage": "continue"})
    assert consent.status_code == 200 and 'name="decision"' in consent.text
    assert native_http.client.cookies.get("stage_sso")
    completed = await native_http.client.post("/oauth/login", data={**fields(consent), "decision": "allow"})
    assert completed.status_code == 303
    silent = await native_http.client.get("/oauth/authorize", params={**parameters(), "prompt": "none"})
    assert "code" in parse_qs(urlsplit(silent.headers["location"]).query)


async def test_password_change_invalidates_browser_sso_as_well_as_tokens(native_http: Harness) -> None:
    tokens, _secret, _codes = await enroll(native_http)
    changed = await native_http.client.post(
        "/api/v1/account/password",
        headers={"Authorization": "Bearer " + tokens["access_token"]},
        json={"old_password": PASSWORD, "new_password": "replacement-browser-password"},
    )
    assert changed.status_code == 204
    silent = await native_http.client.get("/oauth/authorize", params={**parameters(), "prompt": "none"})
    assert parse_qs(urlsplit(silent.headers["location"]).query)["error"] == ["login_required"]


async def test_userinfo_post_accepts_only_header_token_and_does_not_serialize_absent_profile(
    native_http: Harness,
) -> None:
    tokens, _secret, _codes = await enroll(native_http)
    response = await native_http.client.post(
        "/oauth/userinfo", headers={"Authorization": "Bearer " + tokens["access_token"]}
    )
    assert response.status_code == 200 and response.json() == {"sub": "account:http"}
    rejected = await native_http.client.post("/oauth/userinfo", data={"access_token": tokens["access_token"]})
    assert rejected.status_code == 400
    duplicate = await native_http.client.get(
        "/oauth/userinfo",
        headers=[("Authorization", "Bearer " + tokens["access_token"]), ("Authorization", "Bearer other")],
    )
    assert duplicate.status_code == 401


async def test_confidential_token_exchange_requires_basic_even_with_public_pkce(native_http: Harness) -> None:
    await enroll(native_http)
    secret = "synthetic-client-secret-with-at-least-thirty-two-bytes"
    async with native_http.sessions.begin() as session:
        client = await session.get(NativeClientTable, CLIENT)
        assert client is not None
        client.token_endpoint_auth_method = "client_secret_basic"
        client.client_secret_hash = digest_secret(secret)
    redirect = await native_http.client.get("/oauth/authorize", params={**parameters(), "prompt": "none"})
    code = parse_qs(urlsplit(redirect.headers["location"]).query)["code"][0]
    body = {"grant_type": "authorization_code", "redirect_uri": CALLBACK, "code": code, "code_verifier": VERIFIER}
    denied = await native_http.client.post("/oauth/token", data={**body, "client_id": CLIENT})
    assert denied.status_code == 401 and denied.headers["www-authenticate"].startswith("Basic ")
    accepted = await native_http.client.post(
        "/oauth/token", data=body, headers={"Authorization": basic(CLIENT, secret).decode()}
    )
    assert accepted.status_code == 200


async def test_logout_get_requires_confirmation_and_cancel_preserves_sso_and_tokens(native_http: Harness) -> None:
    tokens, _secret, _codes = await enroll(native_http)
    page = await native_http.client.get("/oauth/logout", params={"id_token_hint": tokens["id_token"]})
    assert page.status_code == 200 and "退出登录" in page.text
    assert page.headers["referrer-policy"] == "same-origin"
    assert "HttpOnly" in page.headers["set-cookie"] and "SameSite=lax" in page.headers["set-cookie"]
    active = await native_http.client.get(
        "/oauth/userinfo", headers={"Authorization": "Bearer " + tokens["access_token"]}
    )
    assert active.status_code == 200
    canceled = await native_http.client.post("/oauth/logout", data={**fields(page), "decision": "deny"})
    assert canceled.status_code == 200 and "已取消" in canceled.text
    assert native_http.client.cookies.get("stage_sso") and not native_http.client.cookies.get("stage_logout")
    silent = await native_http.client.get("/oauth/authorize", params={**parameters(), "prompt": "none"})
    assert "code" in parse_qs(urlsplit(silent.headers["location"]).query)


async def test_oauth_authorization_server_metadata_is_consistent_and_protected(native_http: Harness) -> None:
    oauth = await native_http.client.get(
        "/.well-known/oauth-authorization-server", headers={"Host": "attacker.invalid"}
    )
    oidc = await native_http.client.get("/.well-known/openid-configuration")
    assert oauth.status_code == 200
    assert oauth.json()["issuer"] == ISSUER and oauth.json().items() <= oidc.json().items()
    assert {
        "authorization_endpoint",
        "token_endpoint",
        "jwks_uri",
        "code_challenge_methods_supported",
    } <= oauth.json().keys()
    assert not {"id_token_signing_alg_values_supported", "userinfo_endpoint", "claims_supported"} & oauth.json().keys()
    assert oauth.headers["cache-control"] == "no-store" and oauth.headers["referrer-policy"] == "no-referrer"
    assert (
        oauth.headers["content-security-policy"]
        == "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    )


async def test_rp_logout_post_initiation_does_not_revoke_and_rejects_invalid_hint(native_http: Harness) -> None:
    tokens, _secret, _codes = await enroll(native_http)
    page = await native_http.client.post("/oauth/logout", data={"id_token_hint": tokens["id_token"]})
    assert page.status_code == 200 and "确认退出登录" in page.text
    assert set(fields(page)) == {"request_ref", "csrf_token"}
    active = await native_http.client.get(
        "/oauth/userinfo", headers={"Authorization": "Bearer " + tokens["access_token"]}
    )
    assert active.status_code == 200
    invalid = await native_http.client.post("/oauth/logout", data={"id_token_hint": "invalid-signed-token"})
    assert invalid.status_code == 400 and "location" not in invalid.headers
    assert "invalid-signed-token" not in invalid.text


async def test_cross_site_post_logout_reenters_issuer_before_binding_samesite_cookie(native_http: Harness) -> None:
    tokens, _secret, _codes = await enroll(native_http)
    sso = native_http.client.cookies.get("stage_sso")
    assert sso is not None
    native_http.client.cookies.delete("stage_sso")
    relay = await native_http.client.post(
        "/oauth/logout",
        data={"id_token_hint": tokens["id_token"], "state": "opaque-logout-state"},
        headers={"Origin": "https://rp.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert relay.status_code == 200 and 'id="logout-init"' in relay.text
    assert "set-cookie" not in relay.headers
    assert relay.headers["referrer-policy"] == "same-origin"
    assert "script-src 'nonce-" in relay.headers["content-security-policy"]
    assert "form-action 'self'" in relay.headers["content-security-policy"] and "rp.example" not in relay.text
    assert "<noscript>" in relay.text
    async with native_http.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NativeLogoutRequestTable)) == 0
    native_http.client.cookies.set("stage_sso", sso, domain="127.0.0.1", path="/")
    page = await native_http.client.post(
        "/oauth/logout",
        data=fields(relay),
        headers={"Origin": ISSUER, "Sec-Fetch-Site": "same-origin"},
    )
    assert page.status_code == 200 and "确认退出登录" in page.text
    active = await native_http.client.get(
        "/oauth/userinfo", headers={"Authorization": "Bearer " + tokens["access_token"]}
    )
    assert active.status_code == 200
    completed = await native_http.client.post(
        "/oauth/logout",
        data={**fields(page), "decision": "allow"},
        headers={"Origin": ISSUER},
    )
    assert completed.status_code == 200 and "已退出登录" in completed.text
    denied = await native_http.client.get(
        "/oauth/userinfo", headers={"Authorization": "Bearer " + tokens["access_token"]}
    )
    assert denied.status_code == 401


@pytest.mark.parametrize(
    "mixed",
    [
        {"id_token_hint": "a", "csrf_token": "b"},
        {"client_id": CLIENT, "decision": "allow"},
        {"request_ref": "a", "csrf_token": "b", "decision": "allow", "id_token_hint": "c"},
    ],
)
async def test_logout_post_never_mixes_protocol_initiation_and_confirmation(
    native_http: Harness, mixed: dict[str, str]
) -> None:
    response = await native_http.client.post("/oauth/logout", data=mixed)
    assert response.status_code == 400 and "set-cookie" not in response.headers
    async with native_http.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NativeLogoutRequestTable)) == 0


async def test_logout_anonymous_budget_is_persistent_and_independent_from_authorization(native_http: Harness) -> None:
    async with native_http.sessions.begin() as session:
        session.add(
            NativeLoginThrottleTable(
                bucket_hash=digest_secret("native-login-peer:logout:127.0.0.1"),
                failures=30,
                window_start=datetime.now(UTC),
            )
        )
    rejected = await native_http.client.get("/oauth/logout", headers={"X-Forwarded-For": "10.0.0.1"})
    assert rejected.status_code == 429 and "retry-after" in rejected.headers
    async with native_http.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NativeLogoutRequestTable)) == 0
    allowed = await native_http.client.get("/oauth/authorize", params=parameters())
    assert allowed.status_code == 200


async def test_confirmed_logout_revokes_browser_sessions_and_uses_only_registered_return(native_http: Harness) -> None:
    tokens, _secret, _codes = await enroll(native_http)
    callback = "http://127.0.0.1:49152/logout-return?registered=1"
    async with native_http.sessions.begin() as session:
        client = await session.get(NativeClientTable, CLIENT)
        assert client is not None
        client.post_logout_redirect_uris = [callback]
    page = await native_http.client.get(
        "/oauth/logout",
        params={
            "id_token_hint": tokens["id_token"],
            "post_logout_redirect_uri": callback,
            "state": "logout-state",
        },
    )
    assert page.status_code == 200 and "http://127.0.0.1:49152" in page.headers["content-security-policy"]
    result = await native_http.client.post("/oauth/logout", data={**fields(page), "decision": "allow"})
    assert result.status_code == 303
    assert parse_qs(urlsplit(result.headers["location"]).query) == {"registered": ["1"], "state": ["logout-state"]}
    assert result.headers["referrer-policy"] == "no-referrer"
    assert not native_http.client.cookies.get("stage_sso") and not native_http.client.cookies.get("stage_logout")
    denied = await native_http.client.get(
        "/oauth/userinfo", headers={"Authorization": "Bearer " + tokens["access_token"]}
    )
    assert denied.status_code == 401
    silent = await native_http.client.get("/oauth/authorize", params={**parameters(), "prompt": "none"})
    assert parse_qs(urlsplit(silent.headers["location"]).query)["error"] == ["login_required"]


@pytest.mark.parametrize("kind", ["csrf", "cookie", "origin", "cross_site"])
async def test_logout_confirmation_rejects_csrf_without_revoking_session(native_http: Harness, kind: str) -> None:
    tokens, _secret, _codes = await enroll(native_http)
    page = await native_http.client.get("/oauth/logout", params={"id_token_hint": tokens["id_token"]})
    form = fields(page)
    headers = {}
    if kind == "csrf":
        form["csrf_token"] = "not-the-csrf-token"
    elif kind == "cookie":
        native_http.client.cookies.delete("stage_logout")
    elif kind == "origin":
        headers["Origin"] = "https://attacker.invalid"
    else:
        headers["Sec-Fetch-Site"] = "cross-site"
    denied = await native_http.client.post("/oauth/logout", data={**form, "decision": "allow"}, headers=headers)
    assert denied.status_code in {400, 403} and "location" not in denied.headers
    active = await native_http.client.get(
        "/oauth/userinfo", headers={"Authorization": "Bearer " + tokens["access_token"]}
    )
    assert active.status_code == 200


async def test_logout_hint_cannot_authorize_an_arbitrary_redirect(native_http: Harness) -> None:
    tokens, _secret, _codes = await enroll(native_http)
    response = await native_http.client.get(
        "/oauth/logout",
        params={
            "id_token_hint": tokens["id_token"],
            "post_logout_redirect_uri": "https://attacker.invalid/callback",
        },
    )
    assert response.status_code == 400 and "location" not in response.headers
    assert (
        "attacker.invalid" not in response.text
        and "attacker.invalid" not in response.headers["content-security-policy"]
    )


async def test_current_user_session_http_pages_and_revocation_do_not_expose_secrets(native_http: Harness) -> None:
    first, _secret, _codes = await enroll(native_http)
    redirected = await native_http.client.get("/oauth/authorize", params={**parameters(), "prompt": "none"})
    second = await exchange(native_http, redirected)
    headers = {"Authorization": "Bearer " + first["access_token"]}
    page = await native_http.client.get("/api/v1/account/sessions", params={"limit": 1}, headers=headers)
    assert page.status_code == 200
    initial = page.json()
    assert len(initial["items"]) == 1 and initial["next_after"]
    following = await native_http.client.get(
        "/api/v1/account/sessions", params={"after": initial["next_after"], "limit": 1}, headers=headers
    )
    assert following.status_code == 200 and following.json()["next_after"] is None
    items = [*initial["items"], *following.json()["items"]]
    assert all(
        set(item)
        == {
            "session_ref",
            "client_id",
            "client_display_name",
            "auth_time",
            "expires_at",
            "revoked_at",
            "current",
            "active",
        }
        for item in items
    )
    target = next(item["session_ref"] for item in items if not item["current"])
    revoked = await native_http.client.delete("/api/v1/account/sessions/" + target, headers=headers)
    assert revoked.status_code == 204
    denied = await native_http.client.get(
        "/oauth/userinfo", headers={"Authorization": "Bearer " + second["access_token"]}
    )
    assert denied.status_code == 401
    cleared = await native_http.client.post("/api/v1/account/sessions/revoke-all", headers=headers)
    assert cleared.status_code == 200 and cleared.json()["credential_version"] > 1
    invalidated = await native_http.client.get("/api/v1/account/sessions", headers=headers)
    assert invalidated.status_code == 401


async def test_current_user_consent_http_listing_and_revocation(native_http: Harness) -> None:
    tokens, _secret, _codes = await enroll(native_http)
    page = await native_http.client.get("/oauth/authorize", params={**parameters(), "prompt": "consent"})
    approved = await native_http.client.post("/oauth/login", data={**fields(page), "decision": "allow"})
    assert approved.status_code == 303
    headers = {"Authorization": "Bearer " + tokens["access_token"]}
    listed = await native_http.client.get("/api/v1/account/consents", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["next_after"] is None
    consent = listed.json()["items"][0]
    assert set(consent) == {"client_id", "client_display_name", "client_version", "scopes", "granted_at", "current"}
    assert consent["client_id"] == CLIENT and consent["scopes"] == ["openid"] and consent["current"] is True
    revoked = await native_http.client.delete("/api/v1/account/consents/" + CLIENT, headers=headers)
    assert revoked.status_code == 204
    denied = await native_http.client.get("/oauth/userinfo", headers=headers)
    assert denied.status_code == 401


@pytest.mark.parametrize("query", ["limit=0", "limit=51", "limit=-1", "limit=1&limit=2", "account_ref=other"])
async def test_account_session_query_is_bounded_and_never_accepts_an_actor_selector(
    native_http: Harness, query: str
) -> None:
    response = await native_http.client.get("/api/v1/account/sessions?" + query)
    assert response.status_code == 400 and response.json()["error"]["code"] == "REQUEST_INVALID"
