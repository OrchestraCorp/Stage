"""Compute a fixed authentication deadline while the organization guard is held."""

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity_foundation import OrganizationHumanAuthenticationPolicyTable


async def current_authentication_deadline(
    session: AsyncSession,
    organization_ref: str,
    authenticated_at: datetime,
    *,
    default_seconds: int,
    max_age_seconds: int | None = None,
) -> datetime:
    """Combine the operation, RP and current organization limits without refreshing MFA.

    Callers hold the organization guard until their final read or commit. They
    compare this deadline with the clock again after every final database wait;
    this function does not recursively acquire locks or extend authentication.
    Invalid required policy data fails closed by returning an already-due limit.
    """
    limit = default_seconds
    if max_age_seconds is not None:
        if type(max_age_seconds) is not int or max_age_seconds <= 0:
            return authenticated_at
        limit = min(limit, max_age_seconds)
    policy = await session.get(OrganizationHumanAuthenticationPolicyTable, organization_ref, populate_existing=True)
    if policy is not None and policy.mode == "required":
        if type(policy.max_auth_age_seconds) is not int or not 1 <= policy.max_auth_age_seconds <= 86400:
            return authenticated_at
        limit = min(limit, policy.max_auth_age_seconds)
    return authenticated_at + timedelta(seconds=limit)
