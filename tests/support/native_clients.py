"""Shared synthetic builders for native clients."""

from typing import Any

from app.native_auth.client_administration import (
    ConfigureClientPlan,
)


def configuration(**changes: Any) -> ConfigureClientPlan:
    return ConfigureClientPlan.model_validate(
        {
            "environment": "test",
            "organization_ref": "tenant-a",
            "client_id": "knowledge-web",
            "expected_version": 0,
            "display_name": "Knowledge application",
            "redirect_uris": ["https://knowledge.example/callback"],
            **changes,
        }
    )
