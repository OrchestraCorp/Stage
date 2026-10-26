"""Bounded, destructive benchmark of synthetic human commands in a disposable PG database.

Requires TEST_DATABASE_URL=postgresql+asyncpg://.../stage_test_* (or stage_ci_*)
and STAGE_ALLOW_TEST_DATABASE_RESET=1. Run alone: it resets application tables
before and after execution. Uses repository test fixtures, signed RSA JWTs and
mock JWKS; no real IdP, TCP HTTP, Redis, or production capacity claim is involved.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker  # noqa: E402

from app.authz import administration, loader  # noqa: E402
from app.authz.administration import AuthorizationRevocationRequest, revoke_authorization  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.db import create_engine, create_session_factory  # noqa: E402
from app.execution import kernel  # noqa: E402
from app.execution.storage import lock_tenant  # noqa: E402
from app.identity import service as identity_module  # noqa: E402
from app.identity.evidence import HumanAuthenticationEvidence  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.authorization import AuthorizationResourceTable, RelationTupleTable  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.execution import AuditRecordTable, CommandExecutionTable  # noqa: E402
from app.models.work import WorkItemTable  # noqa: E402
from app.providers.identity import IDENTITY, IdentityFacade  # noqa: E402
from app.providers.storage import STORAGE, Storage  # noqa: E402
from app.providers.work import WORK, human_work_plugin  # noqa: E402
from app.runtime import ComponentContext, PluginSpec, RuntimeHost  # noqa: E402
from scripts.check_database import validate_test_database  # noqa: E402
from tests.support.authenticated_commands import identity_service, jwt_client, seed_authenticated_command  # noqa: E402
from tests.support.authorization import ORG, PERSON  # noqa: E402
from tests.support.identity_tokens import token  # noqa: E402

QUERY: dict[str, str | int] = {
    "profile_ref": "local-human",
    "profile_version": 1,
    "assignment_ref": "assignment:engineering",
}
LEVELS = (1, 4, 8, 16)


class BenchmarkCheckFailed(RuntimeError):
    """Only fixed diagnostic strings; safe to print without database parameters."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BenchmarkCheckFailed(message)


def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "")
    validate_test_database(url, os.environ.get("STAGE_ALLOW_TEST_DATABASE_RESET"))
    if make_url(url).drivername != "postgresql+asyncpg":
        raise ValueError("The concurrency baseline requires a disposable PostgreSQL database")
    return url


def source_fingerprints(root: Path = ROOT) -> dict[str, Any]:
    """Hash application source and explicit test inputs, never runtime configuration."""
    tree = hashlib.sha256()
    app_paths = sorted((root / "app").rglob("*.py"))
    for path in app_paths:
        tree.update(path.relative_to(root).as_posix().encode() + b"\0")
        tree.update(hashlib.sha256(path.read_bytes()).digest())
    selected = (
        "app/api/work.py",
        "app/providers/work.py",
        "app/identity/service.py",
        "app/identity/authentication.py",
        "app/authz/authenticated.py",
        "app/authz/loader.py",
        "app/authz/decision.py",
        "app/authz/administration.py",
        "app/execution/kernel.py",
        "app/execution/storage.py",
        "app/features/work/details.py",
        "app/runtime/host.py",
        "scripts/benchmark_human_work.py",
        "tests/support/authenticated_commands.py",
        "tests/support/authorization.py",
        "tests/support/database_commands.py",
        "tests/support/work_details.py",
        "tests/support/identity_tokens.py",
        "uv.lock",
    )
    return {
        "app_python_tree_sha256": tree.hexdigest(),
        "app_python_files": len(app_paths),
        "selected_files_sha256": {
            relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
            for relative in selected
            if (root / relative).is_file()
        },
    }


async def observed_guard_block(holder: AsyncSession, holder_pid: int) -> None:
    """Prove a request is waiting for this holder, before cancelling or timing out."""
    query = text(
        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity AS waiting "
        "WHERE waiting.datname = current_database() AND waiting.wait_event_type = 'Lock' "
        "AND :holder_pid = ANY(pg_blocking_pids(waiting.pid)) "
        "AND waiting.query LIKE '%execution_tenant_guard%')"
    )
    async with asyncio.timeout(2):
        while True:
            await holder.execute(text("SELECT pg_stat_clear_snapshot()"))
            if await holder.scalar(query, {"holder_pid": holder_pid}):
                return
            await asyncio.sleep(0.005)


