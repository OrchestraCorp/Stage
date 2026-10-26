"""Shared synthetic builders for identity administration."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.identity.administration import (
    BootstrapPlan,
    bootstrap,
)

OPERATOR = "local-os:test:uid:1000"


def plan_data() -> dict[str, Any]:
    return {
        "environment": "test",
        "tenant_ref": "tenant-a",
        "display_name": "Fictional development organization",
        "issuer": "http://127.0.0.1:18080/realms/stage-test",
        "jwks_url": "http://127.0.0.1:18080/realms/stage-test/protocol/openid-connect/certs",
        "audience": "stage-api",
        "provider_ref": "provider-a",
        "trust_ref": "trust-a",
        "profiles": [
            {
                "profile_ref": "human",
                "profile_version": 1,
                "subject_namespace": "human/sub",
                "credential_kind": "human_oidc",
                "allowed_clients": ["maestro-test"],
            },
            {
                "profile_ref": "service",
                "profile_version": 1,
                "subject_namespace": "service/sub",
                "credential_kind": "workload_oauth2",
                "allowed_clients": ["worker-test"],
            },
        ],
        "identities": [
            {
                "principal_ref": "human-a",
                "principal_type": "human",
                "subject": "issuer-human-a",
                "profile_ref": "human",
                "profile_version": 1,
                "client_ref": "maestro-test",
                "external_identity_ref": "external-human",
                "membership_ref": "membership-human",
            },
            {
                "principal_ref": "service-a",
                "principal_type": "service",
                "subject": "issuer-service-a",
                "profile_ref": "service",
                "profile_version": 1,
                "client_ref": "worker-test",
                "external_identity_ref": "external-service",
                "membership_ref": "membership-service",
                "workload_ref": "workload-a",
            },
        ],
    }


async def install(sessions: async_sessionmaker[AsyncSession]) -> None:
    await bootstrap(sessions, BootstrapPlan.model_validate(plan_data()), actor_ref=OPERATOR, idempotency_key="install")
