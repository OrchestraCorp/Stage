"""Current human authorization facts from the deployment's authoritative database.

This is an internal adapter, not authentication. Its ExecutionContext must come
from a trusted host adapter. No route may deserialize that context from a body.
All authorization writers acquire the same organization guard; the caller keeps
that guard until the command transaction commits. No cached decision or network
request participates in this loader.
"""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.context import AttributeContext, AuthorizationContext, RelationTuple, RoleAssignment, validity_failure
from app.authz.permissions import ACTION_POLICIES, BUILTIN_POLICY_RELEASE_ID, Permission
from app.core.enums import PrincipalType
from app.execution.contracts import ExecutionContext
from app.execution.encoding import digest
from app.execution.errors import AuthorizationDenied
from app.execution.storage import lock_tenant
from app.models.authorization import (
    AuthorizationResourceTable,
    AuthorizationScopeTable,
    PermissionActionTable,
    RelationTupleTable,
    RoleAssignmentTable,
    RoleDefinitionTable,
    RoleVersionActionTable,
    RoleVersionTable,
)
from app.models.execution import TenantGuardTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import OrganizationMembershipTable, OrganizationRootTable
from app.models.work import WorkItemTable


def role_permissions_digest(
    organization_ref: str,
    role_ref: str,
    role_revision: int,
    policy_release_ref: str,
    actions: Iterable[str],
) -> str:
    """Canonical digest of the immutable role-version capability set."""
    return digest(
        {
            "schema": "stage-role-permissions-v1",
            "organization_ref": organization_ref,
            "role_ref": role_ref,
            "role_revision": role_revision,
            "policy_release_ref": policy_release_ref,
            "actions": sorted(actions),
        }
    )


_SUPPORTED_ACTIONS = frozenset(
    {
        Permission.TASK_VIEW,
        Permission.TASK_METADATA_UPDATE,
        Permission.TASK_COMMIT_CREATE,
        Permission.TASK_DELIVERY_SUBMIT,
        Permission.TASK_ACCEPTANCE_REVIEW,
        Permission.TASK_ASSIGN,
    }
)


@dataclass(frozen=True, slots=True)
class ValidatedRoleVersion:
    role: RoleDefinitionTable
    revision: RoleVersionTable
    actions: tuple[str, ...]
    registered: tuple[PermissionActionTable, ...]


async def load_role_version(
    session: AsyncSession, organization_ref: str, assignment: RoleAssignmentTable
) -> ValidatedRoleVersion | None:
    """Read a pinned capability set under the caller-held organization guard.

    Shared by execution and assignment discovery so neither trusts an inactive,
    mutated or partially supported revision. This is not an authorization grant.
    """
    if assignment.organization_ref != organization_ref or assignment.role_ref is None:
        return None
    refresh = {"populate_existing": True}
    role = await session.scalar(
        select(RoleDefinitionTable)
        .where(
            RoleDefinitionTable.organization_ref == organization_ref,
            RoleDefinitionTable.role_ref == assignment.role_ref,
        )
        .execution_options(**refresh)
    )
    revision = await session.scalar(
        select(RoleVersionTable)
        .where(
            RoleVersionTable.organization_ref == organization_ref,
            RoleVersionTable.role_ref == assignment.role_ref,
            RoleVersionTable.role_revision == assignment.role_revision,
        )
        .execution_options(**refresh)
    )
    if (
        role is None
        or role.status != "active"
        or role.role_code != assignment.role_code
        or revision is None
        or revision.status != "active"
        or revision.revoked_at is not None
        or revision.policy_release_ref != BUILTIN_POLICY_RELEASE_ID
    ):
        return None
    actions = tuple(
        await session.scalars(
            select(RoleVersionActionTable.action_ref)
            .where(
                RoleVersionActionTable.organization_ref == organization_ref,
                RoleVersionActionTable.role_ref == assignment.role_ref,
                RoleVersionActionTable.role_revision == assignment.role_revision,
            )
            .order_by(RoleVersionActionTable.action_ref)
            .limit(len(_SUPPORTED_ACTIONS) + 1)
        )
    )
    # Validate the whole published set: unsupported entries must not become
    # latent permissions when a later deployment adds a same-named policy.
    if not actions or any(granted not in _SUPPORTED_ACTIONS or granted not in ACTION_POLICIES for granted in actions):
        return None
    registered = tuple(
        await session.scalars(
            select(PermissionActionTable)
            .where(PermissionActionTable.action_ref.in_(actions))
            .execution_options(**refresh)
        )
    )
    if len(registered) != len(actions) or any(
        entry.status != "active" or entry.resource_kind != "work_item" or entry.contract_version != "1"
        for entry in registered
    ):
        return None
    if revision.content_digest != role_permissions_digest(
        organization_ref, revision.role_ref, revision.role_revision, revision.policy_release_ref, actions
    ):
        return None
    return ValidatedRoleVersion(role, revision, actions, registered)


