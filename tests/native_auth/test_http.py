"""Actual password/TOTP -> PKCE -> native account HTTP flows and browser boundaries."""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pyotp
import pytest
from sqlalchemy import func, select

from app.models.native_auth import (
    NativeAccountTable,
    NativeBrowserSessionTable,
    NativeClientTable,
    NativeLoginRequestTable,
    NativeLoginThrottleTable,
    NativeSessionTable,
)
from app.native_auth.crypto import digest_secret
from app.native_auth.errors import NativeCommitUnknown
from app.native_auth.keys import KeyRing
from app.native_auth.oauth import NativeOAuth
from tests.support.native_http import (
    CALLBACK,
    CLIENT,
    ISSUER,
    PASSWORD,
    Harness,
    begin,
    enroll,
    exchange,
    fields,
    parameters,
    password,
)
from tests.support.native_http import (
    http_keyring as http_keyring,
)
from tests.support.native_http import (
    native_http as native_http,
)


async def test_browser_enrollment_login_refresh_userinfo_and_logout(native_http: Harness) -> None:
    tokens, secret, codes = await enroll(native_http)
    client = native_http.client
    user = await client.get("/oauth/userinfo", headers={"Authorization": "Bearer " + tokens["access_token"]})
    assert user.json() == {"sub": "account:http"}
    response = await client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "client_id": CLIENT, "refresh_token": tokens["refresh_token"]},
    )
    assert response.status_code == 200
    assert response.json()["refresh_token"] != tokens["refresh_token"]
    revoke = await client.post("/oauth/revoke", data={"client_id": CLIENT, "token": response.json()["refresh_token"]})
    assert revoke.status_code == 200
    assert (
        await client.get("/oauth/userinfo", headers={"Authorization": "Bearer " + tokens["access_token"]})
    ).status_code == 401
    async with native_http.sessions() as session:
        account = await session.get(NativeAccountTable, "account:http")
        assert account is not None and secret not in str(account.totp_secret)
        assert not set(codes) & set(account.recovery_hashes)
        assert account.password_hash is not None and PASSWORD not in account.password_hash
    for response in (user, revoke):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"


async def test_discovery_and_html_use_configured_issuer_not_host_headers(native_http: Harness) -> None:
    headers = {"Host": "attacker.invalid", "X-Forwarded-Host": "attacker.invalid", "X-Forwarded-Proto": "https"}
    response = await native_http.client.get("/.well-known/openid-configuration", headers=headers)
    assert response.json()["issuer"] == ISSUER
    assert response.json()["authorization_endpoint"] == ISSUER + "/oauth/authorize"
    assert response.json()["acr_values_supported"] == ["urn:orchestra:acr:pwd-totp"]
    page = await native_http.client.get("/oauth/authorize", params=parameters(), headers=headers)
    assert f'action="{ISSUER}/oauth/login"' in page.text and "attacker.invalid" not in page.text
    keys = (await native_http.client.get("/oauth/jwks")).json()["keys"]
    assert len(keys) == 1 and not {"d", "p", "q", "private_pem"} & keys[0].keys()


@pytest.mark.parametrize("kind", ["csrf", "cookie", "origin", "duplicate_cookie", "fetch_site", "request_ref"])
async def test_browser_factor_binding_rejects_swapped_or_cross_origin_requests(native_http: Harness, kind: str) -> None:
    form = await begin(native_http)
    headers = {}
    if kind == "csrf":
        form["csrf_token"] = "wrong-csrf"
    elif kind == "request_ref":
        form["request_ref"] = "wrong-request"
    elif kind == "cookie":
        native_http.client.cookies.clear()
    elif kind == "duplicate_cookie":
        cookie = native_http.client.cookies.get("stage_native_login")
        headers["cookie"] = f"stage_native_login={cookie}; stage_native_login=another-secret-value"
    elif kind == "origin":
        headers["origin"] = "https://attacker.invalid"
    else:
        headers["sec-fetch-site"] = "cross-site"
    response = await native_http.client.post(
        "/oauth/login", data={**form, "username": "http-user", "password": PASSWORD}, headers=headers
    )
    assert response.status_code in {400, 403}
    assert PASSWORD not in response.text and "enrollment-secret" not in response.text
    async with native_http.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NativeSessionTable)) == 0
        login = await session.scalar(select(NativeLoginRequestTable))
        assert login is not None and login.status == "password"


