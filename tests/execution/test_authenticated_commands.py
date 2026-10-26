"""A verified human identity remains current through metadata commit and recovery."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.authenticated import AuthenticatedAuthorizationLoader
from app.execution.contracts import CommandEffect, CommandEnvelope, ExecutionContext
from app.execution.errors import CommitOutcomeUnknown, ContextExpired, RequestDeadlineExceeded
from app.execution.storage import UnitOfWork
from app.features.work.details import update_work_details
from app.identity.administration import RevocationRequest, revoke
from app.identity.authentication import AuthenticationRejected
from app.identity.service import (
    IdentityDenied,
)
from app.models.identity import PrincipalTable
from app.models.identity_foundation import (
    ExternalIdentityTable,
    TrustStateTable,
)
from app.storage.coordination import LockMode, lock_organization
from tests.support.authenticated_commands import (
    audit_snapshot,
    authenticated_context,
    authenticated_kernel,
    identity_service,
    jwt_client,
    seed_authenticated_command,
)
from tests.support.authorization import ORG, PERSON
from tests.support.database_commands import counts
from tests.support.identity_tokens import token
from tests.support.work_details import assert_unchanged, command


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


async def test_refreshed_credentials_replay_the_same_command_without_rewriting_original_evidence(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    await seed_authenticated_command(persistence_sessions)
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        engine = authenticated_kernel(persistence_sessions, identity)
        first_bearer = token(signing_key, claims={"jti": "first-login-token"})
        first_context = await authenticated_context(identity, first_bearer)
        first = await engine.execute(first_context, command())
        original_audit = await audit_snapshot(persistence_sessions)
        refreshed_bearer = token(signing_key, claims={"jti": "refreshed-login-token"})
        refreshed = replace(await authenticated_context(identity, refreshed_bearer), request_id="request:retry")
        assert first_context.authentication is not None and refreshed.authentication is not None
        assert first_context.authentication.credential.evidence_ref != refreshed.authentication.credential.evidence_ref
        replay = await engine.execute(refreshed, command())
        assert replay.replayed and replay.execution_id == first.execution_id
        assert await counts(persistence_sessions) == (2, 1, 1, 0)
        assert (await engine.get_result(refreshed, first.execution_id)).result == first.result
        assert await audit_snapshot(persistence_sessions) == original_audit
        # The audit correlates the original authentication, not the token used to read it later.
        assert first_context.authentication.credential.evidence_ref in str(original_audit)
        assert refreshed.authentication.credential.evidence_ref not in str(original_audit)
        assert first_bearer not in str(original_audit) and refreshed_bearer not in str(original_audit)
        assert "subject-1" not in str(original_audit)


@pytest.mark.parametrize("target", ["principal", "membership", "profile", "mapping", "trust"])
async def test_issued_identity_does_not_survive_revocation_before_command_transaction(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey, target: str
) -> None:
    await seed_authenticated_command(persistence_sessions)
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        context = await authenticated_context(identity, token(signing_key))
        if target in {"principal", "membership", "profile"}:
            request = RevocationRequest.model_validate(
                {
                    "environment": "test",
                    "kind": target,
                    "target_ref": {
                        "principal": PERSON,
                        "membership": "membership:alice",
                        "profile": "local-human",
                    }[target],
                    "expected_version": 1,
                    "organization_ref": ORG if target == "membership" else None,
                    "profile_version": 1 if target == "profile" else None,
                }
            )
            await revoke(persistence_sessions, request, actor_ref="operator", idempotency_key="revoke-before-command")
        else:
            # Mapping/key changes have no public administration API in this slice.
            # Model their trusted writer with the same lock order as the command.
            async with persistence_sessions.begin() as session:
                trust = await session.scalar(select(TrustStateTable).with_for_update())
                assert trust is not None
                await session.scalar(
                    select(PrincipalTable).where(PrincipalTable.principal_ref == PERSON).with_for_update()
                )
                await lock_organization(session, ORG, mode=LockMode.EXCLUSIVE)
                if target == "trust":
                    trust.key_status_revision += 1
                    trust.version += 1
                else:
                    mapping = await session.get(ExternalIdentityTable, "identity")
                    assert mapping is not None
                    mapping.status = "revoked"
                    mapping.version += 1
        with pytest.raises(AuthenticationRejected if target == "trust" else IdentityDenied):
            await authenticated_kernel(persistence_sessions, identity).execute(context, command())
    await assert_unchanged(persistence_sessions)


async def test_authentication_expiry_during_handler_rolls_back_work_result_and_audit(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    await seed_authenticated_command(persistence_sessions)
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        context = await authenticated_context(identity, token(signing_key))
        assert context.authentication is not None
        now = [datetime.now(UTC)]

        async def expire_after_update(
            uow: UnitOfWork, ctx: ExecutionContext, envelope: CommandEnvelope, payload: BaseModel
        ) -> CommandEffect:
            effect = await update_work_details(uow, ctx, envelope, payload)
            now[0] = context.credential_expires_at
            return effect

        engine = authenticated_kernel(persistence_sessions, identity, handler=expire_after_update, clock=lambda: now[0])
        with pytest.raises(AuthenticationRejected, match="identity_evidence_expired"):
            await engine.execute(context, command())
    await assert_unchanged(persistence_sessions)


async def test_expiry_while_final_revalidation_awaits_is_checked_again_before_commit(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_authenticated_command(persistence_sessions)
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        context = await authenticated_context(identity, token(signing_key))
        now = [datetime.now(UTC)]
        original_validate = AuthenticatedAuthorizationLoader.validate_before_commit

        async def expire_after_validation(
            self: AuthenticatedAuthorizationLoader, session: AsyncSession, ctx: ExecutionContext, sampled_at: datetime
        ) -> None:
            await original_validate(self, session, ctx, sampled_at)
            # A validation query can finish after the time sampled before its await.
            now[0] = context.credential_expires_at

        monkeypatch.setattr(AuthenticatedAuthorizationLoader, "validate_before_commit", expire_after_validation)
        engine = authenticated_kernel(persistence_sessions, identity, clock=lambda: now[0])
        with pytest.raises(ContextExpired):
            await engine.execute(context, command())
    await assert_unchanged(persistence_sessions)


async def test_request_deadline_rolls_back_without_reporting_valid_authentication_as_expired(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    await seed_authenticated_command(persistence_sessions)
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        authenticated = await authenticated_context(identity, token(signing_key))
        now = [datetime.now(UTC)]
        deadline = now[0] + timedelta(seconds=2)
        context = replace(authenticated, deadline=deadline)

        async def exceed_deadline(
            uow: UnitOfWork, ctx: ExecutionContext, envelope: CommandEnvelope, payload: BaseModel
        ) -> CommandEffect:
            effect = await update_work_details(uow, ctx, envelope, payload)
            now[0] = deadline
            return effect

        engine = authenticated_kernel(persistence_sessions, identity, handler=exceed_deadline, clock=lambda: now[0])
        with pytest.raises(RequestDeadlineExceeded):
            await engine.execute(context, command())
    await assert_unchanged(persistence_sessions)


async def test_identity_change_inside_handler_cannot_commit_even_after_initial_authorization(
    persistence_sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey
) -> None:
    await seed_authenticated_command(persistence_sessions)
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        context = await authenticated_context(identity, token(signing_key))

        async def invalid_security_mutation(
            uow: UnitOfWork, ctx: ExecutionContext, envelope: CommandEnvelope, payload: BaseModel
        ) -> CommandEffect:
            effect = await update_work_details(uow, ctx, envelope, payload)
            mapping = await uow.session.get(ExternalIdentityTable, "identity")
            assert mapping is not None
            mapping.status = "revoked"
            mapping.version += 1
            return effect

        with pytest.raises(IdentityDenied):
            await authenticated_kernel(persistence_sessions, identity, handler=invalid_security_mutation).execute(
                context, command()
            )
    await assert_unchanged(persistence_sessions)
    async with persistence_sessions() as session:
        mapping = await session.get(ExternalIdentityTable, "identity")
        assert mapping is not None and mapping.status == "active" and mapping.version == 1


async def test_lost_commit_reply_is_recoverable_with_fresh_authentication_and_original_idempotency_key(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_authenticated_command(persistence_sessions)
    original_commit = UnitOfWork._commit_by_owner

    async def lose_commit_reply(uow: UnitOfWork) -> None:
        await original_commit(uow)
        raise ConnectionError("simulated lost COMMIT acknowledgement")

    monkeypatch.setattr(UnitOfWork, "_commit_by_owner", lose_commit_reply)
    async with jwt_client(signing_key) as client:
        identity = identity_service(persistence_sessions, client)
        engine = authenticated_kernel(persistence_sessions, identity)
        with pytest.raises(CommitOutcomeUnknown) as lost:
            await engine.execute(await authenticated_context(identity, token(signing_key)), command())
        refreshed = await authenticated_context(identity, token(signing_key, claims={"jti": "after-network-reconnect"}))
        recovered = await engine.get_result(refreshed, lost.value.execution_id)
        replay = await engine.execute(refreshed, command())
        assert recovered.execution_id == replay.execution_id == lost.value.execution_id
        assert recovered.result["version"] == 2 and replay.replayed
        assert await counts(persistence_sessions) == (2, 1, 1, 0)
