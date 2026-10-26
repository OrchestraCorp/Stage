import asyncio
from copy import deepcopy
from dataclasses import replace
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.loader import DatabaseAuthorizationLoader, role_permissions_digest
from app.authz.permissions import BUILTIN_POLICY_RELEASE_ID
from app.execution.contracts import CommandEffect, CommandEnvelope, CommandSpec, ExecutionContext, Handler, OutboxIntent
from app.execution.errors import AuthorizationDenied, ExecutionNotFound
from app.execution.kernel import CommandKernel
from app.execution.storage import UnitOfWork, lock_tenant
from app.identity import administration
from app.identity.administration import RevocationRequest, revoke
from app.models.authorization import RoleVersionTable
from app.models.execution import AuditRecordTable
from app.models.identity_foundation import (
    BootstrapRecordTable,
    ExternalIdentityTable,
    IdentityProviderTable,
    TrustStateTable,
)
from app.models.work import WorkItemTable
from tests.support.authorization import NOW, ORG, PERSON, TARGET, execution_context
from tests.support.database_commands import (
    counts,
    seed,
)
from tests.support.postgres import wait_until_blocked_by as _wait_until_blocked_by

RevocationKind = Literal["principal", "membership", "role_version"]


class AssignInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str


class AssignResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int


def command() -> CommandEnvelope:
    return CommandEnvelope("assign", "v1", TARGET, 1, "assign-once", {"note": "assigned"})


async def assign(uow: UnitOfWork, _ctx: ExecutionContext, cmd: CommandEnvelope, _payload: BaseModel) -> CommandEffect:
    """Test-only mutation, not the product's task assignment implementation."""
    item = await uow.session.get(WorkItemTable, 1)
    assert item is not None
    before = {"version": item.version}
    item.version += 1
    return CommandEffect(
        result={"version": item.version},
        before=before,
        after={"version": item.version},
        messages=(OutboxIntent("task.assigned", "test", TARGET, "task.view", "assign:" + cmd.idempotency_key, {}),),
    )


def kernel(sessions: async_sessionmaker[AsyncSession], handler: Handler = assign) -> CommandKernel:
    loader = DatabaseAuthorizationLoader()
    return CommandKernel(
        sessions,
        loader=loader,
        coordinator=loader,
        commands=[CommandSpec("assign", "v1", "task.assign", "task.view", AssignInput, AssignResult, handler)],
        clock=lambda: NOW,
    )


async def audit_evidence(sessions: async_sessionmaker[AsyncSession], execution_id: str) -> dict[str, Any]:
    async with sessions() as session:
        audit = await session.scalar(select(AuditRecordTable).where(AuditRecordTable.execution_id == execution_id))
        assert audit is not None and audit.authorization_json is not None
        return deepcopy(audit.authorization_json)


