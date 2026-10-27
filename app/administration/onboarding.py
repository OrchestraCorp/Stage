"""Bounded security transactions for new employee enrollment and delivery recovery."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.administration.errors import AdminCommitUnknown, AdminConflict, AdminDenied, AdminNotFound
from app.administration.onboarding_contracts import (
    EmployeeActivationChange,
    EmployeeActivationReissue,
    EmployeeActivationView,
    EmployeeAuditList,
    EmployeeAuditView,
    EmployeeCreateRequest,
    EmployeeDelivery,
    EmployeeOperationResult,
)
from app.authz.authenticated import HumanIdentityVerifier
from app.authz.management import authorize_manager
from app.execution.encoding import digest
from app.execution.storage import UnitOfWork, managed_uow
from app.identity.evidence import HumanAuthenticationEvidence
from app.models.execution import TenantGuardTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import (
    ExternalIdentityTable,
    IdentityControlOperationTable,
    OrganizationMembershipTable,
    TrustStateTable,
)
from app.models.native_auth import NativeAccountActivationTable, NativeAccountTable
from app.native_auth.activation import MAX_VERSION, clear_activation_secrets, pending_activation_facts
from app.native_auth.config import NativeAuthConfig
from app.native_auth.crypto import digest_secret, random_secret
from app.native_auth.errors import NativeCredentialError
from app.native_auth.onboarding_source import (
    NATIVE_ONBOARDING_NAMESPACE,
    NATIVE_ONBOARDING_PROVIDER,
    load_onboarding_source,
)
from app.storage.coordination import LockMode, assert_organization_lock, lock_command_key, lock_organization

NAMESPACE = "administration.onboarding.v1"


def activation_view(row: NativeAccountActivationTable, account: NativeAccountTable) -> EmployeeActivationView:
    return EmployeeActivationView.model_validate(
        {
            "activation_ref": row.activation_ref,
            "principal_ref": row.principal_ref,
            "membership_ref": row.membership_ref,
            "account_ref": row.account_ref,
            "username": account.username,
            "status": row.status,
            "version": row.version,
            "expires_at": row.expires_at,
            "completed_at": row.completed_at,
        }
    )


class EmployeeAdministration:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], identity: HumanIdentityVerifier, config: NativeAuthConfig
    ) -> None:
        self.sessions = sessions
        self.identity = identity
        self.config = config

    @asynccontextmanager
    async def _transaction(
        self,
        evidence: HumanAuthenticationEvidence,
        *,
        key: str | None = None,
        activation_ref: str | None = None,
        write: bool = False,
    ) -> AsyncIterator[UnitOfWork]:
        if evidence.organization_ref != self.config.organization_ref or evidence.environment != self.config.environment:
            raise AdminDenied()
        async with managed_uow(self.sessions) as uow:
            session = uow.session
            if key is not None:
                await lock_command_key(
                    session,
                    organization_ref=evidence.organization_ref,
                    actor_ref=evidence.principal_ref,
                    command_name="administration.onboarding",
                    command_version="1",
                    idempotency_key=key,
                )
            for trust_ref in sorted({evidence.trust_ref, self.config.trust_ref}):
                if (
                    await session.scalar(
                        select(TrustStateTable).where(TrustStateTable.trust_ref == trust_ref).with_for_update(read=True)
                    )
                    is None
                ):
                    raise AdminDenied()
            await self.identity.acquire_human_locks(session, evidence)
            target = None
            if activation_ref is not None:
                target = await session.scalar(
                    select(NativeAccountActivationTable.principal_ref).where(
                        NativeAccountActivationTable.activation_ref == activation_ref,
                        NativeAccountActivationTable.organization_ref == evidence.organization_ref,
                    )
                )
                if target == evidence.principal_ref:
                    raise AdminDenied()
                if target is not None:
                    await session.scalar(
                        select(PrincipalTable)
                        .where(PrincipalTable.principal_ref == target)
                        .with_for_update(read=not write)
                    )
            await lock_organization(
                session, evidence.organization_ref, mode=LockMode.EXCLUSIVE if write else LockMode.SHARED
            )
            if activation_ref is not None:
                current_target = await session.scalar(
                    select(NativeAccountActivationTable.principal_ref).where(
                        NativeAccountActivationTable.activation_ref == activation_ref,
                        NativeAccountActivationTable.organization_ref == evidence.organization_ref,
                    )
                )
                if current_target != target:
                    raise AdminDenied()
            await self.identity.validate_human(session, evidence, datetime.now(UTC))
            yield uow

    async def _finish(
        self, session: AsyncSession, evidence: HumanAuthenticationEvidence, assignment_ref: str, action: str
    ) -> None:
        await self.identity.validate_human(session, evidence, datetime.now(UTC))
        await authorize_manager(session, evidence, assignment_ref, action, datetime.now(UTC))
        assert_organization_lock(session, evidence.organization_ref)

    async def _commit(
        self,
        uow: UnitOfWork,
        evidence: HumanAuthenticationEvidence,
        assignment_ref: str,
        action: str,
        row: NativeAccountActivationTable,
    ) -> None:
        guard = await uow.session.get(TenantGuardTable, evidence.organization_ref, populate_existing=True)
        if guard is None or guard.epoch >= MAX_VERSION:
            raise AdminConflict("OBJECT_VERSION_CONFLICT")
        guard.epoch += 1
        await uow.flush()
        source_deadline = None
        if row.status != "revoked":
            source = await load_onboarding_source(uow.session, self.config, expected=row.source_pin)
            try:
                await pending_activation_facts(uow.session, row)
            except NativeCredentialError:
                raise AdminConflict("ACTIVATION_CONFLICT") from None
            if source["expires_at"] is not None:
                source_deadline = datetime.fromisoformat(source["expires_at"])
        await self._finish(uow.session, evidence, assignment_ref, action)
        if row.status != "revoked":
            now = datetime.now(UTC)
            if now >= row.expires_at or source_deadline is not None and now >= source_deadline:
                raise AdminConflict("ACTIVATION_CONFLICT")
        assert_organization_lock(uow.session, evidence.organization_ref, mode=LockMode.EXCLUSIVE)
        try:
            await uow._commit_by_owner()
        except BaseException:
            raise AdminCommitUnknown(assignment_ref) from None

    @staticmethod
    async def _previous(
        session: AsyncSession, evidence: HumanAuthenticationEvidence, key: str, input_digest: str | None = None
    ) -> EmployeeOperationResult | None:
        row = await session.scalar(
            select(IdentityControlOperationTable).where(
                IdentityControlOperationTable.operation_type == NAMESPACE,
                IdentityControlOperationTable.actor_kind == "human",
                IdentityControlOperationTable.actor_ref == evidence.principal_ref,
                IdentityControlOperationTable.idempotency_key == key,
                IdentityControlOperationTable.authentication_json["organization_ref"].as_string()
                == evidence.organization_ref,
            )
        )
        if row is None:
            return None
        if input_digest is not None and row.input_digest != input_digest:
            raise AdminConflict("IDEMPOTENCY_CONFLICT")
        return EmployeeOperationResult.model_validate(row.result_json).model_copy(update={"replayed": True})

    @staticmethod
    def _journal(
        session: AsyncSession,
        evidence: HumanAuthenticationEvidence,
        *,
        key: str,
        input_digest: str,
        operation_type: Literal["person.create", "person.activation.reissue", "person.activation.revoke"],
        assignment_ref: str,
        authority_version: int,
        request_id: str,
        reason: str,
        result: EmployeeActivationView,
        before: EmployeeActivationView | None,
    ) -> EmployeeOperationResult:
        response = EmployeeOperationResult(
            operation_ref=str(uuid4()),
            operation_type=operation_type,
            result=result,
            replayed=False,
            completed_at=datetime.now(UTC),
        )
        authentication = evidence.audit_snapshot()
        authentication.update(
            assignment_ref=assignment_ref, authority_version=authority_version, request_id=request_id, reason=reason
        )
        session.add(
            IdentityControlOperationTable(
                operation_ref=response.operation_ref,
                operation_type=NAMESPACE,
                idempotency_key=key,
                input_digest=input_digest,
                actor_kind="human",
                actor_ref=evidence.principal_ref,
                authentication_json=authentication,
                target_ref=result.principal_ref,
                before_json=before.model_dump(mode="json") if before else None,
                after_json=result.model_dump(mode="json"),
                result_json=response.model_dump(mode="json"),
                completed_at=response.completed_at,
            )
        )
        return response

    def _delivery(self, result: EmployeeOperationResult, code: str | None = None) -> EmployeeDelivery:
        return EmployeeDelivery(
            operation=result, activation_code=code, activation_url=self.config.issuer + "/oauth/activate"
        )

    async def create(
        self,
        evidence: HumanAuthenticationEvidence,
        *,
        assignment_ref: str,
        request: EmployeeCreateRequest,
        request_id: str,
        idempotency_key: str,
    ) -> EmployeeDelivery:
        action = "administration.people.create"
        input_digest = digest(
            {"action": action, "assignment_ref": assignment_ref, "request": request.model_dump(mode="json")}
        )
        async with self._transaction(evidence, key=idempotency_key, write=True) as uow:
            session = uow.session
            manager = await authorize_manager(session, evidence, assignment_ref, action, datetime.now(UTC))
            previous = await self._previous(session, evidence, idempotency_key, input_digest)
            if previous is not None:
                await self._finish(session, evidence, assignment_ref, action)
                return self._delivery(previous)
            if manager.authority.version != request.expected_authority_version:
                raise AdminConflict("OBJECT_VERSION_CONFLICT")
            source = await load_onboarding_source(session, self.config)
            if (
                await session.scalar(
                    select(NativeAccountTable.account_ref).where(NativeAccountTable.username == request.username)
                )
                is not None
            ):
                raise AdminConflict("ACCOUNT_CONFLICT")
            activation_ref, principal_ref, membership_ref, account_ref, external_ref = (str(uuid4()) for _ in range(5))
            now = datetime.now(UTC)
            code = random_secret()
            session.add(PrincipalTable(principal_ref=principal_ref, principal_type="human", status="suspended"))
            await session.flush()
            session.add_all(
                [
                    OrganizationMembershipTable(
                        organization_ref=evidence.organization_ref,
                        membership_ref=membership_ref,
                        principal_ref=principal_ref,
                        principal_type="human",
                        status="suspended",
                        valid_from=now,
                        source_ref=activation_ref,
                    ),
                    ExternalIdentityTable(
                        external_identity_ref=external_ref,
                        provider_ref=NATIVE_ONBOARDING_PROVIDER,
                        subject_namespace=NATIVE_ONBOARDING_NAMESPACE,
                        subject_exact=account_ref,
                        principal_ref=principal_ref,
                        principal_type="human",
                        link_evidence_ref=activation_ref,
                        status="suspended",
                    ),
                ]
            )
            account = NativeAccountTable(
                account_ref=account_ref,
                principal_ref=principal_ref,
                username=request.username,
                password_hash=None,
                status="pending",
                totp_bootstrap_allowed=False,
            )
            session.add(account)
            try:
                await session.flush()
            except IntegrityError:
                raise AdminConflict("ACCOUNT_CONFLICT") from None
            row = NativeAccountActivationTable(
                activation_ref=activation_ref,
                account_ref=account_ref,
                organization_ref=evidence.organization_ref,
                principal_ref=principal_ref,
                membership_ref=membership_ref,
                external_identity_ref=external_ref,
                source_pin=source,
                code_hash=digest_secret(code),
                status="issued",
                version=1,
                expires_at=now + timedelta(seconds=request.expires_in_seconds),
            )
            session.add(row)
            await session.flush()
            result = self._journal(
                session,
                evidence,
                key=idempotency_key,
                input_digest=input_digest,
                operation_type="person.create",
                assignment_ref=assignment_ref,
                authority_version=manager.authority.version,
                request_id=request_id,
                reason=request.reason,
                result=activation_view(row, account),
                before=None,
            )
            await self._commit(uow, evidence, assignment_ref, action, row)
            return self._delivery(result, code)

    async def change(
        self,
        evidence: HumanAuthenticationEvidence,
        *,
        assignment_ref: str,
        activation_ref: str,
        request: EmployeeActivationChange,
        request_id: str,
        idempotency_key: str,
        reissue: bool,
    ) -> EmployeeDelivery:
        if reissue != isinstance(request, EmployeeActivationReissue):
            raise ValueError("activation_change_request_mismatch")
        action = "administration.people.activation.reissue" if reissue else "administration.people.activation.revoke"
        input_digest = digest(
            {
                "action": action,
                "assignment_ref": assignment_ref,
                "activation_ref": activation_ref,
                "request": request.model_dump(mode="json"),
            }
        )
        async with self._transaction(evidence, key=idempotency_key, activation_ref=activation_ref, write=True) as uow:
            session = uow.session
            manager = await authorize_manager(session, evidence, assignment_ref, action, datetime.now(UTC))
            previous = await self._previous(session, evidence, idempotency_key, input_digest)
            if previous is not None:
                await self._finish(session, evidence, assignment_ref, action)
                return self._delivery(previous)
            row = await session.get(NativeAccountActivationTable, activation_ref, populate_existing=True)
            if row is None or row.organization_ref != evidence.organization_ref:
                raise AdminNotFound()
            if (
                manager.authority.version != request.expected_authority_version
                or row.version != request.expected_activation_version
                or row.version >= MAX_VERSION
            ):
                raise AdminConflict("OBJECT_VERSION_CONFLICT")
            try:
                principal, member, external, account = await pending_activation_facts(session, row)
            except NativeCredentialError:
                raise AdminConflict("ACTIVATION_CONFLICT") from None
            before = activation_view(row, account)
            clear_activation_secrets(row)
            row.version += 1
            row.failures = 0
            code = None
            if reissue:
                if not isinstance(request, EmployeeActivationReissue):
                    raise ValueError("reissue_request_required")
                row.source_pin = await load_onboarding_source(session, self.config)
                row.status = "issued"
                row.expires_at = datetime.now(UTC) + timedelta(seconds=request.expires_in_seconds)
                code = random_secret()
                row.code_hash = digest_secret(code)
            else:
                row.status = "revoked"
                principal.status = external.status = member.status = "revoked"
                principal.disabled_at = member.ended_at = datetime.now(UTC)
                principal.version += 1
                external.version += 1
                member.version += 1
                account.status = "disabled"
                account.credential_version += 1
            result = self._journal(
                session,
                evidence,
                key=idempotency_key,
                input_digest=input_digest,
                operation_type="person.activation.reissue" if reissue else "person.activation.revoke",
                assignment_ref=assignment_ref,
                authority_version=manager.authority.version,
                request_id=request_id,
                reason=request.reason,
                result=activation_view(row, account),
                before=before,
            )
            await self._commit(uow, evidence, assignment_ref, action, row)
            return self._delivery(result, code)

    async def inspect(
        self, evidence: HumanAuthenticationEvidence, *, assignment_ref: str, activation_ref: str
    ) -> EmployeeActivationView:
        async with self._transaction(evidence, activation_ref=activation_ref) as uow:
            await self._finish(uow.session, evidence, assignment_ref, "administration.people.read")
            row = await uow.session.get(NativeAccountActivationTable, activation_ref, populate_existing=True)
            if row is None or row.organization_ref != evidence.organization_ref:
                raise AdminNotFound()
            account = await uow.session.get(NativeAccountTable, row.account_ref, populate_existing=True)
            if account is None:
                raise AdminNotFound()
            result = activation_view(row, account)
            await self._finish(uow.session, evidence, assignment_ref, "administration.people.read")
            return result

    async def operation(
        self, evidence: HumanAuthenticationEvidence, *, assignment_ref: str, idempotency_key: str
    ) -> EmployeeOperationResult:
        async with self._transaction(evidence, key=idempotency_key) as uow:
            await self._finish(uow.session, evidence, assignment_ref, "administration.people.read")
            result = await self._previous(uow.session, evidence, idempotency_key)
            if result is None:
                raise AdminNotFound()
            await self._finish(uow.session, evidence, assignment_ref, "administration.people.read")
            return result

    async def audit(
        self, evidence: HumanAuthenticationEvidence, *, assignment_ref: str, limit: int = 20, after: str | None = None
    ) -> EmployeeAuditList:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("invalid_page_limit")
        async with self._transaction(evidence) as uow:
            await self._finish(uow.session, evidence, assignment_ref, "administration.audit.read")
            query = select(IdentityControlOperationTable).where(
                IdentityControlOperationTable.operation_type == NAMESPACE,
                IdentityControlOperationTable.actor_kind == "human",
                IdentityControlOperationTable.authentication_json["organization_ref"].as_string()
                == evidence.organization_ref,
            )
            if after is not None:
                query = query.where(IdentityControlOperationTable.operation_ref > after)
            rows = tuple(
                await uow.session.scalars(query.order_by(IdentityControlOperationTable.operation_ref).limit(limit + 1))
            )
            items = tuple(
                EmployeeAuditView(
                    operation=EmployeeOperationResult.model_validate(row.result_json),
                    actor_ref=row.actor_ref,
                    assignment_ref=row.authentication_json["assignment_ref"],
                    request_id=row.authentication_json["request_id"],
                    reason=row.authentication_json["reason"],
                )
                for row in rows[:limit]
            )
            await self._finish(uow.session, evidence, assignment_ref, "administration.audit.read")
            return EmployeeAuditList(
                items=items, next_after=rows[limit - 1].operation_ref if len(rows) > limit else None
            )
