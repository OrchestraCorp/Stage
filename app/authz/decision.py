from dataclasses import dataclass
from datetime import UTC, datetime

from app.authz.attributes import evaluate_attributes
from app.authz.context import AuthorizationContext, is_aware
from app.authz.permissions import ACTION_POLICIES, Permission, RoleCode, permissions_of
from app.authz.relations import relation_for_group
from app.core.enums import DecisionOutcome, PrincipalType


@dataclass(frozen=True)
class AuthorizationDecision:
    outcome: DecisionOutcome
    action: str
    subject_ref: str
    resource_ref: str
    matched_roles: tuple[str, ...] = ()
    matched_relations: tuple[str, ...] = ()
    failed_constraints: tuple[str, ...] = ()
    policy_release_id: str = "unreleased"
    evaluated_at: datetime | None = None
    decision_id: str = "unpersisted"


class AuthorizationEngine:
    """Fail-closed RBAC + ReBAC + ABAC authorization kernel."""

    def decide(self, context: AuthorizationContext) -> AuthorizationDecision:
        now = context.now or datetime.now(UTC)
        if not is_aware(now):
            return self._deny(context, datetime.now(UTC), "time.naive")
        now = now.astimezone(UTC)
        try:
            principal_type = PrincipalType(context.principal_type)
        except ValueError:
            return self._deny(context, now, "principal.type.unknown")
        try:
            permission = Permission(context.action)
        except ValueError:
            return self._deny(context, now, "action.unknown")
        policy = ACTION_POLICIES.get(permission)
        if policy is None:
            return self._deny(context, now, "action.unregistered")

        assignment = context.assignment
        if assignment is None:
            return self._deny(context, now, "assignment.missing")
        try:
            role = RoleCode(assignment.role_code)
        except ValueError:
            return self._deny(context, now, "role.unknown")

        effective_subjects: tuple[str, ...] = (context.subject_ref,)
        if principal_type == PrincipalType.AGENT:
            delegation = context.delegation
            if delegation is None:
                return self._deny(context, now, "delegation.missing", outcome=DecisionOutcome.NEEDS_DELEGATION)
            if delegation.agent_ref != context.subject_ref:
                return self._deny(context, now, "delegation.agent_mismatch", outcome=DecisionOutcome.NEEDS_DELEGATION)
            if not delegation.is_active_at(now):
                return self._deny(context, now, "delegation.expired", outcome=DecisionOutcome.EXPIRED)
            if not delegation.permits(
                action=context.action,
                resource_ref=context.resource_ref,
                organization_ref=context.organization_ref or "",
            ):
                return self._deny(context, now, "delegation.scope", outcome=DecisionOutcome.NEEDS_DELEGATION)
            if (
                delegation.assignment_ref != assignment.assignment_ref
                or delegation.delegator_ref != assignment.principal_ref
            ):
                return self._deny(
                    context,
                    now,
                    "delegation.assignment_mismatch",
                    outcome=DecisionOutcome.NEEDS_DELEGATION,
                )
            if assignment.organization_ref != delegation.organization_ref:
                return self._deny(
                    context,
                    now,
                    "delegation.organization_mismatch",
                    outcome=DecisionOutcome.NEEDS_DELEGATION,
                )
            if context.attributes.delegation_active is not True:
                return self._deny(context, now, "delegation.active", outcome=DecisionOutcome.EXPIRED)
            if context.attributes.delegator_active is not True:
                return self._deny(context, now, "delegator.active")
            effective_subjects = (context.subject_ref, delegation.delegator_ref)
        elif context.delegation is not None:
            return self._deny(context, now, "delegation.unexpected")

        if assignment.principal_ref not in effective_subjects:
            return self._deny(context, now, "assignment.principal_mismatch")
        if not assignment.is_active_at(now):
            return self._deny(context, now, "assignment.expired", outcome=DecisionOutcome.EXPIRED)
        if context.organization_ref is None or context.resource_organization_ref is None:
            return self._deny(context, now, "organization.missing")
        if context.organization_ref != context.resource_organization_ref:
            return self._deny(context, now, "organization.mismatch")
        if not assignment.covers(
            organization_ref=context.organization_ref,
            resource_scopes=context.resource_scope_refs,
        ):
            return self._deny(context, now, "scope.missing")

        granted = permissions_of((role,))
        if permission not in granted:
            return self._deny(context, now, "permission.missing", matched_roles=(role.value,))

        matched_relations: list[str] = []
        for group in policy.relation_any_of:
            matched = relation_for_group(
                context.relations,
                subjects=effective_subjects,
                relation_names=group,
                resource_ref=context.resource_ref,
                now=now,
            )
            if matched is None:
                return self._deny(
                    context,
                    now,
                    "relation.missing:" + "|".join(sorted(group)),
                    matched_roles=(role.value,),
                    matched_relations=tuple(matched_relations),
                )
            matched_relations.append(matched)

        failed = evaluate_attributes(context.attributes, require_object_version=policy.require_object_version)
        if policy.allowed_resource_states and context.attributes.resource_state not in policy.allowed_resource_states:
            failed = (*failed, "resource.state")
        if failed:
            return self._deny(
                context,
                now,
                *failed,
                matched_roles=(role.value,),
                matched_relations=tuple(matched_relations),
            )
        return AuthorizationDecision(
            outcome=DecisionOutcome.ALLOWED,
            action=context.action,
            subject_ref=context.subject_ref,
            resource_ref=context.resource_ref,
            matched_roles=(role.value,),
            matched_relations=tuple(matched_relations),
            policy_release_id=context.policy_release_id,
            evaluated_at=now,
        )

    @staticmethod
    def _deny(
        context: AuthorizationContext,
        now: datetime,
        *reasons: str,
        outcome: DecisionOutcome = DecisionOutcome.DENIED,
        matched_roles: tuple[str, ...] = (),
        matched_relations: tuple[str, ...] = (),
    ) -> AuthorizationDecision:
        return AuthorizationDecision(
            outcome=outcome,
            action=context.action,
            subject_ref=context.subject_ref,
            resource_ref=context.resource_ref,
            matched_roles=matched_roles,
            matched_relations=matched_relations,
            failed_constraints=tuple(reasons),
            policy_release_id=context.policy_release_id,
            evaluated_at=now,
        )
