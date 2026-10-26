"""Native authentication facts; no business roles or implicit organization grants."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TimestampMixin
from app.models.identity_foundation import _json
from app.storage.types import ExactString, UTCDateTime, utc_now


class NativeAccountTable(TimestampMixin, Base):
    __tablename__ = "native_account"
    __table_args__ = (
        ForeignKeyConstraint(
            ("principal_ref", "principal_type"),
            ("principal.principal_ref", "principal.principal_type"),
            name="fk_native_account_human",
        ),
        CheckConstraint("principal_type = 'human'", name="ck_native_account_human"),
        CheckConstraint("credential_version > 0", name="ck_native_account_version"),
        CheckConstraint("status IN ('active', 'disabled')", name="ck_native_account_status"),
        CheckConstraint("failures >= 0 AND last_totp_step >= -1", name="ck_native_account_counters"),
        CheckConstraint("length(account_ref) > 0 AND length(username) > 0", name="ck_native_account_keys"),
    )
    account_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    principal_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False, unique=True)
    principal_type: Mapped[str] = mapped_column(
        ExactString(32), nullable=False, default="human", server_default="human"
    )
    username: Mapped[str] = mapped_column(ExactString(128), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    credential_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    status: Mapped[str] = mapped_column(ExactString(32), nullable=False, default="active", server_default="active")
    totp_secret: Mapped[str | None] = mapped_column(Text)
    totp_bootstrap_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    last_totp_step: Mapped[int] = mapped_column(BigInteger, nullable=False, default=-1, server_default=text("-1"))
    recovery_hashes: Mapped[list[str]] = mapped_column(_json(), nullable=False, default=list)
    failures: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime())


class NativeClientTable(TimestampMixin, Base):
    __tablename__ = "native_client"
    client_id: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    redirect_uris: Mapped[list[str]] = mapped_column(_json(), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    display_name: Mapped[str] = mapped_column(String(256), nullable=False, default="", server_default="")
    token_endpoint_auth_method: Mapped[str] = mapped_column(
        ExactString(32), nullable=False, default="none", server_default="none"
    )
    client_secret_hash: Mapped[str | None] = mapped_column(ExactString(64))
    allowed_scopes: Mapped[list[str]] = mapped_column(
        _json(), nullable=False, default=lambda: ["openid"], server_default=text("'[\"openid\"]'")
    )
    post_logout_redirect_uris: Mapped[list[str]] = mapped_column(
        _json(), nullable=False, default=list, server_default=text("'[]'")
    )
    require_consent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    resource_audience: Mapped[str] = mapped_column(
        ExactString(128), nullable=False, default="stage-api", server_default="stage-api"
    )
    application_type: Mapped[str] = mapped_column(
        ExactString(32), nullable=False, default="native", server_default="native"
    )


class NativeBrowserSessionTable(Base):
    __tablename__ = "native_browser_session"
    session_hash: Mapped[str] = mapped_column(ExactString(64), primary_key=True)
    account_ref: Mapped[str] = mapped_column(
        ExactString(128), ForeignKey("native_account.account_ref"), nullable=False, index=True
    )
    credential_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    auth_time: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class NativeConsentTable(Base):
    __tablename__ = "native_consent"
    account_ref: Mapped[str] = mapped_column(
        ExactString(128), ForeignKey("native_account.account_ref"), primary_key=True
    )
    client_id: Mapped[str] = mapped_column(ExactString(128), ForeignKey("native_client.client_id"), primary_key=True)
    client_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(_json(), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class NativeLogoutRequestTable(Base):
    __tablename__ = "native_logout_request"
    request_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    browser_hash: Mapped[str] = mapped_column(ExactString(64), nullable=False)
    csrf_hash: Mapped[str] = mapped_column(ExactString(64), nullable=False)
    browser_session_hash: Mapped[str | None] = mapped_column(
        ExactString(64), ForeignKey("native_browser_session.session_hash")
    )
    client_id: Mapped[str | None] = mapped_column(ExactString(128), ForeignKey("native_client.client_id"))
    post_logout_redirect_uri: Mapped[str | None] = mapped_column(String(2048))
    state: Mapped[str | None] = mapped_column(String(1024))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class NativeLoginRequestTable(TimestampMixin, Base):
    __tablename__ = "native_login_request"
    __table_args__ = (
        CheckConstraint("status IN ('password','mfa','enroll','authenticated','done')", name="ck_native_login_status"),
        CheckConstraint("length(browser_hash) = 64 AND length(csrf_hash) = 64", name="ck_native_login_hashes"),
        CheckConstraint("credential_version IS NULL OR credential_version > 0", name="ck_native_login_version"),
        Index("ix_native_login_expiry", "expires_at"),
    )
    request_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    client_id: Mapped[str] = mapped_column(ExactString(128), ForeignKey("native_client.client_id"), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    state: Mapped[str] = mapped_column(String(1024), nullable=False)
    nonce: Mapped[str | None] = mapped_column(String(1024))
    code_challenge: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    scope: Mapped[str] = mapped_column(ExactString(128), nullable=False, default="openid")
    browser_hash: Mapped[str] = mapped_column(ExactString(64), nullable=False)
    csrf_hash: Mapped[str] = mapped_column(ExactString(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(ExactString(32), nullable=False, default="password")
    account_ref: Mapped[str | None] = mapped_column(ExactString(128), ForeignKey("native_account.account_ref"))
    credential_version: Mapped[int | None] = mapped_column(BigInteger)
    password_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    pending_totp: Mapped[str | None] = mapped_column(Text)
    client_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    response_mode: Mapped[str] = mapped_column(ExactString(32), nullable=False, default="query", server_default="query")
    prompt: Mapped[str] = mapped_column(ExactString(128), nullable=False, default="", server_default="")
    max_age: Mapped[int | None] = mapped_column(BigInteger)
    login_hint: Mapped[str | None] = mapped_column(String(128))
    consent_granted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    authentication_reused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    browser_session_hash: Mapped[str | None] = mapped_column(
        ExactString(64), ForeignKey("native_browser_session.session_hash")
    )


class NativeSessionTable(TimestampMixin, Base):
    __tablename__ = "native_session"
    __table_args__ = (
        CheckConstraint("credential_version > 0", name="ck_native_session_version"),
        CheckConstraint("auth_time < expires_at", name="ck_native_session_validity"),
        Index("ix_native_session_account", "account_ref", "revoked_at"),
        Index("ix_native_session_client", "client_id", "revoked_at"),
        Index("ix_native_session_browser", "browser_session_hash"),
    )
    session_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    account_ref: Mapped[str] = mapped_column(ExactString(128), ForeignKey("native_account.account_ref"), nullable=False)
    client_id: Mapped[str] = mapped_column(ExactString(128), ForeignKey("native_client.client_id"), nullable=False)
    credential_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    auth_time: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    acr: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    amr: Mapped[list[str]] = mapped_column(_json(), nullable=False)
    nonce: Mapped[str | None] = mapped_column(String(1024))
    client_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    scope: Mapped[str] = mapped_column(ExactString(128), nullable=False, default="openid", server_default="openid")
    browser_session_hash: Mapped[str | None] = mapped_column(
        ExactString(64), ForeignKey("native_browser_session.session_hash")
    )


class NativeAuthorizationCodeTable(Base):
    __tablename__ = "native_authorization_code"
    code_hash: Mapped[str] = mapped_column(ExactString(64), primary_key=True)
    session_ref: Mapped[str] = mapped_column(ExactString(128), ForeignKey("native_session.session_ref"), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    code_challenge: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class NativeRefreshTokenTable(Base):
    __tablename__ = "native_refresh_token"
    token_hash: Mapped[str] = mapped_column(ExactString(64), primary_key=True)
    session_ref: Mapped[str] = mapped_column(ExactString(128), ForeignKey("native_session.session_ref"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    scope: Mapped[str | None] = mapped_column(ExactString(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=utc_now())


class NativeAuthEventTable(IdMixin, Base):
    __tablename__ = "native_auth_event"
    event: Mapped[str] = mapped_column(ExactString(96), nullable=False)
    account_ref: Mapped[str | None] = mapped_column(ExactString(128), ForeignKey("native_account.account_ref"))
    client_id: Mapped[str | None] = mapped_column(ExactString(128))
    detail: Mapped[dict[str, Any]] = mapped_column(_json(), nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=utc_now())


class NativeLoginThrottleTable(Base):
    __tablename__ = "native_login_throttle"
    __table_args__ = (CheckConstraint("failures >= 0", name="ck_native_throttle_failures"),)
    bucket_hash: Mapped[str] = mapped_column(ExactString(64), primary_key=True)
    failures: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    window_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    blocked_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
