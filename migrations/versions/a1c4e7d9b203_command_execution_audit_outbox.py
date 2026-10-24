"""Add tenant-scoped command, audit and outbox persistence.

Revision ID: a1c4e7d9b203
Revises: 6d9e0b2f4a71
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

revision: str = "a1c4e7d9b203"
down_revision: str | None = "6d9e0b2f4a71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "execution_tenant_guard",
    "command_execution",
    "execution_audit",
    "execution_outbox",
    "outbox_delivery_attempt",
)


# Freeze storage semantics here; future application models must not alter this revision.
def _exact_string(length: int, dialect: str) -> sa.String:
    if dialect == "mysql":
        return mysql.VARCHAR(length, charset="utf8mb4", collation="utf8mb4_0900_bin")
    return sa.String(length, collation="C" if dialect == "postgresql" else "BINARY")


def _timestamp(dialect: str) -> sa.DateTime:
    return mysql.DATETIME(fsp=6) if dialect == "mysql" else sa.DateTime(timezone=dialect == "postgresql")


def _created_at(dialect: str) -> sa.Column[datetime]:
    default = {
        "mysql": "(UTC_TIMESTAMP(6))",
        "postgresql": "CURRENT_TIMESTAMP",
        "sqlite": "(STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))",
    }[dialect]
    return sa.Column("created_at", _timestamp(dialect), nullable=False, server_default=sa.text(default))


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in ("mysql", "postgresql", "sqlite"):
        raise RuntimeError(f"Unsupported Stage database: {dialect}")

    op.create_table(
        "execution_tenant_guard",
        sa.Column("tenant_ref", _exact_string(128, dialect), primary_key=True),
        sa.Column("epoch", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint("length(trim(tenant_ref)) > 0", name="ck_execution_tenant_guard_ref"),
        sa.CheckConstraint("epoch >= 0", name="ck_execution_tenant_guard_epoch"),
    )
    op.create_table(
        "command_execution",
        sa.Column("execution_id", _exact_string(36, dialect), primary_key=True),
        sa.Column("tenant_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("actor_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("effective_subject_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("command_name", _exact_string(96, dialect), nullable=False),
        sa.Column("command_version", _exact_string(32, dialect), nullable=False),
        sa.Column("idempotency_key", _exact_string(128, dialect), nullable=False),
        sa.Column("payload_digest", _exact_string(64, dialect), nullable=False),
        sa.Column("context_digest", _exact_string(64, dialect), nullable=False),
        sa.Column("target_ref", _exact_string(256, dialect), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("request_id", _exact_string(128, dialect), nullable=False),
        sa.Column("assignment_ref", _exact_string(128, dialect), nullable=True),
        sa.Column("delegation_ref", _exact_string(128, dialect), nullable=True),
        sa.Column("status", _exact_string(16, dialect), nullable=False, server_default="running"),
        sa.Column("result_json", sa.JSON(none_as_null=True), nullable=True),
        _created_at(dialect),
        sa.Column("completed_at", _timestamp(dialect), nullable=True),
        sa.UniqueConstraint(
            "tenant_ref",
            "actor_ref",
            "command_name",
            "command_version",
            "idempotency_key",
            name="uk_command_execution_idempotency",
        ),
        sa.UniqueConstraint("tenant_ref", "execution_id", name="uk_command_execution_tenant_id"),
        sa.CheckConstraint(
            "length(trim(execution_id)) > 0 AND length(trim(tenant_ref)) > 0 "
            "AND length(trim(actor_ref)) > 0 AND length(trim(effective_subject_ref)) > 0 "
            "AND length(trim(command_name)) > 0 AND length(trim(command_version)) > 0 "
            "AND length(trim(idempotency_key)) > 0 AND length(trim(target_ref)) > 0 "
            "AND length(trim(request_id)) > 0",
            name="ck_command_execution_keys",
        ),
        sa.CheckConstraint(
            "length(payload_digest) = 64 AND length(context_digest) = 64", name="ck_command_execution_digests"
        ),
        sa.CheckConstraint("expected_version >= 0", name="ck_command_execution_version"),
        sa.CheckConstraint("status IN ('running', 'succeeded')", name="ck_command_execution_status"),
    )
    op.create_table(
        "execution_audit",
        sa.Column("audit_id", _exact_string(36, dialect), primary_key=True),
        sa.Column("tenant_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("execution_id", _exact_string(36, dialect), nullable=False),
        sa.Column("actor_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("effective_subject_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("action", _exact_string(96, dialect), nullable=False),
        sa.Column("target_ref", _exact_string(256, dialect), nullable=False),
        sa.Column("decision_id", _exact_string(128, dialect), nullable=False),
        sa.Column("policy_release_id", _exact_string(128, dialect), nullable=False),
        sa.Column("request_id", _exact_string(128, dialect), nullable=False),
        sa.Column("before_json", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("after_json", sa.JSON(none_as_null=True), nullable=True),
        _created_at(dialect),
        sa.ForeignKeyConstraint(
            ("tenant_ref", "execution_id"),
            ("command_execution.tenant_ref", "command_execution.execution_id"),
            name="fk_execution_audit_command",
        ),
        sa.UniqueConstraint("tenant_ref", "execution_id", name="uk_execution_audit_command"),
        sa.CheckConstraint(
            "length(trim(audit_id)) > 0 AND length(trim(tenant_ref)) > 0 AND length(trim(execution_id)) > 0 "
            "AND length(trim(actor_ref)) > 0 AND length(trim(effective_subject_ref)) > 0 "
            "AND length(trim(action)) > 0 AND length(trim(target_ref)) > 0 "
            "AND length(trim(decision_id)) > 0 AND length(trim(policy_release_id)) > 0 "
            "AND length(trim(request_id)) > 0",
            name="ck_execution_audit_keys",
        ),
    )
    op.create_table(
        "execution_outbox",
        sa.Column("message_id", _exact_string(36, dialect), primary_key=True),
        sa.Column("tenant_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("execution_id", _exact_string(36, dialect), nullable=False),
        sa.Column("topic", _exact_string(128, dialect), nullable=False),
        sa.Column("destination", _exact_string(128, dialect), nullable=False),
        sa.Column("aggregate_ref", _exact_string(256, dialect), nullable=False),
        sa.Column("payload_json", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("read_action", _exact_string(96, dialect), nullable=False),
        sa.Column("dedupe_key", _exact_string(128, dialect), nullable=False),
        sa.Column("status", _exact_string(16, dialect), nullable=False, server_default="pending"),
        sa.Column("available_at", _timestamp(dialect), nullable=False),
        sa.Column("lease_token", _exact_string(36, dialect), nullable=True),
        sa.Column("lease_owner", _exact_string(128, dialect), nullable=True),
        sa.Column("lease_until", _timestamp(dialect), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _created_at(dialect),
        sa.Column("sent_at", _timestamp(dialect), nullable=True),
        sa.Column("last_error_code", _exact_string(96, dialect), nullable=True),
        sa.Column("receipt_ref", _exact_string(256, dialect), nullable=True),
        sa.ForeignKeyConstraint(
            ("tenant_ref", "execution_id"),
            ("command_execution.tenant_ref", "command_execution.execution_id"),
            name="fk_execution_outbox_command",
        ),
        sa.UniqueConstraint("tenant_ref", "destination", "dedupe_key", name="uk_execution_outbox_dedupe"),
        sa.UniqueConstraint("tenant_ref", "message_id", name="uk_execution_outbox_tenant_id"),
        sa.CheckConstraint(
            "length(trim(message_id)) > 0 AND length(trim(tenant_ref)) > 0 AND length(trim(execution_id)) > 0 "
            "AND length(trim(topic)) > 0 AND length(trim(destination)) > 0 "
            "AND length(trim(aggregate_ref)) > 0 AND length(trim(read_action)) > 0 "
            "AND length(trim(dedupe_key)) > 0",
            name="ck_execution_outbox_keys",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivering', 'sent', 'unknown', 'failed', 'blocked')",
            name="ck_execution_outbox_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_execution_outbox_attempt_count"),
    )
    op.create_index("ix_execution_outbox_status_available", "execution_outbox", ("status", "available_at"))
    op.create_table(
        "outbox_delivery_attempt",
        sa.Column("attempt_id", _exact_string(36, dialect), primary_key=True),
        sa.Column("tenant_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("message_id", _exact_string(36, dialect), nullable=False),
        sa.Column("lease_token", _exact_string(36, dialect), nullable=False),
        sa.Column("worker_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("operation", _exact_string(16, dialect), nullable=False, server_default="send"),
        sa.Column("status", _exact_string(16, dialect), nullable=False, server_default="delivering"),
        sa.Column("started_at", _timestamp(dialect), nullable=False),
        sa.Column("completed_at", _timestamp(dialect), nullable=True),
        sa.Column("error_code", _exact_string(96, dialect), nullable=True),
        sa.Column("receipt_ref", _exact_string(256, dialect), nullable=True),
        sa.ForeignKeyConstraint(
            ("tenant_ref", "message_id"),
            ("execution_outbox.tenant_ref", "execution_outbox.message_id"),
            name="fk_outbox_delivery_attempt_message",
        ),
        sa.CheckConstraint(
            "length(trim(attempt_id)) > 0 AND length(trim(tenant_ref)) > 0 AND length(trim(message_id)) > 0 "
            "AND length(trim(lease_token)) > 0 AND length(trim(worker_ref)) > 0",
            name="ck_outbox_delivery_attempt_keys",
        ),
        sa.CheckConstraint(
            "status IN ('delivering', 'sent', 'unknown', 'failed', 'blocked')",
            name="ck_outbox_delivery_attempt_status",
        ),
        sa.CheckConstraint("operation IN ('send', 'reconcile')", name="ck_outbox_delivery_attempt_operation"),
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("This downgrade requires an online connection to check for retained execution data")
    connection = op.get_bind()
    for table_name in TABLES:
        if connection.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first() is not None:
            raise RuntimeError(
                f"Downgrade requires empty execution tables; {table_name} contains retained data. "
                "No execution tables have been removed."
            )
    for table_name in reversed(TABLES):
        op.drop_table(table_name)