async def test_real_loader_command_commit_replay_and_revoked_result_read(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(persistence_sessions)
    engine = kernel(persistence_sessions)
    first = await engine.execute(execution_context(), command())
    evidence = await audit_evidence(persistence_sessions, first.execution_id)
    assert evidence == {
        "schema": "stage-authorization-evidence-v1",
        "organization_ref": ORG,
        "principal_ref": PERSON,
        "membership_ref": "membership:alice",
        "assignment_ref": "assignment:engineering",
        "role_ref": "role:engineering",
        "role_revision": 1,
        "role_digest": role_permissions_digest(
            ORG, "role:engineering", 1, BUILTIN_POLICY_RELEASE_ID, ("task.assign", "task.view")
        ),
        "policy_release_ref": BUILTIN_POLICY_RELEASE_ID,
        "action": "task.assign",
        "resource_ref": TARGET,
        "resource_scopes": ["scope:engineering"],
        "matched_relations": ["owner_of"],
        "evaluated_at": NOW.isoformat(),
        "fact_versions": sorted(
            [
                ["organization_root", ORG, 1],
                ["principal", PERSON, 1],
                ["principal.identity_epoch", PERSON, 0],
                ["tenant_membership", "membership:alice", 1],
                ["role_assignment", "assignment:engineering", 1],
                ["role_definition", "role:engineering", 1],
                ["role_version", "role:engineering@1", 1],
                ["authorization_resource", TARGET, 1],
                ["work_item", TARGET, 1],
                ["execution_tenant_guard.epoch", ORG, 0],
                ["authorization_scope", "scope:engineering", 1],
                ["permission_action", "task.assign", 1],
                ["permission_action", "task.view", 1],
                ["relation_tuple", "relation:eligible", 1],
            ]
        ),
    }
    replay = await engine.execute(replace(execution_context(), request_id="request:replay"), command())
    assert first.result == {"version": 2}
    assert replay.replayed and replay.execution_id == first.execution_id
    assert await counts(persistence_sessions) == (2, 1, 1, 1)
    assert (await engine.get_result(execution_context(), first.execution_id)).result == first.result
    # Replay reauthorizes task.view against current facts (work version 2),
    # but it must not rewrite the original task.assign evidence (version 1).
    assert await audit_evidence(persistence_sessions, first.execution_id) == evidence

    async with persistence_sessions.begin() as session:
        await lock_tenant(session, ORG)
        role = await session.get(RoleVersionTable, (ORG, "role:engineering", 1))
        assert role is not None
        role.status = "revoked"
        role.revoked_at = NOW
        role.version += 1
    with pytest.raises(AuthorizationDenied):
        await engine.execute(execution_context(), command())
    with pytest.raises(AuthorizationDenied):
        await engine.get_result(execution_context(), first.execution_id)
    assert await counts(persistence_sessions) == (2, 1, 1, 1)
    assert await audit_evidence(persistence_sessions, first.execution_id) == evidence


async def test_real_loader_rejects_forged_actor_and_rolls_back_no_side_effects(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(persistence_sessions)
    engine = kernel(persistence_sessions)
    with pytest.raises(AuthorizationDenied):
        await engine.execute(
            replace(execution_context(), actor_ref="person:other", effective_subject_ref="person:other"), command()
        )
    assert await counts(persistence_sessions) == (1, 0, 0, 0)
    with pytest.raises(ExecutionNotFound):
        await engine.get_result(execution_context(), "00000000-0000-0000-0000-000000000000")


async def test_database_loader_without_its_lock_coordinator_rejects_with_no_side_effects(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(persistence_sessions)
    engine = CommandKernel(
        persistence_sessions,
        loader=DatabaseAuthorizationLoader(),
        commands=[CommandSpec("assign", "v1", "task.assign", "task.view", AssignInput, AssignResult, assign)],
        clock=lambda: NOW,
    )
    with pytest.raises(AuthorizationDenied):
        await engine.execute(execution_context(), command())
    assert await counts(persistence_sessions) == (1, 0, 0, 0)


async def _prepare_admin(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires PostgreSQL row lock and blocking PID semantics")
    await seed(sessions)
    async with sessions.begin() as session:
        session.add(TrustStateTable(trust_ref="trust:fixture", purpose="authentication", configuration_digest="a" * 64))
        await session.flush()
        session.add(
            IdentityProviderTable(
                provider_ref="provider:fixture", issuer_exact="https://idp.example.test", trust_ref="trust:fixture"
            )
        )
        await session.flush()
        session.add(
            ExternalIdentityTable(
                external_identity_ref="external:alice",
                provider_ref="provider:fixture",
                subject_namespace="human/sub",
                subject_exact="alice",
                principal_ref=PERSON,
                principal_type="human",
                link_evidence_ref="fixture:link",
            )
        )
        await session.flush()
        session.add(
            BootstrapRecordTable(
                bootstrap_ref="bootstrap:fixture",
                environment="test",
                bootstrap_type=administration.BOOTSTRAP_TYPE,
                configuration_digest="a" * 64,
                initial_identity_ref="external:alice",
                initial_grant_refs=[],
                completed_at=NOW,
            )
        )


async def _revoke(
    sessions: async_sessionmaker[AsyncSession],
    kind: RevocationKind,
    *,
    locked: asyncio.Event | None = None,
    release: asyncio.Event | None = None,
    pids: list[int] | None = None,
) -> None:
    if kind == "role_version":
        async with sessions.begin() as session:
            await lock_tenant(session, ORG)
            if pids is not None:
                pid = await session.scalar(text("SELECT pg_backend_pid()"))
                assert pid is not None
                pids.append(pid)
            if locked is not None and release is not None:
                locked.set()
                await release.wait()
            role = await session.get(RoleVersionTable, (ORG, "role:engineering", 1))
            assert role is not None
            role.status = "revoked"
            role.revoked_at = NOW
            role.version += 1
        return
    request = RevocationRequest(
        environment="test",
        kind=kind,
        target_ref=PERSON if kind == "principal" else "membership:alice",
        organization_ref=ORG if kind == "membership" else None,
        expected_version=1,
    )
    await revoke(sessions, request, actor_ref="operator:test", idempotency_key="revoke:" + kind)


@pytest.mark.parametrize("kind", ("principal", "membership", "role_version"))
async def test_revocation_first_blocks_command_and_denies_after_commit(
    persistence_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, kind: RevocationKind
) -> None:
    await _prepare_admin(persistence_sessions)
    locked = asyncio.Event()
    release = asyncio.Event()
    pids: list[int] = []
    original_lock = administration._lock_target

    async def hold_target(session: AsyncSession, request: RevocationRequest) -> Any:
        row = await original_lock(session, request)
        pid = await session.scalar(text("SELECT pg_backend_pid()"))
        assert pid is not None
        pids.append(pid)
        locked.set()
        await release.wait()
        return row

    monkeypatch.setattr(administration, "_lock_target", hold_target)
    writer = asyncio.create_task(_revoke(persistence_sessions, kind, locked=locked, release=release, pids=pids))
    tasks: list[asyncio.Task[Any]] = [writer]
    try:
        await asyncio.wait_for(locked.wait(), 10)
        command_task = asyncio.create_task(kernel(persistence_sessions).execute(execution_context(), command()))
        tasks.append(command_task)
        await _wait_until_blocked_by(persistence_sessions, pids[0])
        assert not command_task.done()
        release.set()
        await asyncio.wait_for(writer, 10)
        with pytest.raises(AuthorizationDenied):
            await asyncio.wait_for(command_task, 10)
        assert await counts(persistence_sessions) == (1, 0, 0, 0)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("kind", ("principal", "membership", "role_version"))
async def test_command_first_commits_before_waiting_revocation_and_replay_is_denied(
    persistence_sessions: async_sessionmaker[AsyncSession], kind: RevocationKind
) -> None:
    await _prepare_admin(persistence_sessions)
    entered = asyncio.Event()
    release = asyncio.Event()
    pids: list[int] = []

    async def holding_handler(
        uow: UnitOfWork, ctx: ExecutionContext, cmd: CommandEnvelope, payload: BaseModel
    ) -> CommandEffect:
        pid = await uow.session.scalar(text("SELECT pg_backend_pid()"))
        assert pid is not None
        pids.append(pid)
        entered.set()
        await release.wait()
        return await assign(uow, ctx, cmd, payload)

    engine = kernel(persistence_sessions, holding_handler)
    command_task = asyncio.create_task(engine.execute(execution_context(), command()))
    tasks: list[asyncio.Task[Any]] = [command_task]
    try:
        await asyncio.wait_for(entered.wait(), 10)
        writer = asyncio.create_task(_revoke(persistence_sessions, kind))
        tasks.append(writer)
        await _wait_until_blocked_by(persistence_sessions, pids[0])
        assert not writer.done()
        release.set()
        assert (await asyncio.wait_for(command_task, 10)).result == {"version": 2}
        await asyncio.wait_for(writer, 10)
        with pytest.raises(AuthorizationDenied):
            await engine.execute(execution_context(), command())
        assert await counts(persistence_sessions) == (2, 1, 1, 1)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
