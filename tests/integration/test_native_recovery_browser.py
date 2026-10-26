"""Opt-in Chromium factor rebind against a fully migrated disposable Stage runtime.

No development account is touched. Select both TEST_DATABASE_URL and an installed
STAGE_PLAYWRIGHT_MODULE explicitly; the browser changes only this test's fixture.
"""

import asyncio
import json
import os
import signal
import socket
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.identity.assurance_administration import ConfigureHumanAuthenticationRequest, configure_human_authentication
from app.main import create_app
from app.native_auth.administration import InstallPlan, install
from app.native_auth.keys import read_encryption_key
from scripts.native_auth_admin import generate_keys
from scripts.native_auth_smoke import login
from tests.support.live_database import MEMBERSHIP, ORG, PERSON, install_fixture
from tests.support.live_database import migrated_sessions as _migrated_sessions

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") or not os.environ.get("STAGE_PLAYWRIGHT_MODULE"),
    reason="select disposable PostgreSQL and an installed Playwright module for real Chromium recovery acceptance",
)
migrated_sessions = _migrated_sessions


async def test_real_browser_factor_reset_requires_rebind_proof_and_new_otp(
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
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as callback_reservation,
    ):
        listener.bind(("127.0.0.1", 0))
        callback_reservation.bind(("127.0.0.1", 0))
        issuer = f"http://127.0.0.1:{listener.getsockname()[1]}"
        fixture: dict[str, Any] = {
            "purpose": "isolated-native-recovery-browser-smoke",
            "issuer": issuer,
            "client_id": "maestro-recovery-browser",
            "redirect_uri": f"http://127.0.0.1:{callback_reservation.getsockname()[1]}/callback",
            "username": "native-recovery-browser",
            "password": "synthetic-native-recovery-browser-password",
            "profile_ref": "stage-native-human",
            "profile_version": 1,
            "principal_ref": PERSON,
        }
        await install(
            migrated_sessions,
            InstallPlan.model_validate(
                {
                    "environment": "test",
                    "organization_ref": ORG,
                    "issuer": issuer,
                    "client_id": fixture["client_id"],
                    "redirect_uris": [fixture["redirect_uri"]],
                    "account_ref": "native:recovery-browser",
                    "principal_ref": PERSON,
                    "membership_ref": MEMBERSHIP,
                    "external_identity_ref": "identity:recovery-browser",
                    "username": fixture["username"],
                    "password": fixture["password"],
                }
            ),
            actor_ref="operator:recovery-browser",
            idempotency_key="install-native-recovery-browser",
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
            actor_ref="operator:recovery-browser",
            idempotency_key="recovery-browser-native-policy",
        )
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
            server = uvicorn.Server(uvicorn.Config(application, lifespan="off", log_level="critical", access_log=False))
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(15):
                    while not server.started:
                        if serving.done():
                            await serving
                            pytest.fail("native recovery runtime exited before startup", pytrace=False)
                        await asyncio.sleep(0.02)
                async with httpx.AsyncClient(
                    base_url=issuer, timeout=15, follow_redirects=False, trust_env=False
                ) as client:
                    initial = await login(client, fixture, enroll=True)
                    revoked = await client.post(
                        "/oauth/revoke",
                        data={"client_id": fixture["client_id"], "token": initial["refresh_token"]},
                    )
                    assert revoked.status_code == 200
                fixture_path = tmp_path / "recovery-browser-fixture.json"
                descriptor = os.open(fixture_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "w") as output:
                    json.dump(fixture, output)
                old_secret = fixture["otp_secret"]
                # The Node process owns the real callback socket from here.
                callback_reservation.close()
                process = await asyncio.create_subprocess_exec(
                    "node",
                    "scripts/native_auth_recovery_browser_smoke.cjs",
                    str(fixture_path),
                    env={**os.environ, "STAGE_SMOKE_PYTHON": sys.executable},
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=125)
                except BaseException:
                    if process.returncode is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    await asyncio.shield(process.wait())
                    raise
                if process.returncode != 0:
                    # The Node wrapper prints only a fixed error, phase, and type.
                    try:
                        error = json.loads(stderr.decode().strip().splitlines()[-1])
                        phase = str(error["phase"])
                    except (ValueError, KeyError, IndexError):
                        phase = "subprocess_startup"
                    pytest.fail(f"native recovery browser failed during {phase}", pytrace=False)
                report = json.loads(stdout)
                assert report["browser"] == "Chromium" and report["passed"] == 14
                assert {
                    "fresh_browser_password_alone_has_no_enrollment_secret",
                    "rebound_registered_callback_and_pkce",
                    "rebound_same_person_business_identity",
                    "used_rebind_code_cannot_reopen_enrollment",
                    "final_session_revoked",
                } <= set(report["checks"])
                replacement = json.loads(fixture_path.read_text())
                if replacement.get("otp_secret") == old_secret or "pending_enrollment_code" in replacement:
                    pytest.fail("private browser fixture did not finish replacing its factor", pytrace=False)
                assert len(replacement["recovery_codes"]) == 10
                assert fixture_path.stat().st_mode & 0o077 == 0
            finally:
                server.should_exit = True
                await asyncio.wait_for(serving, 15)
