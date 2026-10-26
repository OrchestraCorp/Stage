"""Shared synthetic builders for assignment http."""

from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.main import create_app
from app.providers.assignments import ASSIGNMENTS, assignments_plugin
from app.providers.identity import IDENTITY, IdentityFacade
from app.providers.storage import STORAGE, Storage
from app.providers.work import WORK, human_work_plugin
from app.runtime import ComponentContext, PluginSpec, RuntimeHost
from tests.support.authenticated_commands import identity_service, jwt_client, seed_authenticated_command
from tests.support.identity_tokens import token

QUERY: dict[str, str | int] = {"profile_ref": "local-human", "profile_version": 1}


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
async def assignment_client(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    request: pytest.FixtureRequest,
) -> AsyncIterator[httpx.AsyncClient]:
    await seed_authenticated_command(persistence_sessions)
    async with jwt_client(signing_key) as verifier_client:
        identity = identity_service(persistence_sessions, verifier_client)

        def storage_setup(ctx: ComponentContext) -> None:
            ctx.provide(STORAGE, Storage(persistence_sessions.kw["bind"], persistence_sessions))

        def identity_setup(ctx: ComponentContext) -> None:
            ctx.effect(lambda: identity.aclose, label="test.identity")
            ctx.provide(IDENTITY, IdentityFacade(ctx, identity))

        config = Settings(
            env="test",
            database_url="sqlite+aiosqlite://",
            redis_url=None,
            identity_request_timeout_seconds=getattr(request, "param", 10),
        )
        host = RuntimeHost()
        host.load(PluginSpec("test.storage", storage_setup, provides=(STORAGE,)))
        host.load(PluginSpec("test.identity", identity_setup, requires=(STORAGE,), provides=(IDENTITY,)))
        host.load(assignments_plugin(config))
        host.load(human_work_plugin(config))
        assert (await host.await_ready([ASSIGNMENTS, WORK], timeout=2)).ready
        app = create_app(config)
        app.state.runtime = SimpleNamespace(host=host, ready=True)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://stage",
                params=QUERY,
                headers={"Authorization": f"Bearer {token(signing_key)}"},
            ) as client:
                yield client
        finally:
            assert (await host.close(timeout=2)).successful
