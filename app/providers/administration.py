"""Administration composes current human identity with one bounded storage lease."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from app.administration.contracts import (
    ActivationRequest,
    AdminAssignmentList,
    AdministrationMe,
    AuditList,
    GrantRequest,
    OperationResult,
    PeopleList,
    PersonView,
    RevokeRequest,
)
from app.core.config import Settings
from app.identity.evidence import HumanAuthenticationEvidence
from app.providers.identity import IDENTITY
from app.providers.native_auth import NATIVE_AUTH
from app.providers.storage import STORAGE
from app.runtime import ComponentContext, MissingService, PluginSpec, ServiceKey

if TYPE_CHECKING:
    from app.administration.onboarding import EmployeeAdministration
    from app.administration.service import AdministrationService

ADMINISTRATION: ServiceKey["AdministrationFacade"] = ServiceKey("stage", "administration", "1")


class AdministrationFacade:
    def __init__(self, context: ComponentContext, timeout_seconds: float, *, native_enabled: bool = False) -> None:
        self._context = context
        self._timeout_seconds = timeout_seconds
        self._native_enabled = native_enabled

    @asynccontextmanager
    async def employee_operation(
        self, token: str, *, profile_ref: str, profile_version: int
    ) -> AsyncIterator[tuple["EmployeeAdministration", HumanAuthenticationEvidence]]:
        from app.administration.onboarding import EmployeeAdministration

        if not self._native_enabled:
            raise MissingService("employee_onboarding_requires_native_auth")
        async with asyncio.timeout(self._timeout_seconds), self._context.call(IDENTITY, STORAGE, NATIVE_AUTH) as call:
            identity = call.require(IDENTITY)
            evidence = await identity.authenticate_human(
                token, profile_ref=profile_ref, profile_version=profile_version
            )
            yield (
                EmployeeAdministration(call.require(STORAGE).sessions, identity, call.require(NATIVE_AUTH).config),
                evidence,
            )

    @asynccontextmanager
    async def _operation(
        self, token: str, profile_ref: str, profile_version: int
    ) -> AsyncIterator[tuple["AdministrationService", HumanAuthenticationEvidence]]:
        from app.administration.service import AdministrationService

        async with asyncio.timeout(self._timeout_seconds), self._context.call(IDENTITY, STORAGE) as call:
            identity = call.require(IDENTITY)
            evidence = await identity.authenticate_human(
                token, profile_ref=profile_ref, profile_version=profile_version
            )
            yield AdministrationService(call.require(STORAGE).sessions, identity), evidence

    async def me(self, token: str, *, profile_ref: str, profile_version: int) -> AdministrationMe:
        async with self._operation(token, profile_ref, profile_version) as (service, evidence):
            return await service.me(evidence)

    async def activate(
        self,
        token: str,
        *,
        profile_ref: str,
        profile_version: int,
        request: ActivationRequest,
        request_id: str,
        idempotency_key: str,
    ) -> OperationResult:
        async with self._operation(token, profile_ref, profile_version) as (service, evidence):
            return await service.activate(
                evidence, request=request, request_id=request_id, idempotency_key=idempotency_key
            )

    async def people(
        self,
        token: str,
        *,
        profile_ref: str,
        profile_version: int,
        assignment_ref: str,
        limit: int = 20,
        after: str | None = None,
    ) -> PeopleList:
        async with self._operation(token, profile_ref, profile_version) as (service, evidence):
            return await service.people(evidence, assignment_ref=assignment_ref, limit=limit, after=after)

    async def person(
        self,
        token: str,
        *,
        profile_ref: str,
        profile_version: int,
        assignment_ref: str,
        principal_ref: str,
    ) -> PersonView:
        async with self._operation(token, profile_ref, profile_version) as (service, evidence):
            return await service.person(evidence, assignment_ref=assignment_ref, principal_ref=principal_ref)

    async def assignments(
        self,
        token: str,
        *,
        profile_ref: str,
        profile_version: int,
        assignment_ref: str,
        principal_ref: str,
        limit: int = 20,
        after: str | None = None,
    ) -> AdminAssignmentList:
        async with self._operation(token, profile_ref, profile_version) as (service, evidence):
            return await service.assignments(
                evidence, assignment_ref=assignment_ref, principal_ref=principal_ref, limit=limit, after=after
            )

    async def grant(
        self,
        token: str,
        *,
        profile_ref: str,
        profile_version: int,
        assignment_ref: str,
        principal_ref: str,
        request: GrantRequest,
        request_id: str,
        idempotency_key: str,
    ) -> OperationResult:
        async with self._operation(token, profile_ref, profile_version) as (service, evidence):
            return await service.grant(
                evidence,
                assignment_ref=assignment_ref,
                principal_ref=principal_ref,
                request=request,
                request_id=request_id,
                idempotency_key=idempotency_key,
            )

    async def revoke(
        self,
        token: str,
        *,
        profile_ref: str,
        profile_version: int,
        assignment_ref: str,
        principal_ref: str,
        target_assignment_ref: str,
        request: RevokeRequest,
        request_id: str,
        idempotency_key: str,
    ) -> OperationResult:
        async with self._operation(token, profile_ref, profile_version) as (service, evidence):
            return await service.revoke(
                evidence,
                assignment_ref=assignment_ref,
                principal_ref=principal_ref,
                target_assignment_ref=target_assignment_ref,
                request=request,
                request_id=request_id,
                idempotency_key=idempotency_key,
            )

    async def operation(
        self,
        token: str,
        *,
        profile_ref: str,
        profile_version: int,
        assignment_ref: str,
        idempotency_key: str,
    ) -> OperationResult:
        async with self._operation(token, profile_ref, profile_version) as (service, evidence):
            return await service.operation(evidence, assignment_ref=assignment_ref, idempotency_key=idempotency_key)

    async def audit(
        self,
        token: str,
        *,
        profile_ref: str,
        profile_version: int,
        assignment_ref: str,
        limit: int = 20,
        after: str | None = None,
    ) -> AuditList:
        async with self._operation(token, profile_ref, profile_version) as (service, evidence):
            return await service.audit(evidence, assignment_ref=assignment_ref, limit=limit, after=after)


def administration_plugin(config: Settings) -> PluginSpec:
    def setup(ctx: ComponentContext) -> None:
        ctx.provide(
            ADMINISTRATION,
            AdministrationFacade(
                ctx, config.command_request_timeout_seconds, native_enabled=config.native_auth_enabled
            ),
        )

    requirements = (IDENTITY, STORAGE, NATIVE_AUTH) if config.native_auth_enabled else (IDENTITY, STORAGE)
    return PluginSpec("administration", setup, requires=requirements, provides=(ADMINISTRATION,))
