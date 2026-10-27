"""Explicit migration preserves people, rejects ambiguous links and keeps secrets private."""

import argparse
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.identity.administration import IdentityAdministrationError, operation_result
from app.identity.service import content_digest, profile_content
from app.models.authorization import RoleAssignmentTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import (
    AuthenticationProfileTable,
    ExternalIdentityTable,
    IdentityControlOperationTable,
    OrganizationMembershipTable,
)
from app.models.native_auth import NativeAccountTable, NativeClientTable
from app.native_auth.administration import INSTALL_OPERATION, NAMESPACE, PROFILE, InstallPlan, install
from app.native_auth.crypto import verify_password
from app.native_auth.keys import load_keyring, read_encryption_key
from scripts.native_auth_admin import generate_keys, prepare_key_rotation, read_private_plan, run
from tests.support.identity_administration import OPERATOR
from tests.support.identity_administration import install as install_existing_identity
from tests.support.native_administration import (
    DIGEST_KEY,
    PASSWORD,
    install_native,
    plan_values,
)


async def test_native_install_preserves_identity_and_never_grants_business_authority(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await install_existing_identity(persistence_sessions)
    first = await install_native(persistence_sessions)
    assert await install_native(persistence_sessions) == first
    recovered = await operation_result(
        persistence_sessions,
        environment="test",
        actor_ref=OPERATOR,
        operation_type=INSTALL_OPERATION,
        idempotency_key="native-install",
    )
    assert recovered == first
    assert first.result["business_grants"] == [] and first.result["totp_enrollment_required"]
    async with persistence_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PrincipalTable)) == 2
        assert await session.scalar(select(func.count()).select_from(OrganizationMembershipTable)) == 2
        assert await session.scalar(select(func.count()).select_from(RoleAssignmentTable)) == 0
        account = await session.get(NativeAccountTable, "account:native-human")
        assert account is not None and account.principal_ref == "human-a" and account.totp_secret is None
        assert account.password_hash is not None
        assert account.password_hash.startswith("$argon2id$") and await verify_password(account.password_hash, PASSWORD)
        mapping = await session.get(ExternalIdentityTable, "external:native-human")
        assert mapping is not None and mapping.subject_exact == account.account_ref
        assert mapping.subject_namespace == NAMESPACE and mapping.link_evidence_ref == first.operation_ref
        assert await session.get(ExternalIdentityTable, "external-human") is not None
        profile = await session.get(AuthenticationProfileTable, (PROFILE, 1))
        assert profile is not None and profile.config_schema == "stage.native-access.v1"
        assert profile.digest == content_digest(profile_content(profile))
        journal = await session.get(IdentityControlOperationTable, first.operation_ref)
        assert journal is not None
        assert PASSWORD not in json.dumps([journal.before_json, journal.after_json, journal.result_json])


async def test_password_is_part_of_keyed_replay_identity_without_becoming_a_fast_db_oracle(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await install_existing_identity(persistence_sessions)
    await install_native(persistence_sessions)
    plan = InstallPlan.model_validate(plan_values())
    assert PASSWORD not in repr(plan) and PASSWORD not in plan.model_dump_json()
    payload = plan.digest_payload(DIGEST_KEY)
    assert "password" not in payload and PASSWORD not in json.dumps(payload)
    assert payload != plan.digest_payload(b"another-private-deployment-key-000000000")
    with pytest.raises(IdentityAdministrationError, match="idempotency_conflict"):
        await install_native(persistence_sessions, password="Different-synthetic-password-2026!")
    async with persistence_sessions() as session:
        account = await session.get(NativeAccountTable, "account:native-human")
        assert account is not None and await verify_password(account.password_hash, PASSWORD)


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"principal_ref": "unknown"}, "existing_active_human_required"),
        ({"principal_ref": "service-a", "membership_ref": "membership-service"}, "existing_active_human_required"),
        ({"membership_ref": "membership-service"}, "existing_current_membership_required"),
        ({"membership_ref": "unknown"}, "existing_current_membership_required"),
        ({"organization_ref": "unknown"}, "existing_organization_required"),
        ({"environment": "dev"}, "identity_database_environment_mismatch"),
    ],
)
async def test_install_requires_exact_existing_human_and_membership(
    persistence_sessions: async_sessionmaker[AsyncSession], changes: dict[str, Any], expected: str
) -> None:
    await install_existing_identity(persistence_sessions)
    with pytest.raises(IdentityAdministrationError, match=expected):
        await install_native(persistence_sessions, **changes)
    async with persistence_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NativeAccountTable)) == 0
        assert await session.get(AuthenticationProfileTable, (PROFILE, 1)) is None


