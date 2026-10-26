"""Separate initial factor enrollment from proof-authorized replacement.

Revision ID: b7f0d3a9216c
Revises: e8a1c6d9f240
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7f0d3a9216c"
down_revision: str | None = "e8a1c6d9f240"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "native_account",
        sa.Column("totp_bootstrap_allowed", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    # An existing enrolled or previously changed account must never fall back to
    # password-only enrollment. Ambiguous old reset state requires operator proof.
    op.execute(
        sa.text(
            "UPDATE native_account SET totp_bootstrap_allowed = false "
            "WHERE totp_secret IS NOT NULL OR credential_version > 1"
        )
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT count(*) FROM native_account")).scalar_one():
        raise RuntimeError("Downgrade cannot discard native factor enrollment authorization")
    op.drop_column("native_account", "totp_bootstrap_allowed")
