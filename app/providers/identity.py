"""Identity is an application service; Core only owns its lifetime and dependency lease."""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.identity.evidence import HumanAuthenticationEvidence
from app.identity.service import IdentityService, SelfIdentity
from app.native_auth.keys import load_keyring
from app.providers.storage import STORAGE
from app.runtime import ComponentContext, PluginSpec, ServiceKey

IDENTITY: ServiceKey["IdentityFacade"] = ServiceKey("stage", "identity", "1")


class IdentityFacade:
    def __init__(self, context: ComponentContext, service: IdentityService) -> None:
        self.context = context
        self.service = service

    async def read_self(
        self,
        access_token: str,
        *,
        profile_ref: str,
        profile_version: int,
    ) -> SelfIdentity:
        async with self.context.call(STORAGE):
            return await self.service.read_self(
                access_token,
                profile_ref=profile_ref,
                profile_version=profile_version,
            )

    async def authenticate_human(
        self,
        access_token: str,
        *,
        profile_ref: str,
        profile_version: int,
    ) -> HumanAuthenticationEvidence:
        async with self.context.call(STORAGE):
            return await self.service.authenticate_human(
                access_token, profile_ref=profile_ref, profile_version=profile_version
            )

    async def acquire_human_locks(self, session: AsyncSession, evidence: HumanAuthenticationEvidence) -> None:
        # The command facade holds IDENTITY and STORAGE for the entire transaction.
        await self.service.acquire_human_locks(session, evidence)

    async def validate_human(self, session: AsyncSession, evidence: HumanAuthenticationEvidence, now: datetime) -> None:
        await self.service.validate_human(session, evidence, now)


def identity_plugin(config: Settings) -> PluginSpec:
    def setup(ctx: ComponentContext) -> None:
        service = IdentityService(
            ctx.require(STORAGE).sessions,
            environment=config.env,
            request_timeout_seconds=config.identity_request_timeout_seconds,
            native_keyring=load_keyring(config.native_auth_key_file or "", config.env)
            if config.native_auth_enabled
            else None,
            native_issuer=config.native_auth_issuer if config.native_auth_enabled else None,
        )
        ctx.effect(lambda: service.aclose, label="identity.verifier")
        ctx.provide(IDENTITY, IdentityFacade(ctx, service))

    return PluginSpec("identity", setup, requires=(STORAGE,), provides=(IDENTITY,))
