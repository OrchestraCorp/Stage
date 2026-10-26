"""Shared synthetic builders for native administration."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.native_auth.administration import InstallPlan, install
from tests.support.identity_administration import OPERATOR

DIGEST_KEY = b"private-deployment-key-for-test-only-00000000"


PASSWORD = "Synthetic-password-only-2026!"


def plan_values() -> dict[str, Any]:
    return {
        "environment": "test",
        "organization_ref": "tenant-a",
        "issuer": "http://127.0.0.1:8080",
        "client_id": "maestro-native",
        "redirect_uris": ["http://127.0.0.1:49152/callback"],
        "account_ref": "account:native-human",
        "principal_ref": "human-a",
        "membership_ref": "membership-human",
        "external_identity_ref": "external:native-human",
        "username": "native-human",
        "password": PASSWORD,
    }


async def install_native(sessions: async_sessionmaker[AsyncSession], **changes: Any) -> Any:
    return await install(
        sessions,
        InstallPlan.model_validate({**plan_values(), **changes}),
        actor_ref=OPERATOR,
        idempotency_key="native-install",
        credential_digest_key=DIGEST_KEY,
    )
