from datetime import datetime

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TimestampMixin
from app.storage.types import ExactString, UTCDateTime


class RoleAssignmentTable(IdMixin, TimestampMixin, Base):
    __tablename__ = "role_assignment"
    __table_args__ = (
        UniqueConstraint("principal_ref", "role_code", "scope_ref", name="uk_role_assignment"),
        CheckConstraint(
            "length(trim(principal_ref)) > 0 AND length(trim(role_code)) > 0 AND length(trim(scope_ref)) > 0",
            name="ck_role_assignment_keys",
        ),
        CheckConstraint("length(trim(status)) > 0", name="ck_role_assignment_status"),
        CheckConstraint(
            "valid_from IS NULL OR expires_at IS NULL OR valid_from < expires_at", name="ck_role_assignment_window"
        ),
    )

    principal_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False, index=True)
    role_code: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    scope_ref: Mapped[str] = mapped_column(ExactString(256), nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(ExactString(32), nullable=False, default="active")


class RelationTupleTable(IdMixin, TimestampMixin, Base):
    __tablename__ = "relation_tuple"
    __table_args__ = (
        UniqueConstraint("subject_ref", "relation", "resource_ref", name="uk_relation_tuple"),
        CheckConstraint(
            "length(trim(subject_ref)) > 0 AND length(trim(relation)) > 0 AND length(trim(resource_ref)) > 0",
            name="ck_relation_tuple_keys",
        ),
        CheckConstraint("length(trim(status)) > 0", name="ck_relation_tuple_status"),
        CheckConstraint(
            "valid_from IS NULL OR expires_at IS NULL OR valid_from < expires_at", name="ck_relation_tuple_window"
        ),
    )

    subject_ref: Mapped[str] = mapped_column(ExactString(256), nullable=False, index=True)
    relation: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    resource_ref: Mapped[str] = mapped_column(ExactString(256), nullable=False, index=True)
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(ExactString(32), nullable=False, default="active")
