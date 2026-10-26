import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from typing import cast
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import ForeignKeyConstraint, MetaData, Table, select
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.engine import Dialect, make_url
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.schema import CreateTable, conv

from app.core.db import create_engine
from app.models.authorization import RelationTupleTable, RoleAssignmentTable
from app.models.base import Base
from app.models.identity import PrincipalTable
from app.models.work import WorkItemTable
from app.storage.types import ExactString, UTCDateTime

# Retain historical MySQL DDL coverage without accepting MySQL runtime URLs.
DIALECTS = (mysql.dialect(), postgresql.dialect(), sqlite.dialect())
DATABASE_URLS = ["sqlite+aiosqlite:///:memory:"]
if os.environ.get("TEST_DATABASE_URL"):
    DATABASE_URLS.append(os.environ["TEST_DATABASE_URL"])


@pytest.mark.parametrize("dialect", DIALECTS, ids=lambda value: value.name)
def test_ddl_declares_exact_comparison_and_timestamp_storage(dialect: Dialect) -> None:
    ddl = str(CreateTable(cast(Table, PrincipalTable.__table__)).compile(dialect=dialect))
    if dialect.name == "mysql":
        assert "DATETIME(6)" in ddl
        assert "utf8mb4_0900_bin" in ddl
        assert "UTC_TIMESTAMP(6)" in ddl
    elif dialect.name == "postgresql":
        assert "TIMESTAMP WITH TIME ZONE" in ddl
        assert 'COLLATE "C"' in ddl
    else:
        assert 'COLLATE "BINARY"' in ddl
        assert "STRFTIME" in ddl


@pytest.mark.parametrize("dialect", DIALECTS, ids=lambda value: value.name)
def test_utc_adapter_normalizes_offsets_and_rejects_naive_times(dialect: Dialect) -> None:
    instant = datetime(2026, 10, 19, 16, 3, 4, 123456, tzinfo=timezone(timedelta(hours=8)))
    adapter = UTCDateTime()
    stored = adapter.process_bind_param(instant, dialect)
    expected = instant.astimezone(UTC)
    assert stored == (expected if dialect.name == "postgresql" else expected.replace(tzinfo=None))
    assert adapter.process_result_value(stored, dialect) == expected
    with pytest.raises(ValueError, match="timezone"):
        adapter.process_bind_param(datetime(2026, 10, 19), dialect)
    if dialect.name == "postgresql":
        with pytest.raises(ValueError, match="naive"):
            adapter.process_result_value(datetime(2026, 10, 19), dialect)


@pytest.mark.parametrize("model", (PrincipalTable, RoleAssignmentTable, RelationTupleTable, WorkItemTable))
def test_every_machine_string_has_explicit_comparison(model: type[Base]) -> None:
    for column in model.__table__.columns:
        if column.name not in ("title", "description") and column.type.python_type is str:
            assert isinstance(column.type, ExactString), column.name


@pytest_asyncio.fixture(params=DATABASE_URLS, ids=lambda value: make_url(value).get_backend_name())
async def storage(request: pytest.FixtureRequest) -> AsyncIterator[tuple[AsyncEngine, dict[str, Table]]]:
    # Never touch the application's tables or drop a database/schema. Even the
    # explicitly supplied integration URL only gets unique, test-owned tables.
    url = request.param
    assert make_url(url).drivername in ("sqlite+aiosqlite", "postgresql+asyncpg")
    engine = create_engine(url)
    metadata = MetaData()
    prefix = "stage_probe_" + uuid4().hex[:12] + "_"
    tables = {}
    for model in (PrincipalTable, RoleAssignmentTable, RelationTupleTable, WorkItemTable):
        original = cast(Table, model.__table__)
        table = original.to_metadata(metadata, name=prefix + original.name)
        # These isolated probes test value encodings, not the authorization FK
        # graph. Schema/loader integration tests exercise the real references.
        for constraint in tuple(table.constraints):
            if isinstance(constraint, ForeignKeyConstraint):
                table.constraints.remove(constraint)
                for foreign_key in constraint.elements:
                    table.foreign_keys.discard(foreign_key)
                    foreign_key.parent.foreign_keys.discard(foreign_key)
        # PostgreSQL unique constraints create schema-wide named indexes.
        # Probe tables must coexist with tables created by Alembic above.
        for constraint in table.constraints:
            if isinstance(constraint.name, str):
                constraint.name = conv(prefix + constraint.name)
        for number, index in enumerate(table.indexes):
            # to_metadata may already include the renamed table in index names;
            # use one prefix and allow dialect-aware truncation for long names.
            index.name = conv(f"{prefix}{original.name}_idx_{number}")
        tables[original.name] = table
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        yield engine, tables
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await engine.dispose()


async def test_time_round_trip_and_server_defaults(storage: tuple[AsyncEngine, dict[str, Table]]) -> None:
    engine, tables = storage
    table = tables["role_assignment"]
    instant = datetime(2026, 10, 19, 16, 3, 4, 123456, tzinfo=timezone(timedelta(hours=8)))
    async with engine.begin() as connection:
        await connection.execute(
            table.insert().values(
                principal_ref="person:1", role_code="custom-role", scope_ref="org:1", valid_from=instant
            )
        )
        row = (await connection.execute(select(table))).one()
        assert row.valid_from == instant.astimezone(UTC)
        assert row.valid_from.tzinfo == UTC
        assert row.created_at.tzinfo == UTC
        assert row.updated_at.tzinfo == UTC
        assert abs((datetime.now(UTC) - row.created_at).total_seconds()) < 60
    with pytest.raises(StatementError, match="timezone"):
        async with engine.begin() as connection:
            await connection.execute(
                table.insert().values(
                    principal_ref="person:2",
                    role_code="custom-role",
                    scope_ref="org:1",
                    valid_from=datetime(2026, 1, 1),
                )
            )


async def test_identifiers_are_case_accent_and_space_exact(storage: tuple[AsyncEngine, dict[str, Table]]) -> None:
    engine, tables = storage
    table = tables["principal"]
    values = ("Agent:a", "agent:a", "agént:a", "agent:a ")
    async with engine.begin() as connection:
        for value in values:
            await connection.execute(table.insert().values(principal_ref=value, principal_type="agent"))
        for value in values:
            rows = (await connection.execute(select(table).where(table.c.principal_ref == value))).all()
            assert len(rows) == 1
            assert rows[0].principal_ref == value
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(table.insert().values(principal_ref="agent:a", principal_type="agent"))


@pytest.mark.parametrize(
    ("table_name", "values"),
    (
        ("principal", {"principal_ref": "person:1", "principal_type": "root"}),
        ("principal", {"principal_ref": " ", "principal_type": "human"}),
        ("principal", {"principal_ref": "person:1", "principal_type": "human", "status": " "}),
        ("work_item", {"title": "task", "scope_ref": "org:1", "version": 0}),
        ("work_item", {"title": "task", "scope_ref": "org:1", "lifecycle": " "}),
        (
            "role_assignment",
            {
                "principal_ref": "person:1",
                "role_code": "custom-role",
                "scope_ref": "org:1",
                "valid_from": datetime(2026, 1, 2, tzinfo=UTC),
                "expires_at": datetime(2026, 1, 2, tzinfo=UTC),
            },
        ),
    ),
)
async def test_invalid_storage_invariants_are_rejected(
    storage: tuple[AsyncEngine, dict[str, Table]], table_name: str, values: dict[str, object]
) -> None:
    engine, tables = storage
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(tables[table_name].insert().values(**values))
