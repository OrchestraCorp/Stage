import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.context import AuthorizationContext
from app.core.enums import PrincipalType
from app.execution.contracts import (
    CommandEffect,
    CommandEnvelope,
    ExecutionContext,
)
from app.execution.errors import (
    AuthorizationDenied,
    CommitOutcomeUnknown,
    ContextExpired,
    ExecutionNotFound,
    IdempotencyConflict,
    InvalidCommand,
    TenantNotProvisioned,
    TransactionOwnershipError,
)
from app.execution.kernel import CommandKernel
from app.execution.storage import UnitOfWork
from app.models.execution import OutboxRecordTable
from app.models.identity import PrincipalTable
from app.storage.coordination import LockMode, lock_organization
from tests.support.command_kernel import (
    TestFactsLoader,
    claim,
    command,
    context,
    counts,
    seed,
    spec,
)


def kernel(sessions: async_sessionmaker[AsyncSession], **kwargs: Any) -> CommandKernel:
    return CommandKernel(sessions, loader=TestFactsLoader(), commands=[spec()], **kwargs)


async def test_atomic_result_audit_outbox_and_replay(persistence_sessions: async_sessionmaker[AsyncSession]) -> None:
    sessions = persistence_sessions
    await seed(sessions)
    engine = kernel(sessions)
    first = await engine.execute(context(), command())
    second = await engine.execute(replace(context(), request_id="request:2"), command())
    assert first.result == {"version": 2}
    assert not first.replayed and second.replayed and first.execution_id == second.execution_id
    assert await counts(sessions) == (2, 1, 1, 1)
    assert (await engine.get_result(context(), first.execution_id)).result == first.result


