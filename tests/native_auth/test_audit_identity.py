"""Independent audit regressions for native identity and business read boundaries."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.execution.errors import AuthorizationDenied, ContextExpired, RequestDeadlineExceeded
from app.execution.kernel import CommandKernel
from app.identity.authentication import AuthenticationAssuranceRequired, AuthenticationRejected
from app.identity.native import NATIVE_ACR
from app.identity.service import PROFILE_SCHEMA, IdentityService, content_digest, profile_content
from app.models.authorization import RelationTupleTable, RoleAssignmentTable
from app.models.execution import CommandExecutionTable
from app.models.identity_foundation import (
    AuthenticationProfileTable,
    ExternalIdentityTable,
    OrganizationAuthenticationBindingTable,
    OrganizationHumanAuthenticationPolicyTable,
)
from app.models.work import WorkItemTable
from app.native_auth.keys import KeyRing
from app.providers import human as human_provider
from app.providers.identity import IDENTITY, IdentityFacade
from app.providers.storage import STORAGE, Storage
from app.providers.work import WORK, human_work_plugin
from app.runtime import ComponentContext, PluginSpec, RuntimeHost
from tests.support import native_identity as identity_support
from tests.support.authenticated_commands import authenticated_context, authenticated_kernel
from tests.support.authorization import ORG, PERSON
from tests.support.native_identity import native_context
from tests.support.work_details import assert_unchanged, command

native_keyring = identity_support.native_keyring


@pytest.mark.parametrize("expires", ["authentication", "mfa", "assignment", "relation"])
async def test_native_work_read_rechecks_expiry_after_the_final_database_read(
    persistence_sessions: async_sessionmaker[AsyncSession],
    native_keyring: KeyRing,
    monkeypatch: pytest.MonkeyPatch,
    expires: str,
) -> None:
    identity, _, _, tokens = await native_context(persistence_sessions, native_keyring)
    offset = [timedelta()]
    if expires in {"assignment", "relation"}:
        async with persistence_sessions.begin() as session:
            model = RoleAssignmentTable if expires == "assignment" else RelationTupleTable
            grant = await session.scalar(select(model))
            assert grant is not None
            grant.expires_at = datetime.now(UTC) + timedelta(seconds=15)
    elif expires == "mfa":
        async with persistence_sessions.begin() as session:
            session.add(
                OrganizationHumanAuthenticationPolicyTable(
                    organization_ref=ORG,
                    version=1,
                    mode="required",
                    rules_json=[
                        {
                            "profile_ref": "local-human",
                            "profile_version": 1,
                            "required_acr": NATIVE_ACR,
                            "required_amr": ["pwd", "otp"],
                        }
                    ],
                    max_auth_age_seconds=15,
                )
            )

    class ReadClock:
        @staticmethod
        def now(zone: Any = None) -> datetime:
            return datetime.now(UTC) + offset[0]

    class ReadKernel(CommandKernel):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs, clock=lambda: ReadClock.now(UTC))

    original_get = AsyncSession.get
    read_completed = False

    async def delayed_get(self: AsyncSession, entity: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal read_completed
        result = await original_get(self, entity, *args, **kwargs)
        if entity is WorkItemTable:
            # Model the last database await completing across a temporal boundary.
            # No sleep or concurrent security-table mutation is involved.
            offset[0] += timedelta(seconds=61 if expires == "authentication" else 16)
            read_completed = True
        return result

    monkeypatch.setattr(human_provider, "datetime", ReadClock)
    monkeypatch.setattr(human_provider, "CommandKernel", ReadKernel)
    monkeypatch.setattr(AsyncSession, "get", delayed_get)

    def storage_setup(ctx: ComponentContext) -> None:
        ctx.provide(STORAGE, Storage(persistence_sessions.kw["bind"], persistence_sessions))

    def identity_setup(ctx: ComponentContext) -> None:
        ctx.effect(lambda: identity.aclose, label="audit.identity")
        ctx.provide(IDENTITY, IdentityFacade(ctx, identity))

    host = RuntimeHost()
    host.load(PluginSpec("audit.storage", storage_setup, provides=(STORAGE,)))
    host.load(PluginSpec("audit.identity", identity_setup, requires=(STORAGE,), provides=(IDENTITY,)))
    host.load(
        human_work_plugin(
            Settings(env="test", database_url="sqlite+aiosqlite://", redis_url=None, command_request_timeout_seconds=60)
        )
    )
    try:
        assert (await host.await_ready([WORK], timeout=2)).ready
        async with host.call(WORK) as call:
            with pytest.raises((AuthenticationRejected, ContextExpired, RequestDeadlineExceeded, AuthorizationDenied)):
                await call.require(WORK).get_item(
                    tokens["access_token"],
                    work_item_id=1,
                    profile_ref="local-human",
                    profile_version=1,
                    assignment_ref="assignment:engineering",
                    request_id="audit-final-read-expiry",
                )
        assert read_completed
    finally:
        assert (await host.close(timeout=2)).successful


@pytest.mark.parametrize("lookup", ["execution_id", "operation_key", "replay"])
async def test_native_result_recovery_rechecks_expiry_after_loading_the_result(
    persistence_sessions: async_sessionmaker[AsyncSession],
    native_keyring: KeyRing,
    monkeypatch: pytest.MonkeyPatch,
    lookup: str,
) -> None:
    identity, _, context, _ = await native_context(persistence_sessions, native_keyring)
    offset = [timedelta()]
    kernel = authenticated_kernel(persistence_sessions, identity, clock=lambda: datetime.now(UTC) + offset[0])
    request = command()
    try:
        committed = await kernel.execute(context, request)
        original_scalar = AsyncSession.scalar
        read_completed = False

        async def delayed_scalar(self: AsyncSession, statement: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal read_completed
            result = await original_scalar(self, statement, *args, **kwargs)
            if isinstance(result, CommandExecutionTable):
                offset[0] += timedelta(seconds=61)
                read_completed = True
            return result

        monkeypatch.setattr(AsyncSession, "scalar", delayed_scalar)
        with pytest.raises((AuthenticationRejected, ContextExpired)):
            if lookup == "execution_id":
                await kernel.get_result(context, committed.execution_id)
            elif lookup == "operation_key":
                await kernel.get_result_by_key(
                    context,
                    command_name=request.name,
                    command_version=request.version,
                    idempotency_key=request.idempotency_key,
                    target_ref=request.target_ref,
                )
            else:
                await kernel.execute(context, request)
        assert read_completed
    finally:
        await identity.aclose()


async def test_native_signed_role_claims_never_replace_current_assignment_permission(
    persistence_sessions: async_sessionmaker[AsyncSession], native_keyring: KeyRing
) -> None:
    identity, oauth, _, tokens = await native_context(persistence_sessions, native_keyring)
    try:
        claims = native_keyring.verify_access(tokens["access_token"], oauth.config.issuer, oauth.config.audience)
        elevated = native_keyring.sign({**claims, "roles": ["admin"], "permissions": ["task.metadata.update"]})
        async with persistence_sessions.begin() as session:
            assignment = await session.scalar(select(RoleAssignmentTable))
            assert assignment is not None
            assignment.status = "revoked"
            assignment.revoked_at = datetime.now(UTC)
            assignment.version += 1
        context = await authenticated_context(identity, elevated)
        with pytest.raises(AuthorizationDenied):
            await authenticated_kernel(persistence_sessions, identity).execute(context, command())
        await assert_unchanged(persistence_sessions)
    finally:
        await identity.aclose()


async def test_native_mfa_policy_excludes_an_admitted_legacy_profile_even_with_strong_claims(
    persistence_sessions: async_sessionmaker[AsyncSession], native_keyring: KeyRing
) -> None:
    identity, oauth, old_context, tokens = await native_context(persistence_sessions, native_keyring)
    try:
        async with persistence_sessions.begin() as session:
            legacy = AuthenticationProfileTable(
                profile_ref="legacy-human",
                profile_version=1,
                provider_ref="provider",
                subject_namespace="keycloak-human-v1",
                credential_kind="human_oidc",
                stage_audience="stage-api",
                config_schema=PROFILE_SCHEMA,
                config_json={"jwks_url": oauth.config.issuer + "/oauth/jwks", "allowed_clients": ["maestro"]},
            )
            legacy.digest = content_digest(profile_content(legacy))
            session.add(legacy)
            session.add(
                ExternalIdentityTable(
                    external_identity_ref="legacy-identity",
                    provider_ref="provider",
                    subject_namespace="keycloak-human-v1",
                    subject_exact="legacy-subject",
                    principal_ref=PERSON,
                    principal_type="human",
                    link_evidence_ref="audit-explicit-migration",
                )
            )
            await session.flush()
            session.add(
                OrganizationAuthenticationBindingTable(
                    organization_ref=ORG,
                    binding_ref="legacy-binding",
                    profile_ref="legacy-human",
                    profile_version=1,
                    environment="test",
                    constraints_json={"allowed_clients": ["maestro"]},
                    valid_from=datetime.now(UTC) - timedelta(days=1),
                )
            )
            session.add(
                OrganizationHumanAuthenticationPolicyTable(
                    organization_ref=ORG,
                    version=1,
                    mode="required",
                    rules_json=[
                        {
                            "profile_ref": "local-human",
                            "profile_version": 1,
                            "required_acr": NATIVE_ACR,
                            "required_amr": ["pwd", "otp"],
                        }
                    ],
                    max_auth_age_seconds=900,
                )
            )
        claims = native_keyring.verify_access(tokens["access_token"], oauth.config.issuer, oauth.config.audience)
        legacy_token = native_keyring.sign(
            {
                **claims,
                "sub": "legacy-subject",
                "azp": "maestro",
                "typ": "Bearer",
                "stage_credential_kind": "human_oidc",
            },
            typ="JWT",
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=native_keyring.jwks()))
        ) as client:
            migration_identity = IdentityService(
                persistence_sessions,
                environment="test",
                http_client=client,
                native_keyring=native_keyring,
                native_issuer=oauth.config.issuer,
            )
            with pytest.raises(AuthenticationAssuranceRequired):
                await migration_identity.read_self(legacy_token, profile_ref="legacy-human", profile_version=1)
            with pytest.raises(AuthenticationRejected):
                await migration_identity.read_self(legacy_token, profile_ref="local-human", profile_version=1)
            with pytest.raises(AuthenticationRejected):
                await migration_identity.read_self(
                    tokens["access_token"], profile_ref="legacy-human", profile_version=1
                )
            assert (
                await migration_identity.read_self(tokens["access_token"], profile_ref="local-human", profile_version=1)
            ).principal_ref == PERSON
            await migration_identity.aclose()
        with pytest.raises(AuthenticationRejected):
            await authenticated_kernel(persistence_sessions, identity).execute(old_context, command())
        fresh_context = await authenticated_context(identity, tokens["access_token"])
        assert (await authenticated_kernel(persistence_sessions, identity).execute(fresh_context, command())).result[
            "version"
        ] == 2
    finally:
        await identity.aclose()
