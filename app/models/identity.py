from sqlalchemy import CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TimestampMixin
from app.storage.types import ExactString


class PrincipalTable(IdMixin, TimestampMixin, Base):
    __tablename__ = "principal"
    __table_args__ = (
        CheckConstraint("length(trim(principal_ref)) > 0", name="ck_principal_ref"),
        CheckConstraint("principal_type IN ('human', 'agent', 'service')", name="ck_principal_type"),
        CheckConstraint("length(trim(status)) > 0", name="ck_principal_status"),
    )

    principal_ref: Mapped[str] = mapped_column(ExactString(128), unique=True, nullable=False)
    principal_type: Mapped[str] = mapped_column(ExactString(32), nullable=False)
    status: Mapped[str] = mapped_column(ExactString(32), nullable=False, default="active")