def distribution(values: list[float]) -> dict[str, float | int]:
    """Milliseconds, with linearly interpolated percentiles (small sample, not an SLO)."""
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        if not ordered:
            return 0.0
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return round((ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)) * 1000, 3)

    return {
        "samples": len(ordered),
        "p50": percentile(0.5),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": percentile(1),
    }


@dataclass
class Sample:
    index: int
    method: str
    started: float = 0
    elapsed: float = 0
    status: str = ""
    pool: list[float] = field(default_factory=list)
    guard: list[float] = field(default_factory=list)
    response: httpx.Response | None = field(default=None, repr=False)
    guard_entered: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


ACTIVE: ContextVar[Sample | None] = ContextVar("benchmark_request", default=None)


@contextmanager
def instrument(engine: AsyncEngine) -> Iterator[None]:
    """Test-only wrappers; no production code or SQL lock strategy changes."""
    pool: Any = engine.sync_engine.pool
    original_checkout = pool._do_get

    def checkout() -> Any:
        start = time.perf_counter()
        try:
            return original_checkout()
        finally:
            if (sample := ACTIVE.get()) is not None:
                sample.pool.append(time.perf_counter() - start)

    async def guard(session: AsyncSession, organization: str) -> None:
        sample = ACTIVE.get()
        start = time.perf_counter()
        if sample is not None:
            sample.guard_entered.set()
        try:
            await lock_tenant(session, organization)
        finally:
            if sample is not None:
                sample.guard.append(time.perf_counter() - start)

    with ExitStack() as stack:
        stack.enter_context(patch.object(pool, "_do_get", checkout))
        for module in (kernel, loader, administration, identity_module):
            stack.enter_context(patch.object(module, "lock_tenant", guard))
        yield


def summary(name: str, concurrency: int, samples: list[Sample], elapsed: float) -> dict[str, Any]:
    return {
        "scenario": name,
        "concurrency": concurrency,
        "requests": len(samples),
        "elapsed_seconds": round(elapsed, 4),
        "requests_per_second": round(len(samples) / elapsed, 3),
        "status_counts": dict(sorted(Counter(sample.status for sample in samples).items())),
        "request_latency_ms": distribution([sample.elapsed for sample in samples]),
        "pool_checkout_per_request_ms": distribution([sum(sample.pool) for sample in samples]),
        "organization_guard_per_request_ms": distribution([sum(sample.guard) for sample in samples]),
        "pool_checkout_calls": sum(len(sample.pool) for sample in samples),
        "organization_guard_calls": sum(len(sample.guard) for sample in samples),
    }


async def send(client: httpx.AsyncClient, sample: Sample, item_id: int, key: str) -> Sample:
    active = ACTIVE.set(sample)
    sample.started = time.perf_counter()
    try:
        path = f"/api/v1/work-items/{item_id}"
        if sample.method == "PUT":
            response = await client.put(
                path + "/details",
                headers={"Idempotency-Key": key},
                json={"title": f"Synthetic {key}", "description": "Benchmark fixture", "expected_version": 1},
            )
        else:
            response = await client.get(path)
        sample.response = response
        sample.status = str(response.status_code)
        return sample
    except asyncio.CancelledError:
        sample.status = "cancelled"
        raise
    finally:
        sample.elapsed = time.perf_counter() - sample.started
        ACTIVE.reset(active)


async def batch(
    count: int,
    concurrency: int,
    operation: Callable[[int], Awaitable[Sample]],
    *,
    progress: asyncio.Event | None = None,
) -> tuple[list[Sample], float]:
    pending = iter(range(count))
    samples: list[Sample] = []
    started = time.perf_counter()

    async def worker() -> None:
        for index in pending:
            samples.append(await operation(index))
            if progress is not None and len(samples) >= concurrency:
                progress.set()

    async with asyncio.TaskGroup() as group:
        for _ in range(concurrency):
            group.create_task(worker())
    return samples, time.perf_counter() - started


