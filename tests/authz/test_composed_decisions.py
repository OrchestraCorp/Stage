from datetime import UTC, datetime
from typing import Any, cast

from app.authz.context import AttributeContext, AuthorizationContext, RelationTuple, RoleAssignment
from app.authz.decision import AuthorizationEngine
from app.core.enums import DecisionOutcome, PrincipalType


def _context(**kwargs: object) -> AuthorizationContext:
    now = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
    values = {
        "request_id": "req-1",
        "subject_ref": "member:alice",
        "principal_type": PrincipalType.HUMAN,
        "action": "task.delivery.submit",
        "resource_ref": "work_item:1",
        "organization_ref": "org:acme",
        "resource_organization_ref": "org:acme",
        "resource_scope_refs": ("org:acme", "work_item:1"),
        "assignment": RoleAssignment(
            assignment_ref="assignment:alice-eng",
            principal_ref="member:alice",
            role_code="engineering-member",
            organization_ref="org:acme",
            scope_refs=("org:acme", "work_item:1"),
        ),
        "relations": (RelationTuple(subject_ref="member:alice", relation="assignee_of", resource_ref="work_item:1"),),
        "now": now,
        "attributes": AttributeContext(
            resource_classification="internal",
            classification_ceiling="internal",
            subject_active=True,
            object_version=7,
            expected_object_version=7,
        ),
        "policy_release_id": "policy:test",
    }
    values.update(kwargs)
    return AuthorizationContext(**cast(dict[str, Any], values))


def test_rbac_rebac_abac_allow() -> None:
    decision = AuthorizationEngine().decide(_context())
    assert decision.outcome is DecisionOutcome.ALLOWED


def test_missing_relation_denies_even_with_role() -> None:
    ctx = _context(relations=())
    decision = AuthorizationEngine().decide(ctx)
    assert decision.outcome is DecisionOutcome.DENIED
    assert "relation.missing:assignee_of" in decision.failed_constraints


def test_attribute_constraint_denies() -> None:
    ctx = _context(
        attributes=AttributeContext(
            subject_active=True,
            resource_classification="restricted",
            classification_ceiling="internal",
            object_version=7,
            expected_object_version=7,
        )
    )
    decision = AuthorizationEngine().decide(ctx)
    assert decision.outcome is DecisionOutcome.DENIED
    assert "resource.classification" in decision.failed_constraints
