"""Server-minted, short-lived human evidence; never a request or persistence DTO."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.identity.authentication import VerifiedCredential


@dataclass(frozen=True, slots=True)
class HumanAuthenticationEvidence:
    """Verified identity facts that must be revalidated in the commit transaction.

    The credential contains verified claims, never the bearer token. This object
    is an internal capability of the trusted authentication path, not a signed
    portable authorization grant and not a substitute for current permission.
    """

    credential: VerifiedCredential = field(repr=False)
    trust_ref: str
    provider_ref: str
    profile_fingerprint: str
    principal_ref: str
    principal_version: int
    identity_epoch: int
    recovery_epoch: int
    organization_ref: str
    organization_version: int
    membership_ref: str
    membership_version: int
    external_identity_ref: str
    external_identity_version: int
    binding_ref: str
    binding_version: int
    environment: str
    valid_until: datetime
    assurance_policy_version: int = 0
    assurance_policy_digest: str = ""

    def audit_snapshot(self) -> dict[str, Any]:
        """Explicit audit allowlist excludes bearer tokens and external subjects."""
        return {
            "evidence_ref": self.credential.evidence_ref,
            "profile_ref": self.credential.profile_ref,
            "profile_version": self.credential.profile_version,
            "profile_fingerprint": self.profile_fingerprint,
            "provider_ref": self.provider_ref,
            "trust_ref": self.trust_ref,
            "principal_ref": self.principal_ref,
            "principal_type": "human",
            "principal_version": self.principal_version,
            "identity_epoch": self.identity_epoch,
            "recovery_epoch": self.recovery_epoch,
            "organization_ref": self.organization_ref,
            "organization_version": self.organization_version,
            "membership_ref": self.membership_ref,
            "membership_version": self.membership_version,
            "external_identity_ref": self.external_identity_ref,
            "external_identity_version": self.external_identity_version,
            "binding_ref": self.binding_ref,
            "binding_version": self.binding_version,
            "environment": self.environment,
            "validated_at": self.credential.validated_at.isoformat(),
            "credential_expires_at": self.credential.credential_expires_at.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "assurance_policy_version": self.assurance_policy_version,
            "assurance_policy_digest": self.assurance_policy_digest,
            "authenticated_at": self.credential.authenticated_at.isoformat()
            if self.credential.authenticated_at is not None
            else None,
            "acr": self.credential.acr,
            "amr": list(self.credential.amr),
        }