async def test_password_failure_does_not_reflect_username_password_or_reveal_account(native_http: Harness) -> None:
    form = await begin(native_http)
    unknown = await native_http.client.post(
        "/oauth/login", data={**form, "username": "<script>unknown</script>", "password": "synthetic-wrong-password"}
    )
    wrong = await password(native_http, form, "synthetic-wrong-password")
    assert unknown.status_code == wrong.status_code == 400
    assert unknown.text == wrong.text
    assert "<script>" not in wrong.text and "synthetic-wrong-password" not in wrong.text
    assert "enrollment-secret" not in wrong.text


async def test_continue_cannot_skip_password_or_second_factor(native_http: Harness) -> None:
    form = await begin(native_http)
    early = await native_http.client.post("/oauth/login", data={**form, "stage": "continue"})
    assert early.status_code == 400 and "location" not in early.headers
    assert (await password(native_http, form)).status_code == 200
    early = await native_http.client.post("/oauth/login", data={**form, "stage": "continue"})
    assert early.status_code == 400 and "location" not in early.headers


async def test_password_change_revokes_access_and_refresh(native_http: Harness) -> None:
    tokens, _secret, _codes = await enroll(native_http)
    response = await native_http.client.post(
        "/api/v1/account/password",
        headers={"Authorization": "Bearer " + tokens["access_token"]},
        json={"old_password": PASSWORD, "new_password": "synthetic-replacement-password"},
    )
    assert response.status_code == 204
    assert (
        await native_http.client.get("/oauth/userinfo", headers={"Authorization": "Bearer " + tokens["access_token"]})
    ).status_code == 401
    assert (
        await native_http.client.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "client_id": CLIENT, "refresh_token": tokens["refresh_token"]},
        )
    ).status_code == 400
    form = await begin(native_http)
    assert (await password(native_http, form)).status_code == 400
    assert (await password(native_http, form, "synthetic-replacement-password")).status_code == 200


