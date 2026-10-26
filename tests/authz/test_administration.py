"""The local registrar is bounded, atomic, idempotent and terminal on revoke."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.administration import (
    AuthorizationRevocationRequest,
    InstallAuthorizationPlan,
    install_authorization,
    revoke_authorization,
)
from app.authz.loader import role_permissions_digest
from app.authz.permissions import BUILTIN_POLICY_RELEASE_ID
from app.identity.administration import IdentityAdministrationError, IdentityOperationUnknown, operation_result
from app.models.authorization import (
    AuthorizationScopeTable,
    RelationTupleTable,
    RoleAssignmentTable,
    RoleVersionTable,
)
from app.models.execution import TenantGuardTable
from app.models.identity_foundation import IdentityControlOperationTable, OrganizationMembershipTable
from app.models.work import WorkItemTable
from tests.support.identity_administration import OPERATOR, install

ORG = "tenant-a"


def plan_data(**changes: Any) -> dict[str, Any]:
    return {
        "environment": "test",
        "organization_ref": ORG,
        "expected_organization_version": 1,
        "role_ref": "role:engineering",
        "role_code": "engineering-owner",
        "display_name": "Engineering task owner",
        "role_revision": 1,
        "actions": ["task.view", "task.assign"],
        "scopes": [{"scope_ref": "scope:engineering", "scope_kind": "department"}],
        "assignments": [
            {
                "assignment_ref": "assignment:alice",
                "principal_ref": "human-a",
                "membership_ref": "membership-human",
                "expected_membership_version": 1,
                "scope_ref": "scope:engineering",
            }
        ],
        "resources": [{"resource_ref": "work_item:1", "scope_ref": "scope:engineering", "expected_work_version": 1}],
        "acls": [
            {
                "relation_ref": "relation:alice",
                "principal_ref": "human-a",
                "membership_ref": "membership-human",
                "expected_membership_version": 1,
                "relation": "owner_of",
                "resource_ref": "work_item:1",
            }
        ],
        **changes,
    }


@pytest.fixture
async def installed_sessions(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    await install(persistence_sessions)
    async with persistence_sessions.begin() as session:
        session.add(
            WorkItemTable(
                id=1,
                title="Existing work",
                scope_ref="scope:engineering",
                lifecycle="draft",
                classification="internal",
                version=1,
            )
        )
    return persistence_sessions


async def _install(sessions: async_sessionmaker[AsyncSession]) -> Any:
    return await install_authorization(
        sessions, InstallAuthorizationPlan.model_validate(plan_data()), actor_ref=OPERATOR, idempotency_key="grant"
    )


async def test_install_records_exact_role_grants_and_replays_without_epoch_change(
    installed_sessions: async_sessionmaker[AsyncSession],
) -> None:
    sessions = installed_sessions
    first = await _install(sessions)
    assert await _install(sessions) == first
    recovered = await operation_result(
        sessions,
        environment="test",
        actor_ref=OPERATOR,
        operation_type="install.authorization",
        idempotency_key="grant",
    )
    assert recovered == first
    async with sessions() as session:
        role = await session.get(RoleVersionTable, (ORG, "role:engineering", 1))
        assignment = await session.scalar(select(RoleAssignmentTable))
        relation = await session.scalar(select(RelationTupleTable))
        assert role is not None
        assert role.content_digest == role_permissions_digest(
            ORG, "role:engineering", 1, BUILTIN_POLICY_RELEASE_ID, ("task.view", "task.assign")
        )
        assert assignment is not None and assignment.grant_source_ref == first.operation_ref
        assert (
            relation is not None and relation.source_ref == first.operation_ref and relation.source_kind == "direct_acl"
        )
        guard = await session.get(TenantGuardTable, ORG)
        assert guard is not None and guard.epoch == 2
        assert await session.scalar(select(func.count()).select_from(IdentityControlOperationTable)) == 2


@pytest.mark.parametrize(
    ("changes", "error"),
    (
        ({"environment": "dev"}, "identity_database_environment_mismatch"),
        ({"organization_ref": "forged-root"}, "identity_database_organization_mismatch"),
        ({"expected_organization_version": 2}, "organization_version_conflict"),
    ),
)
async def test_install_rejects_wrong_environment_or_root(
    installed_sessions: async_sessionmaker[AsyncSession], changes: dict[str, Any], error: str
) -> None:
    with pytest.raises(IdentityAdministrationError, match=error):
        await install_authorization(
            installed_sessions,
            InstallAuthorizationPlan.model_validate(plan_data(**changes)),
            actor_ref=OPERATOR,
            idempotency_key="bad",
        )
    async with installed_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RoleVersionTable)) == 0


@pytest.mark.parametrize(
    "case",
    [
        "service",
        "membership_version",
        "missing_work",
        "scope_mismatch",
        "work_version",
        "scope_cycle",
        "missing_parent",
    ],
)
async def test_invalid_install_rolls_back_all_partial_grants(
    installed_sessions: async_sessionmaker[AsyncSession], case: str
) -> None:
    data = plan_data()
    if case == "service":
        for row in (*data["assignments"], *data["acls"]):
            row["principal_ref"] = "service-a"
            row["membership_ref"] = "membership-service"
    elif case == "membership_version":
        data["assignments"][0]["expected_membership_version"] = 2
    elif case == "missing_work":
        data["resources"][0]["resource_ref"] = "work_item:2"
    elif case == "scope_mismatch":
        data["resources"][0]["scope_ref"] = "scope:other"
        data["scopes"].append({"scope_ref": "scope:other", "scope_kind": "project"})
    elif case == "work_version":
        data["resources"][0]["expected_work_version"] = 2
    elif case == "scope_cycle":
        data["scopes"][0]["parent_scope_ref"] = "scope:engineering"
    else:
        data["scopes"][0]["parent_scope_ref"] = "scope:missing"
    with pytest.raises(IdentityAdministrationError):
        await install_authorization(
            installed_sessions, InstallAuthorizationPlan.model_validate(data), actor_ref=OPERATOR, idempotency_key="bad"
        )
    async with installed_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RoleVersionTable)) == 0
        assert await session.scalar(select(func.count()).select_from(AuthorizationScopeTable)) == 0
        assert await session.scalar(select(func.count()).select_from(RoleAssignmentTable)) == 0
        assert await session.scalar(select(func.count()).select_from(IdentityControlOperationTable)) == 1
        guard = await session.get(TenantGuardTable, ORG)
        assert guard is not None and guard.epoch == 1


async def test_ended_membership_cannot_receive_new_authority(
    installed_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with installed_sessions.begin() as session:
        await session.execute(
            update(OrganizationMembershipTable)
            .where(OrganizationMembershipTable.membership_ref == "membership-human")
            .values(status="left", ended_at=datetime.now(UTC), version=2)
        )
    with pytest.raises(IdentityAdministrationError, match="membership_inactive"):
        await _install(installed_sessions)


async def test_conflicting_replay_and_new_key_cannot_replace_published_rows(
    installed_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _install(installed_sessions)
    changed = InstallAuthorizationPlan.model_validate(plan_data(actions=["task.view"]))
    with pytest.raises(IdentityAdministrationError, match="idempotency_conflict"):
        await install_authorization(installed_sessions, changed, actor_ref=OPERATOR, idempotency_key="grant")
    with pytest.raises(IdentityAdministrationError, match="scope_already_exists"):
        await install_authorization(installed_sessions, changed, actor_ref=OPERATOR, idempotency_key="another")
    async with installed_sessions() as session:
        role = await session.get(RoleVersionTable, (ORG, "role:engineering", 1))
        assert role is not None and role.content_digest == role_permissions_digest(
            ORG, "role:engineering", 1, BUILTIN_POLICY_RELEASE_ID, ("task.view", "task.assign")
        )


async def test_new_role_revision_does_not_rebind_old_assignment(
    installed_sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _install(installed_sessions)
    data = plan_data(
        role_revision=2, expected_role_definition_version=1, actions=["task.view"], scopes=[], resources=[], acls=[]
    )
    data["assignments"][0]["assignment_ref"] = "assignment:alice:v2"
    await install_authorization(
        installed_sessions, InstallAuthorizationPlan.model_validate(data), actor_ref=OPERATOR, idempotency_key="v2"
    )
    async with installed_sessions() as session:
        rows = {
            row.assignment_ref: row.role_revision for row in (await session.scalars(select(RoleAssignmentTable))).all()
        }
        assert rows == {"assignment:alice": 1, "assignment:alice:v2": 2}


@pytest.mark.parametrize(
    ("kind", "target", "revision"),
    [
        ("assignment", "assignment:alice", None),
        ("relation", "relation:alice", None),
        ("role_version", "role:engineering", 1),
        ("scope", "scope:engineering", None),
        ("resource", "work_item:1", None),
    ],
)
async def test_revocation_is_versioned_terminal_and_idempotent(
    installed_sessions: async_sessionmaker[AsyncSession], kind: str, target: str, revision: int | None
) -> None:
    sessions = installed_sessions
    await _install(sessions)
    request = AuthorizationRevocationRequest.model_validate(
        {
            "environment": "test",
            "organization_ref": ORG,
            "kind": kind,
            "target_ref": target,
            "expected_version": 1,
            "role_revision": revision,
        }
    )
    result = await revoke_authorization(sessions, request, actor_ref=OPERATOR, idempotency_key="revoke")
    assert result.result["status"] == "revoked" and result.result["version"] == 2
    assert result.result["organization_authz_epoch"] == 3
    assert await revoke_authorization(sessions, request, actor_ref=OPERATOR, idempotency_key="revoke") == result
    with pytest.raises(IdentityAdministrationError, match="version_conflict"):
        await revoke_authorization(sessions, request, actor_ref=OPERATOR, idempotency_key="stale")
    with pytest.raises(IdentityAdministrationError, match="authorization_already_ended"):
        await revoke_authorization(
            sessions, request.model_copy(update={"expected_version": 2}), actor_ref=OPERATOR, idempotency_key="ended"
        )


@pytest.mark.parametrize("operation", ["install", "revoke"])
async def test_commit_response_loss_recovers_by_original_operator_and_key(
    installed_sessions: async_sessionmaker[AsyncSession], operation: str
) -> None:
    sessions = installed_sessions
    if operation == "revoke":
        await _install(sessions)

    def lose_response(_session: Any) -> None:
        raise SQLAlchemyError("simulated response loss after commit")

    request = AuthorizationRevocationRequest(
        environment="test",
        organization_ref=ORG,
        kind="assignment",
        target_ref="assignment:alice",
        expected_version=1,
    )
    event.listen(AsyncSession.sync_session_class, "after_commit", lose_response)
    try:
        with pytest.raises(IdentityOperationUnknown):
            if operation == "install":
                await _install(sessions)
            else:
                await revoke_authorization(sessions, request, actor_ref=OPERATOR, idempotency_key="revoke")
    finally:
        event.remove(AsyncSession.sync_session_class, "after_commit", lose_response)
    recovered = await operation_result(
        sessions,
        environment="test",
        actor_ref=OPERATOR,
        operation_type="install.authorization" if operation == "install" else "revoke.authorization.assignment",
        idempotency_key="grant" if operation == "install" else "revoke",
    )
    assert recovered.operation_ref
    if operation == "install":
        assert await _install(sessions) == recovered
    else:
        assert await revoke_authorization(sessions, request, actor_ref=OPERATOR, idempotency_key="revoke") == recovered


@pytest.mark.parametrize("case", ["action", "relation", "prod", "extra", "duplicate", "alias", "invalid_ref"])
def test_plan_accepts_only_supported_explicit_authority(case: str) -> None:
    data = plan_data()
    if case == "action":
        data["actions"] = ["task.delivery.submit"]
    elif case == "relation":
        data["acls"][0]["relation"] = "assignee_of"
    elif case == "prod":
        data["environment"] = "prod"
    elif case == "extra":
        data["dynamic_python"] = "unauthorized.module"
    elif case == "duplicate":
        data["actions"] = ["task.view", "task.view"]
    elif case == "alias":
        data["tenant_ref"] = data.pop("organization_ref")
    else:
        data["resources"][0]["resource_ref"] = "work_item:01"
    with pytest.raises(ValidationError):
        InstallAuthorizationPlan.model_validate(data)


@pytest.mark.parametrize(
    ("kind", "invalid"),
    [
        ("scope", "scope:\x00bad"),
        ("scope", "scope:\x1fbad"),
        ("assignment", "assignment:\x7fbad"),
        ("role", "role:\x01bad"),
    ],
)
def test_control_characters_cannot_register_unqueryable_authorization_references(kind: str, invalid: str) -> None:
    data = plan_data()
    if kind == "scope":
        data["scopes"][0]["scope_ref"] = invalid
    elif kind == "assignment":
        data["assignments"][0]["assignment_ref"] = invalid
    else:
        data["role_ref"] = invalid
    with pytest.raises(ValidationError):
        InstallAuthorizationPlan.model_validate(data)


def test_cli_refuses_production_without_exposing_database_credentials() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/authorization_admin.py",
            "--environment",
            "dev",
            "result",
            "--operation-type",
            "install.authorization",
            "--idempotency-key",
            "grant",
        ],
        env={
            **os.environ,
            "STAGE_ENV": "prod",
            "STAGE_DATABASE_URL": "postgresql+asyncpg://private-user:private-password@127.0.0.1/stage",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1 and "development_environment_required" in result.stderr
    assert "private-user" not in result.stderr and "private-password" not in result.stderr


@pytest.mark.parametrize("depth", [64, 65])
async def test_scope_registration_accepts_depth_64_and_rejects_depth_65(
    installed_sessions: async_sessionmaker[AsyncSession], depth: int
) -> None:
    sessions = installed_sessions
    first_index = 0
    if depth == 65:
        async with sessions.begin() as session:
            session.add(AuthorizationScopeTable(organization_ref=ORG, scope_ref="depth:0", scope_kind="organization"))
        first_index = 1
    data = plan_data()
    data["scopes"] = [
        {
            "scope_ref": f"depth:{index}",
            "scope_kind": "project",
            "parent_scope_ref": f"depth:{index - 1}" if index else None,
        }
        for index in range(first_index, depth)
    ]
    data["assignments"][0]["scope_ref"] = f"depth:{depth - 1}"
    data["resources"][0]["scope_ref"] = f"depth:{depth - 1}"
    async with sessions.begin() as session:
        await session.execute(update(WorkItemTable).where(WorkItemTable.id == 1).values(scope_ref=f"depth:{depth - 1}"))
    plan = InstallAuthorizationPlan.model_validate(data)
    if depth == 64:
        result = await install_authorization(sessions, plan, actor_ref=OPERATOR, idempotency_key="depth")
        assert len(result.result["scope_refs"]) == 64
    else:
        with pytest.raises(IdentityAdministrationError, match="scope_depth_exceeded"):
            await install_authorization(sessions, plan, actor_ref=OPERATOR, idempotency_key="depth")
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(AuthorizationScopeTable)) == 1
