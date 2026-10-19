from datetime import UTC, datetime, timedelta
from typing import Any, cast

from app.authz.context import AttributeContext, AuthorizationContext, Delegation, RelationTuple, RoleAssignment
from app.authz.decision import AuthorizationEngine
from app.core.enums import DecisionOutcome, PrincipalType

NOW = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)


def context(**changes: object) -> AuthorizationContext:
    values: dict[str, object] = {
        "request_id": "req-security",
        "subject_ref": "member:alice",
        "principal_type": PrincipalType.HUMAN,
        "action": "task.delivery.submit",
        "resource_ref": "work_item:1",
        "organization_ref": "org:acme",
        "resource_organization_ref": "org:acme",
        "resource_scope_refs": ("org:acme", "work_item:1"),
        "assignment": RoleAssignment(
            "assignment:alice", "member:alice", "engineering-member", "org:acme", ("org:acme",)
        ),
        "relations": (RelationTuple("member:alice", "assignee_of", "work_item:1"),),
        "now": NOW,
        "attributes": AttributeContext(True, "internal", "internal", 4, 4),
    }
    values.update(changes)
    return AuthorizationContext(**cast(dict[str, Any], values))


def test_unknown_action_and_role_fail_closed() -> None:
    unknown_action = AuthorizationEngine().decide(context(action="task.never")).failed_constraints
    unknown_role = (
        AuthorizationEngine()
        .decide(context(assignment=RoleAssignment("a", "member:alice", "root", "org:acme", ("org:acme",))))
        .failed_constraints
    )
    assert "action.unknown" in unknown_action
    assert "role.unknown" in unknown_role


def test_scope_without_record_relation_is_denied() -> None:
    decision = AuthorizationEngine().decide(context(relations=()))
    assert decision.outcome is DecisionOutcome.DENIED
    assert any(item.startswith("relation.missing:") for item in decision.failed_constraints)


def test_cross_organization_and_stale_version_are_denied() -> None:
    cross_org = AuthorizationEngine().decide(context(resource_organization_ref="org:other"))
    stale = AuthorizationEngine().decide(context(attributes=AttributeContext(True, "internal", "internal", 3, 4)))
    assert "organization.mismatch" in cross_org.failed_constraints
    assert "object.version" in stale.failed_constraints


def test_agent_requires_matching_active_unrevoked_delegation() -> None:
    assignment = RoleAssignment("assignment:alice", "member:alice", "engineering-member", "org:acme", ("org:acme",))
    delegation = Delegation(
        "delegation:1",
        "agent:maestro",
        "member:alice",
        assignment.assignment_ref,
        "org:acme",
        ("task.delivery.submit",),
        ("work_item:1",),
        NOW - timedelta(minutes=1),
        NOW + timedelta(minutes=1),
    )
    base = context(
        subject_ref="agent:maestro",
        principal_type=PrincipalType.AGENT,
        assignment=assignment,
        delegation=delegation,
        attributes=AttributeContext(
            True,
            "internal",
            "internal",
            4,
            4,
            delegation_active=True,
            delegator_active=True,
        ),
    )
    assert AuthorizationEngine().decide(base).outcome is DecisionOutcome.ALLOWED
    revoked_delegation = Delegation(
        delegation.delegation_ref,
        delegation.agent_ref,
        delegation.delegator_ref,
        delegation.assignment_ref,
        delegation.organization_ref,
        delegation.allowed_actions,
        delegation.resource_refs,
        delegation.valid_from,
        delegation.expires_at,
        revoked_at=NOW - timedelta(seconds=1),
    )
    revoked = AuthorizationEngine().decide(
        context(
            subject_ref="agent:maestro",
            principal_type=PrincipalType.AGENT,
            assignment=assignment,
            delegation=revoked_delegation,
            attributes=AttributeContext(
                True, "internal", "internal", 4, 4, delegation_active=True, delegator_active=True
            ),
        )
    )
    assert revoked.outcome is DecisionOutcome.EXPIRED


def test_naive_clock_and_missing_attributes_fail_closed() -> None:
    naive = AuthorizationEngine().decide(context(now=datetime(2026, 10, 19, 10, 0)))
    missing = AuthorizationEngine().decide(context(attributes=AttributeContext(subject_active=True)))
    assert "time.naive" in naive.failed_constraints
    assert "resource.classification.missing" in missing.failed_constraints
