"""Operator factor reset delivers its one-use proof only to a private output file."""

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.identity.administration import IdentityAdministrationError
from app.models.native_auth import NativeAccountTable
from app.native_auth.crypto import digest_secret
from app.native_auth.errors import NativeCommitUnknown
from scripts.native_auth_admin import run
from tests.support.identity_administration import install as install_existing_identity
from tests.support.native_administration import install_native


async def reset_args(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> argparse.Namespace:
    await install_existing_identity(sessions)
    await install_native(sessions)
    engine = sessions.kw["bind"]
    monkeypatch.setenv("STAGE_DATABASE_URL", engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("STAGE_ENV", "test")
    monkeypatch.setenv("STAGE_NATIVE_AUTH_ENABLED", "false")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"account_ref": "account:native-human", "expected_version": 1, "reset_totp": True}))
    plan.chmod(0o600)
    key = tmp_path / "key"
    key.write_bytes(Fernet.generate_key())
    key.chmod(0o600)
    return argparse.Namespace(
        command="reset",
        environment="test",
        plan=plan,
        organization_ref="tenant-a",
        encryption_key_file=key,
        enrollment_output=tmp_path / "enrollment.json",
    )


async def test_factor_reset_writes_private_proof_and_keeps_public_result_secret_free(
    persistence_sessions: async_sessionmaker[AsyncSession], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = await reset_args(persistence_sessions, tmp_path, monkeypatch)
    result = await run(args)
    material = json.loads(args.enrollment_output.read_text())
    code = material["enrollment_code"]
    assert len(code) >= 40
    assert stat.S_IMODE(args.enrollment_output.stat().st_mode) == 0o600
    assert code not in json.dumps(result) and result["credential_version"] == 2
    async with persistence_sessions() as session:
        account = await session.get(NativeAccountTable, "account:native-human")
        assert account and not account.totp_bootstrap_allowed
        assert account.recovery_hashes == [digest_secret(code)]


@pytest.mark.parametrize("destination", ["missing", "exists"])
async def test_invalid_proof_destination_does_not_change_credentials(
    persistence_sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
) -> None:
    args = await reset_args(persistence_sessions, tmp_path, monkeypatch)
    if destination == "missing":
        args.enrollment_output = None
    else:
        args.enrollment_output.write_text("keep-existing-file")
    with pytest.raises((IdentityAdministrationError, FileExistsError)):
        await run(args)
    async with persistence_sessions() as session:
        account = await session.get(NativeAccountTable, "account:native-human")
        assert account and account.credential_version == 1 and account.totp_bootstrap_allowed
    if destination == "exists":
        assert args.enrollment_output.read_text() == "keep-existing-file"


async def test_proof_delivery_failure_does_not_claim_credential_change_was_rejected(
    persistence_sessions: async_sessionmaker[AsyncSession], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = await reset_args(persistence_sessions, tmp_path, monkeypatch)

    def fail_sync(*_: Any) -> None:
        raise OSError("synthetic output failure")

    monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises(NativeCommitUnknown):
        await run(args)
    async with persistence_sessions() as session:
        account = await session.get(NativeAccountTable, "account:native-human")
        assert account and account.credential_version == 2 and not account.totp_bootstrap_allowed


def test_private_operator_plan_rejects_pipe_without_waiting_for_a_writer(tmp_path: Path) -> None:
    pipe = tmp_path / "operator-input"
    os.mkfifo(pipe, 0o600)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys;from scripts.native_auth_admin import read_private_plan;read_private_plan(sys.argv[1])",
            str(pipe),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0 and "plan_requires_private_owned_regular_file" in result.stderr


@pytest.mark.parametrize("field", ["account_ref", "client_id"])
@pytest.mark.parametrize("value", ["人" * 43, "😀" * 33])
def test_native_registration_rejects_identifiers_that_its_token_adapter_cannot_accept(field: str, value: str) -> None:
    from app.native_auth.administration import InstallPlan
    from tests.support.native_administration import plan_values

    with pytest.raises(ValueError, match="native_token_identifier_exceeds_utf8_limit"):
        InstallPlan.model_validate({**plan_values(), field: value})


def test_native_registration_preserves_unicode_inside_the_token_byte_budget() -> None:
    from app.native_auth.administration import InstallPlan
    from tests.support.native_administration import plan_values

    value = "人员:" + "人" * 40
    installed = InstallPlan.model_validate({**plan_values(), "account_ref": value, "client_id": value})
    assert installed.account_ref == value and installed.client_id == value
