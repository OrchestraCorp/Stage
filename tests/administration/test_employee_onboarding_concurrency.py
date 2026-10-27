"""Observe real PostgreSQL lock contention through employee management and activation."""

import asyncio
from typing import Any

import pyotp
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.native_auth.activation as activation_module
from app.administration.errors import AdminConflict
from app.administration.onboarding import NAMESPACE
from app.administration.onboarding_contracts import (
    EmployeeActivationChange,
    EmployeeActivationReissue,
    EmployeeDelivery,
)
from app.execution.storage import UnitOfWork
from app.models.identity import PrincipalTable
from app.models.identity_foundation import (
    ExternalIdentityTable,
    IdentityControlOperationTable,
    OrganizationMembershipTable,
)
from app.models.native_auth import NativeAccountActivationTable, NativeAccountTable, NativeAuthEventTable
from app.native_auth.activation import ActivationProgress
from app.native_auth.crypto import random_secret
from app.native_auth.errors import NativeCredentialError
from tests.support.employee_onboarding import PASSWORD, employee_case
from tests.support.human_administration import signing_key as signing_key
from tests.support.postgres import finish_tasks, wait_until_blocked_by


@pytest.fixture(autouse=True)
def postgres_only(persistence_sessions: async_sessionmaker[AsyncSession]) -> None:
    if persistence_sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("employee activation arbitration requires observed PostgreSQL row/advisory locks")


async def test_two_consumers_of_one_code_have_one_browser_owner(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with employee_case(persistence_sessions, signing_key) as case:
        issued = await case.create()
        assert issued.activation_code
        ref = issued.operation.result.activation_ref
        browsers = [random_secret(), random_secret()]
        csrf = [random_secret(), random_secret()]
        both_hashed, release_hashes = asyncio.Event(), asyncio.Event()
        owner_held, release_owner = asyncio.Event(), asyncio.Event()
        hash_count = 0
        owner_pid: int | None = None
        real_hash, real_commit = activation_module.hash_password, activation_module.commit_authentication

        async def rendezvous_hash(password: str) -> str:
            nonlocal hash_count
            result = await real_hash(password)
            hash_count += 1
            if hash_count == 2:
                both_hashed.set()
            await release_hashes.wait()
            return result

        async def hold_consumption(session: AsyncSession) -> None:
            nonlocal owner_pid
            owner_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            owner_held.set()
            await release_owner.wait()
            await real_commit(session)

        monkeypatch.setattr(activation_module, "hash_password", rendezvous_hash)
        monkeypatch.setattr(activation_module, "commit_authentication", hold_consumption)
        tasks: list[asyncio.Task[Any]] = [
            asyncio.create_task(
                case.activation.begin(
                    activation_ref=ref,
                    code=issued.activation_code,
                    password=PASSWORD,
                    browser_secret=browsers[index],
                    csrf_token=csrf[index],
                    peer_key=f"code-competitor-{index}",
                )
            )
            for index in range(2)
        ]
        try:
            # Both requests have already verified the same issued code before
            # either can start its second, mutating transaction.
            await asyncio.wait_for(both_hashed.wait(), 10)
            release_hashes.set()
            await asyncio.wait_for(owner_held.wait(), 10)
            assert owner_pid is not None
            await wait_until_blocked_by(persistence_sessions, owner_pid)
            assert all(not task.done() for task in tasks)
            release_owner.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
            assert sum(isinstance(result, ActivationProgress) for result in results) == 1
            assert sum(isinstance(result, NativeCredentialError) for result in results) == 1
            winner = next(index for index, result in enumerate(results) if isinstance(result, ActivationProgress))
            progress = await case.activation.progress(activation_ref=ref, browser_secret=browsers[winner])
            assert progress.status == "enrolling" and progress.totp_secret
            with pytest.raises(NativeCredentialError):
                await case.activation.progress(activation_ref=ref, browser_secret=browsers[1 - winner])
            async with persistence_sessions() as session:
                row = await session.get(NativeAccountActivationTable, ref)
                account = await session.get(NativeAccountTable, issued.operation.result.account_ref)
                assert row and account and row.version == 2 and row.code_hash is None
                assert account.status == "pending" and account.password_hash is None
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(NativeAuthEventTable)
                        .where(
                            NativeAuthEventTable.account_ref == account.account_ref,
                            NativeAuthEventTable.event == "activation.started",
                        )
                    )
                    == 1
                )
        finally:
            release_hashes.set()
            await finish_tasks(release_owner, tasks)


