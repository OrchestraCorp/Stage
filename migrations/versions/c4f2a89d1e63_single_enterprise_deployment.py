"""Constrain each private Stage deployment to one organization.

Revision ID: c4f2a89d1e63
Revises: b7e1a92c8d40

Keep existing organization references and all dependent identity facts. A database
with multiple organizations must be reviewed explicitly before this migration;
the migration never chooses, merges, deletes or invents an organization.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "c4f2a89d1e63"
down_revision: str | None = "b7e1a92c8d40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Single-enterprise migration requires an online connection to inspect organization rows")
    connection = op.get_bind()
    dialect = connection.dialect.name
    if dialect == "postgresql":
        connection.execute(sa.text("LOCK TABLE tenant IN ACCESS EXCLUSIVE MODE"))
    elif dialect == "sqlite":
        # Reserve the SQLite writer before inspecting rows, without changing data.
        connection.execute(sa.text("UPDATE tenant SET tenant_ref = tenant_ref WHERE 0"))
    else:
        raise RuntimeError(f"Unsupported Stage identity database: {dialect}")
    count = connection.scalar(sa.text("SELECT count(*) FROM tenant"))
    if count is not None and count > 1:
        raise RuntimeError(
            f"Single-enterprise deployment requires at most one organization; tenant contains {count} rows. "
            "Review the existing organizations before migration. No rows were merged or removed."
        )
    # ADD COLUMN avoids rebuilding a referenced SQLite table and preserves FKs.
    op.add_column(
        "tenant",
        sa.Column(
            "deployment_slot",
            sa.SmallInteger(),
            sa.CheckConstraint("deployment_slot = 1", name="ck_tenant_deployment_slot"),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )
    op.create_index("uk_tenant_deployment_slot", "tenant", ["deployment_slot"], unique=True)


def downgrade() -> None:
    op.drop_index("uk_tenant_deployment_slot", table_name="tenant")
    # The column-local CHECK is removed with its column on both supported engines.
    op.drop_column("tenant", "deployment_slot")
