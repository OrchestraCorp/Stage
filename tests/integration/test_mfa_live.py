"""Opt-in genuine password/TOTP -> Stage HTTP -> disposable PostgreSQL acceptance."""

import asyncio
import os
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.administration import InstallAuthorizationPlan, install_authorization
from app.core.config import Settings
from app.identity.administration import BootstrapPlan, bootstrap
from app.identity.assurance import HumanAssurancePolicy
from app.identity.assurance_administration import ConfigureHumanAuthenticationRequest, configure_human_authentication
from app.main import create_app
from app.models.execution import AuditRecordTable, CommandExecutionTable
from app.models.work import WorkItemTable
from scripts.identity_mfa_smoke import login_tokens, read_config
from tests.support.live_database import migrated_sessions as migrated_sessions

pytestmark = pytest.mark.skipif(
    not os.environ.get("STAGE_MFA_SMOKE_CREDENTIALS") or not os.environ.get("TEST_DATABASE_URL"),
    reason="select isolated MFA credentials and an explicitly disposable PostgreSQL test database",
)

ORG = "org:mfa-live"
PERSON = "person:mfa-live"
MEMBERSHIP = "membership:mfa-live"
ASSIGNMENT = "assignment:mfa-live"
STRONG = "isolated-mfa-human"
WEAK = "isolated-weak-human"


async def setup_fixture(sessions: async_sessionmaker[AsyncSession], config: dict[str, Any]) -> None:
    plan = BootstrapPlan.model_validate(
        {
            "environment": "test",
            "organization_ref": ORG,
            "display_name": "Isolated MFA acceptance",
            "issuer": config["issuer"],
            "jwks_url": config["issuer"] + "/protocol/openid-connect/certs",
            "audience": config["audience"],
            "provider_ref": "provider:mfa-live",
            "trust_ref": "trust:mfa-live",
            "profiles": [
                {
                    "profile_ref": reference,
                    "profile_version": 1,
                    "subject_namespace": "keycloak-human-v1",
                    "credential_kind": "human_oidc",
                    "allowed_clients": [client],
                }
                for reference, client in ((STRONG, config["human_client"]), (WEAK, config["weak_client"]))
            ],
            "identities": [
                {
                    "principal_ref": PERSON,
                    "principal_type": "human",
                    "subject": config["human_subject"],
                    "profile_ref": STRONG,
                    "profile_version": 1,
                    "client_ref": config["human_client"],
                    "external_identity_ref": "identity:mfa-live",
                    "membership_ref": MEMBERSHIP,
                }
            ],
        }
    )
    await bootstrap(sessions, plan, actor_ref="operator:mfa-live", idempotency_key="mfa-bootstrap")
    async with sessions.begin() as session:
        session.add(WorkItemTable(id=1, title="Synthetic MFA fixture", lifecycle="open", scope_ref="scope:mfa-live"))
    member = {"principal_ref": PERSON, "membership_ref": MEMBERSHIP, "expected_membership_version": 1}
    await install_authorization(
        sessions,
        InstallAuthorizationPlan.model_validate(
            {
                "environment": "test",
                "organization_ref": ORG,
                "expected_organization_version": 1,
                "role_ref": "role:mfa-live",
                "role_code": "metadata-editor",
                "display_name": "MFA fixture editor",
                "role_revision": 1,
                "actions": ["task.view", "task.metadata.update"],
                "scopes": [{"scope_ref": "scope:mfa-live", "scope_kind": "project"}],
                "assignments": [{**member, "assignment_ref": ASSIGNMENT, "scope_ref": "scope:mfa-live"}],
                "resources": [
                    {"resource_ref": "work_item:1", "scope_ref": "scope:mfa-live", "expected_work_version": 1}
                ],
                "acls": [
                    {**member, "relation_ref": "acl:mfa-live", "relation": "owner_of", "resource_ref": "work_item:1"}
                ],
            }
        ),
        actor_ref="operator:mfa-live",
        idempotency_key="mfa-grants",
    )


async def configure(sessions: async_sessionmaker[AsyncSession], *, expected: int, max_age: int) -> None:
    await configure_human_authentication(
        sessions,
        ConfigureHumanAuthenticationRequest(
            environment="test",
            organization_ref=ORG,
            expected_version=expected,
            policy=HumanAssurancePolicy.model_validate(
                {
                    "mode": "required",
                    "max_auth_age_seconds": max_age,
                    "rules": [
                        {
                            "profile_ref": STRONG,
                            "profile_version": 1,
                            "required_acr": "mfa",
                            "required_amr": ["pwd", "otp"],
                        }
                    ],
                }
            ),
        ),
        actor_ref="operator:mfa-live",
        idempotency_key=f"mfa-policy-{expected}",
    )


