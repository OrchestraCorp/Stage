"""Employee onboarding through controlled installs and genuinely verified manager evidence."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.administration.capabilities import UpgradeManagementCapabilitiesPlan, upgrade_management_capabilities
from app.administration.onboarding import EmployeeAdministration
from app.administration.onboarding_contracts import EmployeeCreateRequest, EmployeeDelivery
from app.identity.assurance_administration import ConfigureHumanAuthenticationRequest, configure_human_authentication
from app.models.identity_foundation import OrganizationHumanAuthenticationPolicyTable
from app.native_auth.activation import NativeActivation
from app.native_auth.administration import InstallPlan, install
from app.native_auth.config import NativeAuthConfig
from app.native_auth.credentials import NativeCredentials
from tests.support.authorization import ORG, PERSON
from tests.support.human_administration import AdministrationCase, administration_case

PASSWORD = "Synthetic-employee-password-2026"


@dataclass
class EmployeeCase:
    manager: AdministrationCase
    service: EmployeeAdministration
    activation: NativeActivation
    config: NativeAuthConfig
    encryption_key: bytes

    async def create(self, *, username: str = "new.employee", key: str = "employee-create") -> EmployeeDelivery:
        return await self.service.create(
            self.manager.evidence,
            assignment_ref=self.manager.assignment_ref,
            request=EmployeeCreateRequest(
                username=username,
                expected_authority_version=self.manager.authority_version,
                reason="Verified new employee onboarding",
            ),
            request_id="request:employee",
            idempotency_key=key,
        )


@asynccontextmanager
async def employee_case(
    sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    *,
    upgrade: bool = True,
    issuer: str = "http://127.0.0.1:8080",
) -> AsyncIterator[EmployeeCase]:
    async with administration_case(sessions, signing_key) as manager:
        encryption_key = Fernet.generate_key()
        config = NativeAuthConfig(issuer=issuer, organization_ref=ORG, environment="test")
        await install(
            sessions,
            InstallPlan.model_validate(
                {
                    "environment": "test",
                    "organization_ref": ORG,
                    "issuer": issuer,
                    "client_id": "maestro-native",
                    "redirect_uris": ["http://127.0.0.1:49152/callback"],
                    "account_ref": "native:manager",
                    "principal_ref": PERSON,
                    "membership_ref": "membership:alice",
                    "external_identity_ref": "native:manager-identity",
                    "username": "manager",
                    "password": "Synthetic-manager-password-2026",
                }
            ),
            actor_ref="operator:employee-test",
            idempotency_key="native-employee-install",
            credential_digest_key=encryption_key,
        )
        async with sessions() as session:
            policy = await session.get(OrganizationHumanAuthenticationPolicyTable, ORG)
            assert policy is not None
            expected_version = policy.version
            rules = [
                *policy.rules_json,
                {
                    "profile_ref": "stage-native-human",
                    "profile_version": 1,
                    "required_acr": "urn:orchestra:acr:pwd-totp",
                    "required_amr": ["pwd", "otp"],
                },
            ]
        await configure_human_authentication(
            sessions,
            ConfigureHumanAuthenticationRequest.model_validate(
                {
                    "environment": "test",
                    "organization_ref": ORG,
                    "expected_version": expected_version,
                    "policy": {"mode": "required", "max_auth_age_seconds": 900, "rules": rules},
                }
            ),
            actor_ref="operator:employee-test",
            idempotency_key="employee-native-mfa",
        )
        manager.evidence = await manager.authenticate()
        if upgrade:
            me = await manager.service.me(manager.evidence)
            assert me.authority is not None
            await upgrade_management_capabilities(
                sessions,
                UpgradeManagementCapabilitiesPlan(
                    environment="test",
                    organization_ref=ORG,
                    authority_ref=me.authority.authority_ref,
                    assignment_ref=manager.assignment_ref,
                    expected_authority_version=manager.authority_version,
                    expected_assignment_version=me.authority.assignment_version,
                    target_role_revision=2,
                    reason="Explicit employee onboarding permission",
                ),
                actor_ref="operator:employee-test",
                idempotency_key="enable-employee-onboarding",
            )
            current = await manager.service.me(manager.evidence)
            assert current.authority is not None
            manager.authority_version = current.authority.version
        credentials = NativeCredentials(
            sessions, fernet_key=encryption_key, organization_ref=ORG, trust_ref=config.trust_ref
        )
        activation = NativeActivation(sessions, config, credentials=credentials)
        yield EmployeeCase(
            manager, EmployeeAdministration(sessions, manager.identity, config), activation, config, encryption_key
        )
