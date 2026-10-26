"""Configured peer budgets are independent of the per-account guessing lockout."""

from dataclasses import replace

import pytest

from app.native_auth.config import NativeAuthConfig
from app.providers.native_auth import NATIVE_AUTH
from tests.support import native_http as http_support

http_keyring = http_support.http_keyring
native_http = http_support.native_http


async def test_provider_applies_configured_authorization_budget_without_trusting_forwarded_headers(
    native_http: http_support.Harness,
) -> None:
    host = native_http.app.state.runtime.host
    async with host.call(NATIVE_AUTH) as call:
        facade = call.require(NATIVE_AUTH)
        facade.config = replace(
            facade.config, authorization_attempt_limit=2, peer_attempt_limit=10, rate_window_seconds=60
        )
    for _ in range(2):
        page = await native_http.client.get("/oauth/authorize", params=http_support.parameters())
        assert page.status_code == 200
    denied = await native_http.client.get(
        "/oauth/authorize", params=http_support.parameters(), headers={"X-Forwarded-For": "198.51.100.17"}
    )
    assert denied.status_code == 429 and denied.headers["cache-control"] == "no-store"
    # Failed authorization quota must not consume the separate factor quota.
    response = await native_http.client.post(
        "/oauth/login",
        data={
            **http_support.fields(page),
            "stage": "password",
            "username": "http-user",
            "password": http_support.PASSWORD,
        },
    )
    assert response.status_code == 200 and 'id="enrollment-secret"' in response.text


@pytest.mark.parametrize("value", [0, True, 10001])
def test_invalid_budget_configuration_is_rejected(value: int) -> None:
    with pytest.raises(ValueError):
        NativeAuthConfig(issuer="http://127.0.0.1:8080", organization_ref="org:test", peer_attempt_limit=value)
