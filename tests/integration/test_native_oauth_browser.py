"""Real Chromium SSO, consent, form_post and RP logout against an isolated issuer."""

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
from app.main import create_app
from app.models.native_auth import NativeClientTable
from app.native_auth.administration import InstallPlan, install
from app.native_auth.keys import read_encryption_key
from scripts.native_auth_admin import generate_keys
from scripts.native_auth_smoke import login
from tests.support.live_database import MEMBERSHIP, ORG, PERSON, install_fixture
from tests.support.live_database import migrated_sessions as _migrated_sessions

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") or not os.environ.get("STAGE_PLAYWRIGHT_MODULE"),
    reason="select disposable PostgreSQL and an installed Playwright module for real Chromium OAuth acceptance",
)
migrated_sessions = _migrated_sessions


async def test_real_browser_sso_consent_form_post_and_confirmed_logout(
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
        callback_origin = f"http://127.0.0.1:{callback_reservation.getsockname()[1]}"
        fixture: dict[str, Any] = {
            "purpose": "isolated-native-oauth-browser-smoke",
            "issuer": issuer,
            "client_id": "maestro-oauth-browser",
            "redirect_uri": callback_origin + "/callback",
            "post_logout_redirect_uri": callback_origin + "/logged-out",
            "username": "native-oauth-browser",
            "password": "synthetic-native-oauth-browser-password",
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
                    "account_ref": "native:oauth-browser",
                    "principal_ref": PERSON,
                    "membership_ref": MEMBERSHIP,
                    "external_identity_ref": "identity:oauth-browser",
                    "username": fixture["username"],
                    "password": fixture["password"],
                }
            ),
            actor_ref="operator:oauth-browser",
            idempotency_key="install-native-oauth-browser",
            credential_digest_key=read_encryption_key(encryption),
        )
        async with migrated_sessions.begin() as session:
            registered_client = await session.get(NativeClientTable, fixture["client_id"])
            assert registered_client is not None
            registered_client.post_logout_redirect_uris = [fixture["post_logout_redirect_uri"]]
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
                            pytest.fail("native OAuth runtime exited before startup", pytrace=False)
                        await asyncio.sleep(0.02)
                async with httpx.AsyncClient(
                    base_url=issuer, timeout=15, follow_redirects=False, trust_env=False
                ) as client:
                    initial = await login(client, fixture, enroll=True)
                    revoked = await client.post(
                        "/oauth/revoke", data={"client_id": fixture["client_id"], "token": initial["refresh_token"]}
                    )
                    assert revoked.status_code == 200
                fixture_path = tmp_path / "oauth-browser-fixture.json"
                descriptor = os.open(fixture_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "w") as output:
                    json.dump(fixture, output)
                callback_reservation.close()
                process = await asyncio.create_subprocess_exec(
                    "node",
                    "scripts/native_oauth_browser_smoke.cjs",
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
                    try:
                        error = json.loads(stderr.decode().strip().splitlines()[-1])
                        phase = str(error["phase"])
                    except (ValueError, KeyError, IndexError):
                        phase = "subprocess_startup"
                    pytest.fail(f"native OAuth browser failed during {phase}", pytrace=False)
                report = json.loads(stdout)
                assert report["browser"] == "Chromium" and report["passed"] == 11
                assert {
                    "automatic_form_post_survives_csp_without_referrer",
                    "explicit_consent_post_reaches_registered_callback",
                    "logout_get_only_shows_confirmation",
                    "cross_site_post_logout_relay_binds_samesite_cookie_before_confirmation",
                    "logout_invalidates_all_browser_token_families",
                    "logout_invalidates_browser_sso",
                } <= set(report["checks"])
            finally:
                server.should_exit = True
                await asyncio.wait_for(serving, 15)
