from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from app.authz.context import AttributeContext, AuthorizationContext, RelationTuple, RoleAssignment
from app.authz.decision import AuthorizationEngine
from app.core.enums import DecisionOutcome, PrincipalType

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def assignment(**changes: Any) -> RoleAssignment:
    values: dict[str, Any] = {
        "assignment_ref": "assignment:custom",
        "principal_ref": "person:alice",
        "role_code": "delivery-review-assistant",
        "organization_ref": "org:stage",
        "scope_refs": ("scope:engineering",),
        "permission_actions": ("task.view", "task.assign"),
        "role_ref": "role:custom",
        "role_revision": 3,
        "role_version_digest": "a" * 64,
        "membership_ref": "membership:alice",
    }
    values.update(changes)
    return RoleAssignment(**values)


def context(selected: RoleAssignment) -> AuthorizationContext:
    return AuthorizationContext(
        request_id="request:versioned-role",
        subject_ref="person:alice",
        principal_type=PrincipalType.HUMAN,
        action="task.assign",
        resource_ref="work_item:1",
        organization_ref="org:stage",
        resource_organization_ref="org:stage",
        resource_scope_refs=("scope:engineering",),
        assignment=selected,
        relations=(RelationTuple("person:alice", "owner_of", "work_item:1"),),
        now=NOW,
        attributes=AttributeContext(True, "internal", "internal", 1, 1, resource_state="open"),
    )


def test_custom_role_uses_exact_pinned_actions_and_still_requires_platform_policy() -> None:
    facts = context(assignment())
    allowed = AuthorizationEngine().decide(facts)
    assert allowed.outcome is DecisionOutcome.ALLOWED
    assert allowed.matched_roles == ("delivery-review-assistant",)

    missing_action = AuthorizationEngine().decide(
        replace(facts, assignment=assignment(permission_actions=("task.view",)))
    )
    assert missing_action.outcome is DecisionOutcome.DENIED
    assert missing_action.failed_constraints == ("permission.missing",)

    missing_relation = AuthorizationEngine().decide(replace(facts, relations=()))
    assert missing_relation.outcome is DecisionOutcome.DENIED
    assert missing_relation.failed_constraints == ("relation.missing:owner_of",)
    invalid_state = AuthorizationEngine().decide(
        replace(facts, attributes=replace(facts.attributes, resource_state="closed"))
    )
    assert invalid_state.outcome is DecisionOutcome.DENIED
    assert "resource.state" in invalid_state.failed_constraints


def test_empty_pinned_actions_do_not_fall_back_to_a_builtin_role() -> None:
    facts = context(assignment(role_code="department-owner", permission_actions=()))
    decision = AuthorizationEngine().decide(facts)
    assert decision.outcome is DecisionOutcome.DENIED
    assert decision.failed_constraints == ("permission.missing",)


def test_unknown_action_in_pinned_set_invalidates_the_set() -> None:
    facts = context(assignment(permission_actions=("task.assign", "task.unregistered")))
    decision = AuthorizationEngine().decide(facts)
    assert decision.outcome is DecisionOutcome.DENIED
    assert decision.failed_constraints == ("role.action.unknown",)


@pytest.mark.parametrize(
    "changes",
    [
        {"role_ref": None},
        {"membership_ref": None},
        {"role_revision": None},
        {"role_revision": 0},
        {"role_revision": True},
        {"role_version_digest": None},
        {"role_version_digest": "not-a-digest"},
        {"role_version_digest": "g" * 64},
        {"permission_actions": ["task.assign"]},
    ],
)
def test_incomplete_or_malformed_pinned_metadata_is_denied(changes: dict[str, Any]) -> None:
    decision = AuthorizationEngine().decide(context(assignment(**changes)))
    assert decision.outcome is DecisionOutcome.DENIED
    assert decision.failed_constraints == ("role.version.invalid",)


def test_partial_version_metadata_never_falls_back_to_legacy_role_permissions() -> None:
    decision = AuthorizationEngine().decide(context(assignment(role_code="department-owner", permission_actions=None)))
    assert decision.outcome is DecisionOutcome.DENIED
    assert decision.failed_constraints == ("role.version.incomplete",)