async def test_changed_payload_or_authority_selection_conflicts(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = persistence_sessions
    await seed(sessions)
    engine = kernel(sessions)
    await engine.execute(context(), command())
    with pytest.raises(IdempotencyConflict):
        await engine.execute(context(), replace(command(), payload={"note": "different"}))
    with pytest.raises(IdempotencyConflict):
        await engine.execute(context(), replace(command(), expected_version=2))
    assert await counts(sessions) == (2, 1, 1, 1)


async def test_replay_and_result_lookup_reauthorize_after_revocation(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = persistence_sessions
    await seed(sessions)
    engine = kernel(sessions)
    first = await engine.execute(context(), command())
    async with sessions.begin() as session:
        await lock_organization(session, "tenant:a", mode=LockMode.EXCLUSIVE)
        principal = await session.scalar(select(PrincipalTable).where(PrincipalTable.principal_ref == "person:1"))
        assert principal is not None
        principal.status = "revoked"
    with pytest.raises(AuthorizationDenied):
        await engine.execute(context(), command())
    with pytest.raises(AuthorizationDenied):
        await engine.get_result(context(), first.execution_id)
    assert await counts(sessions) == (2, 1, 1, 1)


async def test_tenant_and_actor_result_isolation(persistence_sessions: async_sessionmaker[AsyncSession]) -> None:
    sessions = persistence_sessions
    await seed(sessions)
    engine = kernel(sessions)
    first = await engine.execute(context(), command())
    with pytest.raises(ExecutionNotFound):
        await engine.get_result(replace(context(), tenant_ref="tenant:b"), first.execution_id)
    with pytest.raises(ExecutionNotFound):
        await engine.get_result(
            replace(context(), actor_ref="person:2", effective_subject_ref="person:2"), first.execution_id
        )
    with pytest.raises(AuthorizationDenied):
        await engine.execute(replace(context(), tenant_ref="tenant:b"), command())
    with pytest.raises(TenantNotProvisioned):
        await engine.execute(replace(context(), tenant_ref="tenant:missing"), command())


@pytest.mark.parametrize(
    "failure",
    [
        "exception",
        "invalid_result",
        "outbox_conflict",
        "commit",
        "uow_commit",
        "rollback",
        "connection_commit",
        "connection_rollback",
    ],
)
@pytest.mark.filterwarnings("ignore:transaction already deassociated from connection:sqlalchemy.exc.SAWarning")
async def test_failed_handler_rolls_back_everything(
    persistence_sessions: async_sessionmaker[AsyncSession],
    failure: str,
) -> None:
    sessions = persistence_sessions
    await seed(sessions)

    async def broken(uow: UnitOfWork, ctx: ExecutionContext, cmd: CommandEnvelope, payload: BaseModel) -> CommandEffect:
        effect = await claim(uow, ctx, cmd, payload)
        await uow.flush()
        if failure == "exception":
            raise RuntimeError("handler failed")
        if failure == "invalid_result":
            return replace(effect, result={"version": "not an integer"})
        if failure == "outbox_conflict":
            return replace(effect, messages=effect.messages * 2)
        if failure == "commit":
            # Even catching the first misuse error must not allow a later commit.
            try:
                await uow.session.commit()
            except TransactionOwnershipError:
                pass
        if failure == "uow_commit":
            await uow.commit()
        if failure == "rollback":
            await uow.session.rollback()
        if failure == "connection_commit":
            await (await uow.session.connection()).commit()
        if failure == "connection_rollback":
            await (await uow.session.connection()).rollback()
        return effect

    engine = CommandKernel(sessions, loader=TestFactsLoader(), commands=[spec(broken)])
    expected: dict[str, type[Exception]] = {
        "exception": RuntimeError,
        "invalid_result": InvalidCommand,
        "outbox_conflict": IntegrityError,
        "commit": TransactionOwnershipError,
        "uow_commit": TransactionOwnershipError,
        "rollback": TransactionOwnershipError,
        "connection_commit": TransactionOwnershipError,
        "connection_rollback": TransactionOwnershipError,
    }
    with pytest.raises(expected[failure]):
        await engine.execute(context(), command())
    assert await counts(sessions) == (1, 0, 0, 0)


async def test_cancel_during_handler_rolls_back(persistence_sessions: async_sessionmaker[AsyncSession]) -> None:
    sessions = persistence_sessions
    await seed(sessions)
    entered = asyncio.Event()

    async def waiting(
        uow: UnitOfWork, ctx: ExecutionContext, cmd: CommandEnvelope, payload: BaseModel
    ) -> CommandEffect:
        effect = await claim(uow, ctx, cmd, payload)
        await uow.flush()
        entered.set()
        await asyncio.Event().wait()
        return effect

    engine = CommandKernel(sessions, loader=TestFactsLoader(), commands=[spec(waiting)])
    task = asyncio.create_task(engine.execute(context(), command()))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await counts(sessions) == (1, 0, 0, 0)


async def test_parallel_same_command_only_commits_once(persistence_sessions: async_sessionmaker[AsyncSession]) -> None:
    sessions = persistence_sessions
    await seed(sessions)
    engine = kernel(sessions)
    results = await asyncio.gather(
        *(engine.execute(replace(context(), request_id=f"r:{i}"), command()) for i in range(8))
    )
    assert len({r.execution_id for r in results}) == 1
    assert sum(not r.replayed for r in results) == 1
    assert await counts(sessions) == (2, 1, 1, 1)


async def test_expired_after_handler_cannot_commit(persistence_sessions: async_sessionmaker[AsyncSession]) -> None:
    sessions = persistence_sessions
    await seed(sessions)
    now = datetime.now(UTC)
    clock = [now]

    async def slow(uow: UnitOfWork, ctx: ExecutionContext, cmd: CommandEnvelope, payload: BaseModel) -> CommandEffect:
        result = await claim(uow, ctx, cmd, payload)
        clock[0] += timedelta(hours=1)
        return result

    engine = CommandKernel(sessions, loader=TestFactsLoader(), commands=[spec(slow)], clock=lambda: clock[0])
    with pytest.raises(ContextExpired):
        await engine.execute(context(), command())
    assert await counts(sessions) == (1, 0, 0, 0)


async def test_commit_reply_loss_returns_unknown_and_reconciles(
    persistence_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = persistence_sessions
    await seed(sessions)
    original_commit = UnitOfWork._commit_by_owner

    async def lose_reply(uow: UnitOfWork) -> None:
        await original_commit(uow)
        raise ConnectionError("simulated lost COMMIT reply")

    monkeypatch.setattr(UnitOfWork, "_commit_by_owner", lose_reply)
    engine = kernel(sessions)
    with pytest.raises(CommitOutcomeUnknown) as error:
        await engine.execute(context(), command())
    assert await counts(sessions) == (2, 1, 1, 1)
    result = await engine.get_result(context(), error.value.execution_id)
    assert result.result == {"version": 2}
    assert (await engine.execute(context(), command())).replayed


async def test_input_validation_and_registration_fail_closed(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = persistence_sessions
    await seed(sessions)
    with pytest.raises(ValueError, match="Duplicate"):
        CommandKernel(sessions, loader=TestFactsLoader(), commands=[spec(), spec()])
    engine = kernel(sessions)
    invalid_payloads: list[dict[str, Any]] = [
        {"note": "ok", "role": "admin"},
        {"note": "x" * 70_000},
        {"note": float("nan")},
    ]
    for payload in invalid_payloads:
        with pytest.raises(InvalidCommand):
            await engine.execute(context(), replace(command(), payload=payload))
    assert await counts(sessions) == (1, 0, 0, 0)


async def test_unknown_commit_survives_rollback_and_close_failures(
    persistence_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = persistence_sessions
    await seed(sessions)
    original_close = AsyncSession.close

    async def fail_commit(uow: UnitOfWork) -> None:
        raise ConnectionError("commit acknowledgement lost")

    async def fail_rollback(session: AsyncSession) -> None:
        raise ConnectionError("connection unavailable during rollback")

    async def fail_close(session: AsyncSession) -> None:
        await original_close(session)
        raise ConnectionError("close diagnostic")

    monkeypatch.setattr(UnitOfWork, "_commit_by_owner", fail_commit)
    monkeypatch.setattr(AsyncSession, "rollback", fail_rollback)
    monkeypatch.setattr(AsyncSession, "close", fail_close)
    engine = kernel(sessions)
    with pytest.raises(CommitOutcomeUnknown) as error:
        await engine.execute(context(), command())
    assert error.value.execution_id
    assert str(error.value) == "COMMIT_OUTCOME_UNKNOWN"
    monkeypatch.undo()
    assert await counts(sessions) == (1, 0, 0, 0)
    with pytest.raises(ExecutionNotFound):
        await engine.get_result(context(), error.value.execution_id)
    assert (await engine.execute(context(), command())).result == {"version": 2}


@pytest.mark.parametrize("expiry", ["assignment", "relation"])
async def test_authority_time_expires_during_handler(
    persistence_sessions: async_sessionmaker[AsyncSession],
    expiry: str,
) -> None:
    sessions = persistence_sessions
    await seed(sessions)
    clock = [datetime.now(UTC)]
    expires_at = clock[0] + timedelta(seconds=1)

    class ExpiringLoader(TestFactsLoader):
        async def load(self, *args: Any, **kwargs: Any) -> AuthorizationContext:
            facts = await super().load(*args, **kwargs)
            if expiry == "assignment":
                assert facts.assignment is not None
                return replace(facts, assignment=replace(facts.assignment, expires_at=expires_at))
            return replace(facts, relations=tuple(replace(r, expires_at=expires_at) for r in facts.relations))

    async def advances(
        uow: UnitOfWork, ctx: ExecutionContext, cmd: CommandEnvelope, payload: BaseModel
    ) -> CommandEffect:
        effect = await claim(uow, ctx, cmd, payload)
        clock[0] += timedelta(seconds=2)
        return effect

    engine = CommandKernel(sessions, loader=ExpiringLoader(), commands=[spec(advances)], clock=lambda: clock[0])
    with pytest.raises(AuthorizationDenied):
        await engine.execute(context(), command())
    assert await counts(sessions) == (1, 0, 0, 0)


@pytest.mark.parametrize("wrong", ["tenant", "actor", "target", "request", "assignment"])
async def test_loader_cannot_mix_authority_bindings(
    persistence_sessions: async_sessionmaker[AsyncSession],
    wrong: str,
) -> None:
    sessions = persistence_sessions
    await seed(sessions)

    class WrongLoader(TestFactsLoader):
        async def load(self, *args: Any, **kwargs: Any) -> AuthorizationContext:
            facts = await super().load(*args, **kwargs)
            if wrong == "tenant":
                return replace(facts, resource_organization_ref="tenant:b")
            if wrong == "actor":
                return replace(facts, subject_ref="person:2")
            if wrong == "target":
                return replace(facts, resource_ref="2")
            if wrong == "request":
                return replace(facts, request_id="stale-request")
            assert facts.assignment is not None
            return replace(facts, assignment=replace(facts.assignment, assignment_ref="unselected"))

    engine = CommandKernel(sessions, loader=WrongLoader(), commands=[spec()])
    with pytest.raises(AuthorizationDenied):
        await engine.execute(context(), command())
    assert await counts(sessions) == (1, 0, 0, 0)


def test_json_budget_rejects_large_shallow_containers_before_serialization(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.execution import encoding

    called = False

    def forbidden_dumps(*args: Any, **kwargs: Any) -> str:
        nonlocal called
        called = True
        raise AssertionError("serialization should not run")

    monkeypatch.setattr(encoding.json, "dumps", forbidden_dumps)
    for oversized in ["x" * 70_000, [None] * 5000, {str(i): None for i in range(5000)}]:
        with pytest.raises(InvalidCommand):
            encoding.json_bytes(oversized)
    assert not called


def test_initial_executor_rejects_creation_version() -> None:
    with pytest.raises(InvalidCommand):
        replace(command(), expected_version=0)


async def test_command_to_delivery_lost_ack_pipeline(persistence_sessions: async_sessionmaker[AsyncSession]) -> None:
    from app.authz.decision import AuthorizationDecision, AuthorizationEngine
    from app.delivery.contracts import (
        DeliveryEnvelope,
        DeliveryReceipt,
        ReconciliationResult,
        ReconciliationStatus,
        SinkCapabilities,
    )
    from app.delivery.worker import OutboxWorker

    sessions = persistence_sessions
    await seed(sessions)
    async with sessions.begin() as session:
        session.add(PrincipalTable(principal_ref="service:delivery", principal_type="service", status="active"))

    class FixtureDeliveryAuthorizer:
        async def authorize(
            self,
            session: AsyncSession,
            *,
            service_ref: str,
            envelope: DeliveryEnvelope,
            operation: str,
            request_id: str,
        ) -> AuthorizationDecision:
            # Fixture-only bridge for the legacy role-based authorization engine.
            # A production ServiceGrant-backed adapter is intentionally not provided.
            execution_context = replace(
                context(),
                actor_ref=service_ref,
                effective_subject_ref=service_ref,
                principal_type=PrincipalType.SERVICE,
                request_id=request_id,
            )
            facts = await TestFactsLoader().load(
                session,
                execution_context,
                action=envelope.read_action,
                target_ref=envelope.aggregate_ref,
                expected_version=None,
                now=datetime.now(UTC),
            )
            return AuthorizationEngine().decide(facts)

    class LostAckSink:
        capabilities = SinkCapabilities(True, True)
        receipt: DeliveryReceipt | None = None
        sends = 0

        async def deliver(self, envelope: DeliveryEnvelope) -> DeliveryReceipt:
            # This lock must be obtainable: no dispatch transaction spans I/O.
            async with sessions.begin() as session:
                await lock_organization(session, envelope.tenant_ref, mode=LockMode.EXCLUSIVE)
            self.sends += 1
            self.receipt = DeliveryReceipt(envelope.message_id, envelope.dedupe_key, "receiver:1")
            raise TimeoutError("receiver accepted, acknowledgement lost")

        async def reconcile(self, envelope: DeliveryEnvelope) -> ReconciliationResult:
            return ReconciliationResult(ReconciliationStatus.DELIVERED, self.receipt)

    engine = kernel(sessions)
    first = await engine.execute(context(), command())
    sink = LostAckSink()
    worker = OutboxWorker(
        sessions,
        authorizer=FixtureDeliveryAuthorizer(),
        sinks={"test-sink": sink},
        service_ref="service:delivery",
        worker_ref="worker:1",
    )
    try:
        report = await worker.run_once("tenant:a")
        assert report.unknown == 1 and sink.sends == 1
        assert (await engine.execute(context(), command())).execution_id == first.execution_id
        assert (await worker.run_once("tenant:a")).claimed == 0
        async with sessions() as session:
            message = await session.scalar(select(OutboxRecordTable))
            assert message is not None and message.status == "unknown"
            message_id = message.message_id
        assert await worker.reconcile_one("tenant:a", message_id) == "sent"
        assert sink.sends == 1 and await counts(sessions) == (2, 1, 1, 1)
        assert (await engine.get_result(context(), first.execution_id)).status == "succeeded"
    finally:
        await worker.close()
