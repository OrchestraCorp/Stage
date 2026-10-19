from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TimestampMixin


class RoleAssignmentTable(IdMixin, TimestampMixin, Base):
    __tablename__ = "role_assignment"
    __table_args__ = (UniqueConstraint("principal_ref", "role_code", "scope_ref", name="uk_role_assignment"),)

    principal_ref: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    role_code: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class RelationTupleTable(IdMixin, TimestampMixin, Base):
    __tablename__ = "relation_tuple"
    __table_args__ = (UniqueConstraint("subject_ref", "relation", "resource_ref", name="uk_relation_tuple"),)

    subject_ref: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    relation: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_ref: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
