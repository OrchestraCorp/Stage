"""Real TCP + full RuntimeHost + migrated PostgreSQL native login and human edit."""

import asyncio
import os
import socket
from pathlib import Path

import httpx
import pytest
import uvicorn
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.identity.assurance_administration import ConfigureHumanAuthenticationRequest, configure_human_authentication
from app.main import create_app
from app.models.execution import AuditRecordTable, CommandExecutionTable, OutboxRecordTable
from app.native_auth.administration import InstallPlan, install
from app.native_auth.keys import read_encryption_key
from scripts.native_auth_admin import generate_keys
from scripts.native_auth_smoke import login
from tests.support.live_database import ASSIGNMENT, MEMBERSHIP, ORG, PERSON, install_fixture
from tests.support.live_database import migrated_sessions as _migrated_sessions

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="requires explicitly disposable PostgreSQL"
)
# Import the migration fixture without collecting the historical Keycloak tests.
migrated_sessions = _migrated_sessions


async def test_real_native_login_policy_edit_replay_refresh_and_immediate_logout(
    migrated_sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    await install_fixture(
        migrated_sessions,
        {
            "issuer": "http://127.0.0.1:18080/realms/uncontacted",
            "audience": "stage-api",
            "human_client": "legacy",
            "human_subject": "existing-human",
        },
    )
    keys, encryption = tmp_path / "keys.json", tmp_path / "encryption.key"
    generate_keys(keys, encryption)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        issuer = f"http://127.0.0.1:{listener.getsockname()[1]}"
        fixture = {
            "issuer": issuer,
            "client_id": "maestro-native",
            "redirect_uri": "http://127.0.0.1:18765/callback",
            "username": "native-live",
            "password": "synthetic-native-live-password",
        }
        plan = InstallPlan.model_validate(
            {
                "environment": "test",
                "organization_ref": ORG,
                "issuer": issuer,
                "client_id": fixture["client_id"],
                "redirect_uris": [fixture["redirect_uri"]],
                "account_ref": "native:live",
                "principal_ref": PERSON,
                "membership_ref": MEMBERSHIP,
                "external_identity_ref": "identity:native-live",
                "username": fixture["username"],
                "password": fixture["password"],
            }
        )
        await install(
            migrated_sessions,
            plan,
            actor_ref="operator:live",
            idempotency_key="install-native-live",
            credential_digest_key=read_encryption_key(encryption),
        )
        await configure_human_authentication(
            migrated_sessions,
            ConfigureHumanAuthenticationRequest.model_validate(
                {
                    "environment": "test",
                    "organization_ref": ORG,
                    "expected_version": 0,
                    "policy": {
                        "mode": "required",
                        "rules": [
                            {
                                "profile_ref": "stage-native-human",
                                "profile_version": 1,
                                "required_acr": "urn:orchestra:acr:pwd-totp",
                                "required_amr": ["pwd", "otp"],
                            }
                        ],
                        "max_auth_age_seconds": 900,
                    },
                }
            ),
            actor_ref="operator:live",
            idempotency_key="native-only-policy",
        )
        settings = Settings(
            env="test",
            database_url=os.environ["TEST_DATABASE_URL"],
            redis_url=None,
            native_auth_enabled=True,
            native_auth_issuer=issuer,
            native_auth_organization_ref=ORG,
            native_auth_key_file=str(keys),
            native_auth_encryption_key_file=str(encryption),
        )
        application = create_app(settings)
        async with application.router.lifespan_context(application):
            server = uvicorn.Server(uvicorn.Config(application, lifespan="off", log_level="critical", access_log=False))
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(15):
                    while not server.started:
                        if serving.done():
                            await serving
                            pytest.fail("native server exited before startup")
                        await asyncio.sleep(0.02)
                async with httpx.AsyncClient(
                    base_url=issuer, timeout=15, follow_redirects=False, trust_env=False
                ) as client:
                    tokens = await login(client, fixture, enroll=True)
                    params = {"profile_ref": "stage-native-human", "profile_version": "1"}
                    business_headers = {"Authorization": "Bearer " + tokens["access_token"]}
                    me = await client.get("/api/v1/identity/me", params=params, headers=business_headers)
                    assert me.status_code == 200 and me.json()["principal_ref"] == PERSON
                    assignments = await client.get(
                        "/api/v1/identity/assignments", params=params, headers=business_headers
                    )
                    assert assignments.status_code == 200
                    assert assignments.json()["items"][0]["assignment_ref"] == ASSIGNMENT
                    params["assignment_ref"] = ASSIGNMENT
                    body = {
                        "title": "Native authenticated edit",
                        "description": "Synthetic HTTP validation",
                        "expected_version": 1,
                    }
                    response = await client.put(
                        "/api/v1/work-items/1/details",
                        params=params,
                        json=body,
                        headers={**business_headers, "Idempotency-Key": "native-live-edit"},
                    )
                    assert response.status_code == 200
                    refresh = await client.post(
                        "/oauth/token",
                        data={
                            "grant_type": "refresh_token",
                            "client_id": fixture["client_id"],
                            "refresh_token": tokens["refresh_token"],
                        },
                    )
                    assert refresh.status_code == 200
                    business_headers = {"Authorization": "Bearer " + refresh.json()["access_token"]}
                    replay = await client.put(
                        "/api/v1/work-items/1/details",
                        params=params,
                        json=body,
                        headers={**business_headers, "Idempotency-Key": "native-live-edit"},
                    )
                    assert replay.status_code == 200 and replay.json()["replayed"]
                    assert (
                        await client.post(
                            "/oauth/revoke",
                            data={"client_id": fixture["client_id"], "token": refresh.json()["refresh_token"]},
                        )
                    ).status_code == 200
                    assert (
                        await client.get("/api/v1/work-items/1", params=params, headers=business_headers)
                    ).status_code == 401
                    assert (
                        await client.get(
                            "/api/v1/work-items/1/operations/native-live-edit", params=params, headers=business_headers
                        )
                    ).status_code == 401
                    assert (await client.get("/ready")).status_code == 200
                async with migrated_sessions() as session:
                    assert await session.scalar(select(func.count()).select_from(CommandExecutionTable)) == 1
                    assert await session.scalar(select(func.count()).select_from(AuditRecordTable)) == 1
                    assert await session.scalar(select(func.count()).select_from(OutboxRecordTable)) == 0
            finally:
                server.should_exit = True
                await asyncio.wait_for(serving, 15)
