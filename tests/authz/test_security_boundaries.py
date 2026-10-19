from collections.abc import MutableMapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest

from app.authz.context import AttributeContext, AuthorizationContext, Delegation, RelationTuple, RoleAssignment
from app.authz.decision import AuthorizationEngine
from app.authz.permissions import (
    ACTION_POLICIES,
    BUILTIN_POLICY_RELEASE_ID,
    ROLE_PERMISSIONS,
    ActionPolicy,
    Permission,
    RoleCode,
)
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
        "attributes": AttributeContext(True, "internal", "internal", 4, 4, resource_state="executing"),
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
            resource_state="executing",
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
                True,
                "internal",
                "internal",
                4,
                4,
                resource_state="executing",
                delegation_active=True,
                delegator_active=True,
            ),
        )
    )
    assert revoked.outcome is DecisionOutcome.DENIED
    assert revoked.failed_constraints == ("delegation.revoked",)


def test_naive_clock_and_missing_attributes_fail_closed() -> None:
    naive = AuthorizationEngine().decide(context(now=datetime(2026, 10, 19, 10, 0)))
    missing = AuthorizationEngine().decide(context(attributes=AttributeContext(subject_active=True)))
    assert "time.naive" in naive.failed_constraints
    assert "resource.classification.missing" in missing.failed_constraints


@pytest.mark.parametrize(
    ("current_version", "expected_version", "failure"),
    [
        (None, 4, "object.version.missing"),
        (4, None, "object.expected_version.missing"),
        (0, 0, "object.version.invalid"),
        (-1, -1, "object.version.invalid"),
        (True, True, "object.version.invalid"),
    ],
)
def test_write_requires_current_and_expected_positive_versions(
    current_version: int | None, expected_version: int | None, failure: str
) -> None:
    attributes = AttributeContext(
        True, "internal", "internal", current_version, expected_version, resource_state="executing"
    )
    decision = AuthorizationEngine().decide(context(attributes=attributes))
    assert decision.outcome is DecisionOutcome.DENIED
    assert failure in decision.failed_constraints


@pytest.mark.parametrize(
    ("action", "role", "relation", "state"),
    [
        ("task.view", "engineering-member", "viewer_of", "closed"),
        ("task.commit.create", "engineering-member", "assignee_of", "open"),
        ("task.delivery.submit", "engineering-member", "assignee_of", "committed"),
        ("task.delivery.submit", "engineering-member", "assignee_of", "executing"),
        ("task.acceptance.review", "department-owner", "reviewer_of", "awaiting_acceptance"),
        ("task.assign", "department-owner", "owner_of", "draft"),
        ("task.assign", "department-owner", "owner_of", "open"),
    ],
)
def test_task_state_rules_are_central_and_cannot_be_omitted(action: str, role: str, relation: str, state: str) -> None:
    base = context()
    assert base.assignment is not None
    ctx = replace(
        base,
        action=action,
        assignment=replace(base.assignment, role_code=role),
        relations=(RelationTuple("member:alice", relation, "work_item:1"),),
        attributes=replace(base.attributes, resource_state=state),
    )
    assert AuthorizationEngine().decide(ctx).outcome is DecisionOutcome.ALLOWED
    for forbidden in (None, "unknown-state"):
        denied = AuthorizationEngine().decide(
            replace(ctx, attributes=replace(ctx.attributes, resource_state=forbidden))
        )
        assert denied.outcome is DecisionOutcome.DENIED
        assert any(reason.startswith("resource.state") for reason in denied.failed_constraints)


def test_query_does_not_require_write_versions_but_cannot_reopen_closed_task() -> None:
    base = context()
    query = replace(
        base,
        action="task.view",
        attributes=replace(base.attributes, object_version=None, expected_object_version=None, resource_state="closed"),
    )
    assert AuthorizationEngine().decide(query).outcome is DecisionOutcome.ALLOWED
    denied = AuthorizationEngine().decide(replace(base, attributes=replace(base.attributes, resource_state="closed")))
    assert denied.failed_constraints == ("resource.state",)


