"""Current assignment discovery with one leased identity/storage transaction."""

import asyncio
from datetime import UTC, datetime

from app.authz.assignments import AssignmentList, load_self_assignments
from app.core.config import Settings
from app.execution.storage import lock_tenant, managed_uow
from app.identity.authentication import AuthenticationRejected
from app.providers.identity import IDENTITY
from app.providers.storage import STORAGE
from app.runtime import ComponentContext, PluginSpec, ServiceKey

ASSIGNMENTS: ServiceKey["AssignmentService"] = ServiceKey("stage", "self-assignments", "1")


class AssignmentService:
    def __init__(self, context: ComponentContext, timeout_seconds: float) -> None:
        self._context = context
        self._timeout_seconds = timeout_seconds

    async def list_self(
        self,
        token: str,
        *,
        profile_ref: str,
        profile_version: int,
        limit: int = 20,
        after: str | None = None,
    ) -> AssignmentList:
        async with asyncio.timeout(self._timeout_seconds), self._context.call(IDENTITY, STORAGE) as call:
            identity = call.require(IDENTITY)
            evidence = await identity.authenticate_human(
                token, profile_ref=profile_ref, profile_version=profile_version
            )
            async with managed_uow(call.require(STORAGE).sessions) as uow:
                # Same writer protocol as human commands, without requiring an
                # assignment before the caller has been able to discover one.
                await identity.acquire_human_locks(uow.session, evidence)
                await lock_tenant(uow.session, evidence.organization_ref)
                await identity.validate_human(uow.session, evidence, datetime.now(UTC))
                result = await load_self_assignments(
                    uow.session, evidence, now=datetime.now(UTC), limit=limit, after=after
                )
                await identity.validate_human(uow.session, evidence, datetime.now(UTC))
                # Authentication verification can await database reads. Apply
                # assignment expiry again using a clock sampled after those.
                now = datetime.now(UTC)
                if now >= evidence.valid_until:
                    raise AuthenticationRejected("identity_evidence_expired")
                return result.current_at(now)


def assignments_plugin(config: Settings) -> PluginSpec:
    def setup(ctx: ComponentContext) -> None:
        ctx.provide(ASSIGNMENTS, AssignmentService(ctx, config.identity_request_timeout_seconds))

    return PluginSpec("self-assignments", setup, requires=(IDENTITY, STORAGE), provides=(ASSIGNMENTS,))