async def owned_items(sessions: async_sessionmaker[AsyncSession], count: int) -> list[int]:
    async with sessions.begin() as session:
        last = await session.scalar(select(func.max(WorkItemTable.id))) or 0
        ids = list(range(last + 1, last + count + 1))
        for item_id in ids:
            reference = f"work_item:{item_id}"
            session.add(
                WorkItemTable(
                    id=item_id,
                    title="Synthetic benchmark item",
                    lifecycle="open",
                    classification="internal",
                    scope_ref="scope:engineering",
                    version=1,
                )
            )
            session.add(
                AuthorizationResourceTable(
                    organization_ref=ORG,
                    resource_ref=reference,
                    resource_kind="work_item",
                    scope_ref="scope:engineering",
                    status="active",
                    version=1,
                )
            )
        await session.flush()
        for item_id in ids:
            reference = f"work_item:{item_id}"
            session.add(
                RelationTupleTable(
                    subject_ref=PERSON,
                    relation="owner_of",
                    resource_ref=reference,
                    organization_ref=ORG,
                    relation_ref=f"relation:benchmark:{item_id}",
                    membership_ref="membership:alice",
                    subject_type="human",
                    relation_type="owner_of",
                    source_kind="direct_acl",
                    source_ref=reference,
                    source_version=1,
                    valid_from=datetime.now(UTC) - timedelta(hours=1),
                    status="active",
                    version=1,
                )
            )
    return ids


async def state(sessions: async_sessionmaker[AsyncSession]) -> tuple[int, int, int]:
    async with sessions() as session:
        executions = await session.scalar(select(func.count()).select_from(CommandExecutionTable)) or 0
        audits = await session.scalar(select(func.count()).select_from(AuditRecordTable)) or 0
        versions = await session.scalar(select(func.sum(WorkItemTable.version - 1))) or 0
        return executions, audits, versions


async def verify_writes(
    sessions: async_sessionmaker[AsyncSession], before: tuple[int, int, int], successful: int
) -> None:
    require(await state(sessions) == tuple(value + successful for value in before), "write/audit/version mismatch")


@asynccontextmanager
async def harness(
    sessions: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> AsyncIterator[tuple[httpx.AsyncClient, RuntimeHost]]:
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    async with jwt_client(signing_key) as verifier:
        identity = identity_service(sessions, verifier)

        def storage_setup(ctx: ComponentContext) -> None:
            ctx.provide(STORAGE, Storage(engine, sessions))

        def identity_setup(ctx: ComponentContext) -> None:
            ctx.effect(lambda: identity.aclose, label="benchmark.identity")
            ctx.provide(IDENTITY, IdentityFacade(ctx, identity))

        config = Settings(
            env="test",
            database_url=engine.url.render_as_string(hide_password=False),
            redis_url=None,
            redis_required=False,
            command_request_timeout_seconds=10,
        )
        host = RuntimeHost()
        host.load(PluginSpec("benchmark.storage", storage_setup, provides=(STORAGE,)))
        host.load(PluginSpec("benchmark.identity", identity_setup, requires=(STORAGE,), provides=(IDENTITY,)))
        host.load(human_work_plugin(config))
        require((await host.await_ready([WORK], timeout=5)).ready, "runtime startup failed")
        app = create_app(config)
        app.state.runtime = SimpleNamespace(host=host, ready=True)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://benchmark",
                params=QUERY,
                headers={"Authorization": f"Bearer {token(signing_key, claims={'exp': int(time.time()) + 3600})}"},
            ) as client:
                require((await client.get("/api/v1/work-items/1")).status_code == 200, "warm-up failed")
                yield client, host
        finally:
            require((await host.close(timeout=5)).successful, "runtime calls/resources did not drain")


async def edit_baseline(
    client: httpx.AsyncClient, sessions: async_sessionmaker[AsyncSession], count: int, concurrency: int, shared: bool
) -> dict[str, Any]:
    name = "same_object_conflict" if shared else "independent_object_edits"
    ids = await owned_items(sessions, 1 if shared else count)
    before = await state(sessions)

    async def operation(index: int) -> Sample:
        return await send(client, Sample(index, "PUT"), ids[0 if shared else index], f"{name}-{concurrency}-{index}")

    samples, elapsed = await batch(count, concurrency, operation)
    expected_successes = 1 if shared else count
    require(sum(sample.status == "200" for sample in samples) == expected_successes, "unexpected edit success count")
    require(all(sample.status in ({"200", "409"} if shared else {"200"}) for sample in samples), "edit status failed")
    require(
        all(
            sample.response is not None and sample.response.json()["error"]["code"] == "OBJECT_VERSION_CONFLICT"
            for sample in samples
            if sample.status == "409"
        ),
        "same-object conflict did not enforce the expected version",
    )
    await verify_writes(sessions, before, expected_successes)
    async with sessions() as session:
        versions = (await session.scalars(select(WorkItemTable.version).where(WorkItemTable.id.in_(ids)))).all()
        require(len(versions) == len(ids) and all(version == 2 for version in versions), "wrong work item version")
    winner = next(sample for sample in samples if sample.status == "200")
    replay = await operation(winner.index)
    require(replay.response is not None and replay.response.json().get("replayed") is True, "idempotent replay failed")
    require(winner.response is not None, "missing successful response")
    assert replay.response is not None and winner.response is not None
    require(replay.response.json()["execution_id"] == winner.response.json()["execution_id"], "replay executed again")
    await verify_writes(sessions, before, expected_successes)
    result = summary(name, concurrency, samples, elapsed)
    result["checks"] = {"writes_audits_versions_match": True, "replay_does_not_write": True}
    return result


