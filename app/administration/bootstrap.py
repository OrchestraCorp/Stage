"""OS-operator nomination of an existing human; acceptance belongs to that human."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, Self
from uuid import uuid4

from pydantic import Field, TypeAdapter, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.administration.errors import AdminDenied
from app.authz.context import validity_failure
from app.authz.loader import role_permissions_digest
from app.authz.management import (
    MANAGEMENT_ACTIONS,
    MANAGEMENT_POLICY_RELEASE,
    MANAGEMENT_ROLE_CODE,
    MANAGEMENT_ROLE_REF,
    MANAGEMENT_SCOPE_REF,
    GrantRule,
    ManagementReference,
    parse_grant_rules,
    validate_grant_rule,
)
from app.control.contracts import AdministrationError, AdministrationResult, StrictPlan
from app.control.operations import (
    acquire_admin_lock,
    append_journal,
    check_database_environment,
    find_replay,
    operation_transaction,
    validate_operator,
)
from app.identity.assurance import HumanAssurancePolicy
from app.identity.native import NATIVE_PROFILE_SCHEMA
from app.identity.service import (
    PROFILE_SCHEMA,
    AdmissionConstraints,
    ProfileConfiguration,
    content_digest,
    current,
    profile_content,
)
from app.models.administration import ManagementAuthorityTable
from app.models.authorization import (
    AuthorizationScopeTable,
    PermissionActionTable,
    RoleAssignmentTable,
    RoleDefinitionTable,
    RoleVersionActionTable,
    RoleVersionTable,
)
from app.models.execution import TenantGuardTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import (
    AuthenticationProfileTable,
    ExternalIdentityTable,
    IdentityProviderTable,
    OrganizationAuthenticationBindingTable,
    OrganizationHumanAuthenticationPolicyTable,
    OrganizationMembershipTable,
    OrganizationRootTable,
    TrustStateTable,
)
from app.storage.coordination import LockMode, lock_organization

NOMINATE_OPERATION = "nominate.first-administrator"
_MAX_VERSION = 2**31 - 1


class NominationPlan(StrictPlan):
    environment: Literal["dev", "test", "prod"]
    organization_ref: ManagementReference
    principal_ref: ManagementReference
    membership_ref: ManagementReference
    profile_ref: ManagementReference
    profile_version: int = Field(strict=True, ge=1, le=_MAX_VERSION)
    external_identity_ref: ManagementReference
    expected_version: int = Field(strict=True, ge=0, le=_MAX_VERSION)
    expires_at: datetime
    reason: str = Field(min_length=1, max_length=500)
    grant_rules: tuple[GrantRule, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def coherent_nomination(self) -> Self:
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("nomination_expiry_timezone_required")
        if not self.reason.strip() or any(
            ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in self.reason
        ):
            raise ValueError("nomination_reason_required")
        if len({(r.role_ref, r.role_revision, r.scope_ref) for r in self.grant_rules}) != len(self.grant_rules):
            raise ValueError("duplicate_grant_rule")
        return self


def authority_snapshot(authority: ManagementAuthorityTable) -> dict[str, object]:
    return {
        "organization_ref": authority.organization_ref,
        "authority_ref": authority.authority_ref,
        "principal_ref": authority.principal_ref,
        "membership_ref": authority.membership_ref,
        "assignment_ref": authority.assignment_ref,
        "profile_ref": authority.profile_ref,
        "profile_version": authority.profile_version,
        "external_identity_ref": authority.external_identity_ref,
        "grant_rules": authority.grant_rules_json,
        "nomination_expires_at": authority.nomination_expires_at.isoformat(),
        "accepted_at": authority.accepted_at.isoformat() if authority.accepted_at is not None else None,
        "status": authority.status,
        "version": authority.version,
    }


async def _lock_candidate(session: AsyncSession, plan: NominationPlan) -> None:
    profile = await session.get(AuthenticationProfileTable, (plan.profile_ref, plan.profile_version))
    provider = await session.get(IdentityProviderTable, profile.provider_ref) if profile is not None else None
    if provider is None:
        raise AdministrationError("nominee_authentication_not_admitted")
    trust = await session.scalar(
        select(TrustStateTable).where(TrustStateTable.trust_ref == provider.trust_ref).with_for_update(read=True)
    )
    principal = await session.scalar(
        select(PrincipalTable)
        .where(PrincipalTable.principal_ref == plan.principal_ref)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if trust is None or trust.purpose != "authentication" or principal is None:
        raise AdministrationError("nominee_authentication_not_admitted")
    roots = (await session.scalars(select(OrganizationRootTable))).all()
    if len(roots) != 1 or roots[0].organization_ref != plan.organization_ref:
        raise AdministrationError("identity_database_organization_mismatch")
    await lock_organization(session, plan.organization_ref, mode=LockMode.EXCLUSIVE)


async def _validate_candidate(session: AsyncSession, plan: NominationPlan, now: datetime) -> datetime:
    root = await session.get(OrganizationRootTable, plan.organization_ref, populate_existing=True)
    principal = await session.scalar(
        select(PrincipalTable)
        .where(PrincipalTable.principal_ref == plan.principal_ref)
        .execution_options(populate_existing=True)
    )
    membership = await session.get(
        OrganizationMembershipTable, (plan.organization_ref, plan.membership_ref), populate_existing=True
    )
    if (
        root is None
        or root.status != "active"
        or principal is None
        or principal.status != "active"
        or principal.principal_type != "human"
        or principal.disabled_at is not None
        or membership is None
        or membership.principal_ref != plan.principal_ref
        or membership.principal_type != "human"
        or membership.ended_at is not None
        or membership.expires_at is not None
        or validity_failure(
            now, valid_from=membership.valid_from, expires_at=membership.expires_at, status=membership.status
        )
        is not None
    ):
        raise AdministrationError("current_nonexpiring_human_membership_required")
    profile = await session.get(
        AuthenticationProfileTable, (plan.profile_ref, plan.profile_version), populate_existing=True
    )
    provider = (
        await session.get(IdentityProviderTable, profile.provider_ref, populate_existing=True)
        if profile is not None
        else None
    )
    external = await session.get(ExternalIdentityTable, plan.external_identity_ref, populate_existing=True)
    if (
        profile is None
        or profile.status != "active"
        or profile.credential_kind != "human_oidc"
        or profile.config_schema not in {PROFILE_SCHEMA, NATIVE_PROFILE_SCHEMA}
        or profile.stage_audience != "stage-api"
        or profile.digest != content_digest(profile_content(profile))
        or provider is None
        or provider.status != "active"
        or external is None
        or external.status != "active"
        or external.principal_ref != plan.principal_ref
        or external.principal_type != "human"
        or external.provider_ref != provider.provider_ref
        or external.subject_namespace != profile.subject_namespace
    ):
        raise AdministrationError("nominee_authentication_not_admitted")
    bindings = (
        await session.scalars(
            select(OrganizationAuthenticationBindingTable)
            .where(
                OrganizationAuthenticationBindingTable.organization_ref == plan.organization_ref,
                OrganizationAuthenticationBindingTable.profile_ref == plan.profile_ref,
                OrganizationAuthenticationBindingTable.profile_version == plan.profile_version,
                OrganizationAuthenticationBindingTable.environment == plan.environment,
                OrganizationAuthenticationBindingTable.status == "active",
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    if len(bindings) != 1 or not current(bindings[0], now):
        raise AdministrationError("nominee_authentication_not_admitted")
    policy_row = await session.get(
        OrganizationHumanAuthenticationPolicyTable, plan.organization_ref, populate_existing=True
    )
    try:
        policy = (
            HumanAssurancePolicy.model_validate(
                {
                    "mode": policy_row.mode,
                    "rules": policy_row.rules_json,
                    "max_auth_age_seconds": policy_row.max_auth_age_seconds,
                }
            )
            if policy_row is not None
            else HumanAssurancePolicy()
        )
        profile_config = ProfileConfiguration.model_validate(profile.config_json)
        constraints = AdmissionConstraints.model_validate(bindings[0].constraints_json)
    except ValidationError:
        raise AdministrationError("required_management_mfa_not_configured") from None
    rule = next(
        (r for r in policy.rules if (r.profile_ref, r.profile_version) == (plan.profile_ref, plan.profile_version)),
        None,
    )
    if (
        policy.mode != "required"
        or rule is None
        or not {"pwd", "otp"}.issubset(rule.required_amr)
        or not set(profile_config.allowed_clients).intersection(constraints.allowed_clients)
        or constraints.required_acr is not None
        and constraints.required_acr != rule.required_acr
    ):
        raise AdministrationError("required_management_mfa_not_configured")
    return min(plan.expires_at, bindings[0].expires_at) if bindings[0].expires_at is not None else plan.expires_at


async def _install_management_role(session: AsyncSession, organization_ref: str) -> None:
    scope = await session.get(AuthorizationScopeTable, (organization_ref, MANAGEMENT_SCOPE_REF), populate_existing=True)
    if scope is None:
        session.add(
            AuthorizationScopeTable(
                organization_ref=organization_ref, scope_ref=MANAGEMENT_SCOPE_REF, scope_kind="organization"
            )
        )
    elif scope.status != "active" or scope.scope_kind != "organization" or scope.parent_scope_ref is not None:
        raise AdministrationError("management_scope_conflict")
    role = await session.get(RoleDefinitionTable, (organization_ref, MANAGEMENT_ROLE_REF), populate_existing=True)
    if role is None:
        session.add(
            RoleDefinitionTable(
                organization_ref=organization_ref,
                role_ref=MANAGEMENT_ROLE_REF,
                role_code=MANAGEMENT_ROLE_CODE,
                display_name="Stage administrator",
                template_source_ref=MANAGEMENT_POLICY_RELEASE,
            )
        )
    elif (
        role.status != "active"
        or role.role_code != MANAGEMENT_ROLE_CODE
        or role.template_source_ref != MANAGEMENT_POLICY_RELEASE
    ):
        raise AdministrationError("management_role_conflict")
    for action in sorted(MANAGEMENT_ACTIONS):
        registered = await session.get(PermissionActionTable, action, populate_existing=True)
        if registered is None:
            session.add(PermissionActionTable(action_ref=action, resource_kind="administration", contract_version="1"))
        elif (
            registered.status != "active"
            or registered.resource_kind != "administration"
            or registered.contract_version != "1"
        ):
            raise AdministrationError("management_action_conflict")
    await session.flush()
    revision = await session.get(RoleVersionTable, (organization_ref, MANAGEMENT_ROLE_REF, 1), populate_existing=True)
    digest = role_permissions_digest(
        organization_ref, MANAGEMENT_ROLE_REF, 1, MANAGEMENT_POLICY_RELEASE, MANAGEMENT_ACTIONS
    )
    if revision is None:
        session.add(
            RoleVersionTable(
                organization_ref=organization_ref,
                role_ref=MANAGEMENT_ROLE_REF,
                role_revision=1,
                policy_release_ref=MANAGEMENT_POLICY_RELEASE,
                content_digest=digest,
            )
        )
        await session.flush()
        session.add_all(
            RoleVersionActionTable(
                organization_ref=organization_ref, role_ref=MANAGEMENT_ROLE_REF, role_revision=1, action_ref=action
            )
            for action in sorted(MANAGEMENT_ACTIONS)
        )
    else:
        actions = tuple(
            await session.scalars(
                select(RoleVersionActionTable.action_ref)
                .where(
                    RoleVersionActionTable.organization_ref == organization_ref,
                    RoleVersionActionTable.role_ref == MANAGEMENT_ROLE_REF,
                    RoleVersionActionTable.role_revision == 1,
                )
                .limit(len(MANAGEMENT_ACTIONS) + 1)
            )
        )
        if (
            revision.status != "active"
            or revision.revoked_at is not None
            or revision.policy_release_ref != MANAGEMENT_POLICY_RELEASE
            or revision.content_digest != digest
            or set(actions) != MANAGEMENT_ACTIONS
            or len(actions) != len(MANAGEMENT_ACTIONS)
        ):
            raise AdministrationError("management_role_conflict")
    await session.flush()


async def nominate(
    sessions: async_sessionmaker[AsyncSession], plan: NominationPlan, *, actor_ref: str, idempotency_key: str
) -> AdministrationResult:
    validate_operator(actor_ref, idempotency_key)
    try:
        reference = TypeAdapter(ManagementReference)
        reference.validate_python(actor_ref)
        reference.validate_python(idempotency_key)
    except (ValidationError, ValueError, UnicodeError):
        raise AdministrationError("invalid_operation_identity") from None
    digest = content_digest(plan.model_dump(mode="json"))
    try:
        async with operation_transaction(sessions, NOMINATE_OPERATION, idempotency_key) as session:
            await acquire_admin_lock(session)
            # This operator boundary can target an already provisioned production
            # foundation; it never calls the legacy dev-only account installer.
            await check_database_environment(session, plan.environment)
            replay = await find_replay(session, actor_ref, NOMINATE_OPERATION, idempotency_key, digest)
            if replay is not None:
                return replay
            now = datetime.now(UTC)
            if not now < plan.expires_at <= now + timedelta(hours=1):
                raise AdministrationError("nomination_expiry_out_of_bounds")
            await _lock_candidate(session, plan)
            authority = await session.get(ManagementAuthorityTable, plan.organization_ref, populate_existing=True)
            if authority is not None and authority.accepted_at is not None:
                raise AdministrationError("first_administrator_already_accepted")
            if (authority is None and plan.expected_version != 0) or (
                authority is not None and authority.version != plan.expected_version
            ):
                raise AdministrationError("version_conflict")
            if authority is not None and (authority.status != "pending" or authority.version == _MAX_VERSION):
                raise AdministrationError("nomination_not_replaceable")
            deadline = await _validate_candidate(session, plan, datetime.now(UTC))
            try:
                parse_grant_rules(plan.grant_rules)
                for rule in plan.grant_rules:
                    await validate_grant_rule(session, plan.organization_ref, rule)
            except AdminDenied:
                raise AdministrationError("grant_rule_not_current_ordinary_authority") from None
            before = authority_snapshot(authority) if authority is not None else None
            if authority is not None:
                previous = await session.scalar(
                    select(RoleAssignmentTable)
                    .where(
                        RoleAssignmentTable.organization_ref == plan.organization_ref,
                        RoleAssignmentTable.assignment_ref == authority.assignment_ref,
                    )
                    .execution_options(populate_existing=True)
                )
                if previous is None or previous.status != "suspended" or previous.version == _MAX_VERSION:
                    raise AdministrationError("nomination_assignment_not_replaceable")
                previous.status, previous.revoked_at = "revoked", datetime.now(UTC)
                previous.version += 1
                await session.flush()
            await _install_management_role(session, plan.organization_ref)
            authority_ref = authority.authority_ref if authority is not None else "management:" + uuid4().hex
            assignment_ref = "assignment:management:" + uuid4().hex
            session.add(
                RoleAssignmentTable(
                    organization_ref=plan.organization_ref,
                    assignment_ref=assignment_ref,
                    principal_ref=plan.principal_ref,
                    principal_type="human",
                    membership_ref=plan.membership_ref,
                    role_ref=MANAGEMENT_ROLE_REF,
                    role_revision=1,
                    role_code=MANAGEMENT_ROLE_CODE,
                    scope_ref=MANAGEMENT_SCOPE_REF,
                    grant_source_ref=authority_ref,
                    valid_from=datetime.now(UTC),
                    status="suspended",
                    version=1,
                )
            )
            await session.flush()
            if authority is None:
                authority = ManagementAuthorityTable(
                    organization_ref=plan.organization_ref, authority_ref=authority_ref, version=1, status="pending"
                )
                session.add(authority)
            else:
                authority.version += 1
            authority.principal_ref, authority.membership_ref = plan.principal_ref, plan.membership_ref
            authority.assignment_ref, authority.profile_ref = assignment_ref, plan.profile_ref
            authority.profile_version, authority.external_identity_ref = (
                plan.profile_version,
                plan.external_identity_ref,
            )
            authority.grant_rules_json = [r.model_dump(mode="json") for r in plan.grant_rules]
            authority.nomination_expires_at = plan.expires_at
            guard = await session.get(TenantGuardTable, plan.organization_ref, populate_existing=True)
            if guard is None or guard.epoch == _MAX_VERSION:
                raise AdministrationError("organization_epoch_exhausted")
            guard.epoch += 1
            after = authority_snapshot(authority)
            result = append_journal(
                session,
                actor_ref=actor_ref,
                operation_type=NOMINATE_OPERATION,
                idempotency_key=idempotency_key,
                digest=digest,
                environment=plan.environment,
                target_ref=authority_ref,
                before=before,
                after={**after, "reason": plan.reason},
                result={**after, "organization_authz_epoch": guard.epoch},
            )
            await session.flush()
            if deadline <= datetime.now(UTC):
                raise AdministrationError("nomination_expired")
            return result
    except IntegrityError:
        raise AdministrationError("management_nomination_conflict") from None
