"""Explicit first-management nomination and acceptance boundary.

Revision ID: a7d4e2c9f501
Revises: c6f3a19d820b
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "a7d4e2c9f501"
down_revision: str | None = "c6f3a19d820b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    def exact(length: int) -> sa.String:
        return sa.String(length, collation="C" if dialect == "postgresql" else "BINARY")

    timestamp = sa.DateTime(timezone=dialect == "postgresql")
    op.create_table(
        "management_authority",
        sa.Column("organization_ref", exact(128), primary_key=True),
        sa.Column("authority_ref", exact(128), nullable=False),
        sa.Column("principal_ref", exact(128), nullable=False),
        sa.Column("membership_ref", exact(128), nullable=False),
        sa.Column("assignment_ref", exact(128), nullable=False),
        sa.Column("profile_ref", exact(128), nullable=False),
        sa.Column("profile_version", sa.BigInteger(), nullable=False),
        sa.Column("external_identity_ref", exact(128), nullable=False),
        sa.Column(
            "grant_rules_json",
            sa.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
            nullable=False,
        ),
        sa.Column("nomination_expires_at", timestamp, nullable=False),
        sa.Column("accepted_at", timestamp, nullable=True),
        sa.Column("status", exact(32), nullable=False, server_default="pending"),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", timestamp, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", timestamp, nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ("organization_ref",), ("organization_root.organization_ref",), name="fk_management_organization"
        ),
        sa.ForeignKeyConstraint(
            ("organization_ref", "membership_ref", "principal_ref"),
            ("tenant_membership.tenant_ref", "tenant_membership.membership_ref", "tenant_membership.principal_ref"),
            name="fk_management_membership",
        ),
        sa.ForeignKeyConstraint(
            ("organization_ref", "assignment_ref", "principal_ref"),
            ("role_assignment.organization_ref", "role_assignment.assignment_ref", "role_assignment.principal_ref"),
            name="fk_management_assignment",
        ),
        sa.ForeignKeyConstraint(
            ("profile_ref", "profile_version"),
            ("authentication_profile.profile_ref", "authentication_profile.profile_version"),
            name="fk_management_profile",
        ),
        sa.ForeignKeyConstraint(
            ("external_identity_ref",),
            ("external_identity.external_identity_ref",),
            name="fk_management_external_identity",
        ),
        sa.UniqueConstraint("authority_ref", name="uk_management_authority_ref"),
        sa.CheckConstraint("status IN ('pending','active','revoked')", name="ck_management_status"),
        sa.CheckConstraint("version > 0 AND profile_version > 0", name="ck_management_versions"),
        sa.CheckConstraint(
            "(status = 'pending' AND accepted_at IS NULL) OR "
            "(status IN ('active','revoked') AND accepted_at IS NOT NULL)",
            name="ck_management_acceptance",
        ),
        sa.CheckConstraint(
            "accepted_at IS NULL OR accepted_at < nomination_expires_at", name="ck_management_acceptance_deadline"
        ),
        sa.CheckConstraint(
            "length(trim(authority_ref)) > 0 AND length(trim(assignment_ref)) > 0", name="ck_management_references"
        ),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT count(*) FROM management_authority")).scalar_one():
        raise RuntimeError("Downgrade cannot discard administrator nomination or acceptance facts")
    op.drop_table("management_authority")
