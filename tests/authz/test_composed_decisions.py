from datetime import UTC, datetime

from app.authz.context import AttributeContext, AuthorizationContext, RelationTuple
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
        "role_codes": ("engineering-member",),
        "relations": (
            RelationTuple(subject_ref="member:alice", relation="assignee_of", resource_ref="work_item:1"),
        ),
        "now": now,
        "attributes": AttributeContext(
            resource_classification="internal",
            classification_ceiling="internal",
            object_version=7,
            expected_object_version=7,
        ),
        "policy_release_id": "policy:test",
    }
    values.update(kwargs)
    return AuthorizationContext(**values)


def test_rbac_rebac_abac_allow() -> None:
    decision = AuthorizationEngine().decide(_context(), required_relations=("assignee_of",))
    assert decision.outcome is DecisionOutcome.ALLOWED


def test_missing_relation_denies_even_with_role() -> None:
    ctx = _context(relations=())
    decision = AuthorizationEngine().decide(ctx, required_relations=("assignee_of",))
    assert decision.outcome is DecisionOutcome.DENIED
    assert "relation.missing:assignee_of" in decision.failed_constraints


def test_attribute_constraint_denies() -> None:
    ctx = _context(attributes=AttributeContext(resource_classification="restricted", classification_ceiling="internal"))
    decision = AuthorizationEngine().decide(ctx, required_relations=("assignee_of",))
    assert decision.outcome is DecisionOutcome.DENIED
    assert "resource.classification" in decision.failed_constraints
