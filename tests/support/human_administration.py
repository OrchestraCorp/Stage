"""Real signed human authentication and bounded first-administrator fixtures."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.administration.bootstrap import NominationPlan, nominate
from app.administration.contracts import ActivationRequest, GrantRequest, OperationResult, RevokeRequest
from app.administration.service import AdministrationService
from app.authz.management import GrantRule
from app.identity.evidence import HumanAuthenticationEvidence
from app.identity.service import IdentityService
from app.models.authorization import RoleVersionTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import ExternalIdentityTable, OrganizationMembershipTable
from tests.support.authenticated_commands import identity_service, jwt_client, seed_authenticated_command
from tests.support.authorization import ORG, PERSON
from tests.support.identity_assurance import configure
from tests.support.identity_tokens import token

BOB = "person:bob"
BOB_MEMBERSHIP = "membership:bob"


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@dataclass
class AdministrationCase:
    sessions: async_sessionmaker[AsyncSession]
    identity: IdentityService
    service: AdministrationService
    signing_key: rsa.RSAPrivateKey
    evidence: HumanAuthenticationEvidence
    nomination: NominationPlan
    assignment_ref: str
    authority_version: int

    async def authenticate(self, *, subject: str = "subject-1", **claims: Any) -> HumanAuthenticationEvidence:
        bearer = token(
            self.signing_key,
            claims={
                "sub": subject,
                "auth_time": int(datetime.now(UTC).timestamp()),
                "acr": "2",
                "amr": ["pwd", "otp"],
                **claims,
            },
        )
        return await self.identity.authenticate_human(bearer, profile_ref="local-human", profile_version=1)

    def grant_request(self, **changes: Any) -> GrantRequest:
        return GrantRequest.model_validate(
            {
                "membership_ref": BOB_MEMBERSHIP,
                "expected_membership_version": 1,
                "expected_authority_version": self.authority_version,
                "role_ref": "role:engineering",
                "role_revision": 1,
                "scope_ref": "scope:engineering",
                "expires_at": datetime.now(UTC) + timedelta(days=1),
                "reason": "Assign bounded engineering responsibilities",
                **changes,
            }
        )

    async def activate(self, *, key: str = "accept-first-administrator") -> OperationResult:
        result = await self.service.activate(
            self.evidence,
            request=ActivationRequest(expected_authority_version=self.authority_version),
            request_id="request:activation",
            idempotency_key=key,
        )
        current = await self.service.me(self.evidence)
        assert current.authority is not None
        self.authority_version = current.authority.version
        return result

    async def grant(
        self, *, key: str = "grant-bob", principal_ref: str = BOB, request: GrantRequest | None = None
    ) -> OperationResult:
        return await self.service.grant(
            self.evidence,
            assignment_ref=self.assignment_ref,
            principal_ref=principal_ref,
            request=request or self.grant_request(),
            request_id="request:grant",
            idempotency_key=key,
        )

    async def revoke(
        self,
        target_assignment_ref: str,
        *,
        key: str = "revoke-bob",
        principal_ref: str = BOB,
        expected_version: int = 1,
    ) -> OperationResult:
        return await self.service.revoke(
            self.evidence,
            assignment_ref=self.assignment_ref,
            principal_ref=principal_ref,
            target_assignment_ref=target_assignment_ref,
            request=RevokeRequest(
                expected_assignment_version=expected_version,
                expected_authority_version=self.authority_version,
                reason="Responsibilities ended",
            ),
            request_id="request:revoke",
            idempotency_key=key,
        )


@asynccontextmanager
async def administration_case(
    sessions: async_sessionmaker[AsyncSession], signing_key: rsa.RSAPrivateKey, *, activate: bool = True
) -> AsyncIterator[AdministrationCase]:
    await seed_authenticated_command(sessions)
    await configure(sessions, max_auth_age_seconds=900)
    async with sessions.begin() as session:
        session.add(PrincipalTable(principal_ref=BOB, principal_type="human"))
        await session.flush()
        session.add(
            OrganizationMembershipTable(
                organization_ref=ORG,
                membership_ref=BOB_MEMBERSHIP,
                principal_ref=BOB,
                principal_type="human",
                source_ref="fixture:existing-person",
                valid_from=datetime.now(UTC) - timedelta(days=1),
            )
        )
        session.add(
            ExternalIdentityTable(
                external_identity_ref="identity:bob",
                provider_ref="provider",
                subject_namespace="keycloak-human-v1",
                subject_exact="subject-bob",
                principal_ref=BOB,
                principal_type="human",
                link_evidence_ref="fixture:existing-person",
            )
        )
        role = await session.get(RoleVersionTable, (ORG, "role:engineering", 1))
        assert role is not None
        rule = GrantRule(
            role_ref="role:engineering",
            role_revision=1,
            content_digest=role.content_digest,
            scope_ref="scope:engineering",
            max_duration_seconds=7 * 86400,
        )
    plan = NominationPlan(
        environment="test",
        organization_ref=ORG,
        principal_ref=PERSON,
        membership_ref="membership:alice",
        profile_ref="local-human",
        profile_version=1,
        external_identity_ref="identity",
        expected_version=0,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        reason="Explicit first-administrator installation",
        grant_rules=(rule,),
    )
    await nominate(sessions, plan, actor_ref="operator:administration-test", idempotency_key="nominate-alice")
    async with jwt_client(signing_key) as client:
        identity = identity_service(sessions, client)
        try:
            evidence = await identity.authenticate_human(
                token(
                    signing_key,
                    claims={"auth_time": int(datetime.now(UTC).timestamp()), "acr": "2", "amr": ["pwd", "otp"]},
                ),
                profile_ref="local-human",
                profile_version=1,
            )
            service = AdministrationService(sessions, identity)
            current = await service.me(evidence)
            assert current.authority is not None
            case = AdministrationCase(
                sessions,
                identity,
                service,
                signing_key,
                evidence,
                plan,
                current.authority.assignment_ref,
                current.authority.version,
            )
            if activate:
                await case.activate()
            yield case
        finally:
            await identity.aclose()