async def cancellation_baseline(
    client: httpx.AsyncClient,
    host: RuntimeHost,
    sessions: async_sessionmaker[AsyncSession],
    engine: AsyncEngine,
    *,
    explicit: bool,
) -> dict[str, Any]:
    item_id = (await owned_items(sessions, 1))[0]
    before = await state(sessions)
    authenticated, proceed = asyncio.Event(), asyncio.Event()
    original = IdentityFacade.authenticate_human
    sample = Sample(0, "PUT")

    async def authenticate(
        self: IdentityFacade, access_token: str, *, profile_ref: str, profile_version: int
    ) -> HumanAuthenticationEvidence:
        evidence = await original(self, access_token, profile_ref=profile_ref, profile_version=profile_version)
        authenticated.set()
        await proceed.wait()
        return evidence

    async with host.call(WORK) as call:
        service = call.require(WORK)
        with (
            patch.object(IdentityFacade, "authenticate_human", authenticate),
            patch.object(service, "_timeout_seconds", 10 if explicit else 0.5),
        ):
            task = asyncio.create_task(send(client, sample, item_id, f"cancel-{explicit}"))
            try:
                await asyncio.wait_for(authenticated.wait(), 5)
                # Authentication has finished and released its locks. Now block the command transaction.
                async with sessions.begin() as holder:
                    await lock_tenant(holder, ORG)
                    holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                    require(isinstance(holder_pid, int), "PostgreSQL holder PID unavailable")
                    sample.guard_entered.clear()
                    proceed.set()
                    await asyncio.wait_for(sample.guard_entered.wait(), 2)
                    await observed_guard_block(holder, holder_pid)
                    if explicit:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                        require(sample.status == "cancelled", "request was not cancelled")
                    else:
                        await asyncio.wait_for(task, 3)
                        require(sample.status == "503", "held guard did not cause bounded timeout")
                    require(engine.sync_engine.pool.checkedout() == 1, "cancelled request leaked a connection")  # type: ignore[attr-defined]
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
    require(engine.sync_engine.pool.checkedout() == 0, "holder connection was not released")  # type: ignore[attr-defined]
    await verify_writes(sessions, before, 0)
    recovered = await send(client, Sample(1, "PUT"), item_id, f"recovery-{explicit}")
    require(recovered.status == "200", "service did not recover after cancellation")
    await verify_writes(sessions, before, 1)
    result = summary("explicit_cancel_at_guard" if explicit else "timeout_at_guard", 1, [sample], sample.elapsed)
    result["checks"] = {
        "postgresql_guard_block_observed": True,
        "cancelled_request_did_not_write": True,
        "connections_released": True,
        "next_write_succeeds": True,
    }
    return result


async def revocation_baseline(
    client: httpx.AsyncClient, sessions: async_sessionmaker[AsyncSession], count: int
) -> dict[str, Any]:
    ids = await owned_items(sessions, count)
    before = await state(sessions)
    progress = asyncio.Event()
    revoked_at = 0.0
    revocation = Sample(-1, "ADMIN")

    async def operation(index: int) -> Sample:
        return await send(client, Sample(index, "GET" if index % 2 else "PUT"), ids[index], f"revocation-{index}")

    async def revoke() -> None:
        nonlocal revoked_at
        await progress.wait()
        active = ACTIVE.set(revocation)
        start = time.perf_counter()
        try:
            result = await revoke_authorization(
                sessions,
                AuthorizationRevocationRequest(
                    environment="test",
                    organization_ref=ORG,
                    kind="assignment",
                    target_ref="assignment:engineering",
                    expected_version=1,
                ),
                actor_ref="benchmark:operator",
                idempotency_key="benchmark-revoke",
            )
            require(result.result["status"] == "revoked", "revocation failed")
            revoked_at = time.perf_counter()
        finally:
            revocation.elapsed = time.perf_counter() - start
            ACTIVE.reset(active)

    async with asyncio.TaskGroup() as group:
        workload = group.create_task(batch(count, 8, operation, progress=progress))
        group.create_task(revoke())
    samples, elapsed = workload.result()
    require(all(sample.status in {"200", "403"} for sample in samples), "unexpected revocation workload status")
    after_revoke = [sample for sample in samples if sample.started >= revoked_at]
    require(all(sample.status == "403" for sample in after_revoke), "revoked assignment authorized a new request")
    writes = sum(sample.method == "PUT" and sample.status == "200" for sample in samples)
    await verify_writes(sessions, before, writes)
    post = [await operation(0), await operation(1)]
    require(all(sample.status == "403" for sample in post), "post-revocation read/write was allowed")
    await verify_writes(sessions, before, writes)
    result = summary("reads_and_edits_during_assignment_revocation", 8, samples, elapsed)
    result["revocation"] = {
        "duration_ms": round(revocation.elapsed * 1000, 3),
        "guard_ms": round(sum(revocation.guard) * 1000, 3),
        "pool_checkout_ms": round(sum(revocation.pool) * 1000, 3),
        "workload_requests_started_after_commit": len(after_revoke),
        "explicit_post_commit_statuses": [sample.status for sample in post],
    }
    result["checks"] = {"writes_audits_versions_match": True, "new_requests_after_revocation_denied": True}
    return result


