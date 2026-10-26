"""Bounded administrator capabilities, independent of ordinary WorkItem policies."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.administration.errors import AdminDenied
from app.authz.context import validity_failure
from app.authz.loader import load_role_version, load_scope_chain, role_permissions_digest
from app.identity.assurance import HumanAssurancePolicy
from app.identity.assurance_verification import verify_human_assurance
from app.identity.authentication import AuthenticationAssuranceRequired, AuthenticationRejected
from app.identity.evidence import HumanAuthenticationEvidence
from app.models.administration import ManagementAuthorityTable
from app.models.authorization import (
    AuthorizationScopeTable,
    PermissionActionTable,
    RoleAssignmentTable,
    RoleDefinitionTable,
    RoleVersionActionTable,
    RoleVersionTable,
)
from app.models.identity import PrincipalTable
from app.models.identity_foundation import (
    ExternalIdentityTable,
    OrganizationHumanAuthenticationPolicyTable,
    OrganizationMembershipTable,
)
from app.storage.coordination import assert_organization_lock

MANAGEMENT_ROLE_REF = "role:stage-administrator"
MANAGEMENT_ROLE_CODE = "stage-administrator"
MANAGEMENT_SCOPE_REF = "scope:stage-administration"
MANAGEMENT_POLICY_RELEASE = "stage-administration:builtin-v1"
MANAGEMENT_ACTIONS = frozenset(
    {
        "administration.people.read",
        "administration.assignment.grant",
        "administration.assignment.revoke",
        "administration.audit.read",
    }
)


def _utf8(value: str) -> str:
    value.encode("utf-8")
    return value


ManagementReference = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[^\s\x00-\x1f\x7f]+$"), AfterValidator(_utf8)
]
ScopeReference = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\s\x00-\x1f\x7f]+$")]


class GrantRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role_ref: ManagementReference
    role_revision: int = Field(strict=True, ge=1, le=2**31 - 1)
    content_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope_ref: ScopeReference
    max_duration_seconds: int = Field(strict=True, ge=1, le=365 * 86400)

    @model_validator(mode="after")
    def ordinary_role_only(self) -> Self:
        self.scope_ref.encode("utf-8")
        if self.role_ref == MANAGEMENT_ROLE_REF:
            raise ValueError("ordinary_role_and_scope_required")
        return self


def parse_grant_rules(value: Any) -> tuple[GrantRule, ...]:
    try:
        rules = TypeAdapter(tuple[GrantRule, ...]).validate_python(value)
        if not 1 <= len(rules) <= 32 or len({(r.role_ref, r.role_revision, r.scope_ref) for r in rules}) != len(rules):
            raise ValueError("invalid_grant_rule_set")
        return rules
    except (ValidationError, ValueError, UnicodeError):
        raise AdminDenied() from None


async def validate_grant_rule(session: AsyncSession, organization_ref: str, rule: GrantRule) -> None:
    assert_organization_lock(session, organization_ref)
    role = await session.get(RoleDefinitionTable, (organization_ref, rule.role_ref), populate_existing=True)
    if role is None or role.role_ref == MANAGEMENT_ROLE_REF:
        raise AdminDenied()
    probe = RoleAssignmentTable(
        organization_ref=organization_ref,
        role_ref=rule.role_ref,
        role_revision=rule.role_revision,
        role_code=role.role_code,
    )
    version = await load_role_version(session, organization_ref, probe)
    if version is None or version.revision.content_digest != rule.content_digest:
        raise AdminDenied()
    if not await load_scope_chain(session, organization_ref, rule.scope_ref):
        raise AdminDenied()


async def require_management_assurance(
    session: AsyncSession, evidence: HumanAuthenticationEvidence, now: datetime
) -> datetime:
    assert_organization_lock(session, evidence.organization_ref)
    credential = evidence.credential
    if now.tzinfo is None or now.utcoffset() is None:
        raise AdminDenied()
    checked_at = max(now, datetime.now(UTC))
    if credential.subject_kind != "human" or credential.credential_kind != "human_oidc":
        raise AdminDenied()
    if (
        credential.authenticated_at is None
        or credential.authenticated_at > checked_at
        or not {"pwd", "otp"}.issubset(credential.amr)
    ):
        raise AuthenticationAssuranceRequired()
    row = await session.get(
        OrganizationHumanAuthenticationPolicyTable, evidence.organization_ref, populate_existing=True
    )
    try:
        policy = (
            HumanAssurancePolicy.model_validate(
                {
                    "mode": row.mode,
                    "rules": row.rules_json,
                    "max_auth_age_seconds": row.max_auth_age_seconds,
                }
            )
            if row is not None
            else HumanAssurancePolicy()
        )
    except ValidationError:
        raise AdminDenied() from None
    rule = next(
        (
            r
            for r in policy.rules
            if (r.profile_ref, r.profile_version) == (credential.profile_ref, credential.profile_version)
        ),
        None,
    )
    if policy.mode != "required" or rule is None or not {"pwd", "otp"}.issubset(rule.required_amr):
        raise AdminDenied()
    assurance = await verify_human_assurance(
        session,
        credential,
        evidence.organization_ref,
        checked_at,
        expected_version=evidence.assurance_policy_version,
        expected_digest=evidence.assurance_policy_digest,
    )
    assert assurance.valid_until is not None
    deadline = min(
        evidence.valid_until,
        credential.credential_expires_at,
        credential.evidence_valid_until,
        credential.authenticated_at + timedelta(seconds=300),
        assurance.valid_until,
    )
    current_time = max(now, datetime.now(UTC))
    if min(credential.authenticated_at + timedelta(seconds=300), assurance.valid_until) <= current_time:
        raise AuthenticationAssuranceRequired()
    if deadline <= current_time:
        raise AuthenticationRejected("identity_evidence_expired")
    return deadline


@dataclass(frozen=True)
class AuthorizedManager:
    authority: ManagementAuthorityTable
    assignment: RoleAssignmentTable
    valid_until: datetime


async def authorize_manager(
    session: AsyncSession, evidence: HumanAuthenticationEvidence, assignment_ref: str, action: str, now: datetime
) -> AuthorizedManager:
    assert_organization_lock(session, evidence.organization_ref)
    if action not in MANAGEMENT_ACTIONS:
        raise AdminDenied()
    deadline = await require_management_assurance(session, evidence, now)
    authority = await session.get(ManagementAuthorityTable, evidence.organization_ref, populate_existing=True)
    if (
        authority is None
        or authority.status != "active"
        or authority.accepted_at is None
        or authority.assignment_ref != assignment_ref
        or authority.principal_ref != evidence.principal_ref
        or authority.membership_ref != evidence.membership_ref
        or authority.external_identity_ref != evidence.external_identity_ref
        or authority.profile_ref != evidence.credential.profile_ref
        or authority.profile_version != evidence.credential.profile_version
    ):
        raise AdminDenied()
    parse_grant_rules(authority.grant_rules_json)
    assignment = await session.scalar(
        select(RoleAssignmentTable)
        .where(
            RoleAssignmentTable.organization_ref == evidence.organization_ref,
            RoleAssignmentTable.assignment_ref == assignment_ref,
        )
        .execution_options(populate_existing=True)
    )
    member = await session.get(
        OrganizationMembershipTable, (evidence.organization_ref, evidence.membership_ref), populate_existing=True
    )
    principal = await session.scalar(
        select(PrincipalTable)
        .where(PrincipalTable.principal_ref == evidence.principal_ref)
        .execution_options(populate_existing=True)
    )
    external = await session.get(ExternalIdentityTable, evidence.external_identity_ref, populate_existing=True)
    role = await session.get(
        RoleDefinitionTable, (evidence.organization_ref, MANAGEMENT_ROLE_REF), populate_existing=True
    )
    revision = await session.get(
        RoleVersionTable, (evidence.organization_ref, MANAGEMENT_ROLE_REF, 1), populate_existing=True
    )
    if (
        assignment is None
        or assignment.principal_ref != evidence.principal_ref
        or assignment.principal_type != "human"
        or assignment.membership_ref != evidence.membership_ref
        or assignment.role_ref != MANAGEMENT_ROLE_REF
        or assignment.role_code != MANAGEMENT_ROLE_CODE
        or assignment.role_revision != 1
        or assignment.scope_ref != MANAGEMENT_SCOPE_REF
        or assignment.grant_source_ref != authority.authority_ref
        or assignment.expires_at is not None
        or assignment.revoked_at is not None
        or assignment.valid_from is None
        or validity_failure(
            now, valid_from=assignment.valid_from, expires_at=assignment.expires_at, status=assignment.status
        )
        is not None
        or member is None
        or member.principal_ref != evidence.principal_ref
        or member.principal_type != "human"
        or member.ended_at is not None
        or member.expires_at is not None
        or validity_failure(now, valid_from=member.valid_from, expires_at=member.expires_at, status=member.status)
        is not None
        or principal is None
        or principal.status != "active"
        or principal.principal_type != "human"
        or principal.disabled_at is not None
        or external is None
        or external.status != "active"
        or external.principal_ref != evidence.principal_ref
        or external.principal_type != "human"
        or external.provider_ref != evidence.provider_ref
        or external.subject_namespace != evidence.credential.subject_namespace
        or external.subject_exact != evidence.credential.subject
        or role is None
        or role.status != "active"
        or role.role_code != MANAGEMENT_ROLE_CODE
        or revision is None
        or revision.status != "active"
        or revision.revoked_at is not None
        or revision.policy_release_ref != MANAGEMENT_POLICY_RELEASE
    ):
        raise AdminDenied()
    actions = tuple(
        await session.scalars(
            select(RoleVersionActionTable.action_ref)
            .where(
                RoleVersionActionTable.organization_ref == evidence.organization_ref,
                RoleVersionActionTable.role_ref == MANAGEMENT_ROLE_REF,
                RoleVersionActionTable.role_revision == 1,
            )
            .limit(len(MANAGEMENT_ACTIONS) + 1)
        )
    )
    registered = (
        await session.scalars(
            select(PermissionActionTable)
            .where(PermissionActionTable.action_ref.in_(actions))
            .execution_options(populate_existing=True)
        )
    ).all()
    scope = await session.get(
        AuthorizationScopeTable, (evidence.organization_ref, MANAGEMENT_SCOPE_REF), populate_existing=True
    )
    if (
        set(actions) != MANAGEMENT_ACTIONS
        or len(actions) != len(MANAGEMENT_ACTIONS)
        or len(registered) != len(MANAGEMENT_ACTIONS)
        or any(
            r.status != "active" or r.resource_kind != "administration" or r.contract_version != "1" for r in registered
        )
        or revision.content_digest
        != role_permissions_digest(
            evidence.organization_ref, MANAGEMENT_ROLE_REF, 1, MANAGEMENT_POLICY_RELEASE, actions
        )
        or scope is None
        or scope.status != "active"
        or scope.scope_kind != "organization"
        or scope.parent_scope_ref is not None
        or deadline <= max(now, datetime.now(UTC))
    ):
        raise AdminDenied()
    return AuthorizedManager(authority, assignment, deadline)
