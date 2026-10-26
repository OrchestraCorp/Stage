"""Join verified human identity with current assignment authorization in one transaction."""

from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.context import AuthorizationContext
from app.authz.loader import DatabaseAuthorizationLoader
from app.core.enums import PrincipalType
from app.execution.contracts import ExecutionContext
from app.execution.errors import AuthorizationDenied
from app.identity.evidence import HumanAuthenticationEvidence


class HumanIdentityVerifier(Protocol):
    async def acquire_human_locks(self, session: AsyncSession, evidence: HumanAuthenticationEvidence) -> None: ...

    async def validate_human(
        self,
        session: AsyncSession,
        evidence: HumanAuthenticationEvidence,
        now: datetime,
    ) -> None: ...


class AuthenticatedAuthorizationLoader:
    def __init__(self, identity: HumanIdentityVerifier) -> None:
        self._identity = identity
        self._authorization = DatabaseAuthorizationLoader()
        self._marker = object()

    @staticmethod
    def _evidence(context: ExecutionContext) -> HumanAuthenticationEvidence:
        evidence = context.authentication
        if (
            evidence is None
            or context.principal_type is not PrincipalType.HUMAN
            or context.actor_ref != evidence.principal_ref
            or context.effective_subject_ref != evidence.principal_ref
            or context.tenant_ref != evidence.organization_ref
            or context.delegation_ref is not None
            or context.assignment_ref is None
        ):
            raise AuthorizationDenied()
        return evidence

    async def acquire_locks(self, session: AsyncSession, context: ExecutionContext) -> None:
        evidence = self._evidence(context)
        # Both identity and authorization are held stable before taking the
        # organization guard. No network verification happens in this transaction.
        await self._identity.acquire_human_locks(session, evidence)
        await self._authorization.acquire_locks(session, context)
        session.info[self._marker] = (session.sync_session.get_transaction(), evidence)

    def _check_locks(self, session: AsyncSession, context: ExecutionContext) -> HumanAuthenticationEvidence:
        evidence = self._evidence(context)
        if not session.in_transaction() or session.info.get(self._marker) != (
            session.sync_session.get_transaction(),
            evidence,
        ):
            raise AuthorizationDenied()
        return evidence

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
        evidence = self._check_locks(session, context)
        facts = await self._authorization.load(
            session,
            context,
            action=action,
            target_ref=target_ref,
            expected_version=expected_version,
            now=now,
        )
        await self._identity.validate_human(session, evidence, now)
        if facts.assignment is not None and facts.assignment.membership_ref != evidence.membership_ref:
            raise AuthorizationDenied()
        # The authorization loader now holds the organization guard. Only
        # this same transaction may invoke the final identity revalidation.
        session.info[(self._marker, "loaded")] = (session.sync_session.get_transaction(), evidence)
        return facts

    async def validate_before_commit(
        self,
        session: AsyncSession,
        context: ExecutionContext,
        now: datetime,
    ) -> None:
        evidence = self._check_locks(session, context)
        if session.info.get((self._marker, "loaded")) != (session.sync_session.get_transaction(), evidence):
            raise AuthorizationDenied()
        await self._identity.validate_human(session, evidence, now)