async def test_totp_reset_requires_fresh_token_password_and_unused_otp(native_http: Harness) -> None:
    tokens, secret, old_codes = await enroll(native_http)
    next_code = pyotp.TOTP(secret).at(datetime.now(UTC) + timedelta(seconds=30))
    response = await native_http.client.post(
        "/api/v1/account/totp/reset",
        headers={"Authorization": "Bearer " + tokens["access_token"]},
        json={"password": PASSWORD, "otp": next_code},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    reset = response.json()
    assert set(reset) == {"credential_version", "enrollment_code"}
    assert reset["credential_version"] >= 2 and len(reset["enrollment_code"]) >= 20
    assert reset["enrollment_code"] not in old_codes
    assert (
        await native_http.client.get("/oauth/userinfo", headers={"Authorization": "Bearer " + tokens["access_token"]})
    ).status_code == 401
    repeated = await native_http.client.post(
        "/api/v1/account/totp/reset",
        headers={"Authorization": "Bearer " + tokens["access_token"]},
        json={"password": PASSWORD, "otp": next_code},
    )
    assert repeated.status_code == 401 and reset["enrollment_code"] not in repeated.text
    form = await begin(native_http)
    password_only = await password(native_http, form)
    assert password_only.status_code == 200
    assert "enrollment-secret" not in password_only.text
    assert "恢复码或重绑码" in password_only.text
    rejected = await native_http.client.post(
        "/oauth/login", data={**form, "stage": "recovery", "recovery_code": old_codes[0]}
    )
    assert rejected.status_code == 400 and "enrollment-secret" not in rejected.text
    rebound = await native_http.client.post(
        "/oauth/login", data={**form, "stage": "recovery", "recovery_code": reset["enrollment_code"]}
    )
    assert rebound.status_code == 200
    new_secret = re.findall(r'<code id="enrollment-secret">([^<]+)</code>', rebound.text)[0]
    assert new_secret != secret
    completed = await native_http.client.post(
        "/oauth/login", data={**form, "stage": "otp", "code": pyotp.TOTP(new_secret).now()}
    )
    assert completed.status_code == 200 and 'id="recovery-codes"' in completed.text
    redirected = await native_http.client.post("/oauth/login", data={**form, "stage": "continue"})
    reissued = await exchange(native_http, redirected)
    assert (
        await native_http.client.get("/oauth/userinfo", headers={"Authorization": "Bearer " + reissued["access_token"]})
    ).status_code == 200


async def test_recovery_requires_new_totp_before_issuing_any_token(native_http: Harness) -> None:
    tokens, old_secret, codes = await enroll(native_http)
    form = await begin(native_http)
    assert (await password(native_http, form)).status_code == 200
    recovery = await native_http.client.post(
        "/oauth/login", data={**form, "stage": "recovery", "recovery_code": codes[0]}
    )
    assert recovery.status_code == 200 and "location" not in recovery.headers
    secret = re.findall(r'<code id="enrollment-secret">([^<]+)</code>', recovery.text)[0]
    assert secret != old_secret
    assert (
        await native_http.client.get("/oauth/userinfo", headers={"Authorization": "Bearer " + tokens["access_token"]})
    ).status_code == 401
    early = await native_http.client.post("/oauth/login", data={**form, "stage": "continue"})
    assert early.status_code == 400
    enrolled = await native_http.client.post(
        "/oauth/login", data={**form, "stage": "otp", "code": pyotp.TOTP(secret).now()}
    )
    assert enrolled.status_code == 200 and codes[0] not in enrolled.text
    redirect = await native_http.client.post("/oauth/login", data={**form, "stage": "continue"})
    assert (await exchange(native_http, redirect))["access_token"] != tokens["access_token"]


@pytest.mark.parametrize(
    ("body", "content_type", "status"),
    [
        ("grant_type=refresh_token&client_id=a&client_id=b&refresh_token=x", "application/x-www-form-urlencoded", 400),
        ("grant_type=%zz", "application/x-www-form-urlencoded", 400),
        ("grant_type=%FF", "application/x-www-form-urlencoded", 400),
        ("grant_type=x&token=" + "x" * 8192, "application/x-www-form-urlencoded", 413),
        ('{"grant_type":"refresh_token"}', "application/json", 415),
        ("grant_type=password&username=http-user&password=" + PASSWORD, "application/x-www-form-urlencoded", 400),
    ],
)
async def test_token_input_is_bounded_and_unambiguous(
    native_http: Harness, body: str, content_type: str, status: int
) -> None:
    response = await native_http.client.post("/oauth/token", content=body, headers={"Content-Type": content_type})
    assert response.status_code == status and PASSWORD not in response.text
    assert response.headers["cache-control"] == "no-store"


async def test_json_duplicate_keys_and_extra_account_fields_are_rejected(native_http: Harness) -> None:
    for body in (
        '{"old_password":"x","old_password":"y","new_password":"synthetic-password"}',
        json.dumps({"old_password": PASSWORD, "new_password": "synthetic-password", "account_ref": "another-person"}),
    ):
        response = await native_http.client.post(
            "/api/v1/account/password", content=body, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400 and PASSWORD not in response.text


@pytest.mark.parametrize("path", ["/oauth/missing", "/oauth/userinfo", "/api/v1/account/missing"])
async def test_all_private_failures_have_browser_security_headers(native_http: Harness, path: str) -> None:
    response = await native_http.client.get(path)
    assert response.status_code in {401, 404}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


async def test_disabled_native_auth_does_not_expose_discovery_or_accept_login(native_http: Harness) -> None:
    native_http.app.state.settings = native_http.app.state.settings.model_copy(update={"native_auth_enabled": False})
    for method, path in (
        ("GET", "/.well-known/openid-configuration"),
        ("GET", "/oauth/jwks"),
        ("POST", "/oauth/login"),
        ("POST", "/api/v1/account/password"),
    ):
        response = await native_http.client.request(method, path)
        assert response.status_code == 404


async def test_authorization_rejects_arbitrary_redirect_duplicate_query_and_extra_scope(native_http: Harness) -> None:
    for key, value in (("redirect_uri", "https://attacker.invalid/callback"), ("scope", "openid admin")):
        response = await native_http.client.get("/oauth/authorize", params={**parameters(), key: value})
        if key == "redirect_uri":
            assert response.status_code == 400 and "location" not in response.headers
        else:
            assert response.status_code == 303
            query = parse_qs(urlsplit(response.headers["location"]).query)
            assert query["error"] == ["invalid_scope"] and query["iss"] == [ISSUER]
    response = await native_http.client.get(
        "/oauth/authorize", params=[*parameters().items(), ("client_id", "another-client")]
    )
    assert response.status_code == 400


async def test_authorization_rate_limit_is_persistent_and_ignores_forwarded_peer(native_http: Harness) -> None:
    async with native_http.sessions.begin() as session:
        session.add(
            NativeLoginThrottleTable(
                bucket_hash=digest_secret("native-login-peer:authorization:127.0.0.1"),
                failures=30,
                window_start=datetime.now(UTC),
            )
        )
    response = await native_http.client.get(
        "/oauth/authorize", params=parameters(), headers={"X-Forwarded-For": "10.1.2.3"}
    )
    assert response.status_code == 429 and "retry-after" in response.headers
    async with native_http.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NativeLoginRequestTable)) == 0


