"""Controlled OAuth clients, browser SSO, consent and logout transactions.

Revision ID: c6f3a19d820b
Revises: b7f0d3a9216c
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "c6f3a19d820b"
down_revision: str | None = "b7f0d3a9216c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLIENT_COLUMNS = [
    "version",
    "display_name",
    "token_endpoint_auth_method",
    "client_secret_hash",
    "allowed_scopes",
    "post_logout_redirect_uris",
    "require_consent",
    "resource_audience",
    "application_type",
]
REQUEST_COLUMNS = [
    "client_version",
    "response_mode",
    "prompt",
    "max_age",
    "login_hint",
    "consent_granted",
    "authentication_reused",
    "browser_session_hash",
]
SESSION_COLUMNS = ["client_version", "scope", "browser_session_hash"]


def upgrade() -> None:
    dialect = op.get_bind().dialect.name

    def exact(length: int) -> sa.String:
        return sa.String(length, collation="C" if dialect == "postgresql" else "BINARY")

    timestamp = sa.DateTime(timezone=dialect == "postgresql")
    json_type = sa.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
    columns = [
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("display_name", sa.String(256), nullable=False, server_default=""),
        sa.Column("token_endpoint_auth_method", exact(32), nullable=False, server_default="none"),
        sa.Column("client_secret_hash", exact(64), nullable=True),
        sa.Column("allowed_scopes", json_type, nullable=False, server_default=sa.text("'[\"openid\"]'")),
        sa.Column("post_logout_redirect_uris", json_type, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("require_consent", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("resource_audience", exact(128), nullable=False, server_default="stage-api"),
        sa.Column("application_type", exact(32), nullable=False, server_default="native"),
    ]
    for column in columns:
        op.add_column("native_client", column)
    op.create_table(
        "native_browser_session",
        sa.Column("session_hash", exact(64), primary_key=True),
        sa.Column("account_ref", exact(128), sa.ForeignKey("native_account.account_ref"), nullable=False),
        sa.Column("credential_version", sa.BigInteger(), nullable=False),
        sa.Column("auth_time", timestamp, nullable=False),
        sa.Column("expires_at", timestamp, nullable=False),
        sa.Column("revoked_at", timestamp, nullable=True),
    )
    op.create_index("ix_native_browser_session_account_ref", "native_browser_session", ["account_ref"])
    op.create_table(
        "native_consent",
        sa.Column("account_ref", exact(128), sa.ForeignKey("native_account.account_ref"), primary_key=True),
        sa.Column("client_id", exact(128), sa.ForeignKey("native_client.client_id"), primary_key=True),
        sa.Column("client_version", sa.BigInteger(), nullable=False),
        sa.Column("scopes", json_type, nullable=False),
        sa.Column("granted_at", timestamp, nullable=False),
    )
    op.create_table(
        "native_logout_request",
        sa.Column("request_ref", exact(128), primary_key=True),
        sa.Column("browser_hash", exact(64), nullable=False),
        sa.Column("csrf_hash", exact(64), nullable=False),
        sa.Column(
            "browser_session_hash", exact(64), sa.ForeignKey("native_browser_session.session_hash"), nullable=True
        ),
        sa.Column("client_id", exact(128), sa.ForeignKey("native_client.client_id"), nullable=True),
        sa.Column("post_logout_redirect_uri", sa.String(2048), nullable=True),
        sa.Column("state", sa.String(1024), nullable=True),
        sa.Column("expires_at", timestamp, nullable=False),
        sa.Column("consumed_at", timestamp, nullable=True),
    )
    with op.batch_alter_table("native_login_request") as batch:
        for column in [
            sa.Column("client_version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
            sa.Column("response_mode", exact(32), nullable=False, server_default="query"),
            sa.Column("prompt", exact(128), nullable=False, server_default=""),
            sa.Column("max_age", sa.BigInteger(), nullable=True),
            sa.Column("login_hint", sa.String(128), nullable=True),
            sa.Column("consent_granted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("authentication_reused", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("browser_session_hash", exact(64), nullable=True),
        ]:
            batch.add_column(column)
        batch.create_foreign_key(
            "fk_native_login_browser", "native_browser_session", ["browser_session_hash"], ["session_hash"]
        )
    with op.batch_alter_table("native_session") as batch:
        batch.add_column(sa.Column("client_version", sa.BigInteger(), nullable=False, server_default=sa.text("1")))
        batch.add_column(sa.Column("scope", exact(128), nullable=False, server_default="openid"))
        batch.add_column(sa.Column("browser_session_hash", exact(64), nullable=True))
        batch.create_foreign_key(
            "fk_native_session_browser", "native_browser_session", ["browser_session_hash"], ["session_hash"]
        )
        batch.create_index("ix_native_session_client", ["client_id", "revoked_at"])
        batch.create_index("ix_native_session_browser", ["browser_session_hash"])
    op.add_column("native_refresh_token", sa.Column("scope", exact(128), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    for table in [
        "native_browser_session",
        "native_consent",
        "native_logout_request",
        "native_account",
        "native_client",
    ]:
        if connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one():
            raise RuntimeError("Downgrade cannot discard OAuth client or browser authorization facts")
    op.drop_column("native_refresh_token", "scope")
    with op.batch_alter_table("native_session") as batch:
        batch.drop_index("ix_native_session_client")
        batch.drop_index("ix_native_session_browser")
        batch.drop_constraint("fk_native_session_browser", type_="foreignkey")
        for name in reversed(SESSION_COLUMNS):
            batch.drop_column(name)
    with op.batch_alter_table("native_login_request") as batch:
        batch.drop_constraint("fk_native_login_browser", type_="foreignkey")
        for name in reversed(REQUEST_COLUMNS):
            batch.drop_column(name)
    op.drop_table("native_logout_request")
    op.drop_table("native_consent")
    op.drop_table("native_browser_session")
    for name in reversed(CLIENT_COLUMNS):
        op.drop_column("native_client", name)