async def verify_http(
    api: httpx.AsyncClient, sessions: async_sessionmaker[AsyncSession], config: dict[str, Any]
) -> None:
    async with (
        httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as weak_browser,
        httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as strong_browser,
    ):
        weak = await login_tokens(weak_browser, config, strong=False)
        weak_params: dict[str, str | int] = {"profile_ref": WEAK, "profile_version": 1}
        weak_header = {"Authorization": "Bearer " + weak["access_token"]}
        assert (
            await api.get("/api/v1/identity/assignments", params=weak_params, headers=weak_header)
        ).status_code == 200
        await configure(sessions, expected=0, max_age=300)
        strong = await login_tokens(strong_browser, config)
        strong_header = {"Authorization": "Bearer " + strong["access_token"]}
        params: dict[str, str | int] = {"profile_ref": STRONG, "profile_version": 1}
        choices = await api.get("/api/v1/identity/assignments", params=params, headers=strong_header)
        assert choices.status_code == 200 and len(choices.json()["items"]) == 1
        selected = choices.json()["items"][0]["assignment_ref"]
        work_params = {**params, "assignment_ref": selected}
        body = {"title": "MFA-authenticated edit", "description": "Synthetic data", "expected_version": 1}
        edited = await api.put(
            "/api/v1/work-items/1/details",
            params=work_params,
            headers={**strong_header, "Idempotency-Key": "mfa-edit"},
            json=body,
        )
        assert edited.status_code == 200 and edited.json()["result"]["version"] == 2
        for path, query in (
            ("/api/v1/identity/me", weak_params),
            ("/api/v1/identity/assignments", weak_params),
            ("/api/v1/work-items/1", {**weak_params, "assignment_ref": selected}),
            ("/api/v1/work-items/1/operations/mfa-edit", {**weak_params, "assignment_ref": selected}),
        ):
            denied = await api.get(path, params=query, headers=weak_header)
            assert denied.status_code == 401 and denied.json()["error"]["code"] == "MFA_REQUIRED"
            assert denied.headers["cache-control"] == "no-store"
            assert "insufficient_user_authentication" in denied.headers["www-authenticate"]
        weak_refresh = await weak_browser.post(
            config["issuer"] + "/protocol/openid-connect/token",
            data={
                "grant_type": "refresh_token",
                "client_id": config["weak_client"],
                "refresh_token": weak["refresh_token"],
            },
        )
        assert weak_refresh.status_code == 200
        still_weak = await api.get(
            "/api/v1/identity/me",
            params=weak_params,
            headers={"Authorization": "Bearer " + weak_refresh.json()["access_token"]},
        )
        assert still_weak.status_code == 401 and still_weak.json()["error"]["code"] == "MFA_REQUIRED"
        # Tighten freshness after a real elapsed interval; refreshing JWT iat
        # cannot restore the original authentication's expired freshness.
        await asyncio.sleep(1.1)
        await configure(sessions, expected=1, max_age=1)
        refreshed = await strong_browser.post(
            config["issuer"] + "/protocol/openid-connect/token",
            data={
                "grant_type": "refresh_token",
                "client_id": config["human_client"],
                "refresh_token": strong["refresh_token"],
            },
        )
        assert refreshed.status_code == 200
        stale = await api.get(
            "/api/v1/work-items/1/operations/mfa-edit",
            params=work_params,
            headers={"Authorization": "Bearer " + refreshed.json()["access_token"]},
        )
        assert stale.status_code == 401 and stale.json()["error"]["code"] == "MFA_REQUIRED"
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(CommandExecutionTable)) == 1
        audit = (await session.scalars(select(AuditRecordTable))).one()
        assert audit.authorization_json is not None
        evidence = audit.authorization_json["authentication"]
        assert evidence["assurance_policy_version"] == 1 and evidence["acr"] == "mfa"
        assert set(evidence["amr"]) == {"pwd", "otp"}
        assert config["otp_secret"] not in str(audit.authorization_json)
        assert strong["access_token"] not in str(audit.authorization_json)


async def test_real_totp_discovery_edit_old_profile_rejection_and_refresh_freshness(
    migrated_sessions: async_sessionmaker[AsyncSession],
) -> None:
    config = read_config(Path(os.environ["STAGE_MFA_SMOKE_CREDENTIALS"]))
    await setup_fixture(migrated_sessions, config)
    app = create_app(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            env="test",
            database_url=os.environ["TEST_DATABASE_URL"],
            redis_url=None,
            redis_required=False,
        )
    )
    async with app.router.lifespan_context(app):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="critical", access_log=False))
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(15):
                    while not server.started:
                        if serving.done():
                            await serving
                            pytest.fail("temporary Stage stopped before startup")
                        await asyncio.sleep(0.02)
                async with (
                    httpx.AsyncClient(
                        base_url=f"http://127.0.0.1:{listener.getsockname()[1]}", timeout=10, trust_env=False
                    ) as api,
                    asyncio.timeout(90),
                ):
                    assert (await api.get("/ready")).status_code == 200
                    await verify_http(api, migrated_sessions, config)
            finally:
                server.should_exit = True
                await asyncio.wait_for(serving, 15)
