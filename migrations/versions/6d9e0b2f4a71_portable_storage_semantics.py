"""Use explicit UTC timestamps, exact identifiers and basic data invariants.

Revision ID: 6d9e0b2f4a71
Revises: 870845fbe1f0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import Connection

revision: str = "6d9e0b2f4a71"
down_revision: str | None = "870845fbe1f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here so a later domain-model edit cannot change a released migration.
IDENTIFIERS: dict[str, dict[str, int]] = {
    "principal": {"principal_ref": 128, "principal_type": 32, "status": 32},
    "role_assignment": {"principal_ref": 128, "role_code": 128, "scope_ref": 256, "status": 32},
    "relation_tuple": {"subject_ref": 256, "relation": 128, "resource_ref": 256, "status": 32},
    "work_item": {"lifecycle": 32, "classification": 32, "scope_ref": 256},
}
CHECKS: dict[str, dict[str, str]] = {
    "principal": {
        "ck_principal_ref": "length(trim(principal_ref)) > 0",
        "ck_principal_type": "principal_type IN ('human', 'agent', 'service')",
        "ck_principal_status": "length(trim(status)) > 0",
    },
    "role_assignment": {
        "ck_role_assignment_keys": (
            "length(trim(principal_ref)) > 0 AND length(trim(role_code)) > 0 AND length(trim(scope_ref)) > 0"
        ),
        "ck_role_assignment_status": "length(trim(status)) > 0",
        "ck_role_assignment_window": "valid_from IS NULL OR expires_at IS NULL OR valid_from < expires_at",
    },
    "relation_tuple": {
        "ck_relation_tuple_keys": (
            "length(trim(subject_ref)) > 0 AND length(trim(relation)) > 0 AND length(trim(resource_ref)) > 0"
        ),
        "ck_relation_tuple_status": "length(trim(status)) > 0",
        "ck_relation_tuple_window": "valid_from IS NULL OR expires_at IS NULL OR valid_from < expires_at",
    },
    "work_item": {
        "ck_work_item_scope": "length(trim(scope_ref)) > 0",
        "ck_work_item_version": "version > 0",
        "ck_work_item_lifecycle": "length(trim(lifecycle)) > 0",
        "ck_work_item_classification": "length(trim(classification)) > 0",
    },
}


def _timestamps(table_name: str) -> tuple[str, ...]:
    columns = ("created_at", "updated_at")
    return columns + ("valid_from", "expires_at") if table_name in ("role_assignment", "relation_tuple") else columns


def _has_data(connection: Connection) -> bool:
    return any(
        connection.execute(sa.text(f"SELECT id FROM {table_name} LIMIT 1")).first() is not None
        for table_name in IDENTIFIERS
    )


def _validate_existing_rows(connection: Connection) -> None:
    # Evaluate enum checks with exact comparison before any DDL. The initial
    # MySQL schema may have a case-insensitive collation, so plain IN is unsafe.
    for table_name, checks in CHECKS.items():
        for name, expression in checks.items():
            if " IN " in expression:
                column, suffix = expression.split(" IN ", 1)
                if connection.dialect.name == "mysql":
                    expression = f"CAST({column} AS BINARY) IN {suffix}"
                elif connection.dialect.name == "postgresql":
                    expression = f'{column} COLLATE "C" IN {suffix}'
                else:
                    expression = f"{column} COLLATE BINARY IN {suffix}"
            invalid = connection.execute(
                sa.text(f"SELECT id FROM {table_name} WHERE NOT ({expression}) LIMIT 1")
            ).first()
            if invalid is not None:
                raise RuntimeError(f"Existing {table_name} row violates {name}; repair data before migration")


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("This migration requires an online connection for existing-data preflight")
    connection = op.get_bind()
    dialect = connection.dialect.name
    if dialect not in ("mysql", "postgresql", "sqlite"):
        raise RuntimeError(f"Unsupported Stage database: {dialect}")
    if dialect != "postgresql" and _has_data(connection):
        if context.get_x_argument(as_dictionary=True).get("legacy_timestamps") != "utc":
            raise RuntimeError(
                "Existing MySQL/SQLite timestamps have no timezone provenance. "
                "Confirm/correct every legacy timestamp to UTC first, then rerun "
                "with -x legacy_timestamps=utc. No conversion has been performed."
            )
    _validate_existing_rows(connection)

    for table_name, identifiers in IDENTIFIERS.items():
        with op.batch_alter_table(table_name, recreate="always" if dialect == "sqlite" else "auto") as batch:
            for column, length in identifiers.items():
                if dialect == "mysql":
                    exact_type = mysql.VARCHAR(length, charset="utf8mb4", collation="utf8mb4_0900_bin")
                else:
                    exact_type = sa.String(length, collation="C" if dialect == "postgresql" else "BINARY")
                batch.alter_column(column, existing_type=sa.String(length), type_=exact_type, existing_nullable=False)
            for column in _timestamps(table_name):
                nullable = column in ("valid_from", "expires_at")
                timestamp_type = mysql.DATETIME(fsp=6) if dialect == "mysql" else sa.DateTime(dialect == "postgresql")
                options: dict[str, object] = {}
                if not nullable:
                    default = {
                        "mysql": "(UTC_TIMESTAMP(6))",
                        "postgresql": "CURRENT_TIMESTAMP",
                        "sqlite": "(STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))",
                    }[dialect]
                    options["server_default"] = sa.text(default)
                batch.alter_column(
                    column,
                    existing_type=sa.DateTime(timezone=True),
                    type_=timestamp_type,
                    existing_nullable=nullable,
                    **options,
                )
            for name, expression in CHECKS[table_name].items():
                batch.create_check_constraint(name, expression)


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("This migration requires an online connection for existing-data preflight")
    connection = op.get_bind()
    if _has_data(connection):
        raise RuntimeError(
            "Downgrade requires empty Stage tables: reverting exact identifiers or timestamp precision "
            "with existing data may lose information. Restore a verified backup instead."
        )
    dialect = connection.dialect.name
    for table_name, identifiers in reversed(IDENTIFIERS.items()):
        with op.batch_alter_table(table_name, recreate="always" if dialect == "sqlite" else "auto") as batch:
            for name in CHECKS[table_name]:
                batch.drop_constraint(name, type_="check")
            for column, length in identifiers.items():
                batch.alter_column(
                    column, existing_type=sa.String(length), type_=sa.String(length), existing_nullable=False
                )
            for column in _timestamps(table_name):
                nullable = column in ("valid_from", "expires_at")
                options: dict[str, object] = {} if nullable else {"server_default": sa.text("CURRENT_TIMESTAMP")}
                batch.alter_column(
                    column,
                    existing_type=sa.DateTime(timezone=dialect == "postgresql"),
                    type_=sa.DateTime(timezone=True),
                    existing_nullable=nullable,
                    **options,
                )