async def test_disabled_principal_and_ended_membership_cannot_be_migrated(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await install_existing_identity(persistence_sessions)
    async with persistence_sessions.begin() as session:
        principal = await session.scalar(select(PrincipalTable).where(PrincipalTable.principal_ref == "human-a"))
        assert principal is not None
        principal.disabled_at = datetime.now(UTC)
    with pytest.raises(IdentityAdministrationError, match="existing_active_human_required"):
        await install_native(persistence_sessions)
    async with persistence_sessions.begin() as session:
        principal = await session.scalar(select(PrincipalTable).where(PrincipalTable.principal_ref == "human-a"))
        member = await session.get(OrganizationMembershipTable, ("tenant-a", "membership-human"))
        assert principal is not None and member is not None
        principal.disabled_at = None
        member.status = "left"
        member.ended_at = datetime.now(UTC)
    with pytest.raises(IdentityAdministrationError, match="existing_current_membership_required"):
        await install_native(persistence_sessions)


async def test_another_account_reuses_identical_foundation_without_mutating_client_or_profile(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await install_existing_identity(persistence_sessions)
    await install_native(persistence_sessions)
    async with persistence_sessions.begin() as session:
        session.add(PrincipalTable(principal_ref="human-b", principal_type="human"))
        await session.flush()
        session.add(
            OrganizationMembershipTable(
                organization_ref="tenant-a",
                membership_ref="membership-human-b",
                principal_ref="human-b",
                principal_type="human",
                source_ref="fixture",
                valid_from=datetime.now(UTC),
            )
        )
    values = {
        **plan_values(),
        "account_ref": "account:second",
        "principal_ref": "human-b",
        "membership_ref": "membership-human-b",
        "external_identity_ref": "external:second",
        "username": "second",
    }
    await install(
        persistence_sessions,
        InstallPlan.model_validate(values),
        actor_ref=OPERATOR,
        idempotency_key="second-account",
        credential_digest_key=DIGEST_KEY,
    )
    async with persistence_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NativeAccountTable)) == 2
        assert await session.scalar(select(func.count()).select_from(NativeClientTable)) == 1
        profile = await session.get(AuthenticationProfileTable, (PROFILE, 1))
        assert profile is not None and profile.config_json["allowed_clients"] == ["maestro-native"]


@pytest.mark.parametrize(
    "uri",
    [
        "http://attacker.example/callback",
        "http://localhost:1234/callback",
        "http://127.0.0.1/callback",
        "https://example.test/*",
        "https://example.test/callback#x",
        "https://example.test/callback?code=existing",
        "https://user:pass@example.test/callback",
        "https://example.test:99999/callback",
        "https://example.test:/callback",
        "https://example.test\\other/callback",
    ],
)
def test_redirect_registration_rejects_ambiguous_or_unsafe_addresses(uri: str) -> None:
    with pytest.raises(ValueError):
        InstallPlan.model_validate({**plan_values(), "redirect_uris": [uri]})


def test_key_generation_and_prepared_rotation_preserve_old_verification(tmp_path: Path) -> None:
    signing = tmp_path / "keys.json"
    encryption = tmp_path / "totp.key"
    generated = generate_keys(signing, encryption)
    assert stat.S_IMODE(signing.stat().st_mode) == stat.S_IMODE(encryption.stat().st_mode) == 0o600
    assert read_encryption_key(encryption)
    original = load_keyring(signing)
    now = int(datetime.now(UTC).timestamp())
    claims = {
        "iss": "https://stage.test",
        "aud": "stage-api",
        "sub": "native",
        "sid": "session",
        "client_id": "maestro",
        "jti": "test",
        "iat": now,
        "exp": now + 60,
        "auth_time": now,
        "acr": "mfa",
        "amr": ["pwd", "otp"],
        "credential_version": 1,
    }
    old_token = original.sign(claims)
    next_file = tmp_path / "next.json"
    result = prepare_key_rotation(signing, next_file)
    rotated = load_keyring(next_file)
    assert result["activation_pending"] and original.active_kid == generated["active_kid"]
    assert rotated.active_kid != original.active_kid
    assert rotated.verify_access(old_token, "https://stage.test", "stage-api")["sub"] == "native"
    raw = json.loads(next_file.read_text())
    assert sum("private_pem" in key for key in raw["keys"]) == 1
    assert "public_pem" in raw["keys"][1]
    with pytest.raises(IdentityAdministrationError):
        generate_keys(signing, encryption)


def test_private_operator_files_reject_public_permissions_and_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "plan.json"
    source.write_text("{}")
    source.chmod(0o644)
    with pytest.raises(IdentityAdministrationError):
        read_private_plan(source)
    source.chmod(0o600)
    assert read_private_plan(source) == b"{}"
    alias = tmp_path / "alias.json"
    alias.symlink_to(source)
    with pytest.raises(OSError):
        read_private_plan(alias)


def test_cli_errors_never_echo_secret_plan(tmp_path: Path) -> None:
    signing = tmp_path / "keys.json"
    encryption = tmp_path / "totp.key"
    generate_keys(signing, encryption)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({**plan_values(), "unexpected_password_field": PASSWORD}))
    plan.chmod(0o600)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("STAGE_")}
    environment.update(STAGE_ENV="test", STAGE_DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/native_auth_admin.py",
            "--environment",
            "test",
            "install",
            "--plan",
            str(plan),
            "--idempotency-key",
            "test",
            "--encryption-key-file",
            str(encryption),
        ],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert result.returncode == 1
    assert PASSWORD not in result.stdout + result.stderr
    assert "native_administration_failed" in result.stderr


@pytest.mark.parametrize("command", ["reset", "disable", "inspect"])
async def test_operator_commands_check_persisted_environment_before_account_access(
    persistence_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    await install_existing_identity(persistence_sessions)
    async with persistence_sessions() as session:
        url = session.get_bind().engine.url.render_as_string(hide_password=False)
    monkeypatch.setenv("STAGE_ENV", "dev")
    monkeypatch.setenv("STAGE_DATABASE_URL", url)
    monkeypatch.setenv("STAGE_NATIVE_AUTH_ENABLED", "false")
    args = argparse.Namespace(command=command, environment="dev", account_ref="unknown")
    with pytest.raises(IdentityAdministrationError, match="identity_database_environment_mismatch"):
        await run(args)
