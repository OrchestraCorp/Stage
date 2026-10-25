"""An identity read cannot retain database connections indefinitely behind a lock."""

import asyncio

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.identity.service import IdentityService
from app.models.execution import TenantGuardTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import TrustStateTable
from tests.identity.test_authentication import jwks, token
from tests.identity.test_concurrency import _wait_until_blocked_by
from tests.identity.test_service import read, seed


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


async def test_overall_deadline_bounds_remote_verification_without_a_db_connection(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
) -> None:
    await seed(persistence_sessions)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_keys(_: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(200, json=jwks(signing_key))

    async with httpx.AsyncClient(transport=httpx.MockTransport(delayed_keys)) as client:
        service = IdentityService(
            persistence_sessions,
            environment="test",
            http_client=client,
            request_timeout_seconds=0.5,
        )
        task = asyncio.create_task(read(service, token(signing_key)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert persistence_sessions.kw["bind"].pool.checkedout() == 0
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 2)
            assert task.done() and not task.cancelled()
            with pytest.raises(TimeoutError):
                task.result()
            assert persistence_sessions.kw["bind"].pool.checkedout() == 0
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("locked_fact", ("trust", "principal", "tenant"))
async def test_postgres_lock_deadline_releases_connection_and_preceding_locks(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    locked_fact: str,
) -> None:
    engine = persistence_sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("row lock cancellation requires PostgreSQL")
    await seed(persistence_sessions)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(signing_key)))
    ) as client:
        service = IdentityService(
            persistence_sessions,
            environment="test",
            http_client=client,
            request_timeout_seconds=2,
        )
        bearer = token(signing_key)
        async with persistence_sessions() as holder, holder.begin():
            model = {"trust": TrustStateTable, "principal": PrincipalTable, "tenant": TenantGuardTable}[locked_fact]
            await holder.execute(select(model).with_for_update())
            holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            assert holder_pid is not None
            task = asyncio.create_task(read(service, bearer))
            try:
                await _wait_until_blocked_by(persistence_sessions, holder_pid)
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(task), 5)
                assert task.done() and not task.cancelled()
                with pytest.raises(TimeoutError):
                    task.result()
                # Only the deliberate test holder is checked out. NOWAIT also
                # proves that the timed-out reader released its earlier SHARE locks.
                assert engine.pool.checkedout() == 1
                await holder.execute(select(TrustStateTable).with_for_update(nowait=True))
                await holder.execute(select(PrincipalTable).with_for_update(nowait=True))
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert (await read(service, bearer)).membership_ref == "member-a"
    assert engine.pool.checkedout() == 0
