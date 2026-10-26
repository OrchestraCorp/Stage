"""Versioned internal contracts, constructed only by trusted host adapters.

Frozen dataclasses are not authentication. No endpoint accepts ExecutionContext
from a request body. A current authorization loader is mandatory in the kernel.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.context import AuthorizationContext
from app.core.enums import PrincipalType
from app.execution.errors import ContextExpired, InvalidCommand, RequestDeadlineExceeded
from app.identity.evidence import HumanAuthenticationEvidence

JsonObject = dict[str, Any]
Clock = Callable[[], datetime]


def utc_clock() -> datetime:
    return datetime.now(UTC)


def require_ref(value: str, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise InvalidCommand()


def require_time(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidCommand()


@dataclass(frozen=True)
class ExecutionContext:
    tenant_ref: str
    actor_ref: str
    request_id: str
    principal_type: PrincipalType
    credential_expires_at: datetime
    effective_subject_ref: str
    assignment_ref: str | None = None
    delegation_ref: str | None = None
    deadline: datetime | None = None
    authentication: HumanAuthenticationEvidence | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for value in (self.tenant_ref, self.actor_ref, self.effective_subject_ref, self.request_id):
            require_ref(value, 128)
        for optional_ref in (self.assignment_ref, self.delegation_ref):
            if optional_ref is not None:
                require_ref(optional_ref, 128)
        if not isinstance(self.principal_type, PrincipalType):
            raise InvalidCommand()
        require_time(self.credential_expires_at)
        if self.deadline is not None:
            require_time(self.deadline)
        evidence = self.authentication
        if evidence is not None:
            if (
                self.principal_type is not PrincipalType.HUMAN
                or self.actor_ref != evidence.principal_ref
                or self.effective_subject_ref != evidence.principal_ref
                or self.tenant_ref != evidence.organization_ref
                or self.delegation_ref is not None
            ):
                raise InvalidCommand()
            require_time(evidence.valid_until)
            if self.credential_expires_at > evidence.valid_until:
                raise InvalidCommand()

    def check_live(self, now: datetime) -> None:
        require_time(now)
        if now >= self.credential_expires_at:
            raise ContextExpired()
        if self.authentication is not None and now >= self.authentication.valid_until:
            raise ContextExpired()
        if self.deadline is not None and now >= self.deadline:
            raise RequestDeadlineExceeded()


@dataclass(frozen=True)
class CommandEnvelope:
    name: str
    version: str
    target_ref: str
    expected_version: int
    idempotency_key: str
    payload: JsonObject

    def __post_init__(self) -> None:
        for value, limit in ((self.name, 96), (self.version, 32), (self.target_ref, 256), (self.idempotency_key, 128)):
            require_ref(value, limit)
        # This initial executor handles existing versioned resources only.
        # Parent-scope authorization for create commands is a separate contract.
        if type(self.expected_version) is not int or not 1 <= self.expected_version <= 2**31 - 1:
            raise InvalidCommand()


@dataclass(frozen=True)
class OutboxIntent:
    topic: str
    destination: str
    aggregate_ref: str
    read_action: str
    dedupe_key: str
    payload: JsonObject

    def __post_init__(self) -> None:
        for value, limit in (
            (self.topic, 128),
            (self.destination, 128),
            (self.aggregate_ref, 256),
            (self.read_action, 96),
            (self.dedupe_key, 128),
        ):
            require_ref(value, limit)


@dataclass(frozen=True)
class CommandEffect:
    result: JsonObject
    before: JsonObject | None = None
    after: JsonObject | None = None
    messages: tuple[OutboxIntent, ...] = ()


@dataclass(frozen=True)
class CommandResult:
    execution_id: str
    result: JsonObject
    replayed: bool
    completed_at: datetime
    status: str = field(default="succeeded", init=False)


class AuthorizationLockCoordinator(Protocol):
    async def acquire_locks(self, session: AsyncSession, context: ExecutionContext) -> None:
        """Acquire trust/global identity locks before the organization guard.

        This hook cannot perform network I/O or finish the caller's transaction.
        Its implementations must discover dependencies before taking later locks.
        """
        ...


class CommandIdentityValidator(Protocol):
    async def validate_before_commit(
        self,
        session: AsyncSession,
        context: ExecutionContext,
        now: datetime,
    ) -> None:
        """Revalidate server-issued authentication evidence with no network or commit."""
        ...


class AuthorizationLoader(Protocol):
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
        """Read authoritative tenant-scoped facts using the current transaction.

        Authorization changes must acquire the same tenant guard. This loader
        must not perform network I/O or return a cached AuthorizationDecision.
        """
        ...


Handler = Callable[["UnitOfWork", ExecutionContext, CommandEnvelope, BaseModel], Awaitable[CommandEffect]]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    version: str
    execute_action: str
    read_action: str
    input_model: type[BaseModel]
    result_model: type[BaseModel]
    handler: Handler

    def __post_init__(self) -> None:
        for value, limit in (
            (self.name, 96),
            (self.version, 32),
            (self.execute_action, 96),
            (self.read_action, 96),
        ):
            require_ref(value, limit)
        if (
            self.input_model.model_config.get("extra") != "forbid"
            or self.result_model.model_config.get("extra") != "forbid"
        ):
            raise ValueError("Command input/result models must forbid extra fields")


# Imported last to avoid introducing a storage dependency into authorization.
from app.execution.storage import UnitOfWork  # noqa: E402