def test_first_claim_requires_explicit_eligibility_without_prior_assignment() -> None:
    base = context()
    claim = replace(
        base,
        action="task.commit.create",
        attributes=replace(base.attributes, resource_state="open"),
        relations=(RelationTuple("member:alice", "eligible_claimant_of", "work_item:1"),),
    )
    assert AuthorizationEngine().decide(claim).outcome is DecisionOutcome.ALLOWED
    assert AuthorizationEngine().decide(replace(claim, relations=())).outcome is DecisionOutcome.DENIED


def test_tenant_membership_alone_does_not_expand_department_assignment() -> None:
    base = context()
    assert base.assignment is not None
    restricted = replace(
        base,
        assignment=replace(base.assignment, scope_refs=("department:engineering",)),
        resource_scope_refs=("org:acme", "department:finance", base.resource_ref),
    )
    assert AuthorizationEngine().decide(restricted).failed_constraints == ("scope.missing",)
    # A deliberately granted organization-wide scope is broader.
    explicit_org = replace(restricted, assignment=replace(base.assignment, scope_refs=("org:acme",)))
    assert AuthorizationEngine().decide(explicit_org).outcome is DecisionOutcome.ALLOWED


@pytest.mark.parametrize(
    ("changes", "outcome", "reason"),
    [
        ({"revoked_at": NOW}, DecisionOutcome.DENIED, "delegation.revoked"),
        ({"status": "revoked"}, DecisionOutcome.DENIED, "delegation.revoked"),
        ({"status": "disabled"}, DecisionOutcome.DENIED, "delegation.inactive"),
        ({"valid_from": NOW + timedelta(seconds=1)}, DecisionOutcome.DENIED, "delegation.not_yet_valid"),
        ({"expires_at": NOW}, DecisionOutcome.EXPIRED, "delegation.expired"),
        (
            {"valid_from": NOW + timedelta(minutes=3)},
            DecisionOutcome.DENIED,
            "delegation.time.invalid_window",
        ),
        ({"valid_from": NOW.replace(tzinfo=None)}, DecisionOutcome.DENIED, "delegation.time.invalid"),
    ],
)
def test_agent_temporal_failures_are_distinguishable(
    changes: dict[str, Any], outcome: DecisionOutcome, reason: str
) -> None:
    base = context()
    assert base.assignment is not None
    delegation = Delegation(
        "delegation:1",
        "agent:maestro",
        "member:alice",
        base.assignment.assignment_ref,
        "org:acme",
        (base.action,),
        (base.resource_ref,),
        NOW - timedelta(minutes=1),
        NOW + timedelta(minutes=1),
    )
    agent = replace(
        base,
        subject_ref="agent:maestro",
        principal_type=PrincipalType.AGENT,
        delegation=replace(delegation, **changes),
        attributes=replace(base.attributes, delegation_active=True, delegator_active=True),
    )
    decision = AuthorizationEngine().decide(agent)
    assert decision.outcome is outcome
    assert decision.failed_constraints == (reason,)


def test_assignment_scope_and_timing_cannot_be_widened_by_relations() -> None:
    base = context()
    assert base.assignment is not None
    for changed_assignment, reason in (
        (replace(base.assignment, principal_ref="member:bob"), "assignment.principal_mismatch"),
        (replace(base.assignment, scope_refs=("department:other",)), "scope.missing"),
        (replace(base.assignment, valid_from=NOW + timedelta(minutes=1)), "assignment.not_yet_valid"),
        (replace(base.assignment, status="disabled"), "assignment.inactive"),
        (replace(base.assignment, expires_at=NOW), "assignment.expired"),
    ):
        decision = AuthorizationEngine().decide(replace(base, assignment=changed_assignment))
        assert decision.outcome is not DecisionOutcome.ALLOWED
        assert decision.failed_constraints == (reason,)


