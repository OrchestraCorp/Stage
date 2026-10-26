"""Shared synthetic builders for live database."""

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.administration import (
    InstallAuthorizationPlan,
    install_authorization,
)
from app.core.db import create_engine, create_session_factory
from app.identity.administration import BootstrapPlan, bootstrap
from app.models.base import Base
from app.models.work import WorkItemTable
from scripts.check_database import validate_test_database

ORG = "org:human-work-smoke"


PERSON = "person:smoke"


MEMBERSHIP = "membership:smoke"


ASSIGNMENT = "assignment:smoke"


PROFILE = "smoke-human"


SCOPE = "scope:smoke"


@pytest.fixture
async def migrated_sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = os.environ["TEST_DATABASE_URL"]
    validate_test_database(url, os.environ.get("STAGE_ALLOW_TEST_DATABASE_RESET"))
    if not url.startswith("postgresql+asyncpg:"):
        pytest.fail("real write smoke requires an explicitly disposable PostgreSQL database")
    engine = create_engine(url)

    async def reset() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))

    try:
        await reset()
        migration = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "alembic",
            "upgrade",
            "head",
            env={**os.environ, "STAGE_DATABASE_URL": url, "STAGE_ENV": "test"},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(migration.communicate(), 30)
        except BaseException:
            # Cancelling communicate does not stop Alembic. Reap the writer
            # before resetting tables, including on test cancellation.
            if migration.returncode is None:
                try:
                    migration.kill()
                except ProcessLookupError:
                    pass
            await asyncio.shield(migration.wait())
            raise
        if migration.returncode != 0:
            pytest.fail("disposable database migration failed", pytrace=False)
        yield create_session_factory(engine)
    finally:
        try:
            await reset()
        finally:
            await engine.dispose()


async def install_fixture(sessions: async_sessionmaker[AsyncSession], config: dict[str, Any]) -> None:
    plan = BootstrapPlan.model_validate(
        {
            "environment": "test",
            "organization_ref": ORG,
            "display_name": "Disposable human work smoke",
            "issuer": config["issuer"],
            "jwks_url": config["issuer"] + "/protocol/openid-connect/certs",
            "audience": config["audience"],
            "provider_ref": "provider:smoke",
            "trust_ref": "trust:smoke",
            "profiles": [
                {
                    "profile_ref": PROFILE,
                    "profile_version": 1,
                    "subject_namespace": "keycloak-human-v1",
                    "credential_kind": "human_oidc",
                    "allowed_clients": [config["human_client"]],
                }
            ],
            "identities": [
                {
                    "principal_ref": PERSON,
                    "principal_type": "human",
                    "subject": config["human_subject"],
                    "profile_ref": PROFILE,
                    "profile_version": 1,
                    "client_ref": config["human_client"],
                    "external_identity_ref": "identity:smoke",
                    "membership_ref": MEMBERSHIP,
                }
            ],
        }
    )
    await bootstrap(sessions, plan, actor_ref="operator:smoke", idempotency_key="bootstrap-smoke")
    async with sessions.begin() as session:
        session.add(WorkItemTable(id=1, title="Synthetic fixture", lifecycle="open", scope_ref=SCOPE))
    member = {"principal_ref": PERSON, "membership_ref": MEMBERSHIP, "expected_membership_version": 1}
    grants = InstallAuthorizationPlan.model_validate(
        {
            "environment": "test",
            "organization_ref": ORG,
            "expected_organization_version": 1,
            "role_ref": "role:smoke-editor",
            "role_code": "smoke-editor",
            "display_name": "Synthetic metadata editor",
            "role_revision": 1,
            "actions": ["task.view", "task.metadata.update"],
            "scopes": [{"scope_ref": SCOPE, "scope_kind": "project"}],
            "assignments": [{**member, "assignment_ref": ASSIGNMENT, "scope_ref": SCOPE}],
            "resources": [{"resource_ref": "work_item:1", "scope_ref": SCOPE, "expected_work_version": 1}],
            "acls": [
                {**member, "relation_ref": "acl:smoke-owner", "relation": "owner_of", "resource_ref": "work_item:1"}
            ],
        }
    )
    await install_authorization(sessions, grants, actor_ref="operator:smoke", idempotency_key="grant-smoke")
