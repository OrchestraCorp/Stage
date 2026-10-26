"""The native issuer becomes ready only for its explicitly installed deployment."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.identity_foundation import (
    AuthenticationProfileTable,
    BootstrapRecordTable,
    IdentityProviderTable,
    TrustStateTable,
)
from app.providers import native_auth as provider_module
from app.providers.native_auth import NATIVE_AUTH, native_auth_plugin
from app.providers.storage import STORAGE, Storage
from app.runtime import ComponentContext, PluginSpec, RuntimeHost
from scripts.native_auth_admin import generate_keys
from tests.support.identity_administration import install as install_identity
from tests.support.native_administration import install_native


@pytest.fixture
async def installed_native(persistence_sessions: async_sessionmaker[AsyncSession], tmp_path: Path) -> Settings:
    await install_identity(persistence_sessions)
    await install_native(persistence_sessions)
    key_file, encryption_file = tmp_path / "signing.json", tmp_path / "encryption.key"
    generate_keys(key_file, encryption_file)
    return Settings(
        env="test",
        native_auth_enabled=True,
        native_auth_issuer="http://127.0.0.1:8080",
        native_auth_organization_ref="tenant-a",
        native_auth_key_file=str(key_file),
        native_auth_encryption_key_file=str(encryption_file),
    )


@asynccontextmanager
async def runtime(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[RuntimeHost]:
    engine = sessions.kw["bind"]
    assert isinstance(engine, AsyncEngine)

    def storage(ctx: ComponentContext) -> None:
        ctx.provide(STORAGE, Storage(engine, sessions))

    host = RuntimeHost()
    host.load(PluginSpec("deployment-test.storage", storage, provides=(STORAGE,)))
    try:
        yield host
    finally:
        report = await host.close(timeout=3)
        assert not report.pending and not report.blocked


async def test_complete_bootstrap_and_native_install_allow_issuer_startup(
    persistence_sessions: async_sessionmaker[AsyncSession],
    installed_native: Settings,
) -> None:
    async with runtime(persistence_sessions) as host:
        handle = host.load(native_auth_plugin(installed_native))
        assert (await host.await_ready([handle, NATIVE_AUTH], timeout=3)).ready
        async with host.call(NATIVE_AUTH) as call:
            facade = call.require(NATIVE_AUTH)
            assert facade.config.issuer == installed_native.native_auth_issuer
            async with facade.services() as services:
                assert services.oauth.config.environment == "test"


@pytest.mark.parametrize(
    "changes",
    [
        {"env": "dev"},
        {"native_auth_issuer": "http://127.0.0.1:8081"},
        {"native_auth_organization_ref": "different-organization"},
    ],
)
async def test_deployment_configuration_mismatch_never_publishes_native_issuer(
    persistence_sessions: async_sessionmaker[AsyncSession],
    installed_native: Settings,
    changes: dict[str, Any],
) -> None:
    settings = installed_native.model_copy(update=changes)
    async with runtime(persistence_sessions) as host:
        handle = host.load(native_auth_plugin(settings))
        report = await host.await_ready([handle], timeout=3)
        assert report.outcome == "failed"
        assert not host.is_ready([NATIVE_AUTH])


@pytest.mark.parametrize(
    "mutation",
    ["wrong_record_environment", "missing_bootstrap", "mixed_environments", "provider_trust", "trust_purpose"],
)
async def test_inconsistent_persistent_deployment_identity_prevents_startup(
    persistence_sessions: async_sessionmaker[AsyncSession],
    installed_native: Settings,
    mutation: str,
) -> None:
    async with persistence_sessions.begin() as session:
        if mutation == "wrong_record_environment":
            record = await session.scalar(
                select(BootstrapRecordTable).where(BootstrapRecordTable.bootstrap_type == "identity-foundation")
            )
            assert record is not None
            record.environment = "dev"
        elif mutation == "missing_bootstrap":
            await session.execute(delete(BootstrapRecordTable))
        elif mutation == "mixed_environments":
            session.add(
                BootstrapRecordTable(
                    bootstrap_ref="conflicting-environment",
                    environment="dev",
                    bootstrap_type="identity-foundation",
                    configuration_digest="b" * 64,
                    initial_identity_ref="external-human",
                    initial_grant_refs=[],
                    completed_at=datetime.now(UTC),
                )
            )
        elif mutation == "provider_trust":
            provider = await session.get(IdentityProviderTable, "provider:native-auth")
            assert provider is not None
            provider.trust_ref = "trust-a"
        else:
            trust = await session.get(TrustStateTable, "trust:native-auth")
            assert trust is not None
            trust.purpose = "release"
    async with runtime(persistence_sessions) as host:
        handle = host.load(native_auth_plugin(installed_native))
        assert (await host.await_ready([handle], timeout=3)).outcome == "failed"
        assert not host.is_ready([NATIVE_AUTH])


async def test_business_admission_status_and_unrelated_bootstrap_do_not_control_issuer_authority(
    persistence_sessions: async_sessionmaker[AsyncSession],
    installed_native: Settings,
) -> None:
    async with persistence_sessions.begin() as session:
        provider = await session.get(IdentityProviderTable, "provider:native-auth")
        profile = await session.scalar(
            select(AuthenticationProfileTable).where(AuthenticationProfileTable.provider_ref == "provider:native-auth")
        )
        assert provider is not None and profile is not None
        provider.status = "revoked"
        profile.status = "revoked"
        profile.config_json = {**profile.config_json, "allowed_clients": []}
        session.add(
            BootstrapRecordTable(
                bootstrap_ref="unrelated-bootstrap",
                environment="dev",
                bootstrap_type="unrelated-component",
                configuration_digest="b" * 64,
                initial_identity_ref="external-human",
                initial_grant_refs=[],
                completed_at=datetime.now(UTC),
            )
        )
    async with runtime(persistence_sessions) as host:
        handle = host.load(native_auth_plugin(installed_native))
        assert (await host.await_ready([handle, NATIVE_AUTH], timeout=3)).ready


async def test_issuer_is_not_published_before_async_deployment_validation_completes(
    persistence_sessions: async_sessionmaker[AsyncSession],
    installed_native: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    original = provider_module._validate_deployment

    async def delayed(sessions: async_sessionmaker[AsyncSession], config: Any) -> None:
        entered.set()
        await release.wait()
        await original(sessions, config)

    monkeypatch.setattr(provider_module, "_validate_deployment", delayed)
    async with runtime(persistence_sessions) as host:
        handle = host.load(native_auth_plugin(installed_native))
        ready = asyncio.create_task(host.await_ready([handle], timeout=3))
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert not host.is_ready([NATIVE_AUTH])
        release.set()
        assert (await ready).ready