def test_agent_cannot_combine_delegator_role_with_its_own_relation() -> None:
    base = context()
    assert base.assignment is not None
    delegation = Delegation(
        "delegation:1",
        "agent:maestro",
        "member:alice",
        base.assignment.assignment_ref,
        "org:acme",
        (base.action,),
        (base.resource_ref,),
        NOW - timedelta(minutes=1),
        NOW + timedelta(minutes=1),
    )
    agent = replace(
        base,
        subject_ref="agent:maestro",
        principal_type=PrincipalType.AGENT,
        delegation=delegation,
        relations=(RelationTuple("agent:maestro", "assignee_of", base.resource_ref),),
        attributes=replace(base.attributes, delegation_active=True, delegator_active=True),
    )
    assert AuthorizationEngine().decide(agent).failed_constraints == ("relation.missing:assignee_of",)
    # The same delegation can use the delegator's existing relationship.
    assert AuthorizationEngine().decide(replace(agent, relations=base.relations)).outcome is DecisionOutcome.ALLOWED

    for changes, reason in (
        ({"delegation_ref": ""}, "delegation.reference.missing"),
        ({"allowed_actions": ("task.view",)}, "delegation.scope"),
        ({"resource_refs": ("work_item:other",)}, "delegation.scope"),
        ({"assignment_ref": "assignment:other"}, "delegation.assignment_mismatch"),
        ({"delegator_ref": "member:bob"}, "delegation.assignment_mismatch"),
    ):
        narrowed = replace(agent, relations=base.relations, delegation=replace(delegation, **changes))
        assert AuthorizationEngine().decide(narrowed).failed_constraints == (reason,)


def test_policy_and_role_registries_are_read_only() -> None:
    actions = cast(MutableMapping[Permission, ActionPolicy], ACTION_POLICIES)
    roles = cast(MutableMapping[RoleCode, frozenset[Permission]], ROLE_PERMISSIONS)
    with pytest.raises(TypeError):
        actions[Permission.TASK_DELIVERY_SUBMIT] = ActionPolicy(Permission.TASK_DELIVERY_SUBMIT, ())
    with pytest.raises(TypeError):
        roles[RoleCode.PLATFORM_OPERATOR] = frozenset(Permission)


def test_each_decision_has_a_traceable_unique_identifier() -> None:
    ctx = context()
    first, second = AuthorizationEngine().decide(ctx), AuthorizationEngine().decide(ctx)
    assert UUID(first.decision_id)
    assert first.decision_id != second.decision_id
    assert first.request_id == ctx.request_id
    assert first.assignment_ref == "assignment:alice"
    denied = AuthorizationEngine().decide(replace(ctx, relations=()))
    assert UUID(denied.decision_id)
    assert denied.request_id == ctx.request_id


@pytest.mark.parametrize(
    ("requested_release", "reason"),
    [
        ("", "policy_release.missing"),
        ("   ", "policy_release.missing"),
        ("unreleased", "policy_release.unknown"),
        ("policy:unloaded-enterprise-release", "policy_release.unknown"),
    ],
)
def test_only_the_actual_builtin_policy_release_can_be_evaluated(requested_release: str, reason: str) -> None:
    decision = AuthorizationEngine().decide(context(policy_release_id=requested_release))
    assert decision.outcome is DecisionOutcome.DENIED
    assert decision.failed_constraints == (reason,)
    assert decision.policy_release_id == BUILTIN_POLICY_RELEASE_ID


def test_default_context_reports_the_actual_builtin_policy_release() -> None:
    decision = AuthorizationEngine().decide(context())
    assert decision.outcome is DecisionOutcome.ALLOWED
    assert decision.policy_release_id == BUILTIN_POLICY_RELEASE_ID


def test_empty_assignment_and_scope_references_cannot_authorize() -> None:
    base = context()
    assert base.assignment is not None
    invalid_assignment = replace(base, assignment=replace(base.assignment, assignment_ref=""))
    invalid_scope = replace(base, resource_scope_refs=("",))
    assert AuthorizationEngine().decide(invalid_assignment).failed_constraints == ("assignment.reference.missing",)
    assert AuthorizationEngine().decide(invalid_scope).failed_constraints == ("scope.invalid",)
