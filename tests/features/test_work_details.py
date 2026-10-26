"""The first human command edits metadata without changing collaboration state."""

from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.loader import DatabaseAuthorizationLoader
from app.authz.permissions import Permission, RoleCode, permissions_of
from app.execution.errors import AuthorizationDenied, IdempotencyConflict, InvalidCommand
from app.execution.kernel import CommandKernel
from app.execution.storage import managed_uow
from app.features.work.details import (
    WORK_DETAILS_COMMAND,
    WorkDetailsConflict,
    WorkDetailsInput,
    update_work_details,
)
from app.models.authorization import RelationTupleTable
from app.models.execution import AuditRecordTable, CommandExecutionTable, OutboxRecordTable
from app.models.work import WorkItemTable
from tests.support.authorization import NOW, TARGET, execution_context
from tests.support.work_details import (
    assert_unchanged,
    command,
    seed,
)


def kernel(sessions: async_sessionmaker[AsyncSession]) -> CommandKernel:
    loader = DatabaseAuthorizationLoader()
    return CommandKernel(
        sessions, loader=loader, coordinator=loader, commands=[WORK_DETAILS_COMMAND], clock=lambda: NOW
    )


@pytest.mark.parametrize("lifecycle", ["draft", "open"])
async def test_metadata_edit_preserves_security_and_work_state_and_replays_once(
    persistence_sessions: async_sessionmaker[AsyncSession], lifecycle: str
) -> None:
    await seed(persistence_sessions)
    async with persistence_sessions.begin() as session:
        work = await session.get(WorkItemTable, 1)
        assert work is not None
        work.lifecycle = lifecycle
    executor = kernel(persistence_sessions)
    first = await executor.execute(execution_context(), command())
    replay = await executor.execute(
        replace(execution_context(), request_id="request:replay"),
        command(payload={"title": "Updated title", "description": "  first line\nsecond line  "}),
    )
    assert not first.replayed and replay.replayed and replay.execution_id == first.execution_id
    assert first.result == {
        "resource_ref": TARGET,
        "title": "Updated title",
        "description": "  first line\nsecond line  ",
        "lifecycle": lifecycle,
        "classification": "internal",
        "version": 2,
    }
    assert (await executor.get_result(execution_context(), first.execution_id)).result == first.result
    async with persistence_sessions() as session:
        work = await session.get(WorkItemTable, 1)
        assert work is not None and work.scope_ref == "scope:engineering"
        assert (work.title, work.description, work.lifecycle, work.classification, work.version) == (
            "Updated title",
            "  first line\nsecond line  ",
            lifecycle,
            "internal",
            2,
        )
        assert await session.scalar(select(func.count()).select_from(CommandExecutionTable)) == 1
        assert await session.scalar(select(func.count()).select_from(OutboxRecordTable)) == 0
        audits = tuple(await session.scalars(select(AuditRecordTable)))
        assert len(audits) == 1
        assert audits[0].action == "task.metadata.update"
        assert audits[0].before_json == {
            "resource_ref": TARGET,
            "title": "Loader fixture",
            "description": None,
            "lifecycle": lifecycle,
            "classification": "internal",
            "version": 1,
        }
        assert audits[0].after_json == first.result


async def test_description_requires_explicit_null_to_clear_and_idempotency_rejects_another_edit(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(persistence_sessions)
    executor = kernel(persistence_sessions)
    await executor.execute(execution_context(), command())
    with pytest.raises(IdempotencyConflict):
        await executor.execute(execution_context(), command(payload={"title": "Different", "description": None}))
    cleared = await executor.execute(
        execution_context(),
        command(
            expected_version=2,
            idempotency_key="clear-description",
            payload={"title": "Updated title", "description": None},
        ),
    )
    assert cleared.result["description"] is None and cleared.result["version"] == 3


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "Valid"},
        {"title": " \n\t ", "description": None},
        {"title": "a" * 257, "description": None},
        {"title": "Valid", "description": "a" * 20_001},
        {"title": 123, "description": None},
        {"title": "Valid", "description": 123},
        {"title": "Bad\x00title", "description": None},
        {"title": "Valid", "description": "bad\ud800text"},
        {"title": "Valid", "description": None, "lifecycle": "closed"},
        {"title": "Valid", "description": None, "scope_ref": "scope:finance"},
        {"title": "Valid", "description": None, "classification": "public"},
        {"title": "Valid", "description": None, "actor_ref": "person:other"},
        {"title": "Valid", "description": None, "human_confirmed": True},
    ],
)
def test_metadata_input_rejects_invalid_or_authority_bearing_fields(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        WorkDetailsInput.model_validate(payload)


@pytest.mark.parametrize("case", ["ungranted", "viewer_only", "stale_version", "committed", "closed"])
async def test_current_authority_and_version_are_required_without_partial_writes(
    persistence_sessions: async_sessionmaker[AsyncSession], case: str
) -> None:
    await seed(persistence_sessions, grant=case != "ungranted")
    lifecycle = "open"
    async with persistence_sessions.begin() as session:
        if case == "viewer_only":
            relation = await session.scalar(select(RelationTupleTable))
            assert relation is not None
            relation.relation = relation.relation_type = "viewer_of"
        if case in {"committed", "closed"}:
            lifecycle = case
            work = await session.get(WorkItemTable, 1)
            assert work is not None
            work.lifecycle = lifecycle
    with pytest.raises(AuthorizationDenied):
        await kernel(persistence_sessions).execute(
            execution_context(), command(expected_version=2 if case == "stale_version" else 1)
        )
    await assert_unchanged(persistence_sessions, lifecycle=lifecycle)


@pytest.mark.parametrize("case", ["stale_version", "lifecycle"])
async def test_handler_keeps_domain_invariants_even_without_policy_adapter(
    persistence_sessions: async_sessionmaker[AsyncSession], case: str
) -> None:
    await seed(persistence_sessions)
    lifecycle = "closed" if case == "lifecycle" else "open"
    async with persistence_sessions.begin() as session:
        work = await session.get(WorkItemTable, 1)
        assert work is not None
        work.lifecycle = lifecycle
    async with managed_uow(persistence_sessions) as uow:
        with pytest.raises(WorkDetailsConflict):
            await update_work_details(
                uow,
                execution_context(),
                command(expected_version=2 if case == "stale_version" else 1),
                WorkDetailsInput(title="Updated", description=None),
            )
    await assert_unchanged(persistence_sessions, lifecycle=lifecycle)


@pytest.mark.parametrize("target_ref", ["work_item:01", "1", "work_item:0", "work_item:2147483648"])
async def test_handler_requires_canonical_existing_work_identity(
    persistence_sessions: async_sessionmaker[AsyncSession], target_ref: str
) -> None:
    await seed(persistence_sessions)
    async with managed_uow(persistence_sessions) as uow:
        with pytest.raises(InvalidCommand):
            await update_work_details(
                uow,
                execution_context(),
                command(target_ref=target_ref),
                WorkDetailsInput(title="Updated", description=None),
            )
    await assert_unchanged(persistence_sessions)


def test_new_metadata_action_is_not_implicitly_granted_to_existing_builtin_roles() -> None:
    assert Permission.TASK_METADATA_UPDATE not in permissions_of(tuple(RoleCode))
