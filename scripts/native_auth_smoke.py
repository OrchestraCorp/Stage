"""Real loopback native login smoke; private fixture values are never printed.

The fixture contains issuer/client_id/redirect_uri/username/password/profile_ref
and profile_version. --enroll explicitly stores the initial TOTP and recovery
material in that same 0600 file. Later runs consume a fresh OTP and revoke only
the session created by this check. No memberships or roles are created.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import secrets
import stat
import sys
import time
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import jwt  # noqa: E402
import pyotp  # noqa: E402
from authlib.oauth2.rfc7636.challenge import create_s256_code_challenge  # noqa: E402

from scripts.identity_idp_smoke import assert_local_url  # noqa: E402


def hidden(response: httpx.Response) -> dict[str, str]:
    return {
        name: unescape(value)
        for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', response.text)
    }


def save_fixture(path: Path, value: dict[str, Any]) -> None:
    # In-place update preserves the private inode; fixture loss needs operator recovery.
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


async def login(
    client: httpx.AsyncClient, config: dict[str, Any], *, private_path: Path | None = None, enroll: bool = False
) -> dict[str, Any]:
    issuer = config["issuer"]
    verifier, state, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    response = await client.get(
        issuer + "/oauth/authorize",
        params={
            "client_id": config["client_id"],
            "redirect_uri": config["redirect_uri"],
            "response_type": "code",
            "scope": "openid",
            "state": state,
            "nonce": nonce,
            "code_challenge": create_s256_code_challenge(verifier),
            "code_challenge_method": "S256",
        },
    )
    assert response.status_code == 200, "authorize_failed"
    form = hidden(response)
    response = await client.post(
        issuer + "/oauth/login",
        data={**form, "stage": "password", "username": config["username"], "password": config["password"]},
    )
    assert response.status_code == 200, "password_failed"
    pending = re.findall(r'<code id="enrollment-secret">([^<]+)</code>', response.text)
    if pending:
        assert enroll, "explicit_enrollment_required"
        config["otp_secret"] = pending[0]
        config.pop("last_totp_step", None)
        if private_path:
            save_fixture(private_path, config)
    assert config.get("otp_secret"), "totp_fixture_required"
    step = int(time.time()) // 30
    if step <= config.get("last_totp_step", -1):
        await asyncio.sleep(30 - time.time() % 30 + 0.1)
    response = await client.post(
        issuer + "/oauth/login", data={**form, "stage": "otp", "code": pyotp.TOTP(config["otp_secret"]).now()}
    )
    assert response.status_code in {200, 303}, "otp_failed"
    assert response.status_code == 303 or 'name="stage" value="continue"' in response.text, "otp_not_accepted"
    config["last_totp_step"] = int(time.time()) // 30
    codes = re.findall(r"<li><code>([^<]+)</code></li>", response.text)
    if pending:
        assert len(codes) == 10, "recovery_material_missing"
        config["recovery_codes"] = codes
    if private_path:
        save_fixture(private_path, config)
    if response.status_code == 200:
        response = await client.post(issuer + "/oauth/login", data={**form, "stage": "continue"})
    assert response.status_code == 303, "code_callback_failed"
    location = response.headers["location"]
    callback = urlsplit(location)
    expected = urlsplit(config["redirect_uri"])
    assert (callback.scheme, callback.netloc, callback.path) == (expected.scheme, expected.netloc, expected.path)
    query = parse_qs(callback.query)
    assert query["state"] == [state]
    response = await client.post(
        issuer + "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": config["client_id"],
            "redirect_uri": config["redirect_uri"],
            "code": query["code"][0],
            "code_verifier": verifier,
        },
    )
    assert response.status_code == 200, "token_exchange_failed"
    tokens = response.json()
    discovery = (await client.get(issuer + "/.well-known/openid-configuration")).json()
    assert discovery["issuer"] == issuer and discovery["jwks_uri"] == issuer + "/oauth/jwks"
    keys = (await client.get(discovery["jwks_uri"])).json()["keys"]
    kid = jwt.get_unverified_header(tokens["id_token"])["kid"]
    key = jwt.PyJWK.from_dict(next(item for item in keys if item["kid"] == kid))
    identity = jwt.decode(
        tokens["id_token"], key.key, algorithms=["RS256"], issuer=issuer, audience=config["client_id"]
    )
    assert identity["nonce"] == nonce and identity["amr"] == ["pwd", "otp"]
    return tokens


async def check(config: dict[str, Any], *, path: Path, enroll: bool) -> list[str]:
    issuer = config["issuer"]
    assert_local_url(issuer)
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
        tokens = await login(client, config, private_path=path, enroll=enroll)
        auth = {"Authorization": "Bearer " + tokens["access_token"]}
        params = {"profile_ref": config["profile_ref"], "profile_version": config["profile_version"]}
        assert (await client.get(issuer + "/oauth/userinfo", headers=auth)).status_code == 200
        me = await client.get(issuer + "/api/v1/identity/me", headers=auth, params=params)
        assert me.status_code == 200 and me.json()["principal_type"] == "human", "human_identity_failed"
        if config.get("principal_ref"):
            assert me.json()["principal_ref"] == config["principal_ref"], "principal_changed"
        assignments = await client.get(issuer + "/api/v1/identity/assignments", headers=auth, params=params)
        assert assignments.status_code == 200, "assignment_discovery_failed"
        invalid = await client.get(
            issuer + "/api/v1/identity/me", headers={"Authorization": "Bearer " + tokens["id_token"]}, params=params
        )
        assert invalid.status_code == 401, "id_token_accepted_as_access"
        refreshed = await client.post(
            issuer + "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": config["client_id"],
                "refresh_token": tokens["refresh_token"],
            },
        )
        assert refreshed.status_code == 200 and refreshed.json()["refresh_token"] != tokens["refresh_token"]
        assert (
            await client.post(
                issuer + "/oauth/revoke",
                data={"client_id": config["client_id"], "token": refreshed.json()["refresh_token"]},
            )
        ).status_code == 200
        assert (await client.get(issuer + "/api/v1/identity/me", headers=auth, params=params)).status_code == 401, (
            "logout_did_not_revoke_business_identity"
        )
        assert (await client.get(issuer + "/ready")).status_code == 200
    return [
        "password_totp_pkce",
        "discovery_jwks_id_token_nonce",
        "userinfo",
        "same_person_identity",
        "assignment_discovery",
        "reject_id_token_as_access",
        "refresh_rotation",
        "logout_immediate_business_rejection",
        "ready",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--enroll", action="store_true")
    args = parser.parse_args()
    try:
        info = args.credentials.stat()
        assert stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077 and info.st_size <= 32768
        config = json.loads(args.credentials.read_text())
        checks = asyncio.run(check(config, path=args.credentials, enroll=args.enroll))
        print(json.dumps({"passed": len(checks), "checks": checks}))
        return 0
    except Exception as error:
        print(json.dumps({"error": "native_smoke_failed", "type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
