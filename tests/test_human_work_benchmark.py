"""Guard the destructive benchmark boundary and its accounting before a PG run."""

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scripts.benchmark_human_work import (
    Sample,
    batch,
    database_url,
    distribution,
    edit_baseline,
    harness,
    instrument,
    source_fingerprints,
    state,
)
from tests.support.authenticated_commands import seed_authenticated_command


@pytest.mark.parametrize(
    ("url", "allow"),
    [
        ("", "1"),
        ("postgresql+asyncpg://user@localhost/stage", "1"),
        ("postgresql+asyncpg://user@localhost/stage_test_baseline", None),
        ("postgresql+asyncpg://user@localhost/stage_test_baseline", "true"),
        ("postgresql+asyncpg://user@localhost/stage_testing", "1"),
        ("sqlite+aiosqlite:///stage_test_baseline.db", "1"),
    ],
)
def test_benchmark_rejects_non_disposable_or_unapproved_database(
    monkeypatch: pytest.MonkeyPatch, url: str, allow: str | None
) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", url)
    if allow is None:
        monkeypatch.delenv("STAGE_ALLOW_TEST_DATABASE_RESET", raising=False)
    else:
        monkeypatch.setenv("STAGE_ALLOW_TEST_DATABASE_RESET", allow)
    with pytest.raises(ValueError):
        database_url()


@pytest.mark.parametrize("name", ["stage_test_baseline", "stage_ci_baseline"])
def test_benchmark_requires_explicit_pg_reset(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    url = f"postgresql+asyncpg://user@localhost/{name}"
    monkeypatch.setenv("TEST_DATABASE_URL", url)
    monkeypatch.setenv("STAGE_ALLOW_TEST_DATABASE_RESET", "1")
    assert database_url() == url


def test_millisecond_percentiles_include_small_samples_and_empty_waits() -> None:
    assert distribution([0.005, 0.001, 0.003]) == {"samples": 3, "p50": 3.0, "p95": 4.8, "p99": 4.96, "max": 5.0}
    assert distribution([]) == {"samples": 0, "p50": 0, "p95": 0, "p99": 0, "max": 0}


def test_source_fingerprint_tracks_code_but_excludes_runtime_secrets(tmp_path: Path) -> None:
    source = tmp_path / "app" / "api" / "work.py"
    source.parent.mkdir(parents=True)
    source.write_text("before = 1\n", encoding="utf-8")
    fixture = tmp_path / "tests" / "support" / "identity_tokens.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("fixture_version = 1\n", encoding="utf-8")
    first = source_fingerprints(tmp_path)
    (tmp_path / ".env").write_text("SECRET=synthetic-secret\n", encoding="utf-8")
    assert source_fingerprints(tmp_path) == first
    assert "synthetic-secret" not in json.dumps(first) and str(tmp_path) not in json.dumps(first)
    source.write_text("after = 2\n", encoding="utf-8")
    second = source_fingerprints(tmp_path)
    assert first["app_python_tree_sha256"] != second["app_python_tree_sha256"]
    assert first["selected_files_sha256"]["app/api/work.py"] != second["selected_files_sha256"]["app/api/work.py"]
    assert second["app_python_files"] == 1
    fixture.write_text("fixture_version = 2\n", encoding="utf-8")
    third = source_fingerprints(tmp_path)
    assert third["app_python_tree_sha256"] == second["app_python_tree_sha256"]
    assert (
        third["selected_files_sha256"]["tests/support/identity_tokens.py"]
        != second["selected_files_sha256"]["tests/support/identity_tokens.py"]
    )


async def test_batch_bounds_in_flight_requests_and_runs_every_index_once() -> None:
    active, peak = 0, 0
    progress = asyncio.Event()

    async def operation(index: int) -> Sample:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return Sample(index, "GET", status="200")

    samples, elapsed = await batch(17, 4, operation, progress=progress)
    assert sorted(sample.index for sample in samples) == list(range(17))
    assert peak == 4 and active == 0 and progress.is_set() and elapsed > 0


async def test_benchmark_edit_accounting_runs_through_real_asgi_and_identity(
    persistence_sessions: async_sessionmaker[AsyncSession],
) -> None:
    # Sequential SQLite is enough for helper/accounting validation; the actual
    # benchmark refuses SQLite because it cannot measure PostgreSQL row locks.
    await seed_authenticated_command(persistence_sessions)
    engine = persistence_sessions.kw["bind"]
    with instrument(engine):
        async with harness(persistence_sessions, engine) as (client, _host):
            independent = await edit_baseline(client, persistence_sessions, 3, 1, False)
            same_object = await edit_baseline(client, persistence_sessions, 3, 1, True)
    assert independent["status_counts"] == {"200": 3}
    assert same_object["status_counts"] == {"200": 1, "409": 2}
    assert independent["organization_guard_calls"] >= 6
    assert independent["pool_checkout_calls"] > 0
    assert await state(persistence_sessions) == (4, 4, 4)