@pytest.mark.parametrize("complete_first", [False, True])
@pytest.mark.parametrize("reissue", [False, True])
async def test_completion_and_management_change_have_one_observed_winner(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
    complete_first: bool,
    reissue: bool,
) -> None:
    async with employee_case(persistence_sessions, signing_key) as case:
        issued = await case.create()
        assert issued.activation_code
        target = issued.operation.result
        browser, csrf = random_secret(), random_secret()
        started = await case.activation.begin(
            activation_ref=target.activation_ref,
            code=issued.activation_code,
            password=PASSWORD,
            browser_secret=browser,
            csrf_token=csrf,
            peer_key="race-owner",
        )
        assert started.totp_secret
        held, release = asyncio.Event(), asyncio.Event()
        owner_pid: int | None = None

        async def hold(session: AsyncSession) -> None:
            nonlocal owner_pid
            owner_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            held.set()
            await release.wait()

        if complete_first:
            real_native_commit = activation_module.commit_authentication

            async def paused_native_commit(session: AsyncSession) -> None:
                await hold(session)
                await real_native_commit(session)

            monkeypatch.setattr(activation_module, "commit_authentication", paused_native_commit)
        else:
            real_manager_commit = case.service._commit

            async def paused_manager_commit(uow: UnitOfWork, *args: Any, **kwargs: Any) -> None:
                await hold(uow.session)
                await real_manager_commit(uow, *args, **kwargs)

            monkeypatch.setattr(case.service, "_commit", paused_manager_commit)

        async def finish_activation() -> ActivationProgress:
            assert started.totp_secret
            return await case.activation.complete(
                activation_ref=target.activation_ref,
                browser_secret=browser,
                csrf_token=csrf,
                code=pyotp.TOTP(started.totp_secret).now(),
                peer_key="race-owner",
            )

        async def change_activation(version: int = 2, key: str = "racing-change") -> EmployeeDelivery:
            request_type = EmployeeActivationReissue if reissue else EmployeeActivationChange
            return await case.service.change(
                case.manager.evidence,
                assignment_ref=case.manager.assignment_ref,
                activation_ref=target.activation_ref,
                request=request_type(
                    expected_authority_version=case.manager.authority_version,
                    expected_activation_version=version,
                    reason="Concurrent enrollment administration",
                ),
                request_id="race-change",
                idempotency_key=key,
                reissue=reissue,
            )

        owner = asyncio.create_task(finish_activation() if complete_first else change_activation())
        tasks: list[asyncio.Task[Any]] = [owner]
        try:
            await asyncio.wait_for(held.wait(), 10)
            waiter = asyncio.create_task(change_activation() if complete_first else finish_activation())
            tasks.append(waiter)
            assert owner_pid is not None
            await wait_until_blocked_by(persistence_sessions, owner_pid)
            assert not waiter.done()
            release.set()
            winner = await asyncio.wait_for(owner, 10)
            with pytest.raises(AdminConflict if complete_first else NativeCredentialError):
                await asyncio.wait_for(waiter, 10)
            expected = "completed" if complete_first else "issued" if reissue else "revoked"
            async with persistence_sessions() as session:
                row = await session.get(NativeAccountActivationTable, target.activation_ref)
                account = await session.get(NativeAccountTable, target.account_ref)
                principal = await session.scalar(
                    select(PrincipalTable).where(PrincipalTable.principal_ref == target.principal_ref)
                )
                member = await session.get(
                    OrganizationMembershipTable, (case.config.organization_ref, target.membership_ref)
                )
                assert row and account and principal and member
                assert row.status == expected and row.version == 3
                assert account.status == ("active" if complete_first else "pending" if reissue else "disabled")
                assert (
                    principal.status
                    == member.status
                    == ("active" if complete_first else "suspended" if reissue else "revoked")
                )
                assert await session.scalar(
                    select(func.count())
                    .select_from(NativeAuthEventTable)
                    .where(
                        NativeAuthEventTable.account_ref == target.account_ref,
                        NativeAuthEventTable.event == "activation.completed",
                    )
                ) == int(complete_first)
                assert await session.scalar(
                    select(func.count())
                    .select_from(IdentityControlOperationTable)
                    .where(
                        IdentityControlOperationTable.operation_type == NAMESPACE,
                        IdentityControlOperationTable.idempotency_key == "racing-change",
                    )
                ) == int(not complete_first)
            if complete_first:
                # Even a freshly reread version cannot turn onboarding into a
                # password reset or cancellation of a completed employee.
                with pytest.raises(AdminConflict, match="ACTIVATION_CONFLICT"):
                    await change_activation(version=3, key="change-after-completion")
            elif reissue:
                assert isinstance(winner, EmployeeDelivery) and winner.activation_code
                replacement = await case.activation.begin(
                    activation_ref=target.activation_ref,
                    code=winner.activation_code,
                    password=PASSWORD,
                    browser_secret=random_secret(),
                    csrf_token=random_secret(),
                    peer_key="replacement-enrollment",
                )
                assert replacement.status == "enrolling" and replacement.totp_secret != started.totp_secret
            else:
                with pytest.raises(AdminConflict, match="ACCOUNT_CONFLICT"):
                    await case.create(key="reuse-cancelled-employee")
        finally:
            await finish_tasks(release, tasks)


