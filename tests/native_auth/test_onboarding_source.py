"""Activation pins existing admission and never follows a replacement source."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.identity.native import NATIVE_ACR, NATIVE_PROFILE_SCHEMA
from app.identity.service import content_digest, profile_content
from app.models.execution import TenantGuardTable
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
from app.native_auth.onboarding_source import NativeAuthenticationDenied, load_onboarding_source
from app.storage.coordination import LockMode, lock_organization

CONFIG = NativeAuthConfig(issuer="http://127.0.0.1:8080", organization_ref="source-test", environment="test")


@pytest.fixture
async def source_sessions(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    async with persistence_sessions.begin() as session:
        session.add(OrganizationRootTable(organization_ref=CONFIG.organization_ref, display_name="Synthetic source"))
        session.add(
            TrustStateTable(
                trust_ref=CONFIG.trust_ref,
                purpose="authentication",
                configuration_digest=content_digest({"issuer": CONFIG.issuer, "purpose": "native-authentication"}),
            )
        )
        await session.flush()
        session.add(TenantGuardTable(tenant_ref=CONFIG.organization_ref))
        session.add(
            IdentityProviderTable(
                provider_ref="provider:native-auth", issuer_exact=CONFIG.issuer, trust_ref=CONFIG.trust_ref
            )
        )
        await session.flush()
        profile = AuthenticationProfileTable(
            profile_ref="stage-native-human",
            profile_version=1,
            provider_ref="provider:native-auth",
            subject_namespace="stage-native-human-v1",
            credential_kind="human_oidc",
            stage_audience="stage-api",
            config_schema=NATIVE_PROFILE_SCHEMA,
            config_json={"jwks_url": CONFIG.issuer + "/oauth/jwks", "allowed_clients": ["maestro"]},
        )
        profile.digest = content_digest(profile_content(profile))
        session.add(profile)
        await session.flush()
        session.add(
            OrganizationAuthenticationBindingTable(
                organization_ref=CONFIG.organization_ref,
                binding_ref="binding:native-auth",
                profile_ref=profile.profile_ref,
                profile_version=1,
                environment=CONFIG.environment,
                valid_from=datetime.now(UTC) - timedelta(minutes=5),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                constraints_json={"allowed_clients": ["maestro"], "required_acr": NATIVE_ACR},
            )
        )
        session.add(
            OrganizationHumanAuthenticationPolicyTable(
                organization_ref=CONFIG.organization_ref,
                mode="required",
                rules_json=[
                    {
                        "profile_ref": profile.profile_ref,
                        "profile_version": 1,
                        "required_acr": NATIVE_ACR,
                        "required_amr": ["pwd", "otp"],
                    }
                ],
                max_auth_age_seconds=900,
            )
        )
        session.add(NativeClientTable(client_id="maestro", redirect_uris=["http://127.0.0.1:49152/callback"]))
    return persistence_sessions


@asynccontextmanager
async def protected_source(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sessions.begin() as session:
        await session.scalar(
            select(TrustStateTable).where(TrustStateTable.trust_ref == CONFIG.trust_ref).with_for_update(read=True)
        )
        await lock_organization(session, CONFIG.organization_ref, mode=LockMode.SHARED)
        yield session


async def source_row(session: AsyncSession, kind: str) -> Any:
    model, key = {
        "root": (OrganizationRootTable, CONFIG.organization_ref),
        "trust": (TrustStateTable, CONFIG.trust_ref),
        "provider": (IdentityProviderTable, "provider:native-auth"),
        "profile": (AuthenticationProfileTable, ("stage-native-human", 1)),
        "binding": (OrganizationAuthenticationBindingTable, (CONFIG.organization_ref, "binding:native-auth")),
        "policy": (OrganizationHumanAuthenticationPolicyTable, CONFIG.organization_ref),
        "client": (NativeClientTable, "maestro"),
    }[kind]
    row = await session.get(model, key)
    assert row is not None
    return row


async def test_source_pin_is_stable_json_and_does_not_write(
    source_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with protected_source(source_sessions) as session:
        pin = await load_onboarding_source(session, CONFIG)
        assert pin == json.loads(json.dumps(pin))
        assert await load_onboarding_source(session, CONFIG, pin) == pin
        assert pin["profile_ref"] == "stage-native-human" and pin["binding_ref"] == "binding:native-auth"
        assert datetime.fromisoformat(pin["expires_at"]) > datetime.now(UTC)
        assert pin["admitted_clients"] == [{"client_id": "maestro", "version": 1}]
        assert not session.new and not session.dirty and not session.deleted


@pytest.mark.parametrize(
    "kind,field",
    [
        ("root", "version"),
        ("trust", "version"),
        ("trust", "trust_config_revision"),
        ("trust", "key_status_revision"),
        ("provider", "version"),
        ("profile", "version"),
        ("binding", "version"),
        ("policy", "version"),
        ("client", "version"),
    ],
)
async def test_any_admission_version_change_requires_explicit_new_issuance(
    source_sessions: async_sessionmaker[AsyncSession], kind: str, field: str
) -> None:
    async with protected_source(source_sessions) as session:
        pin = await load_onboarding_source(session, CONFIG)
    async with source_sessions.begin() as session:
        row = await source_row(session, kind)
        setattr(row, field, getattr(row, field) + 1)
    async with protected_source(source_sessions) as session:
        with pytest.raises(NativeAuthenticationDenied):
            await load_onboarding_source(session, CONFIG, pin)
        assert await load_onboarding_source(session, CONFIG) != pin


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("root", "status", "suspended"),
        ("trust", "purpose", "release"),
        ("trust", "configuration_digest", "0" * 64),
        ("provider", "status", "revoked"),
        ("provider", "issuer_exact", "https://other.example"),
        ("profile", "status", "suspended"),
        ("profile", "credential_kind", "workload_oauth2"),
        ("profile", "subject_namespace", "different-namespace"),
        ("profile", "stage_audience", "stage-userinfo"),
        ("profile", "config_schema", "stage.keycloak-access.v1"),
        ("profile", "digest", "0" * 64),
        ("profile", "config_json", {"jwks_url": "https://other.example/jwks", "allowed_clients": ["maestro"]}),
        ("profile", "config_json", {"jwks_url": CONFIG.issuer + "/oauth/jwks", "allowed_clients": ["other"]}),
        ("profile", "config_json", {"malformed": True}),
        ("binding", "status", "revoked"),
        ("binding", "environment", "prod"),
        ("binding", "constraints_json", {"allowed_clients": ["maestro"], "required_acr": "other-acr"}),
        ("binding", "constraints_json", {"malformed": True}),
        ("policy", "rules_json", [{"profile_ref": "other", "profile_version": 1, "required_acr": NATIVE_ACR}]),
        (
            "policy",
            "rules_json",
            [{"profile_ref": "stage-native-human", "profile_version": 1, "required_acr": NATIVE_ACR}],
        ),
        ("client", "enabled", False),
        ("client", "resource_audience", "stage-userinfo"),
        ("client", "allowed_scopes", []),
    ],
)
async def test_invalid_native_admission_cannot_create_an_activation(
    source_sessions: async_sessionmaker[AsyncSession], kind: str, field: str, value: Any
) -> None:
    async with source_sessions.begin() as session:
        row = await source_row(session, kind)
        setattr(row, field, value)
        if kind == "profile" and field != "digest":
            row.digest = content_digest(profile_content(row))
    async with protected_source(source_sessions) as session:
        with pytest.raises(NativeAuthenticationDenied):
            await load_onboarding_source(session, CONFIG)


@pytest.mark.parametrize("expired", [False, True])
async def test_binding_must_be_current(source_sessions: async_sessionmaker[AsyncSession], expired: bool) -> None:
    async with source_sessions.begin() as session:
        binding = await source_row(session, "binding")
        if expired:
            binding.expires_at = datetime.now(UTC)
        else:
            binding.valid_from = datetime.now(UTC) + timedelta(minutes=1)
    async with protected_source(source_sessions) as session:
        with pytest.raises(NativeAuthenticationDenied):
            await load_onboarding_source(session, CONFIG)


async def test_content_change_without_version_bump_cannot_follow_saved_pin(
    source_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with protected_source(source_sessions) as session:
        pin = await load_onboarding_source(session, CONFIG)
    async with source_sessions.begin() as session:
        binding = await source_row(session, "binding")
        binding.constraints_json = {"allowed_clients": ["maestro"], "required_acr": None}
    async with protected_source(source_sessions) as session:
        with pytest.raises(NativeAuthenticationDenied):
            await load_onboarding_source(session, CONFIG, pin)
