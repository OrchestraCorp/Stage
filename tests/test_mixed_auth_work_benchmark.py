"""Capacity instrumentation and synthetic accounting, independent of reported timings."""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.execution.storage import lock_tenant
from app.native_auth.keys import KeyRing
from scripts.benchmark_mixed_auth_work import (
    ACTIVE,
    DatabaseMeasurements,
    Sample,
    database_url,
    harness,
    login_burst,
    seed,
    workload,
)
from tests.support.authorization import ORG
from tests.support.native_oauth import native_keyring as native_keyring


@pytest.mark.parametrize("name", ["stage", "stage_test_core", "stage_test_baseline", "stage_test_capacity_evil/path"])
def test_mixed_capacity_rejects_shared_or_non_capacity_database(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", f"postgresql+asyncpg://user@localhost/{name}")
    monkeypatch.setenv("STAGE_ALLOW_TEST_DATABASE_RESET", "1")
    with pytest.raises(ValueError):
        database_url()


def test_mixed_capacity_needs_explicit_disposable_database_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://user@localhost/stage_test_capacity_test")
    monkeypatch.delenv("STAGE_ALLOW_TEST_DATABASE_RESET", raising=False)
    with pytest.raises(ValueError):
        database_url()
    monkeypatch.setenv("STAGE_ALLOW_TEST_DATABASE_RESET", "1")
    assert database_url().endswith("/stage_test_capacity_test")


async def test_measurement_excludes_queueing_from_hold_and_reentrant_guard_does_not_reset_it(
    persistence_sessions: async_sessionmaker[AsyncSession],
    native_keyring: KeyRing,
) -> None:
    await seed(persistence_sessions, native_keyring, 1)
    engine = persistence_sessions.kw["bind"]
    # First guard: begin=1, acquired=6 (5 waiting/executing).
    # Reentrant query: begin=8, returned=8.5. Root tx finishes=10.
    # The resource was held once for 10-6=4, not 10-1=9 or 10-8.5=1.5.
    ticks = iter([1.0, 6.0, 8.0, 8.5, 10.0])
    sample = Sample("instrument-check")
    with DatabaseMeasurements(engine, clock=lambda: next(ticks)):
        active = ACTIVE.set(sample)
        try:
            async with persistence_sessions.begin() as session:
                await lock_tenant(session, ORG)
                await lock_tenant(session, ORG)
        finally:
            ACTIVE.reset(active)
    assert sample.guard_acquire == [5.0]
    assert sample.guard_hold == [4.0]
    assert sample.guard_queries == 2 and sample.sql == 2


async def test_transaction_end_keeps_its_original_sample_across_cleanup_tasks(
    persistence_sessions: async_sessionmaker[AsyncSession], native_keyring: KeyRing
) -> None:
    await seed(persistence_sessions, native_keyring, 1)
    engine = persistence_sessions.kw["bind"]
    sample = Sample("rollback")
    with DatabaseMeasurements(engine):
        session = persistence_sessions()
        active = ACTIVE.set(sample)
        await lock_tenant(session, ORG)
        ACTIVE.reset(active)
        # ContextVar is now unset, as in cancellation-safe cleanup task paths.
        await asyncio.create_task(session.close())
    assert len(sample.guard_acquire) == len(sample.guard_hold) == 1
    assert sample.guard_hold[0] >= 0


async def test_native_mixed_workload_checks_all_operations_and_committed_audits(
    persistence_sessions: async_sessionmaker[AsyncSession], native_keyring: KeyRing
) -> None:
    engine = persistence_sessions.kw["bind"]
    oauth, actors = await seed(persistence_sessions, native_keyring, 1)
    with DatabaseMeasurements(engine):
        async with harness(engine, persistence_sessions, native_keyring) as host:
            result = await workload(host, oauth, actors, concurrency=1, count=8)
            login = await login_burst(oauth, actors)
    assert result["all"]["errors"] == {}
    assert result["all"]["operations"] == 8
    assert actors[0].version == 3
    assert login["errors"] == {} and login["operations"] == 1
    assert login["factor_attempt_limit"] == 300 and login["rate_window_seconds"] == 600
    assert login["factor_attempts"] == 2 and login["otp_reused"] is False
    for name, operation in result["by_operation"].items():
        assert operation["operations"] == 2
        assert operation["sql_statements"]["total"] > 0
        assert operation["guard_transactions"] == (4 if name.startswith("work") else 2)
        assert operation["guard_hold_ms_per_transaction"]["p50"] > 0
    async with persistence_sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM command_execution")) == 2
        assert await session.scalar(text("SELECT count(*) FROM execution_audit")) == 2
