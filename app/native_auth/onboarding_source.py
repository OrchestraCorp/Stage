"""Pin the existing native authentication source for an employee activation.

Callers own the transaction and hold the native trust and organization barriers.
This boundary reads current deployment facts; it never installs a provider,
constructs a credential, or turns an activation proof into a login session.
"""

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.assurance import HumanAssurancePolicy
from app.identity.native import NATIVE_ACR, NATIVE_PROFILE_SCHEMA
from app.identity.service import AdmissionConstraints, ProfileConfiguration, content_digest, current, profile_content
from app.models.identity_foundation import (
    AuthenticationProfileTable,
    IdentityProviderTable,
    OrganizationAuthenticationBindingTable,
    OrganizationHumanAuthenticationPolicyTable,
    OrganizationRootTable,
    TrustStateTable,
)
from app.models.native_auth import NativeClientTable
from app.native_auth.config import NativeAuthConfig
from app.native_auth.errors import NativeCredentialError
from app.storage.coordination import assert_organization_lock

NATIVE_ONBOARDING_PROFILE = "stage-native-human"
NATIVE_ONBOARDING_PROVIDER = "provider:native-auth"
NATIVE_ONBOARDING_BINDING = "binding:native-auth"
NATIVE_ONBOARDING_NAMESPACE = "stage-native-human-v1"


class NativeAuthenticationDenied(NativeCredentialError):
    """The deployment no longer admits this activation's exact native source."""

    def __init__(self) -> None:
        super().__init__("activation_unavailable")


async def load_onboarding_source(
    session: AsyncSession, config: NativeAuthConfig, expected: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return a JSON-safe source pin, rejecting changed or unavailable authority.

    A new issuance may explicitly pin the current configuration. Every subsequent
    ceremony step supplies the stored pin, so even compatible version changes
    require a new issuance instead of silently changing an employee's authority.
    ``expires_at`` is the binding deadline; callers also check it immediately
    before committing their own state transition.
    """
    assert_organization_lock(session, config.organization_ref)
    root = await session.get(OrganizationRootTable, config.organization_ref, populate_existing=True)
    trust = await session.get(TrustStateTable, config.trust_ref, populate_existing=True)
    provider = await session.get(IdentityProviderTable, NATIVE_ONBOARDING_PROVIDER, populate_existing=True)
    profile = await session.get(AuthenticationProfileTable, (NATIVE_ONBOARDING_PROFILE, 1), populate_existing=True)
    binding = await session.get(
        OrganizationAuthenticationBindingTable,
        (config.organization_ref, NATIVE_ONBOARDING_BINDING),
        populate_existing=True,
    )
    policy_row = await session.get(
        OrganizationHumanAuthenticationPolicyTable, config.organization_ref, populate_existing=True
    )
    now = datetime.now(UTC)
    if (
        root is None
        or root.deployment_slot != 1
        or not current(root, now)
        or trust is None
        or trust.purpose != "authentication"
        or trust.configuration_digest != content_digest({"issuer": config.issuer, "purpose": "native-authentication"})
        or provider is None
        or not current(provider, now)
        or provider.issuer_exact != config.issuer
        or provider.trust_ref != config.trust_ref
        or profile is None
        or not current(profile, now)
        or profile.provider_ref != provider.provider_ref
        or profile.config_schema != NATIVE_PROFILE_SCHEMA
        or profile.credential_kind != "human_oidc"
        or profile.stage_audience != config.audience
        or profile.subject_namespace != NATIVE_ONBOARDING_NAMESPACE
        or profile.digest != content_digest(profile_content(profile))
        or binding is None
        or not current(binding, now)
        or binding.profile_ref != profile.profile_ref
        or binding.profile_version != profile.profile_version
        or binding.environment != config.environment
        or policy_row is None
    ):
        raise NativeAuthenticationDenied()
    try:
        profile_config = ProfileConfiguration.model_validate(profile.config_json)
        constraints = AdmissionConstraints.model_validate(binding.constraints_json)
        policy = HumanAssurancePolicy.model_validate(
            {
                "mode": policy_row.mode,
                "rules": policy_row.rules_json,
                "max_auth_age_seconds": policy_row.max_auth_age_seconds,
            }
        )
    except ValidationError:
        raise NativeAuthenticationDenied() from None
    rule = next(
        (
            candidate
            for candidate in policy.rules
            if (candidate.profile_ref, candidate.profile_version) == (profile.profile_ref, profile.profile_version)
        ),
        None,
    )
    admitted_clients = set(profile_config.allowed_clients).intersection(constraints.allowed_clients)
    if (
        profile_config.jwks_url != config.issuer + "/oauth/jwks"
        or constraints.required_acr not in {None, NATIVE_ACR}
        or not admitted_clients
        or policy.mode != "required"
        or rule is None
        or rule.required_acr != NATIVE_ACR
        or set(rule.required_amr) != {"pwd", "otp"}
    ):
        raise NativeAuthenticationDenied()
    clients = (
        await session.scalars(
            select(NativeClientTable)
            .where(NativeClientTable.client_id.in_(admitted_clients))
            .execution_options(populate_existing=True)
        )
    ).all()
    eligible_clients = sorted(
        (client.client_id, client.version)
        for client in clients
        if client.enabled and client.resource_audience == config.audience and "openid" in client.allowed_scopes
    )
    if not eligible_clients:
        raise NativeAuthenticationDenied()
    binding_content = {
        "profile_ref": binding.profile_ref,
        "profile_version": binding.profile_version,
        "environment": binding.environment,
        "constraints": constraints.model_dump(mode="json"),
        "valid_from": binding.valid_from.isoformat() if binding.valid_from is not None else None,
        "expires_at": binding.expires_at.isoformat() if binding.expires_at is not None else None,
    }
    pin: dict[str, Any] = {
        "organization_ref": config.organization_ref,
        "organization_version": root.version,
        "environment": config.environment,
        "issuer": config.issuer,
        "trust_ref": trust.trust_ref,
        "trust_version": trust.version,
        "trust_config_revision": trust.trust_config_revision,
        "key_status_revision": trust.key_status_revision,
        "trust_digest": trust.configuration_digest,
        "provider_ref": provider.provider_ref,
        "provider_version": provider.version,
        "profile_ref": profile.profile_ref,
        "profile_version": profile.profile_version,
        "profile_row_version": profile.version,
        "profile_digest": profile.digest,
        "subject_namespace": profile.subject_namespace,
        "binding_ref": binding.binding_ref,
        "binding_version": binding.version,
        "binding_digest": content_digest(binding_content),
        "assurance_policy_version": policy_row.version,
        "assurance_policy_digest": content_digest(policy.model_dump(mode="json")),
        "admitted_clients": [{"client_id": reference, "version": version} for reference, version in eligible_clients],
        "expires_at": binding_content["expires_at"],
    }
    if expected is not None and content_digest(expected) != content_digest(pin):
        raise NativeAuthenticationDenied()
    if not current(binding, datetime.now(UTC)):
        raise NativeAuthenticationDenied()
    return pin
