"""OAuth registration is recoverable and grants no implicit Stage authentication."""

import argparse
import asyncio
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.identity.administration import IdentityAdministrationError, IdentityOperationUnknown, operation_result
from app.identity.assurance_administration import ConfigureHumanAuthenticationRequest, configure_human_authentication
from app.identity.native import NATIVE_ACR
from app.identity.service import profile_content
from app.models.execution import TenantGuardTable
from app.models.identity_foundation import (
    AuthenticationProfileTable,
    IdentityControlOperationTable,
    OrganizationAuthenticationBindingTable,
    OrganizationHumanAuthenticationPolicyTable,
)
from app.models.native_auth import NativeClientTable, NativeSessionTable
from app.native_auth.client_administration import (
    CONFIGURE_OPERATION,
    AdmitClientPlan,
    RotateClientSecretPlan,
    admit_client,
    configure_client,
    rotate_client_secret,
)
from app.native_auth.crypto import digest_secret, random_secret
from scripts.native_oauth_admin import run
from tests.support.identity_administration import OPERATOR
from tests.support.identity_administration import install as install_existing_identity
from tests.support.native_administration import install_native
from tests.support.native_clients import (
    configuration,
)


def admission(**changes: Any) -> AdmitClientPlan:
    return AdmitClientPlan.model_validate(
        {
            "environment": "test",
            "organization_ref": "tenant-a",
            "client_id": "knowledge-web",
            "expected_client_version": 1,
            "profile_ref": "knowledge-web-native",
            "binding_ref": "knowledge-web-native-binding",
            "expected_policy_version": 0,
            **changes,
        }
    )


def rotation(**changes: Any) -> RotateClientSecretPlan:
    return RotateClientSecretPlan.model_validate(
        {
            "environment": "test",
            "organization_ref": "tenant-a",
            "client_id": "knowledge-web",
            "expected_version": 1,
            **changes,
        }
    )


async def setup(sessions: async_sessionmaker[AsyncSession]) -> None:
    await install_existing_identity(sessions)
    await install_native(sessions)


async def configure(sessions: async_sessionmaker[AsyncSession], *, key: str = "configure", **changes: Any) -> Any:
    secret_hash = changes.pop("client_secret_hash", None)
    return await configure_client(
        sessions, configuration(**changes), actor_ref=OPERATOR, idempotency_key=key, client_secret_hash=secret_hash
    )


async def test_registration_defaults_to_consent_and_userinfo_without_changing_admission(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await setup(persistence_sessions)
    async with persistence_sessions() as session:
        profile = await session.get(AuthenticationProfileTable, ("stage-native-human", 1))
        assert profile is not None
        before = profile_content(profile)
    first = await configure(persistence_sessions)
    assert await configure(persistence_sessions) == first
    assert first.result["resource_audience"] == "stage-userinfo"
    assert first.result["require_consent"] is True
    assert first.result["stage_admission_changed"] is False
    async with persistence_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NativeClientTable)) == 2
        profile = await session.get(AuthenticationProfileTable, ("stage-native-human", 1))
        assert profile is not None and profile_content(profile) == before
        assert await session.get(AuthenticationProfileTable, ("knowledge-web-native", 1)) is None
    with pytest.raises(IdentityAdministrationError, match="explicit_stage_api_audience_required"):
        await admit_client(persistence_sessions, admission(), actor_ref=OPERATOR, idempotency_key="admit")