@pytest.mark.parametrize("same_key", [False, True])
async def test_overlapping_same_username_creation_commits_exactly_one_identity(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
    same_key: bool,
) -> None:
    async with employee_case(persistence_sessions, signing_key) as case:
        tables = (PrincipalTable, OrganizationMembershipTable, ExternalIdentityTable, NativeAccountTable)
        async with persistence_sessions() as session:
            before = [await session.scalar(select(func.count()).select_from(table)) for table in tables]
        held, release = asyncio.Event(), asyncio.Event()
        owner_pid: int | None = None
        real_commit = case.service._commit

        async def hold_creation(uow: UnitOfWork, *args: Any, **kwargs: Any) -> None:
            nonlocal owner_pid
            owner_pid = await uow.session.scalar(text("SELECT pg_backend_pid()"))
            held.set()
            await release.wait()
            await real_commit(uow, *args, **kwargs)

        monkeypatch.setattr(case.service, "_commit", hold_creation)
        first = asyncio.create_task(case.create(key="first-create"))
        tasks: list[asyncio.Task[Any]] = [first]
        try:
            await asyncio.wait_for(held.wait(), 10)
            second = asyncio.create_task(case.create(key="first-create" if same_key else "second-create"))
            tasks.append(second)
            assert owner_pid is not None
            await wait_until_blocked_by(persistence_sessions, owner_pid)
            assert not second.done()
            release.set()
            created = await asyncio.wait_for(first, 10)
            assert created.activation_code and not created.operation.replayed
            if same_key:
                replay = await asyncio.wait_for(second, 10)
                assert replay.operation.operation_ref == created.operation.operation_ref
                assert replay.operation.replayed and replay.activation_code is None
            else:
                with pytest.raises(AdminConflict, match="ACCOUNT_CONFLICT"):
                    await asyncio.wait_for(second, 10)
            async with persistence_sessions() as session:
                after = [await session.scalar(select(func.count()).select_from(table)) for table in tables]
                assert all(old is not None and new == old + 1 for old, new in zip(before, after, strict=True))
                assert await session.scalar(select(func.count()).select_from(NativeAccountActivationTable)) == 1
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(IdentityControlOperationTable)
                        .where(IdentityControlOperationTable.operation_type == NAMESPACE)
                    )
                    == 1
                )
        finally:
            await finish_tasks(release, tasks)
