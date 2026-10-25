"""Name the singleton deployment boundary as an organization authorization root.

Revision ID: d6a3c5f8e902
Revises: c4f2a89d1e63

The root is not a legal-entity registry. Rename only its table and reference
column; existing references, dependent rows and singleton constraints survive.
Historical membership, admission and execution column names remain compatible.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d6a3c5f8e902"
down_revision: str | None = "c4f2a89d1e63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Native renames preserve data, checks, indexes and referring foreign keys
    # on PostgreSQL and supported modern SQLite; never rebuild the root table.
    op.rename_table("tenant", "organization_root")
    op.alter_column("organization_root", "tenant_ref", new_column_name="organization_ref")


def downgrade() -> None:
    op.alter_column("organization_root", "organization_ref", new_column_name="tenant_ref")
    op.rename_table("organization_root", "tenant")
