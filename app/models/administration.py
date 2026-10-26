"""One explicit first-administrator nomination and its durable acceptance boundary."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKeyConstraint, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.identity_foundation import _json
from app.storage.types import ExactString, UTCDateTime


class ManagementAuthorityTable(TimestampMixin, Base):
    __tablename__ = "management_authority"
    __table_args__ = (
        ForeignKeyConstraint(
            ("organization_ref",), ("organization_root.organization_ref",), name="fk_management_organization"
        ),
        ForeignKeyConstraint(
            ("organization_ref", "membership_ref", "principal_ref"),
            ("tenant_membership.tenant_ref", "tenant_membership.membership_ref", "tenant_membership.principal_ref"),
            name="fk_management_membership",
        ),
        ForeignKeyConstraint(
            ("organization_ref", "assignment_ref", "principal_ref"),
            ("role_assignment.organization_ref", "role_assignment.assignment_ref", "role_assignment.principal_ref"),
            name="fk_management_assignment",
        ),
        ForeignKeyConstraint(
            ("profile_ref", "profile_version"),
            ("authentication_profile.profile_ref", "authentication_profile.profile_version"),
            name="fk_management_profile",
        ),
        ForeignKeyConstraint(
            ("external_identity_ref",),
            ("external_identity.external_identity_ref",),
            name="fk_management_external_identity",
        ),
        UniqueConstraint("authority_ref", name="uk_management_authority_ref"),
        CheckConstraint("status IN ('pending','active','revoked')", name="ck_management_status"),
        CheckConstraint("version > 0 AND profile_version > 0", name="ck_management_versions"),
        CheckConstraint(
            "(status = 'pending' AND accepted_at IS NULL) OR "
            "(status IN ('active','revoked') AND accepted_at IS NOT NULL)",
            name="ck_management_acceptance",
        ),
        CheckConstraint(
            "accepted_at IS NULL OR accepted_at < nomination_expires_at", name="ck_management_acceptance_deadline"
        ),
        CheckConstraint(
            "length(trim(authority_ref)) > 0 AND length(trim(assignment_ref)) > 0", name="ck_management_references"
        ),
    )

    organization_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    authority_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    principal_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    membership_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    assignment_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    profile_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    profile_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    external_identity_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    grant_rules_json: Mapped[list[dict[str, Any]]] = mapped_column(_json(), nullable=False)
    nomination_expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(ExactString(32), nullable=False, default="pending", server_default="pending")
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