async def test_configuration_replay_requires_same_content_and_current_version(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await setup(persistence_sessions)
    await configure(persistence_sessions)
    with pytest.raises(IdentityAdministrationError, match="idempotency_conflict"):
        await configure(persistence_sessions, display_name="changed")
    with pytest.raises(IdentityAdministrationError, match="version_conflict"):
        await configure(persistence_sessions, key="stale", expected_version=0, enabled=False)
    with pytest.raises(IdentityAdministrationError, match="client_authentication_method_immutable"):
        await configure(
            persistence_sessions, key="method", expected_version=1, token_endpoint_auth_method="client_secret_basic"
        )


async def add_session(sessions: async_sessionmaker[AsyncSession], client_id: str, session_ref: str) -> None:
    now = datetime.now(UTC)
    async with sessions.begin() as session:
        session.add(
            NativeSessionTable(
                session_ref=session_ref,
                account_ref="account:native-human",
                client_id=client_id,
                credential_version=1,
                auth_time=now,
                expires_at=now + timedelta(hours=1),
                acr=NATIVE_ACR,
                amr=["pwd", "otp"],
            )
        )


async def test_disable_and_reenable_never_resurrect_old_sessions_or_revoke_other_clients(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await setup(persistence_sessions)
    await configure(persistence_sessions)
    await add_session(persistence_sessions, "knowledge-web", "old-session")
    await add_session(persistence_sessions, "maestro-native", "unrelated-session")
    disabled = await configure(persistence_sessions, key="disable", expected_version=1, enabled=False)
    enabled = await configure(persistence_sessions, key="enable", expected_version=2, enabled=True)
    assert disabled.result["version"] == 2 and enabled.result["version"] == 3
    async with persistence_sessions() as session:
        old = await session.get(NativeSessionTable, "old-session")
        unrelated = await session.get(NativeSessionTable, "unrelated-session")
        assert old is not None and old.revoked_at is not None
        assert unrelated is not None and unrelated.revoked_at is None


async def test_confidential_secret_rotation_invalidates_existing_sessions_and_journals_no_credential(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await setup(persistence_sessions)
    secret, replacement = random_secret(), random_secret()
    await configure(
        persistence_sessions, token_endpoint_auth_method="client_secret_basic", client_secret_hash=digest_secret(secret)
    )
    await add_session(persistence_sessions, "knowledge-web", "old-session")
    first = await rotate_client_secret(
        persistence_sessions,
        rotation(),
        actor_ref=OPERATOR,
        idempotency_key="rotate",
        client_secret_hash=digest_secret(replacement),
    )
    assert (
        await rotate_client_secret(
            persistence_sessions,
            rotation(),
            actor_ref=OPERATOR,
            idempotency_key="rotate",
            client_secret_hash=digest_secret(replacement),
        )
        == first
    )
    assert first.result["version"] == 2
    async with persistence_sessions() as session:
        client = await session.get(NativeClientTable, "knowledge-web")
        old = await session.get(NativeSessionTable, "old-session")
        journal = await session.get(IdentityControlOperationTable, first.operation_ref)
        assert client is not None and client.client_secret_hash == digest_secret(replacement)
        assert old is not None and old.revoked_at is not None
        assert journal is not None
        stored = json.dumps([journal.before_json, journal.after_json, journal.result_json])
        assert all(value not in stored for value in (secret, replacement, digest_secret(replacement)))


@pytest.mark.parametrize("secret", [None, "short", "g" * 64, "F" * 64])
async def test_confidential_registration_requires_generated_secret_digest(
    persistence_sessions: async_sessionmaker[AsyncSession],
    secret: str | None,
) -> None:
    with pytest.raises(IdentityAdministrationError, match="generated_client_secret_required"):
        await configure(
            persistence_sessions, token_endpoint_auth_method="client_secret_basic", client_secret_hash=secret
        )


async def test_public_client_cannot_acquire_secret_by_rotation(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await setup(persistence_sessions)
    await configure(persistence_sessions)
    with pytest.raises(IdentityAdministrationError, match="confidential_client_required"):
        await rotate_client_secret(
            persistence_sessions,
            rotation(),
            actor_ref=OPERATOR,
            idempotency_key="rotate",
            client_secret_hash=digest_secret(random_secret()),
        )


async def enable_mfa(sessions: async_sessionmaker[AsyncSession]) -> None:
    await configure_human_authentication(
        sessions,
        ConfigureHumanAuthenticationRequest.model_validate(
            {
                "environment": "test",
                "organization_ref": "tenant-a",
                "expected_version": 0,
                "policy": {
                    "mode": "required",
                    "max_auth_age_seconds": 900,
                    "rules": [
                        {
                            "profile_ref": "stage-native-human",
                            "profile_version": 1,
                            "required_acr": NATIVE_ACR,
                            "required_amr": ["pwd", "otp"],
                        }
                    ],
                },
            }
        ),
        actor_ref=OPERATOR,
        idempotency_key="enable-mfa",
    )


async def test_explicit_admission_creates_separate_immutable_profile_and_preserves_mfa_strength(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await setup(persistence_sessions)
    await configure(persistence_sessions, resource_audience="stage-api")
    await enable_mfa(persistence_sessions)
    async with persistence_sessions() as session:
        old = await session.get(AuthenticationProfileTable, ("stage-native-human", 1))
        assert old is not None
        old_content, old_digest = profile_content(old), old.digest
    plan = admission(expected_policy_version=1)
    result = await admit_client(persistence_sessions, plan, actor_ref=OPERATOR, idempotency_key="admit")
    assert await admit_client(persistence_sessions, plan, actor_ref=OPERATOR, idempotency_key="admit") == result
    assert result.result["business_grants"] == [] and result.result["assurance_version"] == 2
    async with persistence_sessions() as session:
        old = await session.get(AuthenticationProfileTable, ("stage-native-human", 1))
        new = await session.get(AuthenticationProfileTable, ("knowledge-web-native", 1))
        binding = await session.get(
            OrganizationAuthenticationBindingTable, ("tenant-a", "knowledge-web-native-binding")
        )
        policy = await session.get(OrganizationHumanAuthenticationPolicyTable, "tenant-a")
        assert old is not None and profile_content(old) == old_content and old.digest == old_digest
        assert new is not None and new.config_json["allowed_clients"] == ["knowledge-web"]
        assert binding is not None and binding.constraints_json["allowed_clients"] == ["knowledge-web"]
        assert policy is not None and policy.mode == "required" and policy.max_auth_age_seconds == 900
        assert len(policy.rules_json) == 2
        assert all(
            rule["required_acr"] == NATIVE_ACR and rule["required_amr"] == ["pwd", "otp"] for rule in policy.rules_json
        )


@pytest.mark.parametrize(
    "change,error",
    [
        ({"expected_policy_version": 2}, "assurance_version_conflict"),
        ({"expected_client_version": 2}, "version_conflict"),
        ({"profile_ref": "stage-native-human"}, "native_admission_already_exists"),
    ],
)
async def test_admission_rejects_stale_policy_and_existing_profile_without_partial_grants(
    persistence_sessions: async_sessionmaker[AsyncSession],
    change: dict[str, Any],
    error: str,
) -> None:
    await setup(persistence_sessions)
    await configure(persistence_sessions, resource_audience="stage-api")
    with pytest.raises(IdentityAdministrationError, match=error):
        await admit_client(persistence_sessions, admission(**change), actor_ref=OPERATOR, idempotency_key="admit")
    async with persistence_sessions() as session:
        assert (
            await session.get(OrganizationAuthenticationBindingTable, ("tenant-a", "knowledge-web-native-binding"))
            is None
        )


@pytest.mark.parametrize("operation", ["configure", "rotate", "admit"])
async def test_client_changes_and_revocation_roll_back_if_journal_fails(
    persistence_sessions: async_sessionmaker[AsyncSession],
    operation: str,
) -> None:
    await setup(persistence_sessions)
    if operation != "configure":
        await configure(
            persistence_sessions,
            resource_audience="stage-api",
            token_endpoint_auth_method="client_secret_basic",
            client_secret_hash=digest_secret(random_secret()),
        )
        await add_session(persistence_sessions, "knowledge-web", "prior-session")
    async with persistence_sessions() as session:
        guard = await session.get(TenantGuardTable, "tenant-a")
        assert guard is not None
        epoch = guard.epoch
    engine = persistence_sessions.kw["bind"]

    def fail_journal(*args: Any) -> None:
        if "INSERT INTO identity_control_operation" in args[2]:
            raise RuntimeError("journal-unavailable")

    event.listen(engine.sync_engine, "before_cursor_execute", fail_journal)
    try:
        with pytest.raises(RuntimeError, match="journal-unavailable"):
            if operation == "configure":
                await configure(persistence_sessions)
            elif operation == "rotate":
                await rotate_client_secret(
                    persistence_sessions,
                    rotation(),
                    actor_ref=OPERATOR,
                    idempotency_key="rotate",
                    client_secret_hash=digest_secret(random_secret()),
                )
            else:
                await admit_client(persistence_sessions, admission(), actor_ref=OPERATOR, idempotency_key="admit")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail_journal)
    async with persistence_sessions() as session:
        guard = await session.get(TenantGuardTable, "tenant-a")
        assert guard is not None and guard.epoch == epoch
        client = await session.get(NativeClientTable, "knowledge-web")
        if operation == "configure":
            assert client is None
        else:
            old = await session.get(NativeSessionTable, "prior-session")
            assert client is not None and client.version == 1 and old is not None and old.revoked_at is None
        assert await session.get(AuthenticationProfileTable, ("knowledge-web-native", 1)) is None


async def test_lost_commit_result_recovers_with_same_key_without_replacing_secret(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await setup(persistence_sessions)

    class LostCommitSession(AsyncSession):
        async def __aexit__(self, *args: Any) -> None:
            await super().__aexit__(*args)
            raise SQLAlchemyError("response lost after committed transaction")

    uncertain: async_sessionmaker[AsyncSession] = async_sessionmaker(
        persistence_sessions.kw["bind"],
        class_=LostCommitSession,
        expire_on_commit=False,
    )
    secret_hash = digest_secret(random_secret())
    with pytest.raises(IdentityOperationUnknown) as failure:
        await configure(uncertain, token_endpoint_auth_method="client_secret_basic", client_secret_hash=secret_hash)
    assert failure.value.operation_type == CONFIGURE_OPERATION
    recovered = await operation_result(
        persistence_sessions,
        environment="test",
        actor_ref=OPERATOR,
        operation_type=CONFIGURE_OPERATION,
        idempotency_key="configure",
    )
    assert (
        await configure(
            persistence_sessions, token_endpoint_auth_method="client_secret_basic", client_secret_hash=secret_hash
        )
        == recovered
    )


async def test_competing_updates_cannot_overwrite_the_same_client_version(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await setup(persistence_sessions)
    await configure(persistence_sessions)
    results = await asyncio.gather(
        configure(persistence_sessions, key="one", expected_version=1, display_name="First"),
        configure(persistence_sessions, key="two", expected_version=1, display_name="Second"),
        return_exceptions=True,
    )
    rejected = [value for value in results if isinstance(value, IdentityAdministrationError)]
    assert len(rejected) == 1 and rejected[0].code == "version_conflict"


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com/callback#",
        "https://example.com/callback#fragment",
        "https://example.com/callback?state=",
        "https://example.com/callback?iss=issuer",
        "https://example.com/callback?error_description=x",
        "https://example.com/callback?error_uri=x",
        "https://example.com/callback?%63ode=bad",
        "https://example.com/*",
        "http://example.com/callback",
        "http://127.0.0.1/callback",
        "https://EXAMPLE.com/callback",
        "https://example.com:443/callback",
        "https://example.com:/callback",
        "https://user@example.com/callback",
        "https://example.com/call\\back",
        "javascript:alert(1)",
    ],
)
@pytest.mark.parametrize("field", ["redirect_uris", "post_logout_redirect_uris"])
def test_redirects_reject_ambiguous_or_protocol_parameter_conflicts(uri: str, field: str) -> None:
    with pytest.raises(ValueError):
        configuration(**{field: [uri]})


@pytest.mark.parametrize(
    "changes",
    [
        {"allowed_scopes": ["profile"]},
        {"allowed_scopes": ["openid", "openid"]},
        {"allowed_scopes": ["openid", "admin"]},
        {"client_id": "人" * 43},
        {"display_name": "unsafe\x00name"},
        {"expected_version": True},
    ],
)
def test_invalid_client_metadata_is_rejected_before_storage(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        configuration(**changes)


async def cli_args(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> argparse.Namespace:
    await setup(sessions)
    monkeypatch.setenv("STAGE_DATABASE_URL", sessions.kw["bind"].url.render_as_string(hide_password=False))
    monkeypatch.setenv("STAGE_ENV", "test")
    monkeypatch.setenv("STAGE_NATIVE_AUTH_ENABLED", "false")
    # Test authority is explicit even when the shell loads a live deployment env.
    monkeypatch.setenv("STAGE_NATIVE_AUTH_ORGANIZATION_REF", "tenant-a")
    monkeypatch.setenv("STAGE_NATIVE_AUTH_ISSUER", "http://127.0.0.1:8080")
    plan = tmp_path / "client-plan.json"
    plan.write_text(configuration(token_endpoint_auth_method="client_secret_basic").model_dump_json())
    plan.chmod(0o600)
    return argparse.Namespace(
        command="configure",
        environment="test",
        plan=plan,
        idempotency_key="cli-configure",
        secret_output=tmp_path / "client-secret.json",
        secret_input=None,
    )


async def test_cli_secret_is_private_reusable_after_unknown_result_and_absent_from_output(
    persistence_sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = await cli_args(persistence_sessions, tmp_path, monkeypatch)
    result = await run(args)
    material = json.loads(args.secret_output.read_text())
    secret = material["client_secret"]
    assert stat.S_IMODE(args.secret_output.stat().st_mode) == 0o600
    assert secret not in json.dumps(result)
    args.secret_input, args.secret_output = args.secret_output, None
    replay = await run(args)
    assert replay["operation_ref"] == result["operation_ref"]
    async with persistence_sessions() as session:
        client = await session.get(NativeClientTable, "knowledge-web")
        assert client is not None and client.client_secret_hash == digest_secret(secret)


@pytest.mark.parametrize("problem", ["missing", "exists", "delivery-failed"])
async def test_cli_cannot_change_db_before_private_secret_is_persisted(
    persistence_sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    args = await cli_args(persistence_sessions, tmp_path, monkeypatch)
    if problem == "missing":
        args.secret_output = None
    elif problem == "exists":
        args.secret_output.write_text("keep-existing-material")
    else:
        import os

        def fail_sync(*_: Any) -> None:
            raise OSError("synthetic output failure")

        monkeypatch.setattr(os, "fsync", fail_sync)
    with pytest.raises((IdentityAdministrationError, OSError)):
        await run(args)
    async with persistence_sessions() as session:
        assert await session.get(NativeClientTable, "knowledge-web") is None
    if problem == "exists":
        assert args.secret_output.read_text() == "keep-existing-material"


async def test_cli_retry_secret_must_match_the_exact_operation_and_plan(
    persistence_sessions: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = await cli_args(persistence_sessions, tmp_path, monkeypatch)
    await run(args)
    args.secret_input, args.secret_output = args.secret_output, None
    args.idempotency_key = "different-operation"
    with pytest.raises(IdentityAdministrationError, match="client_secret_retry_identity_mismatch"):
        await run(args)


@pytest.mark.parametrize("operation", ["configure", "rotate", "admit"])
@pytest.mark.parametrize("bad_identity", ["bad\x00key", "bad key", "\ud800"])
async def test_control_operations_reject_invalid_journal_identity_before_storage(
    persistence_sessions: async_sessionmaker[AsyncSession],
    operation: str,
    bad_identity: str,
) -> None:
    with pytest.raises(IdentityAdministrationError, match="invalid_operation_identity"):
        if operation == "configure":
            await configure_client(
                persistence_sessions, configuration(), actor_ref=OPERATOR, idempotency_key=bad_identity
            )
        elif operation == "rotate":
            await rotate_client_secret(
                persistence_sessions,
                rotation(),
                actor_ref=OPERATOR,
                idempotency_key=bad_identity,
                client_secret_hash=digest_secret(random_secret()),
            )
        else:
            await admit_client(persistence_sessions, admission(), actor_ref=OPERATOR, idempotency_key=bad_identity)


async def test_postgresql_client_disable_waits_for_existing_identity_readers(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    from app.models.identity_foundation import TrustStateTable

    engine = persistence_sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL trust-row lock ordering")
    await setup(persistence_sessions)
    await configure(persistence_sessions)
    attempted = asyncio.Event()

    def query_started(*args: Any) -> None:
        if "FROM trust_state" in args[2] and "FOR UPDATE" in args[2]:
            attempted.set()

    event.listen(engine.sync_engine, "before_cursor_execute", query_started)
    task: asyncio.Task[Any] | None = None
    try:
        async with persistence_sessions() as reader:
            await reader.scalar(
                select(TrustStateTable)
                .where(TrustStateTable.trust_ref == "trust:native-auth")
                .with_for_update(read=True)
            )
            task = asyncio.create_task(
                configure(persistence_sessions, key="disable", expected_version=1, enabled=False)
            )
            await asyncio.wait_for(attempted.wait(), timeout=5)
            await asyncio.sleep(0.05)
            assert not task.done()
            client = await reader.get(NativeClientTable, "knowledge-web")
            assert client is not None and client.enabled
            await reader.commit()
        result = await asyncio.wait_for(task, timeout=5)
        assert result.result["enabled"] is False
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", query_started)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_postgresql_identity_reader_after_client_mutation_waits_for_commit(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    from app.models.identity_foundation import TrustStateTable

    engine = persistence_sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL trust-row lock ordering")
    await setup(persistence_sessions)
    await configure(persistence_sessions)
    mutated, release, read_attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class HeldMutationSession(AsyncSession):
        async def flush(self, objects: Any = None) -> None:
            await super().flush(objects)
            mutated.set()
            await release.wait()

    held_sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        class_=HeldMutationSession,
        expire_on_commit=False,
    )

    async def read_client() -> bool:
        async with persistence_sessions() as session:
            await session.scalar(
                select(TrustStateTable)
                .where(TrustStateTable.trust_ref == "trust:native-auth")
                .with_for_update(read=True)
            )
            client = await session.get(NativeClientTable, "knowledge-web")
            assert client is not None
            return client.enabled

    def query_started(*args: Any) -> None:
        if "FROM trust_state" in args[2] and "FOR SHARE" in args[2]:
            read_attempted.set()

    event.listen(engine.sync_engine, "before_cursor_execute", query_started)
    writer = asyncio.create_task(configure(held_sessions, key="disable", expected_version=1, enabled=False))
    reader: asyncio.Task[bool] | None = None
    try:
        await asyncio.wait_for(mutated.wait(), timeout=5)
        reader = asyncio.create_task(read_client())
        await asyncio.wait_for(read_attempted.wait(), timeout=5)
        await asyncio.sleep(0.05)
        assert not reader.done()
        release.set()
        await asyncio.wait_for(writer, timeout=5)
        assert await asyncio.wait_for(reader, timeout=5) is False
    finally:
        release.set()
        event.remove(engine.sync_engine, "before_cursor_execute", query_started)
        for task in (writer, reader):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*[task for task in (writer, reader) if task is not None], return_exceptions=True)


@pytest.mark.parametrize("field", ["profile_ref", "binding_ref"])
@pytest.mark.parametrize("value", ["人" * 43, "😀" * 33])
def test_admission_identifiers_must_fit_the_token_reference_utf8_budget(field: str, value: str) -> None:
    with pytest.raises(ValueError, match="native_identifier_exceeds_utf8_limit"):
        admission(**{field: value})
