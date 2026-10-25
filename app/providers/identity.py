"""Identity is an application service; Core only owns its lifetime and dependency lease."""

from app.core.config import Settings
from app.identity.service import IdentityService, SelfIdentity
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


def identity_plugin(config: Settings) -> PluginSpec:
    def setup(ctx: ComponentContext) -> None:
        service = IdentityService(
            ctx.require(STORAGE).sessions,
            environment=config.env,
            request_timeout_seconds=config.identity_request_timeout_seconds,
        )
        ctx.effect(lambda: service.aclose, label="identity.verifier")
        ctx.provide(IDENTITY, IdentityFacade(ctx, service))

    return PluginSpec("identity", setup, requires=(STORAGE,), provides=(IDENTITY,))
