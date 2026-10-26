"""Native first-party account, MFA and OAuth credential facts.

Revision ID: e8a1c6d9f240
Revises: 9c2e7a4b6d10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "e8a1c6d9f240"
down_revision: str | None = "9c2e7a4b6d10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = [
    "native_client",
    "native_login_throttle",
    "native_account",
    "native_auth_event",
    "native_login_request",
    "native_session",
    "native_authorization_code",
    "native_refresh_token",
]


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    collation = "C" if dialect == "postgresql" else "BINARY"
    timestamp = sa.DateTime(timezone=dialect == "postgresql")
    now = sa.text("CURRENT_TIMESTAMP" if dialect == "postgresql" else "(STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))")

    def exact(length: int) -> sa.String:
        return sa.String(length, collation=collation)

    op.create_table(
        "native_client",
        sa.Column("client_id", exact(128), nullable=False),
        sa.Column(
            "redirect_uris",
            sa.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", timestamp, server_default=now, nullable=False),
        sa.Column("updated_at", timestamp, server_default=now, nullable=False),
        sa.PrimaryKeyConstraint("client_id"),
    )
    op.create_table(
        "native_login_throttle",
        sa.Column("bucket_hash", exact(64), nullable=False),
        sa.Column("failures", sa.BigInteger(), nullable=False),
        sa.Column("window_start", timestamp, nullable=False),
        sa.Column("blocked_until", timestamp, nullable=True),
        sa.CheckConstraint("failures >= 0", name="ck_native_throttle_failures"),
        sa.PrimaryKeyConstraint("bucket_hash"),
    )
    op.create_table(
        "native_account",
        sa.Column("account_ref", exact(128), nullable=False),
        sa.Column("principal_ref", exact(128), nullable=False),
        sa.Column("principal_type", exact(32), server_default="human", nullable=False),
        sa.Column("username", exact(128), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("credential_version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", exact(32), server_default="active", nullable=False),
        sa.Column("totp_secret", sa.Text(), nullable=True),
        sa.Column("last_totp_step", sa.BigInteger(), server_default=sa.text("-1"), nullable=False),
        sa.Column(
            "recovery_hashes",
            sa.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
            nullable=False,
        ),
        sa.Column("failures", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_until", timestamp, nullable=True),
        sa.Column("created_at", timestamp, server_default=now, nullable=False),
        sa.Column("updated_at", timestamp, server_default=now, nullable=False),
        sa.CheckConstraint("principal_type = 'human'", name="ck_native_account_human"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_native_account_status"),
        sa.CheckConstraint("credential_version > 0", name="ck_native_account_version"),
        sa.CheckConstraint("failures >= 0 AND last_totp_step >= -1", name="ck_native_account_counters"),
        sa.CheckConstraint("length(account_ref) > 0 AND length(username) > 0", name="ck_native_account_keys"),
        sa.ForeignKeyConstraint(
            ["principal_ref", "principal_type"],
            ["principal.principal_ref", "principal.principal_type"],
            name="fk_native_account_human",
        ),
        sa.PrimaryKeyConstraint("account_ref"),
        sa.UniqueConstraint("principal_ref"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "native_auth_event",
        sa.Column("event", exact(96), nullable=False),
        sa.Column("account_ref", exact(128), nullable=True),
        sa.Column("client_id", exact(128), nullable=True),
        sa.Column(
            "detail",
            sa.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", timestamp, server_default=now, nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["account_ref"],
            ["native_account.account_ref"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "native_login_request",
        sa.Column("request_ref", exact(128), nullable=False),
        sa.Column("client_id", exact(128), nullable=False),
        sa.Column("redirect_uri", sa.String(length=2048), nullable=False),
        sa.Column("state", sa.String(length=1024), nullable=False),
        sa.Column("nonce", sa.String(length=1024), nullable=True),
        sa.Column("code_challenge", exact(128), nullable=False),
        sa.Column("scope", exact(128), nullable=False),
        sa.Column("browser_hash", exact(64), nullable=False),
        sa.Column("csrf_hash", exact(64), nullable=False),
        sa.Column("expires_at", timestamp, nullable=False),
        sa.Column("status", exact(32), nullable=False),
        sa.Column("account_ref", exact(128), nullable=True),
        sa.Column("credential_version", sa.BigInteger(), nullable=True),
        sa.Column("password_verified_at", timestamp, nullable=True),
        sa.Column("pending_totp", sa.Text(), nullable=True),
        sa.Column("created_at", timestamp, server_default=now, nullable=False),
        sa.Column("updated_at", timestamp, server_default=now, nullable=False),
        sa.CheckConstraint(
            "status IN ('password','mfa','enroll','authenticated','done')", name="ck_native_login_status"
        ),
        sa.CheckConstraint("credential_version IS NULL OR credential_version > 0", name="ck_native_login_version"),
        sa.CheckConstraint("length(browser_hash) = 64 AND length(csrf_hash) = 64", name="ck_native_login_hashes"),
        sa.ForeignKeyConstraint(
            ["account_ref"],
            ["native_account.account_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["native_client.client_id"],
        ),
        sa.PrimaryKeyConstraint("request_ref"),
    )
    op.create_index("ix_native_login_expiry", "native_login_request", ["expires_at"], unique=False)
    op.create_table(
        "native_session",
        sa.Column("session_ref", exact(128), nullable=False),
        sa.Column("account_ref", exact(128), nullable=False),
        sa.Column("client_id", exact(128), nullable=False),
        sa.Column("credential_version", sa.BigInteger(), nullable=False),
        sa.Column("auth_time", timestamp, nullable=False),
        sa.Column("expires_at", timestamp, nullable=False),
        sa.Column("revoked_at", timestamp, nullable=True),
        sa.Column("acr", exact(128), nullable=False),
        sa.Column(
            "amr",
            sa.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
            nullable=False,
        ),
        sa.Column("nonce", sa.String(length=1024), nullable=True),
        sa.Column("created_at", timestamp, server_default=now, nullable=False),
        sa.Column("updated_at", timestamp, server_default=now, nullable=False),
        sa.CheckConstraint("auth_time < expires_at", name="ck_native_session_validity"),
        sa.CheckConstraint("credential_version > 0", name="ck_native_session_version"),
        sa.ForeignKeyConstraint(
            ["account_ref"],
            ["native_account.account_ref"],
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["native_client.client_id"],
        ),
        sa.PrimaryKeyConstraint("session_ref"),
    )
    op.create_index("ix_native_session_account", "native_session", ["account_ref", "revoked_at"], unique=False)
    op.create_table(
        "native_authorization_code",
        sa.Column("code_hash", exact(64), nullable=False),
        sa.Column("session_ref", exact(128), nullable=False),
        sa.Column("redirect_uri", sa.String(length=2048), nullable=False),
        sa.Column("code_challenge", exact(128), nullable=False),
        sa.Column("expires_at", timestamp, nullable=False),
        sa.Column("consumed_at", timestamp, nullable=True),
        sa.ForeignKeyConstraint(
            ["session_ref"],
            ["native_session.session_ref"],
        ),
        sa.PrimaryKeyConstraint("code_hash"),
    )
    op.create_table(
        "native_refresh_token",
        sa.Column("token_hash", exact(64), nullable=False),
        sa.Column("session_ref", exact(128), nullable=False),
        sa.Column("expires_at", timestamp, nullable=False),
        sa.Column("consumed_at", timestamp, nullable=True),
        sa.Column("created_at", timestamp, server_default=now, nullable=False),
        sa.ForeignKeyConstraint(
            ["session_ref"],
            ["native_session.session_ref"],
        ),
        sa.PrimaryKeyConstraint("token_hash"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    for name in TABLES:
        if connection.execute(sa.text(f"SELECT count(*) FROM {name}")).scalar_one():
            raise RuntimeError("Downgrade cannot discard native authentication facts")
    for name in reversed(TABLES):
        op.drop_table(name)
