"""Pending employee accounts and one-time activation ceremonies.

Revision ID: d8e4b6c2a901
Revises: a7d4e2c9f501
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "d8e4b6c2a901"
down_revision: str | None = "a7d4e2c9f501"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


@contextmanager
def _account_schema_change() -> Iterator[None]:
    connection = op.get_bind()
    sqlite = connection.dialect.name == "sqlite"
    deferred = False
    if sqlite:
        # SQLite's legacy driver does not begin on DDL. Start a write transaction
        # before deferring references to the account table during batch rebuild.
        connection.execute(sa.text("UPDATE native_account SET account_ref = account_ref WHERE 0"))
        deferred = bool(connection.exec_driver_sql("PRAGMA defer_foreign_keys").scalar_one())
        connection.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
    yield
    if sqlite:
        if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise RuntimeError("Employee activation migration cannot preserve foreign-key references")
        if not deferred:
            # DROP TABLE records deferred violations even after its replacement
            # restores every reference. Validate actual facts before clearing them.
            connection.exec_driver_sql("PRAGMA defer_foreign_keys=OFF")


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    def exact(length: int) -> sa.String:
        return sa.String(length, collation="C" if dialect == "postgresql" else "BINARY")

    timestamp = sa.DateTime(timezone=dialect == "postgresql")
    now = sa.text("CURRENT_TIMESTAMP" if dialect == "postgresql" else "(STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))")
    with _account_schema_change():
        with op.batch_alter_table("native_account") as batch:
            batch.drop_constraint("ck_native_account_status", type_="check")
            batch.alter_column("password_hash", existing_type=sa.String(512), nullable=True)
            batch.create_check_constraint("ck_native_account_status", "status IN ('active', 'disabled', 'pending')")
            batch.create_check_constraint(
                "ck_native_account_password", "status != 'active' OR password_hash IS NOT NULL"
            )
        op.create_table(
            "native_account_activation",
            sa.Column("activation_ref", exact(128), primary_key=True),
            sa.Column(
                "account_ref", exact(128), sa.ForeignKey("native_account.account_ref"), nullable=False, unique=True
            ),
            sa.Column("organization_ref", exact(128), nullable=False),
            sa.Column("principal_ref", exact(128), nullable=False),
            sa.Column("membership_ref", exact(128), nullable=False),
            sa.Column(
                "external_identity_ref",
                exact(128),
                sa.ForeignKey("external_identity.external_identity_ref"),
                nullable=False,
                unique=True,
            ),
            sa.Column(
                "source_pin",
                sa.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
                nullable=False,
            ),
            sa.Column("status", exact(32), nullable=False),
            sa.Column("version", sa.BigInteger(), nullable=False),
            sa.Column("code_hash", exact(64), nullable=True),
            sa.Column("expires_at", timestamp, nullable=False),
            sa.Column("browser_hash", exact(64), nullable=True),
            sa.Column("csrf_hash", exact(64), nullable=True),
            sa.Column("ceremony_expires_at", timestamp, nullable=True),
            sa.Column("pending_password_hash", sa.String(512), nullable=True),
            sa.Column("pending_totp", sa.Text(), nullable=True),
            sa.Column("failures", sa.BigInteger(), nullable=False),
            sa.Column("completed_at", timestamp, nullable=True),
            sa.Column("created_at", timestamp, nullable=False, server_default=now),
            sa.Column("updated_at", timestamp, nullable=False, server_default=now),
            sa.ForeignKeyConstraint(
                ("organization_ref", "membership_ref", "principal_ref"),
                ("tenant_membership.tenant_ref", "tenant_membership.membership_ref", "tenant_membership.principal_ref"),
                name="fk_native_activation_membership",
            ),
            sa.CheckConstraint(
                "status IN ('issued','enrolling','completed','revoked')", name="ck_native_activation_status"
            ),
            sa.CheckConstraint("version > 0 AND failures >= 0", name="ck_native_activation_version"),
            sa.CheckConstraint(
                "status NOT IN ('completed','revoked') OR "
                "(code_hash IS NULL AND pending_password_hash IS NULL AND pending_totp IS NULL)",
                name="ck_native_activation_terminal_secrets",
            ),
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT count(*) FROM native_account_activation")).scalar_one():
        raise RuntimeError("Downgrade cannot discard employee activation facts")
    if connection.execute(
        sa.text("SELECT count(*) FROM native_account WHERE status = 'pending' OR password_hash IS NULL")
    ).scalar_one():
        raise RuntimeError("Downgrade cannot discard pending or passwordless native accounts")
    with _account_schema_change():
        op.drop_table("native_account_activation")
        with op.batch_alter_table("native_account") as batch:
            batch.drop_constraint("ck_native_account_password", type_="check")
            batch.drop_constraint("ck_native_account_status", type_="check")
            batch.alter_column("password_hash", existing_type=sa.String(512), nullable=False)
            batch.create_check_constraint("ck_native_account_status", "status IN ('active', 'disabled')")
