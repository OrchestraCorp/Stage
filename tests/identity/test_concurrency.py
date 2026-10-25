"""Real PostgreSQL lock ordering, observed through blocking PIDs rather than timing."""

import asyncio
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.identity import administration
from app.identity.administration import RevocationRequest, revoke
from app.identity.service import IdentityDenied, IdentityService
from app.models.execution import TenantGuardTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import BootstrapRecordTable, TrustStateTable
from tests.identity.test_authentication import jwks, token
from tests.identity.test_service import read, seed

RevocationKind = Literal["principal", "organization", "membership", "profile"]


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _request(kind: RevocationKind) -> RevocationRequest:
    return RevocationRequest(
        environment="test",
        kind=kind,
        target_ref={
            "principal": "person",
            "organization": "tenant-a",
            "membership": "member-a",
            "profile": "local-human",
        }[kind],
        expected_version=1,
        organization_ref="tenant-a" if kind == "membership" else None,
        profile_version=1 if kind == "profile" else None,
    )


async def _prepare(sessions: async_sessionmaker[AsyncSession]) -> None:
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("blocking PID and row lock semantics require PostgreSQL")
    await seed(sessions)
    async with sessions.begin() as session:
        session.add(
            BootstrapRecordTable(
                bootstrap_ref="bootstrap:fixture",
                environment="test",
                bootstrap_type=administration.BOOTSTRAP_TYPE,
                configuration_digest="a" * 64,
                initial_identity_ref="identity",
                initial_grant_refs=[],
                completed_at=datetime.now(UTC),
            )
        )


async def _wait_until_blocked_by(sessions: async_sessionmaker[AsyncSession], blocker_pid: int) -> None:
    # The deadline only limits a broken test. Success requires PostgreSQL to
    # report a real blocking edge, so scheduler delays cannot simulate a race.
    async with asyncio.timeout(10):
        async with sessions() as observer:
            while not await observer.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted AND :blocker = ANY(pg_blocking_pids(pid)))"
                ),
                {"blocker": blocker_pid},
            ):
                await asyncio.sleep(0.01)


async def _assert_epoch_advanced_once(sessions: async_sessionmaker[AsyncSession], kind: RevocationKind) -> None:
    async with sessions() as session:
        if kind == "principal":
            assert await session.scalar(select(PrincipalTable.identity_epoch)) == 1
        elif kind == "profile":
            assert await session.scalar(select(TrustStateTable.trust_config_revision)) == 2
        else:
            assert (
                await session.scalar(select(TenantGuardTable.epoch).where(TenantGuardTable.tenant_ref == "tenant-a"))
                == 1
            )


@pytest.mark.parametrize("kind", ("principal", "organization", "membership", "profile"))
async def test_revocation_holds_lock_first_and_waiting_identity_read_is_denied(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
    kind: RevocationKind,
) -> None:
    await _prepare(persistence_sessions)
    locked = asyncio.Event()
    release = asyncio.Event()
    writer_pid: int | None = None
    original_lock = administration._lock_target

    async def hold_revocation_lock(session: AsyncSession, request: RevocationRequest) -> Any:
        nonlocal writer_pid
        row = await original_lock(session, request)
        writer_pid = await session.scalar(text("SELECT pg_backend_pid()"))
        locked.set()
        await release.wait()
        return row

    monkeypatch.setattr(administration, "_lock_target", hold_revocation_lock)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(signing_key)))
    ) as client:
        service = IdentityService(persistence_sessions, environment="test", http_client=client)
        request = _request(kind)
        writer = asyncio.create_task(
            revoke(persistence_sessions, request, actor_ref="operator", idempotency_key="revoke-first")
        )
        tasks: list[asyncio.Task[Any]] = [writer]
        try:
            await asyncio.wait_for(locked.wait(), 10)
            assert writer_pid is not None
            reader = asyncio.create_task(read(service, token(signing_key)))
            tasks.append(reader)
            await _wait_until_blocked_by(persistence_sessions, writer_pid)
            assert not reader.done()
            release.set()
            result = await asyncio.wait_for(writer, 10)
            assert result.result["status"] == "revoked" and result.result["version"] == 2
            with pytest.raises(IdentityDenied):
                await asyncio.wait_for(reader, 10)
            replay = await revoke(persistence_sessions, request, actor_ref="operator", idempotency_key="revoke-first")
            assert replay == result
            await _assert_epoch_advanced_once(persistence_sessions, kind)
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("kind", ("principal", "organization", "membership", "profile"))
async def test_identity_read_holds_lock_first_and_revocation_waits_for_completion(
    persistence_sessions: async_sessionmaker[AsyncSession],
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
    kind: RevocationKind,
) -> None:
    await _prepare(persistence_sessions)
    locked = asyncio.Event()
    release = asyncio.Event()
    reader_pid: int | None = None
    original_workload = IdentityService._workload

    async def hold_identity_read(self: IdentityService, session: AsyncSession, *args: Any) -> Any:
        nonlocal reader_pid
        reader_pid = await session.scalar(text("SELECT pg_backend_pid()"))
        locked.set()
        await release.wait()
        return await original_workload(self, session, *args)

    monkeypatch.setattr(IdentityService, "_workload", hold_identity_read)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks(signing_key)))
    ) as client:
        service = IdentityService(persistence_sessions, environment="test", http_client=client)
        bearer = token(signing_key)
        reader = asyncio.create_task(read(service, bearer))
        tasks: list[asyncio.Task[Any]] = [reader]
        try:
            await asyncio.wait_for(locked.wait(), 10)
            assert reader_pid is not None
            writer = asyncio.create_task(
                revoke(persistence_sessions, _request(kind), actor_ref="operator", idempotency_key="read-first")
            )
            tasks.append(writer)
            await _wait_until_blocked_by(persistence_sessions, reader_pid)
            assert not writer.done()
            release.set()
            result = await asyncio.wait_for(reader, 10)
            assert result.membership_ref == "member-a" and result.organization_ref == "tenant-a"
            assert (await asyncio.wait_for(writer, 10)).result["status"] == "revoked"
            with pytest.raises(IdentityDenied):
                await read(service, bearer)
            await _assert_epoch_advanced_once(persistence_sessions, kind)
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
