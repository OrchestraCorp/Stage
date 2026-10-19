from sqlalchemy import CheckConstraint, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TimestampMixin
from app.storage.types import ExactString


class WorkItemTable(IdMixin, TimestampMixin, Base):
    __tablename__ = "work_item"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_work_item_version"),
        CheckConstraint("length(trim(scope_ref)) > 0", name="ck_work_item_scope"),
        CheckConstraint("length(trim(lifecycle)) > 0", name="ck_work_item_lifecycle"),
        CheckConstraint("length(trim(classification)) > 0", name="ck_work_item_classification"),
    )

    title: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    lifecycle: Mapped[str] = mapped_column(ExactString(32), nullable=False, default="draft")
    classification: Mapped[str] = mapped_column(ExactString(32), nullable=False, default="internal")
    scope_ref: Mapped[str] = mapped_column(ExactString(256), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
