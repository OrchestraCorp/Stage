"""Migration and storage invariants for command results and outbox delivery."""

import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import Engine, Table, create_engine, event, inspect, select, text
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.engine import Dialect
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.schema import CreateTable

from app.models.base import Base
from app.models.execution import (
    AuditRecordTable,
    CommandExecutionTable,
    DeliveryAttemptTable,
    OutboxRecordTable,
    TenantGuardTable,
)

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_REVISION = "6d9e0b2f4a71"
REVISION = "a1c4e7d9b203"
MODELS = (TenantGuardTable, CommandExecutionTable, AuditRecordTable, OutboxRecordTable, DeliveryAttemptTable)
NEW_TABLES = {model.__tablename__ for model in MODELS}
COMMAND_ID = "11111111-1111-4111-8111-111111111111"
MESSAGE_ID = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)


def _migrate(database: Path, *arguments: str, successful: bool = True) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("STAGE_")}
    environment.update(STAGE_DATABASE_URL=f"sqlite+aiosqlite:///{database}", STAGE_ENV="test")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if successful:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0
    return result


def _command(**changes: object) -> dict[str, object]:
    return {
        "execution_id": COMMAND_ID,
        "tenant_ref": "tenant:a",
        "actor_ref": "human:1",
        "effective_subject_ref": "human:1",
        "command_name": "task.claim",
        "command_version": "1",
        "idempotency_key": "request:1",
        "payload_digest": "a" * 64,
        "context_digest": "b" * 64,
        "target_ref": "task:1",
        "expected_version": 0,
        "request_id": "request:1",
        **changes,
    }


def _audit(**changes: object) -> dict[str, object]:
    return {
        "audit_id": str(uuid4()),
        "tenant_ref": "tenant:a",
        "execution_id": COMMAND_ID,
        "actor_ref": "human:1",
        "effective_subject_ref": "human:1",
        "action": "task.claim",
        "target_ref": "task:1",
        "decision_id": "decision:1",
        "policy_release_id": "stage-authz:builtin-v1",
        "request_id": "request:1",
        **changes,
    }


def _outbox(**changes: object) -> dict[str, object]:
    return {
        "message_id": MESSAGE_ID,
        "tenant_ref": "tenant:a",
        "execution_id": COMMAND_ID,
        "topic": "task.claimed",
        "destination": "test-sink",
        "aggregate_ref": "task:1",
        "payload_json": {"task_ref": "task:1"},
        "read_action": "task.view",
        "dedupe_key": "task:1:claimed",
        "available_at": NOW,
        **changes,
    }


def _attempt(**changes: object) -> dict[str, object]:
    return {
        "attempt_id": str(uuid4()),
        "tenant_ref": "tenant:a",
        "message_id": MESSAGE_ID,
        "lease_token": str(uuid4()),
        "worker_ref": "worker:1",
        "started_at": NOW,
        **changes,
    }


def _insert(engine: Engine, model: type[Base], values: dict[str, object]) -> None:
    with engine.begin() as connection:
        connection.execute(cast(Table, model.__table__).insert().values(**values))


@pytest.fixture(scope="module")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("execution-schema") / "template.db"
    _migrate(database, "upgrade", REVISION)
    return database


@pytest.fixture
def storage(migrated_template: Path, tmp_path: Path) -> Iterator[Engine]:
    database = tmp_path / "execution.db"
    shutil.copyfile(migrated_template, database)
    engine = create_engine(f"sqlite:///{database}")

    @event.listens_for(engine, "connect")
    def _foreign_keys(connection: Any, _record: Any) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    try:
        yield engine
    finally:
        engine.dispose()


def test_execution_migration_cycle_preserves_existing_data_and_has_no_model_drift(tmp_path: Path) -> None:
    database = tmp_path / "cycle.db"
    _migrate(database, "upgrade", PREVIOUS_REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO principal (principal_ref, principal_type, status) VALUES (?, ?, ?)",
            ("human:existing", "human", "active"),
        )
    _migrate(database, "upgrade", REVISION)
    _migrate(database, "check")
    _migrate(database, "downgrade", PREVIOUS_REVISION)
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not NEW_TABLES.intersection(tables)
        assert connection.execute("SELECT principal_ref FROM principal").fetchall() == [("human:existing",)]
    _migrate(database, "upgrade", REVISION)
    _migrate(database, "check")


