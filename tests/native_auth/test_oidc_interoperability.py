"""Interoperate over the real ASGI endpoints using Authlib as an independent RP."""

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pyotp
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.jose import JsonWebToken
from authlib.oidc.core import CodeIDToken
from sqlalchemy import select

from app.models.native_auth import NativeClientTable
from app.native_auth.crypto import digest_secret
from tests.support.native_http import (  # noqa: F401 -- imported pytest fixture
    CALLBACK,
    CLIENT,
    ISSUER,
    PASSWORD,
    VERIFIER,
    Harness,
    fields,
    http_keyring,
    native_http,
)


@pytest.mark.parametrize("confidential", [False, True])
async def test_authlib_code_pkce_oidc_userinfo_refresh_and_revoke(
    native_http: Harness,  # noqa: F811 -- imported pytest fixture
    confidential: bool,
) -> None:
    secret = "synthetic-independent-rp-confidential-secret"
    async with native_http.sessions.begin() as session:
        row = await session.scalar(select(NativeClientTable).where(NativeClientTable.client_id == CLIENT))
        assert row is not None
        row.allowed_scopes = ["openid", "profile"]
        if confidential:
            row.token_endpoint_auth_method = "client_secret_basic"
            row.client_secret_hash = digest_secret(secret)
    discovery = (await native_http.client.get("/.well-known/openid-configuration")).json()
    assert discovery["authorization_response_iss_parameter_supported"] is True
    method = "client_secret_basic" if confidential else "none"
    async with AsyncOAuth2Client(
        CLIENT,
        client_secret=secret if confidential else None,
        token_endpoint_auth_method=method,
        scope="openid profile",
        redirect_uri=CALLBACK,
        code_challenge_method="S256",
        transport=httpx.ASGITransport(app=native_http.app),
    ) as relying_party:
        nonce = "independent-client-generated-nonce"
        url, state = relying_party.create_authorization_url(
            discovery["authorization_endpoint"], code_verifier=VERIFIER, nonce=nonce
        )
        login = await native_http.client.get(url)
        assert login.status_code == 200
        form = fields(login)
        enrollment = await native_http.client.post(
            "/oauth/login", data={**form, "stage": "password", "username": "http-user", "password": PASSWORD}
        )
        assert enrollment.status_code == 200
        factor = re.findall(r'<code id="enrollment-secret">([^<]+)</code>', enrollment.text)[0]
        recovered = await native_http.client.post(
            "/oauth/login", data={**form, "stage": "otp", "code": pyotp.TOTP(factor).now()}
        )
        assert recovered.status_code == 200
        assert len(re.findall(r"<li><code>([^<]+)</code></li>", recovered.text)) == 10
        redirect = await native_http.client.post("/oauth/login", data={**form, "stage": "continue"})
        assert redirect.status_code == 303
        callback = parse_qs(urlsplit(redirect.headers["location"]).query)
        assert callback["iss"] == [discovery["issuer"]]
        issued: dict[str, Any] = await relying_party.fetch_token(
            discovery["token_endpoint"],
            authorization_response=redirect.headers["location"],
            state=state,
            code_verifier=VERIFIER,
        )
        keyset = (await native_http.client.get(discovery["jwks_uri"])).json()
        decoder = JsonWebToken(["RS256"])
        claims = decoder.decode(
            issued["id_token"],
            keyset,
            claims_cls=CodeIDToken,
            claims_options={"iss": {"essential": True, "value": ISSUER}, "aud": {"essential": True, "value": CLIENT}},
            claims_params={"nonce": nonce, "client_id": CLIENT, "access_token": issued["access_token"]},
        )
        claims.validate()
        assert claims["sub"] == "account:http"
        userinfo = await relying_party.get(discovery["userinfo_endpoint"])
        assert userinfo.status_code == 200
        assert userinfo.json() == {"sub": claims["sub"], "preferred_username": "http-user"}
        first_refresh = issued["refresh_token"]
        rotated = await relying_party.refresh_token(discovery["token_endpoint"], refresh_token=first_refresh)
        assert rotated["refresh_token"] != first_refresh
        refreshed_claims = decoder.decode(
            rotated["id_token"],
            keyset,
            claims_cls=CodeIDToken,
            claims_options={"iss": {"value": ISSUER}, "aud": {"value": CLIENT}},
            claims_params={"client_id": CLIENT, "access_token": rotated["access_token"]},
        )
        refreshed_claims.validate()
        assert refreshed_claims["auth_time"] == claims["auth_time"]
        assert "nonce" not in refreshed_claims
        revoke_data = {"token": rotated["refresh_token"], "client_id": CLIENT}
        revoke_options: dict[str, Any] = {"auth": httpx.BasicAuth(CLIENT, secret)} if confidential else {}
        revoke = await native_http.client.post(discovery["revocation_endpoint"], data=revoke_data, **revoke_options)
        assert revoke.status_code == 200
        assert (await relying_party.get(discovery["userinfo_endpoint"])).status_code == 401
