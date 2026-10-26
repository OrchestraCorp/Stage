"""Self-discovery remains a bounded, current projection rather than a grant."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz import assignments
from app.authz.loader import ValidatedRoleVersion, role_permissions_digest
from app.authz.permissions import BUILTIN_POLICY_RELEASE_ID
from app.identity.evidence import HumanAuthenticationEvidence
from app.models.authorization import (
    AuthorizationScopeTable,
    PermissionActionTable,
    RoleAssignmentTable,
    RoleDefinitionTable,
    RoleVersionActionTable,
    RoleVersionTable,
)
from app.models.identity import PrincipalTable
from app.models.identity_foundation import OrganizationMembershipTable
from app.providers import assignments as assignment_provider
from app.providers.identity import IdentityFacade
from tests.support.assignment_http import (
    assignment_client as assignment_client,
)
from tests.support.assignment_http import (
    signing_key as signing_key,
)
from tests.support.authorization import ORG, PERSON
from tests.support.identity_tokens import token
from tests.support.work_http import assert_private

BASE = "/api/v1/identity/assignments"


async def add_assignment(
    session: AsyncSession,
    assignment_ref: str,
    *,
    principal_ref: str = PERSON,
    membership_ref: str = "membership:alice",
) -> RoleAssignmentTable:
    scope_ref = f"scope:{assignment_ref}"
    session.add(AuthorizationScopeTable(organization_ref=ORG, scope_ref=scope_ref, scope_kind="test"))
    await session.flush()
    assignment = RoleAssignmentTable(
        organization_ref=ORG,
        assignment_ref=assignment_ref,
        membership_ref=membership_ref,
        principal_ref=principal_ref,
        principal_type="human",
        role_ref="role:engineering",
        role_revision=1,
        role_code="department-owner",
        scope_ref=scope_ref,
        grant_source_ref="test:explicit-installation",
        valid_from=datetime.now(UTC) - timedelta(days=1),
        status="active",
        version=1,
    )
    session.add(assignment)
    await session.flush()
    return assignment


async def test_discovery_returns_selection_metadata_without_effective_permissions(
    assignment_client: httpx.AsyncClient,
) -> None:
    response = await assignment_client.get(BASE)
    assert response.status_code == 200
    assert_private(response)
    body = response.json()
    assert body["next_after"] is None
    assert len(body["items"]) == 1
    choice = body["items"][0]
    assert choice == {
        "assignment_ref": "assignment:engineering",
        "role_ref": "role:engineering",
        "role_revision": 1,
        "role_code": "department-owner",
        "role_display_name": "Engineering member",
        "scope_ref": "scope:engineering",
        "valid_from": choice["valid_from"],
        "expires_at": None,
        "version": 1,
    }
    assert "subject-1" not in response.text
    assert "membership:alice" not in response.text
    assert "task.metadata.update" not in response.text


async def test_no_assignments_is_a_successful_empty_projection(
    assignment_client: httpx.AsyncClient, persistence_sessions: async_sessionmaker[AsyncSession]
) -> None:
    async with persistence_sessions.begin() as session:
        await session.execute(delete(RoleAssignmentTable))
    response = await assignment_client.get(BASE)
    assert response.status_code == 200 and response.json() == {"items": [], "next_after": None}


async def test_discovered_reference_can_be_selected_but_does_not_survive_revocation(
    assignment_client: httpx.AsyncClient, persistence_sessions: async_sessionmaker[AsyncSession]
) -> None:
    response = await assignment_client.get(BASE)
    assert response.status_code == 200
    selected = response.json()["items"][0]["assignment_ref"]
    edited = await assignment_client.put(
        "/api/v1/work-items/1/details",
        params={"assignment_ref": selected},
        headers={"Idempotency-Key": "selected-via-discovery"},
        json={"title": "Selected current assignment", "description": None, "expected_version": 1},
    )
    assert edited.status_code == 200
    async with persistence_sessions.begin() as session:
        assignment = await session.scalar(select(RoleAssignmentTable))
        assert assignment is not None
        assignment.status = "revoked"
        assignment.revoked_at = datetime.now(UTC)
        assignment.version += 1
    assert (await assignment_client.get(BASE)).json() == {"items": [], "next_after": None}
    denied = await assignment_client.get("/api/v1/work-items/1", params={"assignment_ref": selected})
    assert denied.status_code == 403
    recovery = await assignment_client.get(
        "/api/v1/work-items/1/operations/selected-via-discovery", params={"assignment_ref": selected}
    )
    assert recovery.status_code == 403


async def test_rejoined_member_has_no_choices_from_the_previous_membership(
    assignment_client: httpx.AsyncClient, persistence_sessions: async_sessionmaker[AsyncSession]
) -> None:
    async with persistence_sessions.begin() as session:
        old = await session.get(OrganizationMembershipTable, (ORG, "membership:alice"))
        assert old is not None
        old.status = "revoked"
        old.ended_at = datetime.now(UTC)
        old.version += 1
        await session.flush()
        session.add(
            OrganizationMembershipTable(
                organization_ref=ORG,
                membership_ref="membership:alice-rejoined",
                principal_ref=PERSON,
                principal_type="human",
                source_ref="test:rejoin",
                valid_from=datetime.now(UTC),
            )
        )
    response = await assignment_client.get(BASE)
    assert response.status_code == 200 and response.json() == {"items": [], "next_after": None}


@pytest.mark.parametrize(
    "change",
    [
        "assignment_suspended",
        "assignment_revoked",
        "assignment_expired",
        "assignment_future",
        "role_suspended",
        "role_code",
        "revision_suspended",
        "revision_revoked",
        "revision_digest",
        "revision_policy",
        "catalog_suspended",
        "catalog_contract",
        "catalog_kind",
        "scope_suspended",
        "ancestor_suspended",
        "scope_cycle",
        "missing_action_member",
        "unknown_action_member",
    ],
)
async def test_inactive_or_inconsistent_choices_are_excluded(
    assignment_client: httpx.AsyncClient,
    persistence_sessions: async_sessionmaker[AsyncSession],
    change: str,
) -> None:
    now = datetime.now(UTC)
    async with persistence_sessions.begin() as session:
        assignment = await session.scalar(select(RoleAssignmentTable))
        role = await session.get(RoleDefinitionTable, (ORG, "role:engineering"))
        revision = await session.get(RoleVersionTable, (ORG, "role:engineering", 1))
        permission = await session.get(PermissionActionTable, "task.metadata.update")
        scope = await session.get(AuthorizationScopeTable, (ORG, "scope:engineering"))
        assert assignment is not None and role is not None and revision is not None
        assert permission is not None and scope is not None
        if change == "assignment_suspended":
            assignment.status = "suspended"
        elif change == "assignment_revoked":
            assignment.status, assignment.revoked_at = "revoked", now
        elif change == "assignment_expired":
            assignment.expires_at = now - timedelta(seconds=1)
        elif change == "assignment_future":
            assignment.valid_from = now + timedelta(days=1)
        elif change == "role_suspended":
            role.status = "suspended"
        elif change == "role_code":
            assignment.role_code = "forged-role-code"
        elif change == "revision_suspended":
            revision.status = "suspended"
        elif change == "revision_revoked":
            revision.status, revision.revoked_at = "revoked", now
        elif change == "revision_digest":
            revision.content_digest = "0" * 64
        elif change == "revision_policy":
            revision.policy_release_ref = "unknown-policy-release"
        elif change == "catalog_suspended":
            permission.status = "suspended"
        elif change == "catalog_contract":
            permission.contract_version = "future-version"
        elif change == "catalog_kind":
            permission.resource_kind = "another-resource"
        elif change == "scope_suspended":
            scope.status = "suspended"
        elif change in {"ancestor_suspended", "scope_cycle"}:
            parent = await session.get(AuthorizationScopeTable, (ORG, "scope:finance"))
            assert parent is not None
            scope.parent_scope_ref = parent.scope_ref
            if change == "ancestor_suspended":
                parent.status = "suspended"
            else:
                parent.parent_scope_ref = scope.scope_ref
        elif change == "missing_action_member":
            await session.execute(
                delete(RoleVersionActionTable).where(RoleVersionActionTable.action_ref == "task.metadata.update")
            )
        elif change == "unknown_action_member":
            session.add(
                PermissionActionTable(action_ref="task.future", resource_kind="work_item", contract_version="1")
            )
            await session.flush()
            session.add(
                RoleVersionActionTable(
                    organization_ref=ORG, role_ref="role:engineering", role_revision=1, action_ref="task.future"
                )
            )
            revision.content_digest = role_permissions_digest(
                ORG,
                "role:engineering",
                1,
                BUILTIN_POLICY_RELEASE_ID,
                ("task.view", "task.metadata.update", "task.future"),
            )
    response = await assignment_client.get(BASE)
    assert response.status_code == 200 and response.json() == {"items": [], "next_after": None}


async def test_other_people_old_memberships_and_legacy_rows_are_never_choices(
    assignment_client: httpx.AsyncClient, persistence_sessions: async_sessionmaker[AsyncSession]
) -> None:
    now = datetime.now(UTC)
    async with persistence_sessions.begin() as session:
        session.add(PrincipalTable(principal_ref="person:bob", principal_type="human"))
        await session.flush()
        session.add_all(
            [
                OrganizationMembershipTable(
                    organization_ref=ORG,
                    membership_ref="membership:bob",
                    principal_ref="person:bob",
                    principal_type="human",
                    source_ref="test",
                    valid_from=now - timedelta(days=1),
                ),
                OrganizationMembershipTable(
                    organization_ref=ORG,
                    membership_ref="membership:alice-old",
                    principal_ref=PERSON,
                    principal_type="human",
                    source_ref="test",
                    valid_from=now - timedelta(days=3),
                    ended_at=now - timedelta(days=2),
                    status="revoked",
                ),
            ]
        )
        await session.flush()
        await add_assignment(session, "assignment:bob", principal_ref="person:bob", membership_ref="membership:bob")
        await add_assignment(session, "assignment:alice-old", membership_ref="membership:alice-old")
        session.add(RoleAssignmentTable(principal_ref=PERSON, role_code="legacy", scope_ref="legacy:scope"))
    response = await assignment_client.get(BASE)
    assert response.status_code == 200
    assert [item["assignment_ref"] for item in response.json()["items"]] == ["assignment:engineering"]
    for cursor in ("assignment:bob", "assignment:does-not-exist"):
        page = await assignment_client.get(BASE, params={"after": cursor})
        assert page.status_code == 200
        assert [item["assignment_ref"] for item in page.json()["items"]] == ["assignment:engineering"]


async def test_bounded_pages_progress_even_when_a_page_has_only_filtered_candidates(
    assignment_client: httpx.AsyncClient,
    persistence_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with persistence_sessions.begin() as session:
        for reference in ("assignment:a", "assignment:b", "assignment:c"):
            item = await add_assignment(session, reference)
            if reference != "assignment:c":
                scope = await session.get(AuthorizationScopeTable, (ORG, item.scope_ref))
                assert scope is not None
                scope.status = "suspended"
    inspected: list[str | None] = []
    original = assignments.load_role_version

    async def counted(
        session: AsyncSession, organization_ref: str, assignment: RoleAssignmentTable
    ) -> ValidatedRoleVersion | None:
        inspected.append(assignment.assignment_ref)
        return await original(session, organization_ref, assignment)

    monkeypatch.setattr(assignments, "load_role_version", counted)
    first = await assignment_client.get(BASE, params={"limit": 2})
    assert first.status_code == 200
    assert first.json() == {"items": [], "next_after": "assignment:b"}
    assert inspected == ["assignment:a", "assignment:b"]
    second = await assignment_client.get(BASE, params={"limit": 2, "after": first.json()["next_after"]})
    assert second.status_code == 200
    assert [item["assignment_ref"] for item in second.json()["items"]] == ["assignment:c", "assignment:engineering"]
    assert second.json()["next_after"] is None


@pytest.mark.parametrize(
    "query",
    [
        {"principal_ref": PERSON},
        {"organization_ref": ORG},
        {"tenant_ref": ORG},
        {"membership_ref": "membership:alice"},
        {"assignment_ref": "assignment:engineering"},
        {"limit": 0},
        {"limit": 51},
        {"after": "bad\x00reference"},
        {"after": "bad reference"},
        {"after": "x" * 129},
    ],
)
async def test_caller_cannot_select_another_identity_or_unbounded_page(
    assignment_client: httpx.AsyncClient, query: dict[str, Any]
) -> None:
    response = await assignment_client.get(BASE, params=query)
    assert response.status_code == 422
    assert_private(response)


@pytest.mark.parametrize("credential", [None, "invalid", "service", "expired"])
async def test_authentication_is_required_and_service_credentials_are_not_human_choices(
    assignment_client: httpx.AsyncClient, signing_key: rsa.RSAPrivateKey, credential: str | None
) -> None:
    if credential is None:
        assignment_client.headers.pop("Authorization")
    else:
        bearer = "invalid"
        if credential == "service":
            bearer = token(signing_key, claims={"stage_credential_kind": "service"})
        elif credential == "expired":
            bearer = token(signing_key, claims={"exp": int(datetime.now(UTC).timestamp()) - 100})
        assignment_client.headers["Authorization"] = f"Bearer {bearer}"
    response = await assignment_client.get(BASE)
    assert response.status_code == 401
    assert_private(response)


async def test_final_identity_revalidation_rejects_a_changed_principal(
    assignment_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = assignment_provider.load_self_assignments

    async def changed(
        session: AsyncSession, evidence: HumanAuthenticationEvidence, **kwargs: Any
    ) -> assignments.AssignmentList:
        result = await original(session, evidence, **kwargs)
        principal = await session.scalar(select(PrincipalTable).where(PrincipalTable.principal_ref == PERSON))
        assert principal is not None
        principal.identity_epoch += 1
        await session.flush()
        return result

    monkeypatch.setattr(assignment_provider, "load_self_assignments", changed)
    response = await assignment_client.get(BASE)
    assert response.status_code == 403
    assert_private(response)
    monkeypatch.setattr(assignment_provider, "load_self_assignments", original)
    assert (await assignment_client.get(BASE)).status_code == 200


async def test_identity_expiry_during_final_validation_cannot_escape(
    assignment_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = IdentityFacade.validate_human
    validations = 0

    class ExpiredClock:
        @staticmethod
        def now(_timezone: Any) -> datetime:
            return datetime.now(UTC) + timedelta(days=1)

    async def expires_after_validation(
        self: IdentityFacade, session: AsyncSession, evidence: HumanAuthenticationEvidence, now: datetime
    ) -> None:
        nonlocal validations
        await original(self, session, evidence, now)
        validations += 1
        if validations == 2:
            monkeypatch.setattr(assignment_provider, "datetime", ExpiredClock)

    monkeypatch.setattr(IdentityFacade, "validate_human", expires_after_validation)
    response = await assignment_client.get(BASE)
    assert response.status_code == 401 and validations == 2
    assert_private(response)


async def test_assignment_expiry_during_final_validation_is_filtered(
    assignment_client: httpx.AsyncClient,
    persistence_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    async with persistence_sessions.begin() as session:
        assignment = await session.scalar(select(RoleAssignmentTable))
        assert assignment is not None
        assignment.expires_at = now + timedelta(seconds=10)
    original = IdentityFacade.validate_human
    validations = 0

    class LaterClock:
        @staticmethod
        def now(_timezone: Any) -> datetime:
            return now + timedelta(seconds=15)

    async def expires_after_validation(
        self: IdentityFacade, session: AsyncSession, evidence: HumanAuthenticationEvidence, current: datetime
    ) -> None:
        nonlocal validations
        await original(self, session, evidence, current)
        validations += 1
        if validations == 2:
            monkeypatch.setattr(assignment_provider, "datetime", LaterClock)

    monkeypatch.setattr(IdentityFacade, "validate_human", expires_after_validation)
    response = await assignment_client.get(BASE)
    assert response.status_code == 200 and response.json() == {"items": [], "next_after": None}


@pytest.mark.parametrize("assignment_client", [0.5], indirect=True)
async def test_discovery_timeout_rolls_back_and_releases_runtime_calls(
    assignment_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = assignment_provider.load_self_assignments
    cancelled = asyncio.Event()

    async def stalled(*args: Any, **kwargs: Any) -> assignments.AssignmentList:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    monkeypatch.setattr(assignment_provider, "load_self_assignments", stalled)
    response = await assignment_client.get(BASE)
    assert response.status_code == 503 and cancelled.is_set()
    assert_private(response)
    monkeypatch.setattr(assignment_provider, "load_self_assignments", original)
    assert (await assignment_client.get(BASE)).status_code == 200
