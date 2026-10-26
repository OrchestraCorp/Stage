"""Native access-token authentication; persistent sessions are revalidated under identity locks."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.authentication import (
    AccessTokenProfile,
    AuthenticationRejected,
    VerifiedCredential,
    _bounded_string,
    _numeric_date,
)
from app.native_auth.current_session import validate_native_session
from app.native_auth.keys import KeyRing, NativeKeyError
from app.native_auth.protocol import OAuthError, normalize_scope

NATIVE_PROFILE_SCHEMA = "stage.native-access.v1"
NATIVE_ACR = "urn:orchestra:acr:pwd-totp"


class NativeAuthenticator:
    def __init__(self, profile: AccessTokenProfile, keyring: KeyRing) -> None:
        self.profile = profile
        self.keyring = keyring

    async def authenticate(self, access_token: str) -> VerifiedCredential:
        try:
            claims = self.keyring.verify_access(access_token, self.profile.issuer, self.profile.audience)
            now = datetime.now(UTC)
            subject = _bounded_string(claims["sub"], 128)
            session_ref = _bounded_string(claims["sid"], 128)
            client = _bounded_string(claims["client_id"], 128)
            scope = normalize_scope(claims.get("scope"))
            expires, issued, authenticated = (_numeric_date(claims[key]) for key in ("exp", "iat", "auth_time"))
            if (
                claims["aud"] != self.profile.audience
                or client not in self.profile.allowed_clients
                or claims["acr"] != NATIVE_ACR
                or claims["amr"] != ["pwd", "otp"]
                or scope != claims.get("scope")
                or type(claims["credential_version"]) is not int
                or claims["credential_version"] < 1
                or expires <= now
                or expires <= issued
                or authenticated > issued
                or issued > now
            ):
                raise ValueError("invalid_native_claims")
            return VerifiedCredential(
                evidence_ref=str(uuid4()),
                profile_ref=self.profile.profile_ref,
                profile_version=self.profile.profile_version,
                credential_kind="human_oidc",
                subject_namespace=self.profile.subject_namespace,
                environment=self.profile.environment,
                issuer=self.profile.issuer,
                subject=subject,
                subject_kind="human",
                client_ref=client,
                audiences=(self.profile.audience,),
                scopes=tuple(scope.split()),
                validated_at=now,
                credential_expires_at=expires,
                evidence_valid_until=min(expires, now + timedelta(seconds=self.profile.evidence_ttl_seconds)),
                authenticated_at=authenticated,
                acr=NATIVE_ACR,
                amr=("pwd", "otp"),
                key_id=jwt.get_unverified_header(access_token)["kid"],
                native_session_ref=session_ref,
                native_credential_version=claims["credential_version"],
                native_issued_at=issued,
            )
        except (NativeKeyError, OAuthError, KeyError, ValueError, TypeError, OverflowError, OSError):
            raise AuthenticationRejected("invalid_native_access_token") from None


async def revalidate_native_credential(
    session: AsyncSession, credential: VerifiedCredential, principal_ref: str, now: datetime
) -> None:
    if (
        credential.native_session_ref is None
        or credential.authenticated_at is None
        or credential.native_issued_at is None
    ):
        raise AuthenticationRejected("native_session_required")
    claims: dict[str, Any] = {
        "sub": credential.subject,
        "sid": credential.native_session_ref,
        "client_id": credential.client_ref,
        "credential_version": credential.native_credential_version,
        "auth_time": int(credential.authenticated_at.timestamp()),
        "iat": int(credential.native_issued_at.timestamp()),
        "exp": int(credential.credential_expires_at.timestamp()),
        "acr": credential.acr,
        "amr": list(credential.amr),
        "scope": " ".join(credential.scopes),
        "aud": credential.audiences[0],
    }
    try:
        account, _ = await validate_native_session(session, claims, now=now)
        if account.principal_ref != principal_ref:
            raise AuthenticationRejected("native_subject_mismatch")
    except OAuthError:
        raise AuthenticationRejected("native_session_inactive") from None
