"""Result reads require current authority without inheriting the old write grant."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.authenticated import AuthenticatedAuthorizationLoader
from app.execution.contracts import ExecutionContext
from app.execution.errors import AuthorizationDenied, ExecutionNotFound, IdempotencyConflict
from app.models.authorization import RelationTupleTable, RoleAssignmentTable
from app.models.work import WorkItemTable
from app.storage.coordination import LockMode, lock_organization
from tests.support.authenticated_commands import (
    authenticated_context,
    authenticated_kernel,
    identity_service,
    jwt_client,
    seed_authenticated_command,
)
from tests.support.authorization import ORG, TARGET
from tests.support.authorization_recovery import (
    _install_reader,
)
from tests.support.database_commands import counts
from tests.support.identity_tokens import token
from tests.support.work_details import command


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


async def test_current_read_assignment_can_recover_after_write_assignment_revocation_without_merging_grants(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    await seed_authenticated_command(persistence_sessions)
    await _install_reader(persistence_sessions)
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        context = await authenticated_context(identity, token(signing_key))
        engine = authenticated_kernel(persistence_sessions, identity)
        completed = await engine.execute(context, command())
        async with persistence_sessions.begin() as session:
            await lock_organization(session, ORG, mode=LockMode.EXCLUSIVE)
            assignment = await session.scalar(
                select(RoleAssignmentTable).where(RoleAssignmentTable.assignment_ref == context.assignment_ref)
            )
            assert assignment is not None
            assignment.status = "revoked"
            assignment.revoked_at = datetime.now(UTC)
            assignment.version += 1
        with pytest.raises(AuthorizationDenied):
            await engine.get_result(context, completed.execution_id)
        reader = replace(context, assignment_ref="assignment:reader")
        assert (await engine.get_result(reader, completed.execution_id)).result == completed.result
        recovered = await engine.get_result_by_key(
            reader,
            command_name=command().name,
            command_version=command().version,
            idempotency_key=command().idempotency_key,
            target_ref=TARGET,
        )
        assert recovered.execution_id == completed.execution_id
        # A read of historical evidence is distinct from replaying a write
        # under another selected assignment or creating a fresh write.
        with pytest.raises(IdempotencyConflict):
            await engine.execute(reader, command())
        with pytest.raises(AuthorizationDenied):
            await engine.execute(reader, command(expected_version=2, idempotency_key="reader-cannot-write"))
    assert await counts(persistence_sessions) == (2, 1, 1, 0)


async def test_another_authenticated_reader_cannot_recover_the_original_callers_operation(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    await seed_authenticated_command(persistence_sessions)
    await _install_reader(persistence_sessions, another_person=True)
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        engine = authenticated_kernel(persistence_sessions, identity)
        completed = await engine.execute(await authenticated_context(identity, token(signing_key)), command())
        other = replace(
            await authenticated_context(identity, token(signing_key, claims={"sub": "subject-2"})),
            assignment_ref="assignment:reader",
        )
        with pytest.raises(ExecutionNotFound):
            await engine.get_result(other, completed.execution_id)
        with pytest.raises(ExecutionNotFound):
            await engine.get_result_by_key(
                other,
                command_name=command().name,
                command_version=command().version,
                idempotency_key=command().idempotency_key,
                target_ref=TARGET,
            )
    assert await counts(persistence_sessions) == (2, 1, 1, 0)


@pytest.mark.parametrize("missing", ["permission", "relation", "scope", "classification"])
async def test_stale_object_version_does_not_disclose_conflict_without_current_action_authority(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey, missing: str
) -> None:
    await seed_authenticated_command(persistence_sessions)
    await _install_reader(persistence_sessions)
    async with persistence_sessions.begin() as session:
        await lock_organization(session, ORG, mode=LockMode.EXCLUSIVE)
        if missing == "relation":
            relation = await session.scalar(select(RelationTupleTable))
            assert relation is not None
            relation.relation = relation.relation_type = "viewer_of"
        elif missing == "scope":
            assignment = await session.scalar(
                select(RoleAssignmentTable).where(RoleAssignmentTable.assignment_ref == "assignment:engineering")
            )
            assert assignment is not None
            assignment.scope_ref = "scope:finance"
        elif missing == "classification":
            work = await session.get(WorkItemTable, 1)
            assert work is not None
            work.classification = "restricted"
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        context = await authenticated_context(identity, token(signing_key))
        if missing == "permission":
            context = replace(context, assignment_ref="assignment:reader")
        with pytest.raises(AuthorizationDenied) as rejected:
            await authenticated_kernel(persistence_sessions, identity).execute(context, command(expected_version=2))
        assert type(rejected.value) is AuthorizationDenied
    assert await counts(persistence_sessions) == (1, 0, 0, 0)


@pytest.mark.parametrize("read_path", ["replay", "execution_id", "original_key"])
async def test_read_and_replay_resample_assignment_expiry_after_final_identity_validation(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
    read_path: str,
) -> None:
    await seed_authenticated_command(persistence_sessions)
    now = [datetime.now(UTC)]
    expiry = now[0] + timedelta(seconds=10)
    async with persistence_sessions.begin() as session:
        assignment = await session.scalar(select(RoleAssignmentTable))
        assert assignment is not None
        assignment.expires_at = expiry
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        engine = authenticated_kernel(persistence_sessions, identity, clock=lambda: now[0])
        context = await authenticated_context(identity, token(signing_key))
        now[0] = datetime.now(UTC)
        completed = await engine.execute(context, command())
        validate = AuthenticatedAuthorizationLoader.validate_before_commit

        async def expiry_after_validation(
            self: AuthenticatedAuthorizationLoader, session: AsyncSession, ctx: ExecutionContext, sampled_at: datetime
        ) -> None:
            await validate(self, session, ctx, sampled_at)
            now[0] = expiry

        monkeypatch.setattr(AuthenticatedAuthorizationLoader, "validate_before_commit", expiry_after_validation)
        with pytest.raises(AuthorizationDenied):
            if read_path == "replay":
                await engine.execute(context, command())
            elif read_path == "execution_id":
                await engine.get_result(context, completed.execution_id)
            else:
                await engine.get_result_by_key(
                    context,
                    command_name=command().name,
                    command_version=command().version,
                    idempotency_key=command().idempotency_key,
                    target_ref=TARGET,
                )
    assert await counts(persistence_sessions) == (2, 1, 1, 0)