async def test_recovery_codes_are_not_redisplayed_by_replaying_enrollment(native_http: Harness) -> None:
    form = await begin(native_http)
    response = await password(native_http, form)
    secret = re.findall(r'<code id="enrollment-secret">([^<]+)</code>', response.text)[0]
    payload = {**form, "stage": "otp", "code": pyotp.TOTP(secret).now()}
    response = await native_http.client.post("/oauth/login", data=payload)
    codes = re.findall(r"<li><code>([^<]+)</code></li>", response.text)
    assert len(codes) == 10
    replay = await native_http.client.post("/oauth/login", data=payload)
    assert replay.status_code == 400 and all(code not in replay.text for code in codes)
    assert "location" not in replay.headers
    # The already verified request can still complete, without exposing the codes again.
    assert (await native_http.client.post("/oauth/login", data={**form, "stage": "continue"})).status_code == 303


async def test_account_change_requires_recent_authentication_not_just_recent_access_token(
    native_http: Harness, http_keyring: KeyRing
) -> None:
    tokens, _secret, _codes = await enroll(native_http)
    claims = http_keyring.verify_access(tokens["access_token"], ISSUER, "stage-api")
    old_time = datetime.now(UTC) - timedelta(minutes=6)
    async with native_http.sessions.begin() as session:
        login = await session.get(NativeSessionTable, claims["sid"])
        assert login is not None
        login.auth_time = old_time
        if login.browser_session_hash is not None:
            browser = await session.get(NativeBrowserSessionTable, login.browser_session_hash)
            assert browser is not None
            browser.auth_time = old_time
    claims["auth_time"] = int(old_time.timestamp())
    old_auth = http_keyring.sign(claims)
    response = await native_http.client.post(
        "/api/v1/account/password",
        headers={"Authorization": "Bearer " + old_auth},
        json={"old_password": PASSWORD, "new_password": "synthetic-replacement-password"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "FRESH_AUTHENTICATION_REQUIRED"
    assert response.headers["www-authenticate"] == 'Bearer error="insufficient_user_authentication"'
    assert (
        await native_http.client.get("/oauth/userinfo", headers={"Authorization": "Bearer " + old_auth})
    ).status_code == 200


async def test_id_token_cannot_be_used_as_account_access_token(native_http: Harness) -> None:
    tokens, _secret, _codes = await enroll(native_http)
    response = await native_http.client.post(
        "/api/v1/account/password",
        headers={"Authorization": "Bearer " + tokens["id_token"]},
        json={"old_password": PASSWORD, "new_password": "synthetic-replacement-password"},
    )
    assert response.status_code == 401


async def test_unknown_commit_is_explicit_and_never_echoes_secret(
    native_http: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def uncertain(self: NativeOAuth, *, client_id: str, refresh_token: str, **kwargs: Any) -> dict[str, Any]:
        raise NativeCommitUnknown()

    monkeypatch.setattr(NativeOAuth, "exchange_refresh", uncertain)
    response = await native_http.client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "client_id": CLIENT, "refresh_token": "synthetic-secret-refresh"},
    )
    assert response.status_code == 503 and response.json() == {"error": "commit_outcome_unknown"}
    assert "synthetic-secret-refresh" not in response.text


async def test_login_html_preserves_same_origin_post_proof_without_callback_referrer(native_http: Harness) -> None:
    page = await native_http.client.get("/oauth/authorize", params=parameters())
    assert page.status_code == 200 and page.headers["referrer-policy"] == "same-origin"
    expected_policy = (
        "default-src 'none'; form-action 'self' http://127.0.0.1:49152; frame-ancestors 'none'; base-uri 'none'"
    )
    assert page.headers["content-security-policy"] == expected_policy
    form = fields(page)
    rejected = await password(native_http, form, "synthetic-wrong-password")
    assert rejected.status_code == 400 and rejected.headers["referrer-policy"] == "same-origin"
    assert rejected.headers["content-security-policy"] == expected_policy
    enrolled = await native_http.client.post(
        "/oauth/login",
        data={**form, "username": "http-user", "password": PASSWORD},
        headers={"Origin": ISSUER},
    )
    assert enrolled.status_code == 200 and enrolled.headers["referrer-policy"] == "same-origin"
    assert enrolled.headers["content-security-policy"] == expected_policy
    secret = re.findall(r'<code id="enrollment-secret">([^<]+)</code>', enrolled.text)[0]
    codes = await native_http.client.post(
        "/oauth/login", data={**form, "stage": "otp", "code": pyotp.TOTP(secret).now()}, headers={"Origin": ISSUER}
    )
    assert codes.status_code == 200 and codes.headers["referrer-policy"] == "same-origin"
    assert codes.headers["content-security-policy"] == expected_policy
    redirect = await native_http.client.post(
        "/oauth/login", data={**form, "stage": "continue"}, headers={"Origin": ISSUER}
    )
    assert redirect.status_code == 303 and redirect.headers["referrer-policy"] == "no-referrer"
    assert "49152" not in redirect.headers["content-security-policy"]
    assert redirect.headers["location"].startswith(CALLBACK + "?")
    discovery = await native_http.client.get("/.well-known/openid-configuration")
    assert discovery.headers["referrer-policy"] == "no-referrer"
    invalid = await native_http.client.post("/oauth/login", data={"stage": "invalid"})
    assert invalid.status_code == 400 and invalid.headers["referrer-policy"] == "no-referrer"
    assert "49152" not in invalid.headers["content-security-policy"]


async def test_login_csp_uses_persisted_callback_origin_and_never_post_fields_or_callback_query(
    native_http: Harness,
) -> None:
    registered = CALLBACK + "?label=;script-src%20https://attacker.invalid"
    async with native_http.sessions.begin() as session:
        client = await session.get(NativeClientTable, CLIENT)
        assert client is not None
        client.redirect_uris = [registered]
    page = await native_http.client.get("/oauth/authorize", params={**parameters(), "redirect_uri": registered})
    assert page.status_code == 200
    assert "http://127.0.0.1:49152" in page.headers["content-security-policy"]
    assert "attacker.invalid" not in page.headers["content-security-policy"]
    assert "script-src" not in page.headers["content-security-policy"]
    assert "/callback" not in page.headers["content-security-policy"]
    form = fields(page)
    attack = await native_http.client.post(
        "/oauth/login",
        data={
            **form,
            "username": "http-user",
            "password": PASSWORD,
            "redirect_uri": "https://attacker.invalid/callback",
        },
    )
    assert attack.status_code == 400
    assert "attacker.invalid" not in attack.headers["content-security-policy"]
    assert "49152" not in attack.headers["content-security-policy"]
    valid = await password(native_http, form)
    assert valid.status_code == 200
    assert valid.headers["content-security-policy"] == page.headers["content-security-policy"]
    rejected = await native_http.client.get(
        "/oauth/authorize", params={**parameters(), "redirect_uri": "https://attacker.invalid/callback"}
    )
    assert rejected.status_code == 400
    assert "attacker.invalid" not in rejected.headers["content-security-policy"]
