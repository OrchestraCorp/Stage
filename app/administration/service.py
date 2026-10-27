"""Short human management transactions over the existing identity and role facts.

Authentication occurs before entry. Every method revalidates server-minted
evidence, current management authority and MFA under the storage barriers.
Security writers deliberately do not run as ordinary work-item handlers.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.administration.contracts import (
    ActivationRequest,
    AdminAssignmentList,
    AdminAssignmentView,
    AdministrationMe,
    AuditList,
    AuditView,
    AuthorityView,
    GrantRequest,
    OperationResult,
    PeopleList,
    PersonView,
    RevokeRequest,
)
from app.administration.errors import AdminCommitUnknown, AdminConflict, AdminDenied, AdminNotFound
from app.administration.journal import OPERATION_NAMESPACE, append_operation, find_operation, replay
from app.authz.authenticated import HumanIdentityVerifier
from app.authz.management import (
    MANAGEMENT_ROLE_REF,
    AuthorizedManager,
    GrantRule,
    authorize_manager,
    management_role_contract,
    parse_grant_rules,
    require_management_assurance,
    validate_grant_rule,
)
from app.execution.encoding import digest
from app.execution.storage import UnitOfWork, managed_uow
from app.identity.authentication import AuthenticationAssuranceRequired, AuthenticationRejected
from app.identity.evidence import HumanAuthenticationEvidence
from app.identity.service import current
from app.models.administration import ManagementAuthorityTable
from app.models.authorization import RoleAssignmentTable, RoleDefinitionTable
from app.models.execution import TenantGuardTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import IdentityControlOperationTable, OrganizationMembershipTable
from app.storage.coordination import LockMode, assert_organization_lock, lock_command_key, lock_organization

MAX_VERSION = 2**31 - 1


def _page(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("invalid_page_limit")


def _version(actual: int, expected: int) -> None:
    if actual != expected or actual >= MAX_VERSION:
        raise AdminConflict("OBJECT_VERSION_CONFLICT")


def _deadline(evidence: HumanAuthenticationEvidence, *deadlines: datetime | None) -> None:
    now = datetime.now(UTC)
    if now >= min(evidence.valid_until, evidence.credential.credential_expires_at):
        raise AuthenticationRejected("identity_evidence_expired")
    if any(deadline is not None and now >= deadline for deadline in deadlines):
        raise AuthenticationAssuranceRequired()


def _person_view(principal: PrincipalTable, membership: OrganizationMembershipTable) -> PersonView:
    return PersonView(
        principal_ref=principal.principal_ref,
        principal_status=principal.status,
        principal_version=principal.version,
        membership_ref=membership.membership_ref,
        membership_status=membership.status,
        membership_version=membership.version,
        valid_from=membership.valid_from,
        expires_at=membership.expires_at,
    )


async def _assignment_view(session: AsyncSession, row: RoleAssignmentTable) -> AdminAssignmentView:
    role = await session.get(RoleDefinitionTable, (row.organization_ref, row.role_ref), populate_existing=True)
    if (
        role is None
        or row.assignment_ref is None
        or row.membership_ref is None
        or row.role_ref is None
        or row.role_revision is None
        or row.valid_from is None
    ):
        raise AdminDenied()
    return AdminAssignmentView(
        principal_ref=row.principal_ref,
        membership_ref=row.membership_ref,
        assignment_ref=row.assignment_ref,
        role_ref=row.role_ref,
        role_revision=row.role_revision,
        role_code=row.role_code,
        role_display_name=role.display_name,
        scope_ref=row.scope_ref,
        valid_from=row.valid_from,
        expires_at=row.expires_at,
        status=row.status,
        version=row.version,
    )


class AdministrationService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], identity: HumanIdentityVerifier) -> None:
        self._sessions = sessions
        self._identity = identity

    @asynccontextmanager
    async def _transaction(
        self,
        evidence: HumanAuthenticationEvidence,
        *,
        write: bool = False,
        key: str | None = None,
        target: str | None = None,
    ) -> AsyncIterator[UnitOfWork]:
        async with managed_uow(self._sessions) as uow:
            session = uow.session
            if key is not None:
                await lock_command_key(
                    session,
                    organization_ref=evidence.organization_ref,
                    actor_ref=evidence.principal_ref,
                    command_name="administration.command",
                    command_version="1",
                    idempotency_key=key,
                )
            await self._identity.acquire_human_locks(session, evidence)
            if target is not None and target != evidence.principal_ref:
                # Global credential/principal writers lock before organization.
                # Take target protection in that order too, even on replay.
                await session.scalar(
                    select(PrincipalTable)
                    .where(PrincipalTable.principal_ref == target)
                    .with_for_update(read=True)
                    .execution_options(populate_existing=True)
                )
            await lock_organization(
                session, evidence.organization_ref, mode=LockMode.EXCLUSIVE if write else LockMode.SHARED
            )
            await self._identity.validate_human(session, evidence, datetime.now(UTC))
            deadline = await require_management_assurance(session, evidence, datetime.now(UTC))
            _deadline(evidence, deadline)
            yield uow

    async def _finish(
        self,
        session: AsyncSession,
        evidence: HumanAuthenticationEvidence,
        *,
        assignment_ref: str | None = None,
        action: str = "administration.people.read",
    ) -> None:
        await self._identity.validate_human(session, evidence, datetime.now(UTC))
        deadline = await require_management_assurance(session, evidence, datetime.now(UTC))
        manager = None
        if assignment_ref is not None:
            manager = await authorize_manager(session, evidence, assignment_ref, action, datetime.now(UTC))
        assert_organization_lock(session, evidence.organization_ref)
        _deadline(evidence, deadline, manager.valid_until if manager else None)

    async def _commit(
        self,
        uow: UnitOfWork,
        evidence: HumanAuthenticationEvidence,
        assignment_ref: str,
        action: str,
        *target_deadlines: datetime | None,
    ) -> None:
        guard = await uow.session.get(TenantGuardTable, evidence.organization_ref, populate_existing=True)
        assert guard is not None
        if guard.epoch >= MAX_VERSION:
            raise AdminConflict("ASSIGNMENT_CONFLICT")
        guard.epoch += 1
        await uow.flush()
        await self._finish(uow.session, evidence, assignment_ref=assignment_ref, action=action)
        if any(value is not None and datetime.now(UTC) >= value for value in target_deadlines):
            raise AdminConflict("ASSIGNMENT_CONFLICT")
        assert_organization_lock(uow.session, evidence.organization_ref, mode=LockMode.EXCLUSIVE)
        try:
            await uow._commit_by_owner()
        except BaseException:
            raise AdminCommitUnknown(assignment_ref) from None

    async def me(self, evidence: HumanAuthenticationEvidence) -> AdministrationMe:
        async with self._transaction(evidence) as uow:
            authority = await uow.session.get(ManagementAuthorityTable, evidence.organization_ref)
            view = None
            if authority is not None and self._is_nominee(authority, evidence):
                assignment = await uow.session.scalar(
                    select(RoleAssignmentTable).where(
                        RoleAssignmentTable.organization_ref == evidence.organization_ref,
                        RoleAssignmentTable.assignment_ref == authority.assignment_ref,
                    )
                )
                if assignment is None:
                    raise AdminDenied()
                contract = management_role_contract(assignment.role_revision)
                if contract is None:
                    raise AdminDenied()
                if authority.status == "active":
                    await authorize_manager(
                        uow.session, evidence, authority.assignment_ref, "administration.people.read", datetime.now(UTC)
                    )
                view = AuthorityView.model_validate(
                    {
                        "authority_ref": authority.authority_ref,
                        "status": authority.status,
                        "version": authority.version,
                        "assignment_ref": authority.assignment_ref,
                        "nomination_expires_at": authority.nomination_expires_at,
                        "accepted_at": authority.accepted_at,
                        "grant_rules": parse_grant_rules(authority.grant_rules_json),
                        "assignment_version": assignment.version,
                        "role_revision": contract.revision,
                        "actions": tuple(sorted(contract.actions)),
                    }
                )
            await self._finish(
                uow.session,
                evidence,
                assignment_ref=view.assignment_ref if view is not None and view.status == "active" else None,
            )
            return AdministrationMe(
                principal_ref=evidence.principal_ref, membership_ref=evidence.membership_ref, authority=view
            )

    @staticmethod
    def _is_nominee(authority: ManagementAuthorityTable, evidence: HumanAuthenticationEvidence) -> bool:
        return (
            authority.principal_ref == evidence.principal_ref
            and authority.membership_ref == evidence.membership_ref
            and authority.external_identity_ref == evidence.external_identity_ref
            and authority.profile_ref == evidence.credential.profile_ref
            and authority.profile_version == evidence.credential.profile_version
        )

    async def activate(
        self,
        evidence: HumanAuthenticationEvidence,
        *,
        request: ActivationRequest,
        request_id: str,
        idempotency_key: str,
    ) -> OperationResult:
        async with self._transaction(evidence, write=True, key=idempotency_key) as uow:
            session = uow.session
            authority = await session.get(ManagementAuthorityTable, evidence.organization_ref, populate_existing=True)
            if authority is None or not self._is_nominee(authority, evidence):
                raise AdminDenied()
            input_digest = digest(
                {"type": "activation", "authority": authority.authority_ref, "input": request.model_dump()}
            )
            if authority.accepted_at is not None:
                await authorize_manager(
                    session, evidence, authority.assignment_ref, "administration.people.read", datetime.now(UTC)
                )
                previous = await find_operation(session, evidence, idempotency_key)
                if previous is None:
                    raise AdminConflict("BOOTSTRAP_CLOSED")
                result = replay(previous, input_digest)
                await self._finish(session, evidence, assignment_ref=authority.assignment_ref)
                return result
            _version(authority.version, request.expected_authority_version)
            if authority.status != "pending" or datetime.now(UTC) >= authority.nomination_expires_at:
                raise AdminConflict("BOOTSTRAP_CLOSED")
            assignment = await session.scalar(
                select(RoleAssignmentTable).where(
                    RoleAssignmentTable.organization_ref == evidence.organization_ref,
                    RoleAssignmentTable.assignment_ref == authority.assignment_ref,
                )
            )
            if assignment is None or assignment.status != "suspended" or assignment.version >= MAX_VERSION:
                raise AdminDenied()
            before = await _assignment_view(session, assignment)
            assignment.status = "active"
            assignment.version += 1
            authority.status = "active"
            authority.accepted_at = datetime.now(UTC)
            authority.version += 1
            await uow.flush()
            await authorize_manager(
                session, evidence, authority.assignment_ref, "administration.people.read", datetime.now(UTC)
            )
            result = append_operation(
                session,
                evidence,
                key=idempotency_key,
                input_digest=input_digest,
                operation_type="activation",
                assignment_ref=authority.assignment_ref,
                authority_version=authority.version,
                request_id=request_id,
                result=await _assignment_view(session, assignment),
                before=before,
                reason=None,
            )
            await self._commit(
                uow, evidence, authority.assignment_ref, "administration.people.read", authority.nomination_expires_at
            )
            return result

    async def people(
        self, evidence: HumanAuthenticationEvidence, *, assignment_ref: str, limit: int = 20, after: str | None = None
    ) -> PeopleList:
        _page(limit)
        async with self._transaction(evidence) as uow:
            await authorize_manager(
                uow.session, evidence, assignment_ref, "administration.people.read", datetime.now(UTC)
            )
            query = self._people_query(evidence)
            if after is not None:
                query = query.where(PrincipalTable.principal_ref > after)
            rows = (await uow.session.execute(query.order_by(PrincipalTable.principal_ref).limit(limit + 1))).all()
            selected = rows[:limit]
            result = PeopleList(
                items=tuple(_person_view(principal, member) for principal, member in selected),
                next_after=selected[-1][0].principal_ref if len(rows) > limit else None,
            )
            await self._finish(uow.session, evidence, assignment_ref=assignment_ref)
            return result

    @staticmethod
    def _people_query(
        evidence: HumanAuthenticationEvidence,
    ) -> Select[PrincipalTable, OrganizationMembershipTable]:
        return (
            select(PrincipalTable, OrganizationMembershipTable)
            .join(
                OrganizationMembershipTable, PrincipalTable.principal_ref == OrganizationMembershipTable.principal_ref
            )
            .where(
                OrganizationMembershipTable.organization_ref == evidence.organization_ref,
                OrganizationMembershipTable.status.in_(("active", "suspended")),
                OrganizationMembershipTable.ended_at.is_(None),
                OrganizationMembershipTable.principal_type == "human",
                PrincipalTable.principal_type == "human",
            )
        )

    async def person(
        self, evidence: HumanAuthenticationEvidence, *, assignment_ref: str, principal_ref: str
    ) -> PersonView:
        async with self._transaction(evidence) as uow:
            await authorize_manager(
                uow.session, evidence, assignment_ref, "administration.people.read", datetime.now(UTC)
            )
            row = (
                await uow.session.execute(
                    self._people_query(evidence).where(PrincipalTable.principal_ref == principal_ref)
                )
            ).one_or_none()
            await self._finish(uow.session, evidence, assignment_ref=assignment_ref)
            if row is None:
                raise AdminNotFound()
            return _person_view(*row)

    async def assignments(
        self,
        evidence: HumanAuthenticationEvidence,
        *,
        assignment_ref: str,
        principal_ref: str,
        limit: int = 20,
        after: str | None = None,
    ) -> AdminAssignmentList:
        _page(limit)
        async with self._transaction(evidence) as uow:
            session = uow.session
            await authorize_manager(session, evidence, assignment_ref, "administration.people.read", datetime.now(UTC))
            member = await session.scalar(
                select(OrganizationMembershipTable).where(
                    OrganizationMembershipTable.organization_ref == evidence.organization_ref,
                    OrganizationMembershipTable.principal_ref == principal_ref,
                    OrganizationMembershipTable.principal_type == "human",
                    OrganizationMembershipTable.status.in_(("active", "suspended")),
                    OrganizationMembershipTable.ended_at.is_(None),
                )
            )
            if member is None:
                await self._finish(session, evidence, assignment_ref=assignment_ref)
                raise AdminNotFound()
            query = select(RoleAssignmentTable).where(
                RoleAssignmentTable.organization_ref == evidence.organization_ref,
                RoleAssignmentTable.principal_ref == principal_ref,
                RoleAssignmentTable.membership_ref == member.membership_ref,
                RoleAssignmentTable.role_ref != MANAGEMENT_ROLE_REF,
                RoleAssignmentTable.assignment_ref.is_not(None),
            )
            if after is not None:
                query = query.where(RoleAssignmentTable.assignment_ref > after)
            rows = tuple(await session.scalars(query.order_by(RoleAssignmentTable.assignment_ref).limit(limit + 1)))
            selected = rows[:limit]
            items = []
            for row in selected:
                # A management projection records assignments, including those
                # whose role has since stopped granting permissions. Hiding a
                # historical grant would prevent the administrator inspecting it.
                # This view never serves as a permission decision.
                items.append(await _assignment_view(session, row))
            await self._finish(session, evidence, assignment_ref=assignment_ref)
            return AdminAssignmentList(
                items=tuple(items), next_after=selected[-1].assignment_ref if len(rows) > limit else None
            )

    async def _target(
        self, session: AsyncSession, evidence: HumanAuthenticationEvidence, principal_ref: str, membership_ref: str
    ) -> OrganizationMembershipTable:
        if principal_ref == evidence.principal_ref:
            raise AdminDenied()
        principal = await session.scalar(select(PrincipalTable).where(PrincipalTable.principal_ref == principal_ref))
        member = await session.get(
            OrganizationMembershipTable, (evidence.organization_ref, membership_ref), populate_existing=True
        )
        now = datetime.now(UTC)
        if (
            principal is None
            or principal.principal_type != "human"
            or not current(principal, now)
            or principal.disabled_at is not None
            or member is None
            or member.principal_type != "human"
            or member.principal_ref != principal_ref
            or not current(member, now)
            or member.ended_at is not None
        ):
            raise AdminDenied()
        return member

    async def _rule(
        self,
        session: AsyncSession,
        evidence: HumanAuthenticationEvidence,
        manager: AuthorizedManager,
        role_ref: str,
        role_revision: int,
        scope_ref: str,
    ) -> GrantRule:
        rule = next(
            (
                rule
                for rule in parse_grant_rules(manager.authority.grant_rules_json)
                if (rule.role_ref, rule.role_revision, rule.scope_ref) == (role_ref, role_revision, scope_ref)
            ),
            None,
        )
        if rule is None:
            raise AdminDenied()
        await validate_grant_rule(session, evidence.organization_ref, rule)
        return rule

    async def grant(
        self,
        evidence: HumanAuthenticationEvidence,
        *,
        assignment_ref: str,
        principal_ref: str,
        request: GrantRequest,
        request_id: str,
        idempotency_key: str,
    ) -> OperationResult:
        action = "administration.assignment.grant"
        async with self._transaction(evidence, write=True, key=idempotency_key, target=principal_ref) as uow:
            session = uow.session
            manager = await authorize_manager(session, evidence, assignment_ref, action, datetime.now(UTC))
            if principal_ref == evidence.principal_ref:
                raise AdminDenied()
            await self._rule(session, evidence, manager, request.role_ref, request.role_revision, request.scope_ref)
            input_digest = digest(
                {
                    "type": "assignment.grant",
                    "actor_assignment": assignment_ref,
                    "target": principal_ref,
                    "input": request.model_dump(mode="json"),
                }
            )
            previous = await find_operation(session, evidence, idempotency_key)
            if previous is not None:
                result = replay(previous, input_digest)
                await self._finish(session, evidence, assignment_ref=assignment_ref, action=action)
                return result
            _version(manager.authority.version, request.expected_authority_version)
            member = await self._target(session, evidence, principal_ref, request.membership_ref)
            _version(member.version, request.expected_membership_version)
            rule = await self._rule(
                session, evidence, manager, request.role_ref, request.role_revision, request.scope_ref
            )
            now = datetime.now(UTC)
            if (
                request.expires_at <= now
                or request.expires_at > now + timedelta(seconds=rule.max_duration_seconds)
                or (member.expires_at is not None and request.expires_at > member.expires_at)
            ):
                raise AdminDenied()
            duplicate = await session.scalar(
                select(RoleAssignmentTable).where(
                    RoleAssignmentTable.organization_ref == evidence.organization_ref,
                    RoleAssignmentTable.membership_ref == request.membership_ref,
                    RoleAssignmentTable.role_ref == request.role_ref,
                    RoleAssignmentTable.role_revision == request.role_revision,
                    RoleAssignmentTable.scope_ref == request.scope_ref,
                    RoleAssignmentTable.status.in_(("active", "suspended")),
                )
            )
            if duplicate is not None:
                raise AdminConflict("ASSIGNMENT_CONFLICT")
            role = await session.get(RoleDefinitionTable, (evidence.organization_ref, request.role_ref))
            assert role is not None
            row = RoleAssignmentTable(
                organization_ref=evidence.organization_ref,
                assignment_ref=f"assignment:{uuid4()}",
                principal_ref=principal_ref,
                principal_type="human",
                membership_ref=member.membership_ref,
                role_ref=request.role_ref,
                role_revision=request.role_revision,
                role_code=role.role_code,
                scope_ref=request.scope_ref,
                status="active",
                version=1,
                valid_from=now,
                expires_at=request.expires_at,
                grant_source_ref=manager.authority.authority_ref,
            )
            session.add(row)
            result = append_operation(
                session,
                evidence,
                key=idempotency_key,
                input_digest=input_digest,
                operation_type="assignment.grant",
                assignment_ref=assignment_ref,
                authority_version=manager.authority.version,
                request_id=request_id,
                result=await _assignment_view(session, row),
                before=None,
                reason=request.reason,
            )
            await self._commit(uow, evidence, assignment_ref, action, member.expires_at, request.expires_at)
            return result

    async def revoke(
        self,
        evidence: HumanAuthenticationEvidence,
        *,
        assignment_ref: str,
        principal_ref: str,
        target_assignment_ref: str,
        request: RevokeRequest,
        request_id: str,
        idempotency_key: str,
    ) -> OperationResult:
        action = "administration.assignment.revoke"
        async with self._transaction(evidence, write=True, key=idempotency_key, target=principal_ref) as uow:
            session = uow.session
            manager = await authorize_manager(session, evidence, assignment_ref, action, datetime.now(UTC))
            if principal_ref == evidence.principal_ref:
                raise AdminDenied()
            row = await session.scalar(
                select(RoleAssignmentTable).where(
                    RoleAssignmentTable.organization_ref == evidence.organization_ref,
                    RoleAssignmentTable.assignment_ref == target_assignment_ref,
                    RoleAssignmentTable.principal_ref == principal_ref,
                )
            )
            if row is None or row.role_ref is None or row.role_revision is None or row.membership_ref is None:
                raise AdminDenied()
            await self._rule(session, evidence, manager, row.role_ref, row.role_revision, row.scope_ref)
            input_digest = digest(
                {
                    "type": "assignment.revoke",
                    "actor_assignment": assignment_ref,
                    "target": principal_ref,
                    "assignment": target_assignment_ref,
                    "input": request.model_dump(mode="json"),
                }
            )
            previous = await find_operation(session, evidence, idempotency_key)
            if previous is not None:
                result = replay(previous, input_digest)
                await self._finish(session, evidence, assignment_ref=assignment_ref, action=action)
                return result
            _version(manager.authority.version, request.expected_authority_version)
            _version(row.version, request.expected_assignment_version)
            if row.status not in ("active", "suspended", "expired") or row.revoked_at is not None:
                raise AdminConflict("ASSIGNMENT_CONFLICT")
            before = await _assignment_view(session, row)
            row.status, row.revoked_at, row.version = "revoked", datetime.now(UTC), row.version + 1
            result = append_operation(
                session,
                evidence,
                key=idempotency_key,
                input_digest=input_digest,
                operation_type="assignment.revoke",
                assignment_ref=assignment_ref,
                authority_version=manager.authority.version,
                request_id=request_id,
                result=await _assignment_view(session, row),
                before=before,
                reason=request.reason,
            )
            await self._commit(uow, evidence, assignment_ref, action)
            return result

    async def operation(
        self, evidence: HumanAuthenticationEvidence, *, assignment_ref: str, idempotency_key: str
    ) -> OperationResult:
        async with self._transaction(evidence, key=idempotency_key) as uow:
            await authorize_manager(
                uow.session, evidence, assignment_ref, "administration.people.read", datetime.now(UTC)
            )
            row = await find_operation(uow.session, evidence, idempotency_key)
            await self._finish(uow.session, evidence, assignment_ref=assignment_ref)
            if row is None:
                raise AdminNotFound()
            return replay(row)

    async def audit(
        self, evidence: HumanAuthenticationEvidence, *, assignment_ref: str, limit: int = 20, after: str | None = None
    ) -> AuditList:
        _page(limit)
        action = "administration.audit.read"
        async with self._transaction(evidence) as uow:
            await authorize_manager(uow.session, evidence, assignment_ref, action, datetime.now(UTC))
            query = select(IdentityControlOperationTable).where(
                IdentityControlOperationTable.actor_kind == "human",
                IdentityControlOperationTable.operation_type == OPERATION_NAMESPACE,
                IdentityControlOperationTable.authentication_json["organization_ref"].as_string()
                == evidence.organization_ref,
            )
            if after is not None:
                query = query.where(IdentityControlOperationTable.operation_ref > after)
            rows = tuple(
                await uow.session.scalars(query.order_by(IdentityControlOperationTable.operation_ref).limit(limit + 1))
            )
            items = tuple(
                AuditView(
                    operation_ref=row.operation_ref,
                    operation_type=replay(row).operation_type,
                    actor_ref=row.actor_ref,
                    assignment_ref=row.authentication_json["assignment_ref"],
                    request_id=row.authentication_json["request_id"],
                    target_ref=row.target_ref,
                    reason=row.authentication_json.get("reason"),
                    completed_at=row.completed_at,
                    result=replay(row).result,
                )
                for row in rows[:limit]
            )
            await self._finish(uow.session, evidence, assignment_ref=assignment_ref, action=action)
            return AuditList(items=items, next_after=rows[limit - 1].operation_ref if len(rows) > limit else None)
