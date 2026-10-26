"""Shared synthetic builders for work http."""

import httpx


def assert_private(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    assert "synthetic-token" not in response.text
    assert "synthetic-private" not in response.text
    if response.status_code >= 400:
        assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
