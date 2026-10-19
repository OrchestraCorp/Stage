from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TimestampMixin


class PrincipalTable(IdMixin, TimestampMixin, Base):
    __tablename__ = "principal"

    principal_ref: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    principal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
