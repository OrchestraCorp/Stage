"""Shared synthetic builders for identity assurance."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.identity.assurance_administration import ConfigureHumanAuthenticationRequest, configure_human_authentication
from tests.support.authorization import ORG


def assurance_request(
    *, expected_version: int = 0, profile_version: int = 1, max_auth_age_seconds: int = 600
) -> ConfigureHumanAuthenticationRequest:
    return ConfigureHumanAuthenticationRequest.model_validate(
        {
            "environment": "test",
            "organization_ref": ORG,
            "expected_version": expected_version,
            "policy": {
                "mode": "required",
                "rules": [
                    {
                        "profile_ref": "local-human",
                        "profile_version": profile_version,
                        "required_acr": "2",
                        "required_amr": ["pwd", "otp"],
                    }
                ],
                "max_auth_age_seconds": max_auth_age_seconds,
            },
        }
    )


async def configure(sessions: async_sessionmaker[AsyncSession], *, key: str = "require-mfa", **changes: Any) -> None:
    await configure_human_authentication(
        sessions,
        assurance_request(**changes),
        actor_ref="operator:assurance-test",
        idempotency_key=key,
    )
