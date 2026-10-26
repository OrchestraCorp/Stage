"""Independent transaction review of operator handoff, final deadlines and cancellation."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.administration.bootstrap import nominate
from app.administration.errors import AdminCommitUnknown, AdminConflict
from app.administration.service import AdministrationService
from app.control.contracts import AdministrationError
from app.execution.storage import UnitOfWork
from app.identity.authentication import AuthenticationAssuranceRequired
from app.models.administration import ManagementAuthorityTable
from app.models.authorization import RoleAssignmentTable
from app.models.identity_foundation import IdentityControlOperationTable
from tests.support.authorization import ORG
from tests.support.human_administration import BOB, administration_case
from tests.support.human_administration import signing_key as signing_key
from tests.support.postgres import finish_tasks, wait_until_blocked_by


@pytest.mark.parametrize("deadline_kind", ["mfa", "target"])
async def test_final_write_wait_cannot_cross_mfa_or_target_deadline_and_commit(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
    deadline_kind: str,
) -> None:
    import app.administration.service as service_module

    async with administration_case(persistence_sessions, signing_key) as case:
        if deadline_kind == "mfa":
            case.evidence = await case.authenticate(
                auth_time=int((datetime.now(UTC) - timedelta(seconds=295)).timestamp())
            )
        request = case.grant_request(
            expires_at=datetime.now(UTC) + timedelta(seconds=5 if deadline_kind == "target" else 1000)
        )
        original_flush = UnitOfWork.flush

        class AfterWait(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> Self:
                current = datetime.now(tz) + timedelta(seconds=10)
                return cls.fromtimestamp(current.timestamp(), tz=tz)

        async def wait_after_write(uow: UnitOfWork) -> None:
            await original_flush(uow)
            monkeypatch.setattr(service_module, "datetime", AfterWait)

        monkeypatch.setattr(UnitOfWork, "flush", wait_after_write)
        expected = AuthenticationAssuranceRequired if deadline_kind == "mfa" else AdminConflict
        with pytest.raises(expected):
            await case.grant(request=request)
        async with persistence_sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(RoleAssignmentTable)
                    .where(RoleAssignmentTable.principal_ref == BOB)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(IdentityControlOperationTable)
                    .where(
                        IdentityControlOperationTable.actor_kind == "human",
                        IdentityControlOperationTable.idempotency_key == "grant-bob",
                    )
                )
                == 0
            )


@pytest.mark.parametrize("committed", [False, True])
async def test_cancellation_during_commit_reports_unknown_and_preserves_original_key_recovery(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    async with administration_case(persistence_sessions, signing_key) as case:
        request = case.grant_request()
        original_commit = UnitOfWork._commit_by_owner

        async def cancelled_commit(uow: UnitOfWork) -> None:
            if committed:
                await original_commit(uow)
            raise asyncio.CancelledError()

        with monkeypatch.context() as patch:
            patch.setattr(UnitOfWork, "_commit_by_owner", cancelled_commit)
            with pytest.raises(AdminCommitUnknown):
                await case.grant(request=request)
        result = await case.grant(request=request)
        assert result.replayed == committed
        recovered = await case.service.operation(
            case.evidence, assignment_ref=case.assignment_ref, idempotency_key="grant-bob"
        )
        assert recovered.operation_ref == result.operation_ref
        async with persistence_sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(RoleAssignmentTable)
                    .where(RoleAssignmentTable.principal_ref == BOB)
                )
                == 1
            )


async def test_postgresql_operator_replacement_wins_before_personal_acceptance(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.administration.bootstrap as bootstrap_module

    if persistence_sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL observed operator/person handoff lock order")
    async with administration_case(persistence_sessions, signing_key, activate=False) as case:
        held, release = asyncio.Event(), asyncio.Event()
        pid: int | None = None
        original_install = bootstrap_module._install_management_role

        async def paused_install(session: AsyncSession, organization_ref: str) -> None:
            nonlocal pid
            pid = await session.scalar(text("SELECT pg_backend_pid()"))
            held.set()
            await release.wait()
            await original_install(session, organization_ref)

        monkeypatch.setattr(bootstrap_module, "_install_management_role", paused_install)
        tasks: list[asyncio.Task[Any]] = []
        try:
            replacement = asyncio.create_task(
                nominate(
                    persistence_sessions,
                    case.nomination.model_copy(update={"expected_version": 1, "reason": "Reviewed replacement"}),
                    actor_ref="operator:administration-test",
                    idempotency_key="replace-before-accept",
                )
            )
            tasks.append(replacement)
            await asyncio.wait_for(held.wait(), 5)
            acceptance = asyncio.create_task(case.activate())
            tasks.append(acceptance)
            assert pid is not None
            await wait_until_blocked_by(persistence_sessions, pid)
            release.set()
            replaced = await asyncio.wait_for(replacement, 10)
            assert replaced.result["version"] == 2
            with pytest.raises(AdminConflict, match="OBJECT_VERSION_CONFLICT"):
                await asyncio.wait_for(acceptance, 10)
            async with persistence_sessions() as session:
                authority = await session.get(ManagementAuthorityTable, ORG)
                previous = await session.scalar(
                    select(RoleAssignmentTable).where(RoleAssignmentTable.assignment_ref == case.assignment_ref)
                )
                assert authority is not None and authority.accepted_at is None and authority.version == 2
                assert previous is not None and previous.status == "revoked"
        finally:
            await finish_tasks(release, tasks)


async def test_postgresql_personal_acceptance_permanently_blocks_waiting_operator_replacement(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if persistence_sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL observed person/operator handoff lock order")
    async with administration_case(persistence_sessions, signing_key, activate=False) as case:
        held, release = asyncio.Event(), asyncio.Event()
        pid: int | None = None
        original_commit = AdministrationService._commit

        async def paused_commit(self: AdministrationService, uow: UnitOfWork, *args: Any, **kwargs: Any) -> None:
            nonlocal pid
            pid = await uow.session.scalar(text("SELECT pg_backend_pid()"))
            held.set()
            await release.wait()
            await original_commit(self, uow, *args, **kwargs)

        monkeypatch.setattr(AdministrationService, "_commit", paused_commit)
        tasks: list[asyncio.Task[Any]] = []
        try:
            acceptance = asyncio.create_task(case.activate())
            tasks.append(acceptance)
            await asyncio.wait_for(held.wait(), 5)
            replacement = asyncio.create_task(
                nominate(
                    persistence_sessions,
                    case.nomination.model_copy(update={"expected_version": 1, "reason": "Stale replacement"}),
                    actor_ref="operator:administration-test",
                    idempotency_key="replace-after-accept",
                )
            )
            tasks.append(replacement)
            assert pid is not None
            await wait_until_blocked_by(persistence_sessions, pid)
            release.set()
            assert (await asyncio.wait_for(acceptance, 10)).status == "succeeded"
            with pytest.raises(AdministrationError, match="first_administrator_already_accepted"):
                await asyncio.wait_for(replacement, 10)
            async with persistence_sessions() as session:
                authority = await session.get(ManagementAuthorityTable, ORG)
                assert (
                    authority is not None
                    and authority.accepted_at is not None
                    and authority.assignment_ref == case.assignment_ref
                )
        finally:
            await finish_tasks(release, tasks)
