"""Exercise isolated Keycloak password/TOTP browser login with genuine signed claims.

No browser callback listener, password grant, Stage grant, token output, or secret
output is used. Helpers are reusable by explicitly enabled live Stage tests.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import stat
import struct
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
import jwt

from app.identity.authentication import (
    AuthenticationRejected,
    KeycloakAuthenticator,
    KeycloakProfile,
    VerifiedCredential,
)
from scripts.identity_idp_smoke import assert_local_url


class FixtureForm(HTMLParser):
    def __init__(self, form_id: str) -> None:
        super().__init__()
        self.form_id = form_id
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._inside = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form" and values.get("id") == self.form_id:
            self._inside = True
            self.action = values.get("action")
        if tag == "input" and self._inside and values.get("type") == "hidden" and values.get("name"):
            self.fields[str(values["name"])] = str(values.get("value") or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._inside = False


@dataclass(frozen=True, repr=False)
class LoginAttempt:
    response: httpx.Response
    client_id: str
    verifier: str
    state: str
    nonce: str


def otp_code(secret: str, timestamp: float | None = None) -> str:
    # The fixture imports Keycloak's UTF-8 secret representation, not a Base32 URI.
    counter = int(time.time() if timestamp is None else timestamp) // 30
    digest = hmac.digest(secret.encode(), struct.pack(">Q", counter), "sha1")
    offset = digest[-1] & 15
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def _form(response: httpx.Response, config: dict[str, Any], form_id: str) -> FixtureForm:
    if response.status_code != 200:
        raise RuntimeError("expected an authentication challenge")
    form = FixtureForm(form_id)
    form.feed(response.text)
    if form.action is None:
        raise RuntimeError("expected authentication form missing")
    form.action = urljoin(config["issuer"], form.action)
    if urlsplit(form.action)[:2] != urlsplit(config["issuer"])[:2]:
        raise RuntimeError("authentication form escaped the configured IdP")
    return form


async def start_login(
    client: httpx.AsyncClient,
    config: dict[str, Any],
    *,
    strong: bool = True,
    requested_acr: str | None = None,
) -> LoginAttempt:
    assert_local_url(config["issuer"])
    assert_local_url(config["redirect_uri"])
    verifier, state, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    client_id = str(config["human_client"] if strong else config["weak_client"])
    params = {
        "client_id": client_id,
        "redirect_uri": str(config["redirect_uri"]),
        "response_type": "code",
        "scope": "openid",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
        "max_age": "0",
    }
    if requested_acr is not None:
        params["acr_values"] = requested_acr
    response = await client.get(config["issuer"] + "/protocol/openid-connect/auth?" + urlencode(params))
    # Same loopback-only HTTPX Secure-cookie adjustment as the original IdP fixture.
    for cookie in client.cookies.jar:
        if cookie.domain == "127.0.0.1":
            cookie.secure = False
    return LoginAttempt(response, client_id, verifier, state, nonce)


async def submit_password(client: httpx.AsyncClient, config: dict[str, Any], attempt: LoginAttempt) -> LoginAttempt:
    form = _form(attempt.response, config, "kc-form-login")
    assert form.action is not None
    response = await client.post(
        form.action,
        data={**form.fields, "username": config["human_username"], "password": config["human_password"]},
    )
    return LoginAttempt(response, attempt.client_id, attempt.verifier, attempt.state, attempt.nonce)


async def submit_otp(
    client: httpx.AsyncClient, config: dict[str, Any], attempt: LoginAttempt, code: str | None
) -> LoginAttempt:
    form = _form(attempt.response, config, "kc-otp-login-form")
    assert form.action is not None
    fields = dict(form.fields)
    if code is not None:
        fields["otp"] = code
    response = await client.post(form.action, data=fields)
    return LoginAttempt(response, attempt.client_id, attempt.verifier, attempt.state, attempt.nonce)


async def complete_otp(client: httpx.AsyncClient, config: dict[str, Any], attempt: LoginAttempt) -> LoginAttempt:
    attempt = await submit_otp(client, config, attempt, otp_code(config["otp_secret"]))
    if attempt.response.status_code == 200:
        # Another fixture login may have consumed this interval. Keep OTP reuse
        # disabled and retry once, under the caller's overall deadline.
        _form(attempt.response, config, "kc-otp-login-form")
        await asyncio.sleep(31 - time.time() % 30)
        attempt = await submit_otp(client, config, attempt, otp_code(config["otp_secret"]))
    return attempt


def authorization_result(config: dict[str, Any], attempt: LoginAttempt) -> str:
    response = attempt.response
    if response.status_code != 302:
        raise RuntimeError("authentication did not produce an authorization code")
    actual = urlsplit(response.headers.get("location", ""))
    if actual[:3] != urlsplit(config["redirect_uri"])[:3]:
        raise RuntimeError("unexpected authorization callback")
    query = parse_qs(actual.query)
    if query.get("state") != [attempt.state] or len(query.get("code", [])) != 1 or "error" in query:
        raise RuntimeError("authorization callback state or code mismatch")
    return query["code"][0]


async def exchange_code(client: httpx.AsyncClient, config: dict[str, Any], attempt: LoginAttempt) -> dict[str, Any]:
    code = authorization_result(config, attempt)
    response = await client.post(
        config["issuer"] + "/protocol/openid-connect/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": attempt.client_id,
            "code_verifier": attempt.verifier,
            "redirect_uri": config["redirect_uri"],
        },
    )
    if response.status_code != 200:
        raise RuntimeError("authorization code exchange failed")
    tokens: dict[str, Any] = response.json()
    keys = await client.get(config["issuer"] + "/protocol/openid-connect/certs")
    if keys.status_code != 200:
        raise RuntimeError("verification keys unavailable")
    header = jwt.get_unverified_header(tokens["id_token"])
    matching = [key for key in keys.json()["keys"] if key.get("kid") == header.get("kid")]
    if len(matching) != 1:
        raise RuntimeError("ambiguous verification key")
    claims = jwt.decode(
        tokens["id_token"],
        jwt.PyJWK.from_dict(matching[0]).key,
        algorithms=["RS256"],
        audience=attempt.client_id,
        issuer=config["issuer"],
        options={"require": ["exp", "iat", "sub", "nonce"]},
    )
    if claims["nonce"] != attempt.nonce or claims["sub"] != config["human_subject"] or claims.get("typ") != "ID":
        raise RuntimeError("ID token nonce, subject or usage mismatch")
    return tokens


def profile(config: dict[str, Any], *, strong: bool = True) -> KeycloakProfile:
    return KeycloakProfile(
        profile_ref="isolated-mfa-human" if strong else "isolated-weak-human",
        profile_version=1,
        issuer=config["issuer"],
        jwks_url=config["issuer"] + "/protocol/openid-connect/certs",
        audience=config["audience"],
        credential_kind="human_oidc",
        allowed_clients=(config["human_client"] if strong else config["weak_client"],),
        subject_namespace="keycloak-human-v1",
        environment="dev",
        allow_loopback_http=True,
    )


async def login_tokens(client: httpx.AsyncClient, config: dict[str, Any], *, strong: bool = True) -> dict[str, Any]:
    attempt = await submit_password(client, config, await start_login(client, config, strong=strong))
    if strong:
        attempt = await complete_otp(client, config, attempt)
    return await exchange_code(client, config, attempt)


async def _verified(
    client: httpx.AsyncClient, config: dict[str, Any], tokens: dict[str, Any], *, strong: bool
) -> VerifiedCredential:
    return await KeycloakAuthenticator(profile(config, strong=strong), http_client=client).authenticate(
        tokens["access_token"]
    )


async def _check_refresh(
    client: httpx.AsyncClient,
    config: dict[str, Any],
    tokens: dict[str, Any],
    before: VerifiedCredential,
    *,
    strong: bool,
) -> None:
    # Cross an actual clock tick: refreshing in the same second cannot distinguish
    # preserved auth_time from an incorrect reset to the new token's iat.
    assert before.authenticated_at is not None
    await asyncio.sleep(max(0, before.authenticated_at.timestamp() + 1.1 - time.time()))
    response = await client.post(
        config["issuer"] + "/protocol/openid-connect/token",
        data={
            "grant_type": "refresh_token",
            "client_id": config["human_client"] if strong else config["weak_client"],
            "refresh_token": tokens["refresh_token"],
        },
    )
    if response.status_code != 200:
        raise RuntimeError("refresh failed")
    after = await _verified(client, config, response.json(), strong=strong)
    if (
        after.acr != before.acr
        or set(after.amr) != set(before.amr)
        or after.authenticated_at != before.authenticated_at
    ):
        raise RuntimeError("refresh changed authentication strength or login time")
    # Signature and profile have just been verified by _verified above.
    refreshed_claims = jwt.decode(response.json()["access_token"], options={"verify_signature": False})
    if refreshed_claims["iat"] <= before.authenticated_at.timestamp():
        raise RuntimeError("refresh did not cross the original authentication clock tick")


async def run(config: dict[str, Any]) -> dict[str, Any]:
    checks: list[str] = []
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        weak_tokens = await login_tokens(client, config, strong=False)
        weak = await _verified(client, config, weak_tokens, strong=False)
        if weak.acr != "pwd" or set(weak.amr) != {"pwd"} or weak.authenticated_at is None:
            raise RuntimeError("password-only authentication produced incorrect assurance")
        checks.append("password_only_has_no_otp_assurance")
        await _check_refresh(client, config, weak_tokens, weak, strong=False)
        checks.append("weak_refresh_cannot_gain_mfa")
        # Reuse the weak browser session and explicitly ask the MFA client for pwd.
        # The client's minimum ACR must still require its OTP execution.
        attempt = await submit_password(client, config, await start_login(client, config, requested_acr="pwd"))
        _form(attempt.response, config, "kc-otp-login-form")
        checks.append("weak_session_and_requested_acr_cannot_skip_otp")
        attempt = await submit_otp(client, config, attempt, None)
        _form(attempt.response, config, "kc-otp-login-form")
        checks.append("incomplete_second_factor_has_no_code")
        excluded = {otp_code(config["otp_secret"], time.time() + delta) for delta in (-30, 0, 30)}
        wrong_code = next(f"{value:06d}" for value in range(10) if f"{value:06d}" not in excluded)
        attempt = await submit_otp(client, config, attempt, wrong_code)
        _form(attempt.response, config, "kc-otp-login-form")
        checks.append("incorrect_otp_has_no_code")
        attempt = await complete_otp(client, config, attempt)
        strong_tokens = await exchange_code(client, config, attempt)
        strong = await _verified(client, config, strong_tokens, strong=True)
        if strong.acr != "mfa" or set(strong.amr) != {"pwd", "otp"} or strong.authenticated_at is None:
            raise RuntimeError("completed password and OTP did not produce actual MFA assurance")
        checks.extend(["password_totp_pkce_and_nonce", "actual_acr_amr_and_auth_time"])
        await _check_refresh(client, config, strong_tokens, strong, strong=True)
        checks.append("mfa_refresh_preserves_assurance_and_auth_time")
        response = await client.post(
            config["issuer"] + "/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": config["human_client"],
                "username": config["human_username"],
                "password": config["human_password"],
                "totp": otp_code(config["otp_secret"]),
            },
        )
        if response.status_code not in {400, 401}:
            raise RuntimeError("password grant unexpectedly enabled")
        checks.append("password_grant_disabled_even_with_otp")
    return {"issuer": config["issuer"], "checks": checks}


def read_config(path: Path) -> dict[str, Any]:
    if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_mode & 0o077:
        raise ValueError("credentials must be a private regular file")
    result: dict[str, Any] = json.loads(path.read_text())
    assert_local_url(result["issuer"])
    assert_local_url(result["redirect_uri"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(read_config(args.credentials)))
    except (ValueError, RuntimeError, AuthenticationRejected, httpx.HTTPError, jwt.PyJWTError):
        raise SystemExit("MFA fixture checks failed; no tokens or credentials were logged.") from None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
