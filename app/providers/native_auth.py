"""Native issuer lifecycle: trusted keys and bounded storage leases, never business grants."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.identity_foundation import (
    BootstrapRecordTable,
    IdentityProviderTable,
    OrganizationRootTable,
    TrustStateTable,
)
from app.native_auth.browser import BrowserAuthorization
from app.native_auth.config import NativeAuthConfig
from app.native_auth.credentials import NativeCredentials
from app.native_auth.keys import KeyRing, load_keyring, read_encryption_key
from app.native_auth.logout import NativeLogout
from app.native_auth.oauth import NativeOAuth
from app.native_auth.sessions import AccountSessions
from app.providers.storage import STORAGE
from app.runtime import ComponentContext, PluginSpec, ServiceKey

NATIVE_AUTH: ServiceKey["NativeAuthFacade"] = ServiceKey("stage", "native-auth", "1")


@dataclass(frozen=True)
class NativeAuthServices:
    credentials: NativeCredentials
    oauth: NativeOAuth
    browser: BrowserAuthorization
    logout: NativeLogout
    sessions: AccountSessions


class NativeAuthFacade:
    def __init__(
        self, context: ComponentContext, config: NativeAuthConfig, keyring: KeyRing, encryption_key: bytes
    ) -> None:
        self._context = context
        self.config = config
        self.keyring = keyring
        self._encryption_key = encryption_key

    @asynccontextmanager
    async def services(self) -> AsyncIterator[NativeAuthServices]:
        async with asyncio.timeout(self.config.request_timeout_seconds), self._context.call(STORAGE) as call:
            sessions = call.require(STORAGE).sessions
            credentials = NativeCredentials(
                sessions,
                fernet_key=self._encryption_key,
                organization_ref=self.config.organization_ref,
                trust_ref=self.config.trust_ref,
                issuer_label="OrchestraCorp",
                peer_attempt_limit=self.config.peer_attempt_limit,
                authorization_attempt_limit=self.config.authorization_attempt_limit,
                rate_window_seconds=self.config.rate_window_seconds,
            )
            oauth = NativeOAuth(sessions, self.config, self.keyring, credentials=credentials)
            browser = BrowserAuthorization(sessions, self.config, credentials=credentials, oauth=oauth)
            logout = NativeLogout(sessions, self.config, credentials=credentials, oauth=oauth)
            account_sessions = AccountSessions(sessions, self.config, credentials=credentials, oauth=oauth)
            yield NativeAuthServices(credentials, oauth, browser, logout, account_sessions)


async def _validate_deployment(sessions: async_sessionmaker[AsyncSession], config: NativeAuthConfig) -> None:
    """Bind the issuer to its fixed deployment, independently of business admission.

    Startup owns this bounded read session; no resource remains to dispose. The
    native provider's admission status and profile allowlists belong to Identity
    and deliberately cannot turn an issuer's own authority on or off.
    """
    async with asyncio.timeout(config.request_timeout_seconds), sessions() as session:
        roots = (await session.scalars(select(OrganizationRootTable.organization_ref).limit(2))).all()
        environments = set(
            await session.scalars(
                select(BootstrapRecordTable.environment)
                .where(BootstrapRecordTable.bootstrap_type == "identity-foundation")
                .distinct()
                .limit(2)
            )
        )
        provider = await session.get(IdentityProviderTable, "provider:native-auth")
        trust = await session.get(TrustStateTable, config.trust_ref)
        if (
            roots != [config.organization_ref]
            or environments != {config.environment}
            or provider is None
            or provider.issuer_exact != config.issuer
            or provider.trust_ref != config.trust_ref
            or trust is None
            or trust.purpose != "authentication"
        ):
            raise RuntimeError("native_auth_deployment_mismatch")


def native_auth_plugin(settings: Settings) -> PluginSpec:
    async def setup(ctx: ComponentContext) -> None:
        if not settings.native_auth_enabled:
            raise ValueError("native_auth_is_disabled")
        config = NativeAuthConfig(
            issuer=settings.native_auth_issuer or "",
            organization_ref=settings.native_auth_organization_ref or "",
            environment=settings.env,
            request_timeout_seconds=settings.native_auth_request_timeout_seconds,
            peer_attempt_limit=settings.native_auth_peer_attempt_limit,
            authorization_attempt_limit=settings.native_auth_authorization_attempt_limit,
            rate_window_seconds=settings.native_auth_rate_window_seconds,
        )
        keyring = load_keyring(settings.native_auth_key_file or "", settings.env)
        encryption_key = read_encryption_key(settings.native_auth_encryption_key_file or "", settings.env)
        await _validate_deployment(ctx.require(STORAGE).sessions, config)
        ctx.provide(NATIVE_AUTH, NativeAuthFacade(ctx, config, keyring, encryption_key))

    return PluginSpec("native-auth", setup, requires=(STORAGE,), provides=(NATIVE_AUTH,))
