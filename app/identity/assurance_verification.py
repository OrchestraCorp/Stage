"""Apply the organization-wide policy to every authenticated human entry point."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.assurance import HumanAssurancePolicy
from app.identity.authentication import (
    AuthenticationAssuranceRequired,
    AuthenticationRejected,
    AuthenticationUnavailable,
    VerifiedCredential,
)
from app.models.identity_foundation import OrganizationHumanAuthenticationPolicyTable


@dataclass(frozen=True)
class CurrentHumanAssurance:
    policy_version: int
    policy_digest: str
    valid_until: datetime | None


async def verify_human_assurance(
    session: AsyncSession,
    credential: VerifiedCredential,
    organization_ref: str,
    now: datetime,
    *,
    expected_version: int | None = None,
    expected_digest: str | None = None,
) -> CurrentHumanAssurance:
    """Caller holds the organization guard; no network, commit or cached allow.

    An absent policy is the explicit pre-configuration compatibility state,
    disabled/version 0. Query failure or malformed stored policy never falls
    back to this state. Required mode admits exact profile versions only.
    """
    if credential.subject_kind != "human" or not session.in_transaction():
        raise AuthenticationRejected()
    row = await session.get(OrganizationHumanAuthenticationPolicyTable, organization_ref, populate_existing=True)
    version = row.version if row is not None else 0
    try:
        if row is not None and (type(version) is not int or version < 1):
            raise ValueError("invalid_assurance_version")
        policy = HumanAssurancePolicy.model_validate(
            {"mode": row.mode, "rules": row.rules_json, "max_auth_age_seconds": row.max_auth_age_seconds}
            if row is not None
            else {"mode": "disabled"}
        )
    except (ValidationError, ValueError):
        raise AuthenticationUnavailable("authentication_policy_unavailable") from None
    digest = hashlib.sha256(
        json.dumps(policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if expected_version is not None and (version != expected_version or digest != expected_digest):
        raise AuthenticationRejected("authentication_policy_changed")
    deadline = None
    if policy.mode == "required":
        rule = next(
            (
                rule
                for rule in policy.rules
                if (rule.profile_ref, rule.profile_version) == (credential.profile_ref, credential.profile_version)
            ),
            None,
        )
        if (
            rule is None
            or credential.acr != rule.required_acr
            or not set(rule.required_amr).issubset(credential.amr)
            or credential.authenticated_at is None
        ):
            raise AuthenticationAssuranceRequired()
        assert policy.max_auth_age_seconds is not None
        deadline = credential.authenticated_at + timedelta(seconds=policy.max_auth_age_seconds)
        if now >= deadline:
            raise AuthenticationAssuranceRequired()
    return CurrentHumanAssurance(version, digest, deadline)
