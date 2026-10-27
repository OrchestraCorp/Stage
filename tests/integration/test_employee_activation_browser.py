"""Real issuer forms activate a manager-created employee without granting login or roles."""

import asyncio
import json
import os
import signal
import socket
import sys
from pathlib import Path

import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.main import create_app
from app.models.authorization import RoleAssignmentTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import ExternalIdentityTable, OrganizationMembershipTable
from app.models.native_auth import (
    NativeAccountActivationTable,
    NativeAccountTable,
    NativeAuthorizationCodeTable,
    NativeBrowserSessionTable,
    NativeSessionTable,
)
from scripts.native_auth_admin import generate_keys
from tests.support.authorization import ORG
from tests.support.employee_onboarding import PASSWORD, employee_case
from tests.support.human_administration import signing_key as signing_key
from tests.support.live_database import migrated_sessions as migrated_sessions

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") or not os.environ.get("STAGE_PLAYWRIGHT_MODULE"),
    reason="select disposable PostgreSQL and installed Playwright for real Chromium employee activation acceptance",
)


async def test_real_browser_employee_activation_requires_password_and_totp_without_login_or_grants(
    migrated_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey, tmp_path: Path
) -> None:
    keys, encryption = tmp_path / "keys.json", tmp_path / "encryption.key"
    generate_keys(keys, encryption)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        issuer = f"http://127.0.0.1:{listener.getsockname()[1]}"
        async with employee_case(migrated_sessions, signing_key, issuer=issuer) as case:
            delivery = await case.create(key="employee-browser-activation")
            employee = delivery.operation.result
            assert delivery.activation_code is not None
            encryption.write_bytes(case.encryption_key)
            application = create_app(
                Settings(
                    env="test",
                    database_url=os.environ["TEST_DATABASE_URL"],
                    redis_url=None,
                    native_auth_enabled=True,
                    native_auth_issuer=issuer,
                    native_auth_organization_ref=ORG,
                    native_auth_key_file=str(keys),
                    native_auth_encryption_key_file=str(encryption),
                )
            )
            async with application.router.lifespan_context(application):
                server = uvicorn.Server(
                    uvicorn.Config(application, lifespan="off", log_level="critical", access_log=False)
                )
                serving = asyncio.create_task(server.serve(sockets=[listener]))
                try:
                    async with asyncio.timeout(15):
                        while not server.started:
                            if serving.done():
                                await serving
                                pytest.fail("employee activation runtime exited before startup", pytrace=False)
                            await asyncio.sleep(0.02)
                    fixture_path = tmp_path / "employee-activation-browser-fixture.json"
                    descriptor = os.open(fixture_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(descriptor, "w") as output:
                        json.dump(
                            {
                                "purpose": "isolated-employee-activation-browser-smoke",
                                "issuer": issuer,
                                "activation_ref": employee.activation_ref,
                                "activation_code": delivery.activation_code,
                                "password": PASSWORD,
                            },
                            output,
                        )
                    process = await asyncio.create_subprocess_exec(
                        "node",
                        "scripts/native_auth_activation_browser_smoke.cjs",
                        str(fixture_path),
                        env={**os.environ, "STAGE_SMOKE_PYTHON": sys.executable},
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=True,
                    )
                    try:
                        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=100)
                    except BaseException:
                        if process.returncode is None:
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        await asyncio.shield(process.wait())
                        raise
                    if process.returncode != 0:
                        try:
                            error = json.loads(stderr.decode().strip().splitlines()[-1])
                            phase = str(error["phase"])
                        except (ValueError, KeyError, IndexError):
                            phase = "subprocess_startup"
                        pytest.fail(f"employee activation browser failed during {phase}", pytrace=False)
                    report = json.loads(stdout)
                    assert report["browser"] == "Chromium" and report["passed"] == 9
                    assert {
                        "same_browser_status_resumes_enrollment",
                        "real_totp_activates_and_displays_ten_recovery_codes",
                        "activation_does_not_create_browser_login",
                        "status_refresh_and_post_replay_never_repeat_recovery_codes",
                    } <= set(report["checks"])
                    assert fixture_path.stat().st_mode & 0o077 == 0
                    async with migrated_sessions() as session:
                        row = await session.get(NativeAccountActivationTable, employee.activation_ref)
                        account = await session.get(NativeAccountTable, employee.account_ref)
                        person = await session.scalar(
                            select(PrincipalTable).where(PrincipalTable.principal_ref == employee.principal_ref)
                        )
                        member = await session.get(OrganizationMembershipTable, (ORG, employee.membership_ref))
                        assert row is not None and row.status == "completed" and row.completed_at is not None
                        assert row.code_hash is None and row.pending_password_hash is None and row.pending_totp is None
                        external = await session.get(ExternalIdentityTable, row.external_identity_ref)
                        assert all(
                            fact is not None and fact.status == "active" for fact in (account, person, member, external)
                        )
                        assert (
                            account is not None
                            and len(account.recovery_hashes) == 10
                            and account.totp_secret is not None
                        )
                        for table in (NativeSessionTable, NativeBrowserSessionTable, NativeAuthorizationCodeTable):
                            assert await session.scalar(select(func.count()).select_from(table)) == 0
                        assert (
                            await session.scalar(
                                select(func.count())
                                .select_from(RoleAssignmentTable)
                                .where(RoleAssignmentTable.principal_ref == employee.principal_ref)
                            )
                            == 0
                        )
                finally:
                    server.should_exit = True
                    await asyncio.wait_for(serving, 15)
