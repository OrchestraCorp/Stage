"""Tenant-scoped command results, primary audit records and durable delivery state."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKeyConstraint, Index, Integer, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.storage.types import ExactString, UTCDateTime, utc_now


class TenantGuardTable(Base):
    __tablename__ = "execution_tenant_guard"
    __table_args__ = (
        CheckConstraint("length(trim(tenant_ref)) > 0", name="ck_execution_tenant_guard_ref"),
        CheckConstraint("epoch >= 0", name="ck_execution_tenant_guard_epoch"),
    )

    tenant_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))


class CommandExecutionTable(Base):
    __tablename__ = "command_execution"
    __table_args__ = (
        UniqueConstraint(
            "tenant_ref",
            "actor_ref",
            "command_name",
            "command_version",
            "idempotency_key",
            name="uk_command_execution_idempotency",
        ),
        UniqueConstraint("tenant_ref", "execution_id", name="uk_command_execution_tenant_id"),
        CheckConstraint(
            "length(trim(execution_id)) > 0 AND length(trim(tenant_ref)) > 0 "
            "AND length(trim(actor_ref)) > 0 AND length(trim(effective_subject_ref)) > 0 "
            "AND length(trim(command_name)) > 0 AND length(trim(command_version)) > 0 "
            "AND length(trim(idempotency_key)) > 0 AND length(trim(target_ref)) > 0 "
            "AND length(trim(request_id)) > 0",
            name="ck_command_execution_keys",
        ),
        CheckConstraint(
            "length(payload_digest) = 64 AND length(context_digest) = 64", name="ck_command_execution_digests"
        ),
        CheckConstraint("expected_version >= 0", name="ck_command_execution_version"),
        CheckConstraint("status IN ('running', 'succeeded')", name="ck_command_execution_status"),
    )

    execution_id: Mapped[str] = mapped_column(ExactString(36), primary_key=True)
    tenant_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    actor_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    effective_subject_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    command_name: Mapped[str] = mapped_column(ExactString(96), nullable=False)
    command_version: Mapped[str] = mapped_column(ExactString(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    payload_digest: Mapped[str] = mapped_column(ExactString(64), nullable=False)
    context_digest: Mapped[str] = mapped_column(ExactString(64), nullable=False)
    target_ref: Mapped[str] = mapped_column(ExactString(256), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    assignment_ref: Mapped[str | None] = mapped_column(ExactString(128))
    delegation_ref: Mapped[str | None] = mapped_column(ExactString(128))
    status: Mapped[str] = mapped_column(ExactString(16), nullable=False, default="running", server_default="running")
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=utc_now())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AuditRecordTable(Base):
    __tablename__ = "execution_audit"
    __table_args__ = (
        ForeignKeyConstraint(
            ("tenant_ref", "execution_id"),
            ("command_execution.tenant_ref", "command_execution.execution_id"),
            name="fk_execution_audit_command",
        ),
        UniqueConstraint("tenant_ref", "execution_id", name="uk_execution_audit_command"),
        CheckConstraint(
            "length(trim(audit_id)) > 0 AND length(trim(tenant_ref)) > 0 AND length(trim(execution_id)) > 0 "
            "AND length(trim(actor_ref)) > 0 AND length(trim(effective_subject_ref)) > 0 "
            "AND length(trim(action)) > 0 AND length(trim(target_ref)) > 0 "
            "AND length(trim(decision_id)) > 0 AND length(trim(policy_release_id)) > 0 "
            "AND length(trim(request_id)) > 0",
            name="ck_execution_audit_keys",
        ),
    )

    audit_id: Mapped[str] = mapped_column(ExactString(36), primary_key=True)
    tenant_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    execution_id: Mapped[str] = mapped_column(ExactString(36), nullable=False)
    actor_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    effective_subject_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    action: Mapped[str] = mapped_column(ExactString(96), nullable=False)
    target_ref: Mapped[str] = mapped_column(ExactString(256), nullable=False)
    decision_id: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    policy_release_id: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    request_id: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    authorization_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=utc_now())


class OutboxRecordTable(Base):
    __tablename__ = "execution_outbox"
    __table_args__ = (
        ForeignKeyConstraint(
            ("tenant_ref", "execution_id"),
            ("command_execution.tenant_ref", "command_execution.execution_id"),
            name="fk_execution_outbox_command",
        ),
        UniqueConstraint("tenant_ref", "destination", "dedupe_key", name="uk_execution_outbox_dedupe"),
        UniqueConstraint("tenant_ref", "message_id", name="uk_execution_outbox_tenant_id"),
        CheckConstraint(
            "length(trim(message_id)) > 0 AND length(trim(tenant_ref)) > 0 AND length(trim(execution_id)) > 0 "
            "AND length(trim(topic)) > 0 AND length(trim(destination)) > 0 "
            "AND length(trim(aggregate_ref)) > 0 AND length(trim(read_action)) > 0 "
            "AND length(trim(dedupe_key)) > 0",
            name="ck_execution_outbox_keys",
        ),
        CheckConstraint(
            "status IN ('pending', 'delivering', 'sent', 'unknown', 'failed', 'blocked')",
            name="ck_execution_outbox_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_execution_outbox_attempt_count"),
        Index("ix_execution_outbox_status_available", "status", "available_at"),
    )

    message_id: Mapped[str] = mapped_column(ExactString(36), primary_key=True)
    tenant_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    execution_id: Mapped[str] = mapped_column(ExactString(36), nullable=False)
    topic: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    destination: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    aggregate_ref: Mapped[str] = mapped_column(ExactString(256), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON(none_as_null=True), nullable=False)
    read_action: Mapped[str] = mapped_column(ExactString(96), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    status: Mapped[str] = mapped_column(ExactString(16), nullable=False, default="pending", server_default="pending")
    available_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    lease_token: Mapped[str | None] = mapped_column(ExactString(36))
    lease_owner: Mapped[str | None] = mapped_column(ExactString(128))
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=utc_now())
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error_code: Mapped[str | None] = mapped_column(ExactString(96))
    receipt_ref: Mapped[str | None] = mapped_column(ExactString(256))


class DeliveryAttemptTable(Base):
    __tablename__ = "outbox_delivery_attempt"
    __table_args__ = (
        ForeignKeyConstraint(
            ("tenant_ref", "message_id"),
            ("execution_outbox.tenant_ref", "execution_outbox.message_id"),
            name="fk_outbox_delivery_attempt_message",
        ),
        CheckConstraint(
            "length(trim(attempt_id)) > 0 AND length(trim(tenant_ref)) > 0 AND length(trim(message_id)) > 0 "
            "AND length(trim(lease_token)) > 0 AND length(trim(worker_ref)) > 0",
            name="ck_outbox_delivery_attempt_keys",
        ),
        CheckConstraint(
            "status IN ('delivering', 'sent', 'unknown', 'failed', 'blocked')",
            name="ck_outbox_delivery_attempt_status",
        ),
        CheckConstraint("operation IN ('send', 'reconcile')", name="ck_outbox_delivery_attempt_operation"),
    )

    attempt_id: Mapped[str] = mapped_column(ExactString(36), primary_key=True)
    tenant_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    message_id: Mapped[str] = mapped_column(ExactString(36), nullable=False)
    lease_token: Mapped[str] = mapped_column(ExactString(36), nullable=False)
    worker_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    operation: Mapped[str] = mapped_column(ExactString(16), nullable=False, default="send", server_default="send")
    status: Mapped[str] = mapped_column(
        ExactString(16), nullable=False, default="delivering", server_default="delivering"
    )
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error_code: Mapped[str | None] = mapped_column(ExactString(96))
    receipt_ref: Mapped[str | None] = mapped_column(ExactString(256))
