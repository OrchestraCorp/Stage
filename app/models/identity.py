from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TimestampMixin
from app.storage.types import ExactString, UTCDateTime


class PrincipalTable(IdMixin, TimestampMixin, Base):
    __tablename__ = "principal"
    __table_args__ = (
        CheckConstraint("length(trim(principal_ref)) > 0", name="ck_principal_ref"),
        CheckConstraint("principal_type IN ('human', 'agent', 'service')", name="ck_principal_type"),
        CheckConstraint("length(trim(status)) > 0", name="ck_principal_status"),
        UniqueConstraint("principal_ref", "principal_type", name="uk_principal_ref_type"),
        CheckConstraint("version > 0", name="ck_principal_version"),
        CheckConstraint("identity_epoch >= 0 AND recovery_epoch >= 0", name="ck_principal_epochs"),
    )

    principal_ref: Mapped[str] = mapped_column(ExactString(128), unique=True, nullable=False)
    principal_type: Mapped[str] = mapped_column(ExactString(32), nullable=False)
    status: Mapped[str] = mapped_column(ExactString(32), nullable=False, default="active")
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    identity_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    recovery_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    disabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