@pytest.mark.parametrize("retained_data", ("guard", "delivery_history"))
def test_downgrade_refuses_retained_execution_data_without_removing_tables(storage: Engine, retained_data: str) -> None:
    if retained_data == "guard":
        _insert(storage, TenantGuardTable, {"tenant_ref": "tenant:a"})
    else:
        _insert(storage, CommandExecutionTable, _command())
        _insert(storage, AuditRecordTable, _audit())
        _insert(storage, OutboxRecordTable, _outbox())
        _insert(storage, DeliveryAttemptTable, _attempt())
    database = Path(cast(str, storage.url.database))
    storage.dispose()
    result = _migrate(database, "downgrade", PREVIOUS_REVISION, successful=False)
    assert "Downgrade requires empty execution tables" in result.stderr
    with storage.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == REVISION
        assert NEW_TABLES <= set(inspect(connection).get_table_names())
        retained_table = "execution_tenant_guard" if retained_data == "guard" else "outbox_delivery_attempt"
        assert connection.execute(text(f"SELECT count(*) FROM {retained_table}")).scalar_one() == 1


def test_idempotency_scope_does_not_deduplicate_different_actors_tenants_or_command_versions(storage: Engine) -> None:
    _insert(storage, CommandExecutionTable, _command())
    with pytest.raises(IntegrityError):
        _insert(storage, CommandExecutionTable, _command(execution_id=str(uuid4()), payload_digest="c" * 64))
    for changed in (
        {"tenant_ref": "tenant:b"},
        {"actor_ref": "human:2"},
        {"command_name": "task.submit"},
        {"command_version": "2"},
        {"idempotency_key": "request:2"},
    ):
        _insert(storage, CommandExecutionTable, _command(execution_id=str(uuid4()), **changed))
    with storage.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM command_execution")).scalar_one() == 6


@pytest.mark.parametrize("model", (AuditRecordTable, OutboxRecordTable, DeliveryAttemptTable))
def test_composite_references_reject_cross_tenant_links(storage: Engine, model: type[Base]) -> None:
    _insert(storage, CommandExecutionTable, _command())
    if model is DeliveryAttemptTable:
        _insert(storage, OutboxRecordTable, _outbox())
        values = _attempt(tenant_ref="tenant:b")
    elif model is AuditRecordTable:
        values = _audit(tenant_ref="tenant:b")
    else:
        values = _outbox(tenant_ref="tenant:b")
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        _insert(storage, model, values)
    values["tenant_ref"] = "tenant:a"
    _insert(storage, model, values)


def test_a_command_has_only_one_primary_audit_record(storage: Engine) -> None:
    _insert(storage, CommandExecutionTable, _command())
    _insert(storage, AuditRecordTable, _audit())
    with pytest.raises(IntegrityError):
        _insert(storage, AuditRecordTable, _audit())


def test_outbox_dedupe_is_scoped_to_tenant_and_destination(storage: Engine) -> None:
    _insert(storage, CommandExecutionTable, _command())
    _insert(storage, OutboxRecordTable, _outbox())
    with pytest.raises(IntegrityError):
        _insert(storage, OutboxRecordTable, _outbox(message_id=str(uuid4())))
    _insert(storage, OutboxRecordTable, _outbox(message_id=str(uuid4()), destination="other-sink"))
    other_execution = str(uuid4())
    _insert(storage, CommandExecutionTable, _command(execution_id=other_execution, tenant_ref="tenant:b"))
    _insert(
        storage,
        OutboxRecordTable,
        _outbox(message_id=str(uuid4()), execution_id=other_execution, tenant_ref="tenant:b"),
    )


def test_parent_deletion_cannot_silently_remove_audit_or_delivery_history(storage: Engine) -> None:
    _insert(storage, CommandExecutionTable, _command())
    _insert(storage, AuditRecordTable, _audit())
    _insert(storage, OutboxRecordTable, _outbox())
    _insert(storage, DeliveryAttemptTable, _attempt())
    for model in (CommandExecutionTable, OutboxRecordTable):
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            with storage.begin() as connection:
                connection.execute(cast(Table, model.__table__).delete())
    with storage.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM execution_audit")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM outbox_delivery_attempt")).scalar_one() == 1


