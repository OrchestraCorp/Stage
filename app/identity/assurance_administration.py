"""Explicit local dev/test control of organization-wide human login assurance."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import Field, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.control.contracts import BOOTSTRAP_TYPE, AdministrationError, AdministrationResult, Environment, StrictPlan
from app.control.operations import (
    acquire_admin_lock,
    append_journal,
    check_database_environment,
    find_replay,
    operation_transaction,
    validate_operator,
)
from app.execution.storage import lock_tenant
from app.identity.assurance import AssuranceReference, HumanAssurancePolicy, HumanAssuranceRule
from app.identity.native import NATIVE_PROFILE_SCHEMA
from app.identity.service import (
    PROFILE_SCHEMA,
    AdmissionConstraints,
    ProfileConfiguration,
    content_digest,
    current,
    profile_content,
)
from app.models.execution import TenantGuardTable
from app.models.identity_foundation import (
    AuthenticationProfileTable,
    BootstrapRecordTable,
    IdentityProviderTable,
    OrganizationAuthenticationBindingTable,
    OrganizationHumanAuthenticationPolicyTable,
    OrganizationRootTable,
    TrustStateTable,
)

OPERATION_TYPE = "configure.human-authentication"


class ConfigureHumanAuthenticationRequest(StrictPlan):
    environment: Environment
    organization_ref: AssuranceReference
    expected_version: int = Field(strict=True, ge=0, le=2**63 - 1)
    policy: HumanAssurancePolicy


async def _lock_rule_trusts(session: AsyncSession, rules: tuple[HumanAssuranceRule, ...]) -> None:
    """Keep trust -> organization lock order shared with authenticated requests."""
    trust_refs: set[str] = set()
    for rule in rules:
        profile = await session.get(AuthenticationProfileTable, (rule.profile_ref, rule.profile_version))
        provider = await session.get(IdentityProviderTable, profile.provider_ref) if profile is not None else None
        if provider is None:
            raise AdministrationError("assurance_profile_not_admitted")
        trust_refs.add(provider.trust_ref)
    for trust_ref in sorted(trust_refs):
        row = await session.scalar(
            select(TrustStateTable).where(TrustStateTable.trust_ref == trust_ref).with_for_update(read=True)
        )
        if row is None:
            raise AdministrationError("assurance_profile_not_admitted")


async def _validate_rules(
    session: AsyncSession,
    request: ConfigureHumanAuthenticationRequest,
    now: datetime,
) -> None:
    for rule in request.policy.rules:
        profile = await session.get(
            AuthenticationProfileTable, (rule.profile_ref, rule.profile_version), populate_existing=True
        )
        if (
            profile is None
            or profile.status != "active"
            or profile.credential_kind != "human_oidc"
            or profile.config_schema not in {PROFILE_SCHEMA, NATIVE_PROFILE_SCHEMA}
            or content_digest(profile_content(profile)) != profile.digest
        ):
            raise AdministrationError("assurance_profile_not_admitted")
        provider = await session.get(IdentityProviderTable, profile.provider_ref, populate_existing=True)
        trust = (
            await session.get(TrustStateTable, provider.trust_ref, populate_existing=True)
            if provider is not None
            else None
        )
        if provider is None or provider.status != "active" or trust is None or trust.purpose != "authentication":
            raise AdministrationError("assurance_profile_not_admitted")
        bindings = (
            await session.scalars(
                select(OrganizationAuthenticationBindingTable)
                .where(
                    OrganizationAuthenticationBindingTable.organization_ref == request.organization_ref,
                    OrganizationAuthenticationBindingTable.profile_ref == rule.profile_ref,
                    OrganizationAuthenticationBindingTable.profile_version == rule.profile_version,
                    OrganizationAuthenticationBindingTable.environment == request.environment,
                    OrganizationAuthenticationBindingTable.status == "active",
                )
                .execution_options(populate_existing=True)
            )
        ).all()
        if len(bindings) != 1 or not current(bindings[0], now):
            raise AdministrationError("assurance_profile_not_admitted")
        try:
            profile_config = ProfileConfiguration.model_validate(profile.config_json)
            constraints = AdmissionConstraints.model_validate(bindings[0].constraints_json)
        except ValidationError:
            raise AdministrationError("assurance_profile_not_admitted") from None
        if not set(profile_config.allowed_clients).intersection(constraints.allowed_clients):
            raise AdministrationError("assurance_profile_not_admitted")
        if constraints.required_acr is not None and constraints.required_acr != rule.required_acr:
            raise AdministrationError("assurance_binding_acr_conflict")


def _snapshot(row: OrganizationHumanAuthenticationPolicyTable | None) -> dict[str, Any]:
    if row is None:
        return {"version": 0, "mode": "disabled", "rules": [], "max_auth_age_seconds": None}
    return {
        "version": row.version,
        "mode": row.mode,
        "rules": row.rules_json,
        "max_auth_age_seconds": row.max_auth_age_seconds,
    }


async def configure_human_authentication(
    sessions: async_sessionmaker[AsyncSession],
    request: ConfigureHumanAuthenticationRequest,
    *,
    actor_ref: str,
    idempotency_key: str,
) -> AdministrationResult:
    """Versioned update + organization epoch + recoverable journal in one transaction.

    The caller is the explicit local OS operator, never a bearer-token holder.
    Existing weak profiles may remain installed but cannot satisfy a required
    policy unless the exact profile/version appears in the new assurance rules.
    """
    validate_operator(actor_ref, idempotency_key)
    try:
        reference = TypeAdapter(AssuranceReference)
        reference.validate_python(actor_ref)
        reference.validate_python(idempotency_key)
    except ValidationError:
        raise AdministrationError("invalid_operation_identity") from None
    digest = content_digest(request.model_dump(mode="json"))
    try:
        async with operation_transaction(sessions, OPERATION_TYPE, idempotency_key) as session:
            await acquire_admin_lock(session)
            await check_database_environment(session, request.environment)
            installed = await session.scalar(
                select(BootstrapRecordTable).where(
                    BootstrapRecordTable.environment == request.environment,
                    BootstrapRecordTable.bootstrap_type == BOOTSTRAP_TYPE,
                )
            )
            if installed is None:
                raise AdministrationError("identity_not_initialized")
            organization_ref = await session.scalar(select(OrganizationRootTable.organization_ref))
            if organization_ref != request.organization_ref:
                raise AdministrationError("identity_database_organization_mismatch")
            replay = await find_replay(session, actor_ref, OPERATION_TYPE, idempotency_key, digest)
            if replay is not None:
                return replay
            await _lock_rule_trusts(session, request.policy.rules)
            if await session.get(TenantGuardTable, request.organization_ref) is None:
                raise AdministrationError("identity_not_initialized")
            await lock_tenant(session, request.organization_ref)
            organization = await session.get(OrganizationRootTable, request.organization_ref, populate_existing=True)
            if organization is None or organization.status != "active":
                raise AdministrationError("organization_not_active")
            row = await session.get(
                OrganizationHumanAuthenticationPolicyTable, request.organization_ref, populate_existing=True
            )
            previous = _snapshot(row)
            if request.expected_version != previous["version"]:
                raise AdministrationError("version_conflict")
            if request.expected_version == 2**63 - 1:
                raise AdministrationError("version_exhausted")
            await _validate_rules(session, request, datetime.now(UTC))
            if row is None:
                row = OrganizationHumanAuthenticationPolicyTable(organization_ref=request.organization_ref)
                session.add(row)
            row.mode = request.policy.mode
            row.rules_json = [rule.model_dump(mode="json") for rule in request.policy.rules]
            row.max_auth_age_seconds = request.policy.max_auth_age_seconds
            row.version = request.expected_version + 1
            guard = await session.get(TenantGuardTable, request.organization_ref, populate_existing=True)
            assert guard is not None
            guard.epoch += 1
            after = _snapshot(row)
            return append_journal(
                session,
                actor_ref=actor_ref,
                operation_type=OPERATION_TYPE,
                idempotency_key=idempotency_key,
                digest=digest,
                environment=request.environment,
                target_ref=request.organization_ref,
                result={
                    "organization_ref": request.organization_ref,
                    "organization_authz_epoch": guard.epoch,
                    **after,
                },
                before=previous,
                after=after,
            )
    except IntegrityError:
        raise AdministrationError("assurance_configuration_conflict") from None
