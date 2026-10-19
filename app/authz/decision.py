from dataclasses import dataclass
from datetime import UTC, datetime

from app.authz.attributes import evaluate_attributes
from app.authz.context import AuthorizationContext
from app.authz.permissions import Permission, permissions_of
from app.authz.relations import has_relation
from app.core.enums import DecisionOutcome


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
    """首版纯函数式授权门面；数据库、缓存和审计由上层事务接入。"""

    def decide(
        self,
        context: AuthorizationContext,
        *,
        required_relations: tuple[str, ...] = (),
    ) -> AuthorizationDecision:
        now = context.now or datetime.now(UTC)
        try:
            permission = Permission(context.action)
        except ValueError:
            return AuthorizationDecision(
                outcome=DecisionOutcome.DENIED,
                action=context.action,
                subject_ref=context.subject_ref,
                resource_ref=context.resource_ref,
                failed_constraints=("action.unknown",),
                policy_release_id=context.policy_release_id,
                evaluated_at=now,
            )

        # RoleCode 是 StrEnum；这里局部转换避免让外部配置的未知角色阻断整个请求。
        from app.authz.permissions import RoleCode

        valid_roles_list: list[RoleCode] = []
        for role in context.role_codes:
            try:
                valid_roles_list.append(RoleCode(role))
            except ValueError:
                continue
        valid_roles = tuple(valid_roles_list)
        granted = permissions_of(valid_roles)
        if permission not in granted:
            return AuthorizationDecision(
                outcome=DecisionOutcome.DENIED,
                action=context.action,
                subject_ref=context.subject_ref,
                resource_ref=context.resource_ref,
                matched_roles=tuple(role.value for role in valid_roles),
                failed_constraints=("permission.missing",),
                policy_release_id=context.policy_release_id,
                evaluated_at=now,
            )

        missing_relations = tuple(
            relation
            for relation in required_relations
            if not has_relation(
                context.relations,
                subject_ref=context.subject_ref,
                relation=relation,
                resource_ref=context.resource_ref,
                now=now,
            )
        )
        if missing_relations:
            return AuthorizationDecision(
                outcome=DecisionOutcome.DENIED,
                action=context.action,
                subject_ref=context.subject_ref,
                resource_ref=context.resource_ref,
                matched_roles=tuple(role.value for role in valid_roles),
                failed_constraints=tuple(f"relation.missing:{item}" for item in missing_relations),
                policy_release_id=context.policy_release_id,
                evaluated_at=now,
            )

        failed = evaluate_attributes(context.attributes)
        if failed:
            outcome = DecisionOutcome.EXPIRED if "delegation.active" in failed else DecisionOutcome.DENIED
            return AuthorizationDecision(
                outcome=outcome,
                action=context.action,
                subject_ref=context.subject_ref,
                resource_ref=context.resource_ref,
                matched_roles=tuple(role.value for role in valid_roles),
                matched_relations=required_relations,
                failed_constraints=failed,
                policy_release_id=context.policy_release_id,
                evaluated_at=now,
            )

        return AuthorizationDecision(
            outcome=DecisionOutcome.ALLOWED,
            action=context.action,
            subject_ref=context.subject_ref,
            resource_ref=context.resource_ref,
            matched_roles=tuple(role.value for role in valid_roles),
            matched_relations=required_relations,
            policy_release_id=context.policy_release_id,
            evaluated_at=now,
        )
