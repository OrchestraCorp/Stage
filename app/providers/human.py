"""Shared trusted entry for bounded, authenticated human business operations."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.authenticated import AuthenticatedAuthorizationLoader, HumanIdentityVerifier
from app.core.enums import PrincipalType
from app.execution.contracts import CommandSpec, ExecutionContext
from app.execution.kernel import CommandKernel
from app.identity.evidence import HumanAuthenticationEvidence
from app.providers.identity import IDENTITY
from app.providers.storage import STORAGE
from app.runtime import ComponentContext


def human_execution_context(
    evidence: HumanAuthenticationEvidence,
    *,
    assignment_ref: str,
    request_id: str,
    deadline: datetime,
) -> ExecutionContext:
    return ExecutionContext(
        tenant_ref=evidence.organization_ref,
        actor_ref=evidence.principal_ref,
        effective_subject_ref=evidence.principal_ref,
        request_id=request_id,
        principal_type=PrincipalType.HUMAN,
        credential_expires_at=evidence.valid_until,
        assignment_ref=assignment_ref,
        deadline=deadline,
        authentication=evidence,
    )


def create_human_kernel(
    sessions: async_sessionmaker[AsyncSession],
    identity: HumanIdentityVerifier,
    commands: Sequence[CommandSpec],
) -> CommandKernel:
    """Install all three identity hooks together; none may be omitted."""
    loader = AuthenticatedAuthorizationLoader(identity)
    return CommandKernel(sessions, loader=loader, coordinator=loader, identity_validator=loader, commands=commands)


@dataclass(frozen=True)
class HumanExecution:
    context: ExecutionContext
    kernel: CommandKernel


class HumanOperations:
    """Keep identity/storage leases and the request deadline around all DB work.

    The owning feature declares IDENTITY and STORAGE as Runtime dependencies.
    It supplies only its registered commands and materialized read callbacks.
    """

    def __init__(self, context: ComponentContext, timeout_seconds: float, commands: Sequence[CommandSpec]) -> None:
        self._context = context
        self._timeout_seconds = timeout_seconds
        self._commands = tuple(commands)

    @asynccontextmanager
    async def request(
        self,
        token: str,
        *,
        profile_ref: str,
        profile_version: int,
        assignment_ref: str,
        request_id: str,
    ) -> AsyncIterator[HumanExecution]:
        deadline = datetime.now(UTC) + timedelta(seconds=self._timeout_seconds)
        async with asyncio.timeout(self._timeout_seconds), self._context.call(IDENTITY, STORAGE) as call:
            identity = call.require(IDENTITY)
            evidence = await identity.authenticate_human(
                token, profile_ref=profile_ref, profile_version=profile_version
            )
            context = human_execution_context(
                evidence, assignment_ref=assignment_ref, request_id=request_id, deadline=deadline
            )
            yield HumanExecution(context, create_human_kernel(call.require(STORAGE).sessions, identity, self._commands))
