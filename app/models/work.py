from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TimestampMixin


class WorkItemTable(IdMixin, TimestampMixin, Base):
    __tablename__ = "work_item"

    title: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    classification: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")
    scope_ref: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