@pytest.mark.parametrize(
    ("model", "changes"),
    (
        (TenantGuardTable, {"epoch": -1}),
        (TenantGuardTable, {"tenant_ref": " "}),
        (CommandExecutionTable, {"expected_version": -1}),
        (CommandExecutionTable, {"status": "Succeeded"}),
        (CommandExecutionTable, {"payload_digest": "short"}),
        (CommandExecutionTable, {"context_digest": "short"}),
        (CommandExecutionTable, {"actor_ref": " "}),
        (AuditRecordTable, {"decision_id": " "}),
        (OutboxRecordTable, {"status": "complete"}),
        (OutboxRecordTable, {"attempt_count": -1}),
        (OutboxRecordTable, {"payload_json": None}),
        (DeliveryAttemptTable, {"status": "pending"}),
        (DeliveryAttemptTable, {"lease_token": " "}),
        (DeliveryAttemptTable, {"operation": "retry"}),
    ),
)
def test_invalid_execution_storage_is_rejected(storage: Engine, model: type[Base], changes: dict[str, object]) -> None:
    if model is TenantGuardTable:
        values = {"tenant_ref": "tenant:a", **changes}
    elif model is CommandExecutionTable:
        values = _command(**changes)
    else:
        _insert(storage, CommandExecutionTable, _command())
        if model is AuditRecordTable:
            values = _audit(**changes)
        elif model is OutboxRecordTable:
            values = _outbox(**changes)
        else:
            _insert(storage, OutboxRecordTable, _outbox())
            values = _attempt(**changes)
    with pytest.raises(IntegrityError):
        _insert(storage, model, values)


def test_json_nullability_utc_storage_and_database_defaults(storage: Engine) -> None:
    local_time = datetime(2026, 10, 24, 17, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8)))
    _insert(storage, TenantGuardTable, {"tenant_ref": "tenant:a"})
    _insert(storage, CommandExecutionTable, _command(result_json=None))
    _insert(storage, AuditRecordTable, _audit(before_json=None, after_json={"version": 2}))
    _insert(storage, OutboxRecordTable, _outbox(available_at=local_time))
    _insert(storage, DeliveryAttemptTable, _attempt(started_at=local_time))
    with storage.connect() as connection:
        command = connection.execute(select(CommandExecutionTable.__table__)).one()
        assert command.result_json is None
        assert command.status == "running"
        assert command.created_at.tzinfo == UTC
        assert connection.execute(text("SELECT result_json IS NULL FROM command_execution")).scalar_one() == 1
        audit = connection.execute(select(AuditRecordTable.__table__)).one()
        assert audit.before_json is None
        assert audit.after_json == {"version": 2}
        outbox = connection.execute(select(OutboxRecordTable.__table__)).one()
        assert outbox.available_at == local_time.astimezone(UTC)
        assert outbox.payload_json == {"task_ref": "task:1"}
        assert outbox.status == "pending"
        assert outbox.attempt_count == 0
        assert connection.execute(select(TenantGuardTable.epoch)).scalar_one() == 0
        attempt = connection.execute(select(DeliveryAttemptTable.__table__)).one()
        assert attempt.started_at == local_time.astimezone(UTC)
        assert attempt.status == "delivering"
        assert attempt.operation == "send"
    with pytest.raises(StatementError, match="timezone"):
        _insert(storage, OutboxRecordTable, _outbox(message_id=str(uuid4()), available_at=datetime(2026, 10, 24)))


def test_idempotency_keys_keep_case_accent_and_trailing_space_distinct(storage: Engine) -> None:
    for key in ("Key:a", "key:a", "kéy:a", "key:a "):
        _insert(storage, CommandExecutionTable, _command(execution_id=str(uuid4()), idempotency_key=key))
    with storage.connect() as connection:
        for key in ("Key:a", "key:a", "kéy:a", "key:a "):
            rows = connection.execute(
                select(CommandExecutionTable.execution_id).where(CommandExecutionTable.idempotency_key == key)
            ).all()
            assert len(rows) == 1


@pytest.mark.parametrize(
    "dialect", (mysql.dialect(), postgresql.dialect(), sqlite.dialect()), ids=lambda value: value.name
)
def test_execution_ddl_preserves_portable_exact_keys_utc_and_composite_foreign_keys(dialect: Dialect) -> None:
    ddl = "\n".join(str(CreateTable(cast(Table, model.__table__)).compile(dialect=dialect)) for model in MODELS)
    if dialect.name == "mysql":
        assert "utf8mb4_0900_bin" in ddl
        assert "DATETIME(6)" in ddl
        assert "UTC_TIMESTAMP(6)" in ddl
    elif dialect.name == "postgresql":
        assert 'COLLATE "C"' in ddl
        assert "TIMESTAMP WITH TIME ZONE" in ddl
    else:
        assert 'COLLATE "BINARY"' in ddl
        assert "STRFTIME" in ddl
    assert "FOREIGN KEY(tenant_ref, execution_id) REFERENCES command_execution (tenant_ref, execution_id)" in ddl
    assert "FOREIGN KEY(tenant_ref, message_id) REFERENCES execution_outbox (tenant_ref, message_id)" in ddl
