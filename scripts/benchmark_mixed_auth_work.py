"""Service-level native OAuth/work baseline in an isolated stage_test_capacity* PG database.

The command resets ONLY the explicitly named capacity database, applies migrations,
then runs synthetic users through production service methods. Mixed request curves
exclude password work; a separate login burst includes real Argon2/TOTP and PKCE.
Neither measures HTTP, frontend or external IdP latency. Never use stage_test_core.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pyotp  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from sqlalchemy import event, func, select, text  # noqa: E402
from sqlalchemy.engine import Connection, make_url  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker  # noqa: E402
from sqlalchemy.orm import Session, SessionTransaction  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.core.db import create_engine, create_session_factory  # noqa: E402
from app.features.work.details import WorkDetailsInput  # noqa: E402
from app.identity.native import NATIVE_PROFILE_SCHEMA  # noqa: E402
from app.identity.service import IdentityService, content_digest, profile_content  # noqa: E402
from app.models.authorization import AuthorizationResourceTable, RelationTupleTable, RoleAssignmentTable  # noqa: E402
from app.models.execution import AuditRecordTable, CommandExecutionTable  # noqa: E402
from app.models.identity import PrincipalTable  # noqa: E402
from app.models.identity_foundation import (  # noqa: E402
    AuthenticationProfileTable,
    ExternalIdentityTable,
    IdentityProviderTable,
    OrganizationHumanAuthenticationPolicyTable,
    OrganizationMembershipTable,
)
from app.models.native_auth import (  # noqa: E402
    NativeAccountTable,
    NativeClientTable,
    NativeLoginRequestTable,
    NativeLoginThrottleTable,
)
from app.models.work import WorkItemTable  # noqa: E402
from app.native_auth.browser import BrowserAuthorization  # noqa: E402
from app.native_auth.config import NativeAuthConfig  # noqa: E402
from app.native_auth.credentials import NativeCredentials  # noqa: E402
from app.native_auth.crypto import digest_secret, hash_password, random_secret  # noqa: E402
from app.native_auth.keys import KeyRing  # noqa: E402
from app.native_auth.oauth import NativeOAuth  # noqa: E402
from app.native_auth.protocol import MFA_ACR, MFA_AMR  # noqa: E402
from app.providers.identity import IDENTITY, IdentityFacade  # noqa: E402
from app.providers.storage import STORAGE, Storage  # noqa: E402
from app.providers.work import WORK, human_work_plugin  # noqa: E402
from app.runtime import ComponentContext, PluginSpec, RuntimeHost  # noqa: E402
from scripts.check_database import validate_test_database  # noqa: E402
from tests.support.authenticated_commands import seed_authenticated_command  # noqa: E402
from tests.support.authorization import ORG  # noqa: E402
from tests.support.native_oauth import REDIRECT, VERIFIER, authorize_values  # noqa: E402

LEVELS = (1, 4, 8, 16)
OPERATIONS = ("userinfo", "refresh", "work_read", "work_edit")


@dataclass
class Sample:
    operation: str
    elapsed: float = 0
    error: str = ""
    sql: int = 0
    guard_queries: int = 0
    guard_acquire: list[float] = field(default_factory=list)
    guard_hold: list[float] = field(default_factory=list)


ACTIVE: ContextVar[Sample | None] = ContextVar("mixed_capacity_sample", default=None)


class DatabaseMeasurements:
    """Observe dispatch/acquisition and transaction completion without changing locks.

    Acquisition includes SQL execution/round-trip, not just server lock queueing.
    Holding starts at the first returned guard statement and ends after the root
    SQLAlchemy transaction ends (including commit/rollback and connection return).
    Reentrant guard statements do not restart that clock or double-count a hold.
    """

    def __init__(self, engine: AsyncEngine, clock: Callable[[], float] = time.perf_counter) -> None:
        self.engine, self.clock = engine, clock
        self.connections: dict[Connection, SessionTransaction] = {}
        self.holds: dict[SessionTransaction, tuple[Sample, float]] = {}

    def acquired(self, transaction: SessionTransaction, sample: Sample, started: float, completed: float) -> None:
        if transaction not in self.holds:
            sample.guard_acquire.append(completed - started)
            self.holds[transaction] = (sample, completed)

    def finished(self, transaction: SessionTransaction, completed: float) -> None:
        held = self.holds.pop(transaction, None)
        if held is not None:
            sample, acquired = held
            sample.guard_hold.append(completed - acquired)
        self.connections = {
            connection: root for connection, root in self.connections.items() if root is not transaction
        }

    def after_begin(self, session: Session, transaction: SessionTransaction, connection: Connection) -> None:
        if connection.engine is self.engine.sync_engine and transaction.parent is None:
            self.connections[connection] = transaction

    def after_end(self, session: Session, transaction: SessionTransaction) -> None:
        if transaction.parent is None:
            self.finished(transaction, self.clock())

    def before_sql(self, connection: Connection, cursor: Any, sql: str, params: Any, context: Any, many: bool) -> None:
        sample = ACTIVE.get()
        context.capacity_started = None
        if sample is None:
            return
        sample.sql += 1
        if "execution_tenant_guard" in sql and (
            "FOR UPDATE" in sql.upper() or sql.lstrip().upper().startswith("UPDATE")
        ):
            sample.guard_queries += 1
            context.capacity_started = (sample, self.clock())

    def after_sql(self, connection: Connection, cursor: Any, sql: str, params: Any, context: Any, many: bool) -> None:
        started = context.capacity_started
        transaction = self.connections.get(connection)
        if started is not None and transaction is not None:
            self.acquired(transaction, started[0], started[1], self.clock())

    def __enter__(self) -> DatabaseMeasurements:
        event.listen(Session, "after_begin", self.after_begin)
        event.listen(Session, "after_transaction_end", self.after_end)
        event.listen(self.engine.sync_engine, "before_cursor_execute", self.before_sql)
        event.listen(self.engine.sync_engine, "after_cursor_execute", self.after_sql)
        return self

    def __exit__(self, *args: Any) -> None:
        event.remove(Session, "after_begin", self.after_begin)
        event.remove(Session, "after_transaction_end", self.after_end)
        event.remove(self.engine.sync_engine, "before_cursor_execute", self.before_sql)
        event.remove(self.engine.sync_engine, "after_cursor_execute", self.after_sql)
        if self.holds or self.connections:
            raise RuntimeError("measurement transactions did not finish")


def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "")
    validate_capacity_database(url)
    return url


def validate_capacity_database(url: str) -> None:
    validate_test_database(url, os.environ.get("STAGE_ALLOW_TEST_DATABASE_RESET"))
    parsed = make_url(url)
    if parsed.drivername != "postgresql+asyncpg" or not re.fullmatch(
        r"stage_test_capacity(?:_[a-z0-9]+)?", parsed.database or ""
    ):
        raise ValueError("Only a separate PostgreSQL stage_test_capacity* database is allowed")


def source_digest() -> str:
    digest = hashlib.sha256()
    paths = [
        *sorted((ROOT / "app").rglob("*.py")),
        *sorted((ROOT / "tests/support").glob("*.py")),
        Path(__file__),
        ROOT / "uv.lock",
    ]
    for path in paths:
        digest.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def distribution(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        if not ordered:
            return 0
        position = (len(ordered) - 1) * fraction
        low = int(position)
        return round(
            1000 * (ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (position - low)), 3
        )

    return {"samples": len(values), "p50": percentile(0.5), "p95": percentile(0.95), "max": percentile(1)}


@dataclass
class Actor:
    index: int
    tokens: dict[str, Any] = field(repr=False)
    password: str = field(repr=False)
    totp_secret: str = field(repr=False)
    version: int = 1

    @property
    def account(self) -> str:
        return f"capacity-account:{self.index}"

    @property
    def item(self) -> int:
        return 100 + self.index

    @property
    def assignment(self) -> str:
        return f"capacity-assignment:{self.index}"


async def seed(
    sessions: async_sessionmaker[AsyncSession], keyring: KeyRing, users: int
) -> tuple[NativeOAuth, list[Actor]]:
    await seed_authenticated_command(sessions)
    config = NativeAuthConfig(
        issuer="http://127.0.0.1:8080", organization_ref=ORG, trust_ref="trust", environment="test"
    )
    credentials = NativeCredentials(
        sessions,
        fernet_key=Fernet.generate_key(),
        organization_ref=ORG,
        trust_ref="trust",
        peer_attempt_limit=config.peer_attempt_limit,
        authorization_attempt_limit=config.authorization_attempt_limit,
        rate_window_seconds=config.rate_window_seconds,
    )
    password = random_secret()
    password_hash = await hash_password(password)
    secrets = [credentials.cipher.new_secret() for _ in range(users)]
    valid_from = datetime.now(UTC) - timedelta(hours=1)
    async with sessions.begin() as session:
        profile = await session.get(AuthenticationProfileTable, ("local-human", 1))
        provider = await session.get(IdentityProviderTable, "provider")
        assert profile is not None and provider is not None
        provider.issuer_exact = config.issuer
        profile.config_schema, profile.subject_namespace = NATIVE_PROFILE_SCHEMA, "stage-native-human-v1"
        profile.config_json = {**profile.config_json, "jwks_url": config.issuer + "/oauth/jwks"}
        profile.digest = content_digest(profile_content(profile))
        session.add(
            NativeClientTable(
                client_id="maestro", redirect_uris=[REDIRECT], allowed_scopes=["openid", "profile", "offline_access"]
            )
        )
        session.add(
            OrganizationHumanAuthenticationPolicyTable(
                organization_ref=ORG,
                mode="required",
                version=1,
                max_auth_age_seconds=900,
                rules_json=[
                    {
                        "profile_ref": "local-human",
                        "profile_version": 1,
                        "required_acr": MFA_ACR,
                        "required_amr": MFA_AMR,
                    }
                ],
            )
        )
        for index in range(users):
            session.add(PrincipalTable(principal_ref=f"capacity-person:{index}", principal_type="human"))
        await session.flush()
        for index in range(users):
            person, member, account = (
                f"capacity-person:{index}",
                f"capacity-member:{index}",
                f"capacity-account:{index}",
            )
            session.add(
                OrganizationMembershipTable(
                    organization_ref=ORG,
                    membership_ref=member,
                    principal_ref=person,
                    principal_type="human",
                    source_ref="synthetic-capacity",
                    valid_from=valid_from,
                )
            )
            session.add(
                NativeAccountTable(
                    account_ref=account,
                    principal_ref=person,
                    username=f"capacity-{index}",
                    password_hash=password_hash,
                    totp_secret=credentials.cipher.encrypt(secrets[index]),
                    totp_bootstrap_allowed=False,
                )
            )
            session.add(
                ExternalIdentityTable(
                    external_identity_ref=f"capacity-identity:{index}",
                    provider_ref="provider",
                    subject_namespace="stage-native-human-v1",
                    subject_exact=account,
                    principal_ref=person,
                    principal_type="human",
                    link_evidence_ref="synthetic-capacity",
                )
            )
            item = 100 + index
            session.add(
                WorkItemTable(
                    id=item,
                    title="Synthetic capacity",
                    lifecycle="open",
                    classification="internal",
                    scope_ref="scope:engineering",
                    version=1,
                )
            )
            session.add(
                AuthorizationResourceTable(
                    organization_ref=ORG,
                    resource_ref=f"work_item:{item}",
                    resource_kind="work_item",
                    scope_ref="scope:engineering",
                )
            )
        await session.flush()
        for index in range(users):
            person, member = f"capacity-person:{index}", f"capacity-member:{index}"
            session.add(
                RoleAssignmentTable(
                    principal_ref=person,
                    principal_type="human",
                    role_code="department-owner",
                    role_ref="role:engineering",
                    role_revision=1,
                    scope_ref="scope:engineering",
                    organization_ref=ORG,
                    assignment_ref=f"capacity-assignment:{index}",
                    membership_ref=member,
                    grant_source_ref="synthetic-capacity",
                    valid_from=valid_from,
                )
            )
            session.add(
                RelationTupleTable(
                    subject_ref=person,
                    subject_type="human",
                    relation="owner_of",
                    relation_type="owner_of",
                    resource_ref=f"work_item:{100 + index}",
                    organization_ref=ORG,
                    relation_ref=f"capacity-relation:{index}",
                    membership_ref=member,
                    source_kind="direct_acl",
                    source_ref=f"work_item:{100 + index}",
                    source_version=1,
                    valid_from=valid_from,
                )
            )
    oauth = NativeOAuth(sessions, config, keyring, credentials=credentials)
    actors = []
    for index in range(users):
        request = await oauth.prepare_authorization(
            **{**authorize_values(), "client_id": "maestro", "scope": "openid profile offline_access"}
        )
        async with sessions.begin() as session:
            row = await session.get(NativeLoginRequestTable, request.request_ref)
            assert row is not None
            row.account_ref, row.credential_version = f"capacity-account:{index}", 1
            row.password_verified_at, row.status, row.consent_granted = datetime.now(UTC), "authenticated", True
        response = await oauth.finish_authorization_response(
            request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
        )
        tokens = await oauth.exchange_code(
            client_id="maestro", redirect_uri=REDIRECT, code=response.parameters["code"], code_verifier=VERIFIER
        )
        actors.append(Actor(index, tokens, password, secrets[index]))
    return oauth, actors


@asynccontextmanager
async def harness(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession], keyring: KeyRing
) -> AsyncIterator[RuntimeHost]:
    identity = IdentityService(
        sessions, environment="test", native_keyring=keyring, native_issuer="http://127.0.0.1:8080"
    )

    def storage(ctx: ComponentContext) -> None:
        ctx.provide(STORAGE, Storage(engine, sessions))

    def identities(ctx: ComponentContext) -> None:
        ctx.effect(lambda: identity.aclose, label="capacity.identity")
        ctx.provide(IDENTITY, IdentityFacade(ctx, identity))

    settings = Settings(
        env="test",
        database_url=engine.url.render_as_string(hide_password=False),
        redis_url=None,
        redis_required=False,
        command_request_timeout_seconds=15,
    )
    host = RuntimeHost()
    host.load(PluginSpec("capacity.storage", storage, provides=(STORAGE,)))
    host.load(PluginSpec("capacity.identity", identities, requires=(STORAGE,), provides=(IDENTITY,)))
    host.load(human_work_plugin(settings))
    try:
        if not (await host.await_ready((WORK,))).ready:
            raise RuntimeError("capacity runtime startup failed")
        yield host
    finally:
        if not (await host.close()).successful:
            raise RuntimeError("capacity runtime did not drain")


async def operate(host: RuntimeHost, oauth: NativeOAuth, actor: Actor, operation: str, key: str) -> Sample:
    sample = Sample(operation)
    active, started = ACTIVE.set(sample), time.perf_counter()
    try:
        if operation == "userinfo":
            result = await oauth.userinfo(actor.tokens["access_token"])
            assert result == {"sub": actor.account, "preferred_username": f"capacity-{actor.index}"}
        elif operation == "refresh":
            actor.tokens = await oauth.exchange_refresh(
                client_id="maestro", refresh_token=actor.tokens["refresh_token"]
            )
        else:
            async with host.call(WORK) as call:
                service = call.require(WORK)
                values = dict(
                    work_item_id=actor.item,
                    profile_ref="local-human",
                    profile_version=1,
                    assignment_ref=actor.assignment,
                    request_id=key,
                )
                if operation == "work_read":
                    result = await service.get_item(actor.tokens["access_token"], **values)
                    assert result.version == actor.version
                else:
                    result = await service.update_details(
                        actor.tokens["access_token"],
                        **values,
                        idempotency_key=key,
                        expected_version=actor.version,
                        details=WorkDetailsInput(title=f"Synthetic {key}", description="Capacity only"),
                    )
                    assert result.result["version"] == actor.version + 1
                    actor.version += 1
    except Exception as error:
        sample.error = type(error).__name__  # No exception messages, tokens or connection strings in artifacts.
    finally:
        sample.elapsed = time.perf_counter() - started
        ACTIVE.reset(active)
    return sample


def summarize(samples: list[Sample], elapsed: float) -> dict[str, Any]:
    """Per-operation throughput is its contribution within the same mixed window."""
    return {
        "operations": len(samples),
        "throughput_per_second": round(len(samples) / elapsed, 3),
        "errors": dict(Counter(sample.error for sample in samples if sample.error)),
        "latency_ms": distribution([sample.elapsed for sample in samples]),
        "sql_statements": {
            "total": sum(sample.sql for sample in samples),
            "mean": round(sum(sample.sql for sample in samples) / len(samples), 3),
        },
        "guard_queries": sum(sample.guard_queries for sample in samples),
        "guard_transactions": sum(len(sample.guard_hold) for sample in samples),
        "guard_acquire_ms_per_operation": distribution([sum(sample.guard_acquire) for sample in samples]),
        "guard_hold_ms_per_operation": distribution([sum(sample.guard_hold) for sample in samples]),
        "guard_acquire_ms_per_transaction": distribution(
            [value for sample in samples for value in sample.guard_acquire]
        ),
        "guard_hold_ms_per_transaction": distribution([value for sample in samples for value in sample.guard_hold]),
    }


async def workload(
    host: RuntimeHost, oauth: NativeOAuth, actors: list[Actor], concurrency: int, count: int
) -> dict[str, Any]:
    samples: list[Sample] = []
    started = time.perf_counter()

    async def worker(worker_id: int) -> None:
        actor = actors[worker_id]
        for index in range(count // concurrency):
            operation = OPERATIONS[(index + worker_id) % len(OPERATIONS)]
            samples.append(await operate(host, oauth, actor, operation, f"capacity-{concurrency}-{worker_id}-{index}"))

    async with asyncio.TaskGroup() as group:
        for worker_id in range(concurrency):
            group.create_task(worker(worker_id))
    elapsed = time.perf_counter() - started
    return {
        "concurrency": concurrency,
        "elapsed_seconds": round(elapsed, 3),
        "all": summarize(samples, elapsed),
        "by_operation": {
            operation: summarize([sample for sample in samples if sample.operation == operation], elapsed)
            for operation in OPERATIONS
        },
    }


async def login_burst(oauth: NativeOAuth, actors: list[Actor]) -> dict[str, Any]:
    """One real password/TOTP ceremony per person from one synthetic transport peer."""
    browser = BrowserAuthorization(oauth.sessions, oauth.config, credentials=oauth.credentials, oauth=oauth)
    stages: dict[str, list[float]] = {name: [] for name in ("authorize", "password", "totp", "finish", "token")}
    peer = "198.51.100.23"

    async def login(actor: Actor) -> Sample:
        sample = Sample("password_totp_login")
        active, started = ACTIVE.set(sample), time.perf_counter()
        last = started

        def mark(stage: str) -> None:
            nonlocal last
            now = time.perf_counter()
            stages[stage].append(now - last)
            last = now

        try:
            await oauth.credentials.throttle_authorization(peer)
            request = await oauth.prepare_authorization(
                **{**authorize_values(), "client_id": "maestro", "scope": "openid profile"}
            )
            proof = (request.request_ref, request.browser_secret, request.csrf_token)
            mark("authorize")
            progress = await oauth.credentials.password(
                *proof, f"capacity-{actor.index}", actor.password, client_address=peer
            )
            assert progress.status == "mfa"
            mark("password")
            progress = await oauth.credentials.otp(*proof, pyotp.TOTP(actor.totp_secret).now(), client_address=peer)
            assert progress.status == "authenticated"
            mark("totp")
            assert (await browser.advance(*proof)).kind == "complete"
            response = await oauth.finish_authorization_response(
                request_ref=request.request_ref, browser_secret=request.browser_secret, csrf_token=request.csrf_token
            )
            mark("finish")
            tokens = await oauth.exchange_code(
                client_id="maestro", redirect_uri=REDIRECT, code=response.parameters["code"], code_verifier=VERIFIER
            )
            assert tokens["access_token"] and tokens["id_token"]
            mark("token")
        except Exception as error:
            sample.error = type(error).__name__
        finally:
            sample.elapsed = time.perf_counter() - started
            ACTIVE.reset(active)
        return sample

    started = time.perf_counter()
    samples = await asyncio.gather(*(login(actor) for actor in actors))
    result = summarize(samples, time.perf_counter() - started)
    async with oauth.sessions() as session:
        factor = await session.get(NativeLoginThrottleTable, digest_secret("native-login-peer:" + peer))
        authorization = await session.get(
            NativeLoginThrottleTable, digest_secret("native-login-peer:authorization:" + peer)
        )
        assert factor is not None and authorization is not None
        assert factor.failures == len(actors) * 2 and authorization.failures == len(actors)
        assert factor.blocked_until is None and authorization.blocked_until is None
        result["observed_factor_budget_consumed"] = factor.failures
        result["observed_authorization_budget_consumed"] = authorization.failures
    result["stages_ms"] = {name: distribution(values) for name, values in stages.items()}
    result["same_peer"] = True
    result["factor_attempt_limit"] = oauth.credentials.peer_attempt_limit
    result["authorization_attempt_limit"] = oauth.credentials.authorization_attempt_limit
    result["rate_window_seconds"] = oauth.credentials.rate_window_seconds
    result["factor_attempts"] = len(actors) * 2
    result["otp_reused"] = False
    return result


async def run(url: str, count: int, environment_note: str) -> dict[str, Any]:
    engine = create_engine(url, pool_size=16, max_overflow=0, pool_timeout=15)
    sessions = create_session_factory(engine)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    keyring = KeyRing("capacity-key", [{"kid": "capacity-key", "private_pem": pem}])
    fingerprint = source_digest()
    load_before = os.getloadavg()
    try:
        async with sessions() as session:
            server = await session.scalar(text("SHOW server_version"))
            migration = await session.scalar(text("SELECT version_num FROM alembic_version"))
        oauth, actors = await seed(sessions, keyring, max(LEVELS))
        with DatabaseMeasurements(engine):
            async with harness(engine, sessions, keyring) as host:
                for actor in actors:
                    sample = await operate(host, oauth, actor, "work_read", f"warm-{actor.index}")
                    if sample.error:
                        raise RuntimeError("capacity warmup failed: " + sample.error)
                results = []
                for concurrency in LEVELS:
                    result = await workload(host, oauth, actors, concurrency, count)
                    results.append(result)
                    print(
                        json.dumps(
                            {
                                "concurrency": concurrency,
                                "throughput": result["all"]["throughput_per_second"],
                                "errors": result["all"]["errors"],
                            }
                        ),
                        flush=True,
                    )
                login_result = await login_burst(oauth, actors)
        async with sessions() as session:
            commands = await session.scalar(select(func.count()).select_from(CommandExecutionTable))
            audits = await session.scalar(select(func.count()).select_from(AuditRecordTable))
            versions = await session.scalar(select(func.sum(WorkItemTable.version - 1)))
        expected = count * len(LEVELS) // len(OPERATIONS)
        if (
            login_result["errors"]
            or (commands, audits, versions) != (expected, expected, expected)
            or any(row["all"]["errors"] for row in results)
        ):
            raise RuntimeError("capacity operation/state accounting failed")
        if fingerprint != source_digest():
            raise RuntimeError("source changed during capacity run")
        return {
            "schema": "stage.mixed-native-work-capacity.v1",
            "recorded_at": datetime.now(UTC).isoformat(),
            "source_sha256": fingerprint,
            "postgresql": server,
            "migration_head": migration,
            "configuration": {
                "database_name": make_url(url).database,
                "users": len(actors),
                "sessions": len(actors),
                "scope": "openid profile offline_access",
                "organization_mfa": "required",
                "processes": 1,
                "event_loops": 1,
                "pool_size": 16,
                "rsa_bits": 3072,
                "max_overflow": 0,
                "requests_per_level": count,
                "concurrency_levels": LEVELS,
                "available_cpus": len(os.sched_getaffinity(0)),
                "cgroup_cpu_max": Path("/sys/fs/cgroup/cpu.max").read_text().strip()
                if Path("/sys/fs/cgroup/cpu.max").exists()
                else None,
                "load_average_before": load_before,
                "load_average_after": os.getloadavg(),
                "environment_note": environment_note,
            },
            "boundaries": [
                "Service methods and runtime leases; real PostgreSQL TCP; no HTTP/TLS/frontend.",
                "Synthetic completed MFA fixture; actual PKCE, JWT verification, refresh and current authorization.",
                "Separate shared-peer login burst includes Argon2/TOTP, browser sessions and PKCE issuance.",
                "One person/session/item per worker; equal mix, no intentional object conflict or refresh reuse.",
                "Per-operation throughput is its contribution to the common mixed window, not an isolated capacity.",
                "Guard acquisition includes SQL/driver/round-trip; an upper bound for server lock waiting.",
                "Guard hold starts after acquisition until transaction end, including commit/rollback and pool return.",
                "SQL counts include ORM/cursor statements, excluding driver transaction-control and pre-ping traffic.",
                "Closed-loop warm service baseline; no production SLO, arrival model or multi-process capacity claim.",
            ],
            "results": results,
            "login_burst": login_result,
            "checks": {
                "commands_audits_versions": expected,
                "all_operations_succeeded": True,
                "runtime_drained": True,
                "source_stable": True,
            },
        }
    finally:
        await engine.dispose()


async def reset(url: str) -> None:
    validate_capacity_database(url)
    engine = create_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=256)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--environment-note", default="Developer machine; background load not controlled.")
    args = parser.parse_args()
    if args.count < 64 or args.count % 64:
        raise ValueError("count must be a positive multiple of 64 (at least 64)")
    url = database_url()
    asyncio.run(reset(url))
    environment = dict(os.environ, STAGE_DATABASE_URL=url, STAGE_ENV="test", STAGE_NATIVE_AUTH_ENABLED="false")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=environment, capture_output=True
    )
    if result.returncode:
        raise RuntimeError("isolated capacity migration failed")
    report = asyncio.run(run(url, args.count, args.environment_note))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
