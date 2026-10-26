"""Add organization-wide human login assurance without silently enabling MFA.

Revision ID: 9c2e7a4b6d10
Revises: f4c7b2a1e903
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "9c2e7a4b6d10"
down_revision: str | None = "f4c7b2a1e903"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "organization_human_authentication_policy"


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    exact_collation = "C" if dialect == "postgresql" else "BINARY"
    timestamp = sa.DateTime(timezone=dialect == "postgresql")
    now = sa.text("CURRENT_TIMESTAMP" if dialect == "postgresql" else "(STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))")
    table = op.create_table(
        TABLE,
        sa.Column("organization_ref", sa.String(128, collation=exact_collation), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("mode", sa.String(32, collation=exact_collation), nullable=False),
        sa.Column(
            "rules_json",
            sa.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
            nullable=False,
        ),
        sa.Column("max_auth_age_seconds", sa.BigInteger(), nullable=True),
        sa.Column("created_at", timestamp, server_default=now, nullable=False),
        sa.Column("updated_at", timestamp, server_default=now, nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_ref"],
            ["organization_root.organization_ref"],
            name="fk_human_authentication_policy_organization",
        ),
        sa.CheckConstraint("version > 0", name="ck_human_authentication_policy_version"),
        sa.CheckConstraint("mode IN ('disabled', 'required')", name="ck_human_authentication_policy_mode"),
        sa.CheckConstraint(
            "(mode = 'disabled' AND max_auth_age_seconds IS NULL) OR "
            "(mode = 'required' AND max_auth_age_seconds IS NOT NULL AND max_auth_age_seconds BETWEEN 1 AND 86400)",
            name="ck_human_authentication_policy_age",
        ),
        sa.PrimaryKeyConstraint("organization_ref"),
    )
    organizations = op.get_bind().execute(sa.text("SELECT organization_ref FROM organization_root")).scalars().all()
    for organization_ref in organizations:
        op.get_bind().execute(table.insert().values(organization_ref=organization_ref, mode="disabled", rules_json=[]))


def downgrade() -> None:
    connection = op.get_bind()
    configured = connection.execute(
        sa.text(f"SELECT count(*) FROM {TABLE} WHERE mode <> 'disabled' OR version > 1")
    ).scalar_one()
    journaled = connection.execute(
        sa.text(
            "SELECT count(*) FROM identity_control_operation WHERE operation_type = 'configure.human-authentication'"
        )
    ).scalar_one()
    if configured or journaled:
        raise RuntimeError("Downgrade cannot discard configured human authentication policy")
    op.drop_table(TABLE)