async def load_scope_chain(session: AsyncSession, organization_ref: str, scope_ref: str) -> tuple[tuple[str, int], ...]:
    """Require every ancestor to be active; reject cycles, absence and excess depth."""
    chain: list[tuple[str, int]] = []
    visited: set[str] = set()
    current: str | None = scope_ref
    for _ in range(64):
        if current in visited:
            return ()
        row = await session.scalar(
            select(AuthorizationScopeTable)
            .where(
                AuthorizationScopeTable.organization_ref == organization_ref,
                AuthorizationScopeTable.scope_ref == current,
            )
            .execution_options(populate_existing=True)
        )
        if row is None or row.status != "active":
            return ()
        chain.append((row.scope_ref, row.version))
        visited.add(row.scope_ref)
        current = row.parent_scope_ref
        if current is None:
            return tuple(chain)
    return ()


class DatabaseAuthorizationLoader:
    """Load one selected human assignment and one registered WorkItem.

    P1b.2 supports the code-defined action policies and internal classification
    ceiling. Scope registration is authoritative; legacy WorkItem.scope_ref is
    required to agree until its compatibility field can be removed. Only direct
    viewer/owner ACLs are supported. Assignee, reviewer and eligibility facts
    require future domain loaders rather than arbitrary tuples.
    """

    _DIRECT_RELATIONS = frozenset({"viewer_of", "owner_of"})

    def __init__(self) -> None:
        self._lock_marker = object()

    async def acquire_locks(self, session: AsyncSession, context: ExecutionContext) -> None:
        """Acquire global identity locks before the organization's write guard.

        The marker belongs to this loader and this exact transaction, so a
        prior transaction or a different loader cannot stand in for the lock.
        """
        if not session.in_transaction() or context.principal_type is not PrincipalType.HUMAN:
            raise AuthorizationDenied()
        principal = await session.scalar(
            select(PrincipalTable)
            .where(PrincipalTable.principal_ref == context.actor_ref)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if principal is None or principal.principal_type != "human":
            raise AuthorizationDenied()
        session.info[self._lock_marker] = (
            session.sync_session.get_transaction(),
            context.actor_ref,
            context.tenant_ref,
        )

    async def load(
        self,
        session: AsyncSession,
        context: ExecutionContext,
        *,
        action: str,
        target_ref: str,
        expected_version: int | None,
        now: datetime,
    ) -> AuthorizationContext:
        missing = AuthorizationContext(
            request_id=context.request_id,
            subject_ref=context.actor_ref,
            principal_type=context.principal_type,
            action=action,
            resource_ref=target_ref,
            organization_ref=context.tenant_ref,
            resource_organization_ref=None,
            resource_scope_refs=(),
            assignment=None,
            now=now,
            attributes=AttributeContext(expected_object_version=expected_version),
        )
        lock_identity = (session.sync_session.get_transaction(), context.actor_ref, context.tenant_ref)
        if not session.in_transaction() or session.info.get(self._lock_marker) != lock_identity:
            raise AuthorizationDenied()
        # The kernel already holds this guard. Reacquiring it is harmless and
        # keeps direct internal loader callers on the same writer protocol.
        await lock_tenant(session, context.tenant_ref)
        if now.tzinfo is None or now.utcoffset() is None:
            return missing
        now = now.astimezone(UTC)
        context.check_live(now)
        if (
            context.principal_type is not PrincipalType.HUMAN
            or context.effective_subject_ref != context.actor_ref
            or context.delegation_ref is not None
            or context.assignment_ref is None
        ):
            return missing
        if expected_version is not None and (type(expected_version) is not int or expected_version < 1):
            return missing
        try:
            Permission(action)
        except ValueError:
            return missing
        # Explicit queries refresh ORM instances even if this session loaded a
        # candidate before acquiring its execution locks.
        refresh = {"populate_existing": True}
        root = await session.scalar(
            select(OrganizationRootTable).where(OrganizationRootTable.deployment_slot == 1).execution_options(**refresh)
        )
        principal = await session.scalar(
            select(PrincipalTable).where(PrincipalTable.principal_ref == context.actor_ref).execution_options(**refresh)
        )
        if (
            root is None
            or root.organization_ref != context.tenant_ref
            or root.status != "active"
            or principal is None
            or principal.principal_type != "human"
            or principal.status != "active"
            or principal.disabled_at is not None
        ):
            return missing
        assignment = await session.scalar(
            select(RoleAssignmentTable)
            .where(
                RoleAssignmentTable.organization_ref == root.organization_ref,
                RoleAssignmentTable.assignment_ref == context.assignment_ref,
                RoleAssignmentTable.principal_ref == principal.principal_ref,
                RoleAssignmentTable.principal_type == "human",
            )
            .execution_options(**refresh)
        )
        if assignment is None or assignment.membership_ref is None or assignment.role_ref is None:
            return missing
        if validity_failure(
            now,
            valid_from=assignment.valid_from,
            expires_at=assignment.expires_at,
            status=assignment.status,
            revoked_at=assignment.revoked_at,
        ):
            return missing
        membership = await session.scalar(
            select(OrganizationMembershipTable)
            .where(
                OrganizationMembershipTable.organization_ref == root.organization_ref,
                OrganizationMembershipTable.membership_ref == assignment.membership_ref,
                OrganizationMembershipTable.principal_ref == principal.principal_ref,
                OrganizationMembershipTable.principal_type == "human",
            )
            .execution_options(**refresh)
        )
        if (
            membership is None
            or membership.ended_at is not None
            or validity_failure(
                now,
                valid_from=membership.valid_from,
                expires_at=membership.expires_at,
                status=membership.status,
            )
        ):
            return missing
        validated_role = await load_role_version(session, root.organization_ref, assignment)
        if validated_role is None or action not in validated_role.actions:
            return missing
        role, revision = validated_role.role, validated_role.revision
        actions, registered = validated_role.actions, validated_role.registered
        resource = await session.scalar(
            select(AuthorizationResourceTable)
            .where(
                AuthorizationResourceTable.organization_ref == root.organization_ref,
                AuthorizationResourceTable.resource_ref == target_ref,
            )
            .execution_options(**refresh)
        )
        if resource is None or resource.status != "active" or resource.resource_kind != "work_item":
            return missing
        # A canonical reference avoids multiple idempotency identities for one
        # object (e.g. work_item:1, work_item:01, or a raw numeric reference).
        suffix = target_ref.removeprefix("work_item:")
        if not suffix.isascii() or not suffix.isdigit() or target_ref != f"work_item:{int(suffix)}":
            return missing
        work_id = int(suffix)
        if not 1 <= work_id <= 2**31 - 1:
            return missing
        work = await session.scalar(
            select(WorkItemTable).where(WorkItemTable.id == work_id).with_for_update().execution_options(**refresh)
        )
        if work is None or work.scope_ref != resource.scope_ref:
            return missing
        scope_versions = await load_scope_chain(session, root.organization_ref, resource.scope_ref)
        scopes = tuple(reference for reference, _ in scope_versions)
        if not scopes or assignment.scope_ref not in scopes:
            return missing
        rows = tuple(
            await session.scalars(
                select(RelationTupleTable)
                .where(
                    RelationTupleTable.organization_ref == root.organization_ref,
                    RelationTupleTable.subject_ref == principal.principal_ref,
                    RelationTupleTable.subject_type == "human",
                    RelationTupleTable.membership_ref == membership.membership_ref,
                    RelationTupleTable.resource_ref == target_ref,
                    RelationTupleTable.source_kind == "direct_acl",
                    RelationTupleTable.relation == RelationTupleTable.relation_type,
                    RelationTupleTable.relation_type.in_(self._DIRECT_RELATIONS),
                    RelationTupleTable.revoked_at.is_(None),
                )
                .order_by(RelationTupleTable.relation_ref)
                .execution_options(**refresh)
            )
        )
        relations = tuple(
            RelationTuple(
                subject_ref=row.subject_ref,
                relation=row.relation,
                resource_ref=row.resource_ref,
                valid_from=row.valid_from,
                expires_at=row.expires_at,
                status=row.status,
            )
            for row in rows
        )
        starts = [membership.valid_from]
        if assignment.valid_from is not None:
            starts.append(assignment.valid_from)
        ends = [end for end in (membership.expires_at, assignment.expires_at) if end is not None]
        loaded_assignment = RoleAssignment(
            assignment_ref=context.assignment_ref,
            principal_ref=principal.principal_ref,
            role_code=role.role_code,
            organization_ref=root.organization_ref,
            scope_refs=(assignment.scope_ref,),
            valid_from=max(starts),
            expires_at=min(ends) if ends else None,
            status=assignment.status,
            permission_actions=actions,
            role_ref=revision.role_ref,
            role_revision=revision.role_revision,
            role_version_digest=revision.content_digest,
            membership_ref=membership.membership_ref,
        )
        guard_epoch = await session.scalar(
            select(TenantGuardTable.epoch).where(TenantGuardTable.tenant_ref == root.organization_ref)
        )
        if guard_epoch is None:
            return missing
        # Row versions describe the facts actually read in this transaction.
        # The guard epoch also covers set changes (e.g. a newly-added ACL),
        # which a list of existing row versions alone cannot describe. These
        # are audit evidence, never permission to reuse an earlier decision.
        # role_version references use the last '@' as the revision separator.
        fact_versions: list[tuple[str, str, int]] = [
            ("organization_root", root.organization_ref, root.version),
            ("principal", principal.principal_ref, principal.version),
            ("principal.identity_epoch", principal.principal_ref, principal.identity_epoch),
            ("tenant_membership", membership.membership_ref, membership.version),
            ("role_assignment", context.assignment_ref, assignment.version),
            ("role_definition", role.role_ref, role.version),
            ("role_version", f"{revision.role_ref}@{revision.role_revision}", revision.version),
            ("authorization_resource", resource.resource_ref, resource.version),
            ("work_item", target_ref, work.version),
            ("execution_tenant_guard.epoch", root.organization_ref, guard_epoch),
        ]
        fact_versions.extend(("authorization_scope", reference, version) for reference, version in scope_versions)
        fact_versions.extend(("permission_action", entry.action_ref, entry.version) for entry in registered)
        for row in rows:
            if row.relation_ref is None:
                return missing
            fact_versions.append(("relation_tuple", row.relation_ref, row.version))
        return replace(
            missing,
            resource_organization_ref=root.organization_ref,
            resource_scope_refs=scopes,
            assignment=loaded_assignment,
            relations=relations,
            now=now,
            fact_versions=tuple(sorted(fact_versions)),
            attributes=AttributeContext(
                subject_active=True,
                resource_classification=work.classification,
                classification_ceiling="internal",
                object_version=work.version,
                expected_object_version=expected_version,
                resource_state=work.lifecycle,
            ),
        )