async def run(url: str, count: int) -> dict[str, Any]:
    engine = create_engine(url, pool_size=5, max_overflow=5, pool_timeout=2)
    sessions = create_session_factory(engine)
    try:
        sources = source_fingerprints()
        async with engine.begin() as connection:
            server_version = str(await connection.scalar(text("SHOW server_version")))
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        await seed_authenticated_command(sessions)
        results = []
        with instrument(engine):
            async with harness(sessions, engine) as (client, host):
                for concurrency in LEVELS:
                    for shared in (False, True):
                        results.append(await edit_baseline(client, sessions, count, concurrency, shared))
                for explicit in (False, True):
                    results.append(await cancellation_baseline(client, host, sessions, engine, explicit=explicit))
                results.append(await revocation_baseline(client, sessions, count))
        require(engine.sync_engine.pool.checkedout() == 0, "resources remained checked out")  # type: ignore[attr-defined]
        require(source_fingerprints() == sources, "source files changed during the benchmark")
        return {
            "schema": "stage.p1b.human-work-baseline.v1",
            "recorded_at": datetime.now(UTC).isoformat(),
            "software": {
                "python": sys.version.split()[0],
                "implementation": sys.implementation.name,
                "postgresql_server_version": server_version,
                "dependencies": {
                    name: version(name)
                    for name in ("SQLAlchemy", "asyncpg", "fastapi", "httpx", "PyJWT", "cryptography", "pydantic")
                },
                "sources": sources,
            },
            "configuration": {
                "database": "PostgreSQL",
                "pool_size": 5,
                "max_overflow": 5,
                "pool_timeout_seconds": 2,
                "command_timeout_seconds": 10,
                "requests_per_level": count,
                "concurrency_levels": list(LEVELS),
                "processes": 1,
                "event_loops": 1,
            },
            "measurement_boundaries": [
                "Synthetic single-person/single-organization data in an explicitly disposable database.",
                "ASGI in-process HTTP with actual identity, authorization, runtime and PG command execution.",
                "Locally signed RSA JWT; mock JWKS; excludes real IdP, TCP/TLS, Redis and frontend performance.",
                "Application tables created from ORM metadata; migration verification is a separate check.",
                "Pool checkout includes queueing/acquisition; guard includes SQL round trips and lock waits.",
                "Wait metrics sum all acquisitions per request, including authentication and command transactions.",
                "Cancellation and timeout require a PG lock wait observed via pg_blocking_pids before completion.",
                "Closed-loop bounded concurrency; small sample percentiles are not a capacity or SLO guarantee.",
                "Replay, state verification, warm-up and synthetic cancellation setup are outside edit throughput.",
            ],
            "results": results,
            "checks": {"all_scenarios_passed": True, "runtime_drained": True, "pool_released": True},
        }
    finally:
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.drop_all)
        finally:
            await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=48, choices=range(40, 81), metavar="40..80")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = asyncio.run(run(database_url(), args.requests))
    except Exception as error:
        # Never echo an asyncpg exception, bearer token, URL or credential-bearing traceback.
        detail = f": {error}" if isinstance(error, BenchmarkCheckFailed) else ""
        print(f"Benchmark failed ({type(error).__name__}){detail}; no passing baseline was produced.", file=sys.stderr)
        raise SystemExit(1) from None
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
