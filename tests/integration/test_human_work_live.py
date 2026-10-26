"""Opt-in real Keycloak -> HTTP -> PostgreSQL write in a disposable test database.

Select STAGE_HUMAN_WORK_SMOKE_CREDENTIALS and TEST_DATABASE_URL explicitly.
The migration fixture resets only an authorized stage_test*/stage_ci* database.
No grants or WorkItems are installed into the development service.
"""

import asyncio
import json
import os
import socket
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.administration import (
    AuthorizationRevocationRequest,
    revoke_authorization,
)
from app.core.config import Settings
from app.main import create_app
from app.models.execution import AuditRecordTable, CommandExecutionTable, OutboxRecordTable
from app.models.work import WorkItemTable
from scripts.identity_idp_smoke import assert_local_url, authorization_code
from tests.support.live_database import (
    ASSIGNMENT,
    ORG,
    PERSON,
    PROFILE,
    install_fixture,
)
from tests.support.live_database import (
    migrated_sessions as migrated_sessions,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("STAGE_HUMAN_WORK_SMOKE_CREDENTIALS") or not os.environ.get("TEST_DATABASE_URL"),
    reason="select real development IdP credentials and an explicitly disposable PostgreSQL database",
)


async def check_http(client: httpx.AsyncClient, config: dict[str, Any]) -> str:
    endpoint = config["issuer"] + "/protocol/openid-connect/token"
    code, verifier, _ = await authorization_code(client, config)
    exchanged = await client.post(
        endpoint,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": config["human_client"],
            "code_verifier": verifier,
            "redirect_uri": config["redirect_uri"],
        },
    )
    assert exchanged.status_code == 200
    tokens = exchanged.json()
    params = {"profile_ref": PROFILE, "profile_version": "1", "assignment_ref": ASSIGNMENT}
    headers = {"Authorization": "Bearer " + tokens["access_token"], "Idempotency-Key": "live-details-once"}
    path = "/api/v1/work-items/1"
    item = await client.get(path, params=params, headers=headers)
    assert item.status_code == 200 and item.json()["version"] == 1
    body = {"title": "Human supervised edit", "description": "Synthetic acceptance data", "expected_version": 1}
    first = await client.put(path + "/details", params=params, headers=headers, json=body)
    assert first.status_code == 200
    assert first.json()["result"]["version"] == 2 and not first.json()["replayed"]
    assert first.headers["cache-control"] == "no-store"
    refreshed = await client.post(
        endpoint,
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": config["human_client"],
        },
    )
    assert refreshed.status_code == 200
    headers["Authorization"] = "Bearer " + refreshed.json()["access_token"]
    replay = await client.put(path + "/details", params=params, headers=headers, json=body)
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert replay.json()["execution_id"] == first.json()["execution_id"]
    recovered = await client.get(path + "/operations/live-details-once", params=params, headers=headers)
    assert recovered.status_code == 200 and recovered.json()["execution_id"] == first.json()["execution_id"]
    conflict = await client.put(path + "/details", params=params, headers=headers, json={**body, "title": "Different"})
    assert conflict.status_code == 409
    stale = await client.put(
        path + "/details", params=params, headers={**headers, "Idempotency-Key": "stale-version"}, json=body
    )
    assert stale.status_code == 409
    injected = await client.put(
        path + "/details", params=params, headers=headers, json={**body, "actor_ref": "person:forged"}
    )
    assert injected.status_code == 422
    wrong_usage = await client.get(path, params=params, headers={"Authorization": "Bearer " + tokens["id_token"]})
    assert wrong_usage.status_code == 401
    service = await client.post(
        endpoint, data={"grant_type": "client_credentials"}, auth=(config["service_client"], config["service_secret"])
    )
    assert service.status_code == 200
    wrong_kind = await client.put(
        path + "/details",
        params=params,
        headers={**headers, "Authorization": "Bearer " + service.json()["access_token"]},
        json=body,
    )
    assert wrong_kind.status_code == 401
    # Keep only current request credentials for the post-revocation check.
    client.headers.update(headers)
    client.params = httpx.QueryParams(params)
    return str(refreshed.json()["refresh_token"])


async def test_real_idp_human_edit_commit_recovery_and_revocation(
    migrated_sessions: async_sessionmaker[AsyncSession],
) -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    persistence_sessions = migrated_sessions
    credentials = Path(os.environ["STAGE_HUMAN_WORK_SMOKE_CREDENTIALS"])
    info = credentials.stat()
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
        pytest.fail("fixture credentials must be owned by the operator with mode 0600")
    config = json.loads(credentials.read_text())
    assert_local_url(config["issuer"])
    assert_local_url(config["redirect_uri"])
    await install_fixture(persistence_sessions, config)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        env="test",
        database_url=database_url,
        redis_url=None,
        redis_required=False,
    )
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        await check_running_application(application, persistence_sessions, config)


async def check_running_application(
    application: Any, persistence_sessions: async_sessionmaker[AsyncSession], config: dict[str, Any]
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(application, lifespan="off", log_level="critical", access_log=False))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(15):
                while not server.started:
                    if serving.done():
                        await serving
                        pytest.fail("temporary smoke server stopped before startup")
                    await asyncio.sleep(0.02)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
                timeout=15,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                assert (await client.get("/ready")).status_code == 200
                refresh_token = await check_http(client, config)
                # This JWT profile verifies signatures locally. IdP session
                # logout revokes refresh, but does not revoke an issued access
                # token at Stage before exp or an explicit local revocation.
                async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as idp:
                    logged_out = await idp.post(
                        config["issuer"] + "/protocol/openid-connect/logout",
                        data={"client_id": config["human_client"], "refresh_token": refresh_token},
                    )
                    assert logged_out.status_code == 204
                    refresh_after_logout = await idp.post(
                        config["issuer"] + "/protocol/openid-connect/token",
                        data={
                            "grant_type": "refresh_token",
                            "client_id": config["human_client"],
                            "refresh_token": refresh_token,
                        },
                    )
                    assert refresh_after_logout.status_code == 400
                assert (await client.get("/api/v1/work-items/1")).status_code == 200
                await revoke_authorization(
                    persistence_sessions,
                    AuthorizationRevocationRequest(
                        environment="test",
                        organization_ref=ORG,
                        kind="assignment",
                        target_ref=ASSIGNMENT,
                        expected_version=1,
                    ),
                    actor_ref="operator:smoke",
                    idempotency_key="revoke-smoke",
                )
                denied = await client.get("/api/v1/work-items/1/operations/live-details-once")
                assert denied.status_code == 403 and denied.headers["cache-control"] == "no-store"
                async with persistence_sessions() as session:
                    for table in (CommandExecutionTable, AuditRecordTable):
                        assert await session.scalar(select(func.count()).select_from(table)) == 1
                    assert await session.scalar(select(func.count()).select_from(OutboxRecordTable)) == 0
                    work = await session.get(WorkItemTable, 1)
                    assert work is not None and work.version == 2 and work.lifecycle == "open"
                    audit = await session.scalar(select(AuditRecordTable))
                    assert audit is not None and audit.authorization_json is not None
                    assert audit.authorization_json["authentication"]["principal_ref"] == PERSON
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=15)
