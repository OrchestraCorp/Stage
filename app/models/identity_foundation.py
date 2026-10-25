"""Authoritative identity and organization-admission facts for the first IAM slice.

Profile content and identity-link targets are installed by the trusted registrar.
Lifecycle changes use version checks and the trust/principal/organization lock protocol;
these rows never turn a request claim into an authority by themselves.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.storage.types import ExactString, UTCDateTime, utc_now


def _json() -> JSON:
    return JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


class VersionMixin:
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))


class StatusMixin:
    status: Mapped[str] = mapped_column(ExactString(32), nullable=False, default="active", server_default="active")


class ValidityMixin:
    valid_from: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class TrustStateTable(VersionMixin, TimestampMixin, Base):
    __tablename__ = "trust_state"
    __table_args__ = (
        CheckConstraint("length(trim(trust_ref)) > 0", name="ck_trust_state_ref"),
        CheckConstraint("purpose IN ('authentication', 'release')", name="ck_trust_state_purpose"),
        CheckConstraint("trust_config_revision > 0 AND key_status_revision > 0", name="ck_trust_state_revisions"),
        CheckConstraint("length(configuration_digest) = 64", name="ck_trust_state_digest"),
        CheckConstraint("version > 0", name="ck_trust_state_version"),
    )

    trust_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    purpose: Mapped[str] = mapped_column(ExactString(32), nullable=False)
    trust_config_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    key_status_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    configuration_digest: Mapped[str] = mapped_column(ExactString(64), nullable=False)


class IdentityProviderTable(VersionMixin, StatusMixin, TimestampMixin, Base):
    __tablename__ = "identity_provider"
    __table_args__ = (
        ForeignKeyConstraint(("trust_ref",), ("trust_state.trust_ref",), name="fk_identity_provider_trust"),
        UniqueConstraint("issuer_exact", name="uk_identity_provider_issuer"),
        CheckConstraint(
            "length(trim(provider_ref)) > 0 AND length(trim(issuer_exact)) > 0", name="ck_identity_provider_keys"
        ),
        CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="ck_identity_provider_status"),
        CheckConstraint("version > 0", name="ck_identity_provider_version"),
    )

    provider_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    issuer_exact: Mapped[str] = mapped_column(ExactString(512), nullable=False)
    trust_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)


class AuthenticationProfileTable(VersionMixin, StatusMixin, TimestampMixin, Base):
    __tablename__ = "authentication_profile"
    __table_args__ = (
        ForeignKeyConstraint(("provider_ref",), ("identity_provider.provider_ref",), name="fk_auth_profile_provider"),
        CheckConstraint(
            "length(trim(profile_ref)) > 0 AND length(trim(subject_namespace)) > 0 "
            "AND length(trim(stage_audience)) > 0 AND length(trim(config_schema)) > 0",
            name="ck_auth_profile_keys",
        ),
        CheckConstraint("profile_version > 0 AND version > 0", name="ck_auth_profile_versions"),
        CheckConstraint("credential_kind IN ('human_oidc', 'workload_oauth2')", name="ck_auth_profile_kind"),
        CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="ck_auth_profile_status"),
        CheckConstraint("length(digest) = 64", name="ck_auth_profile_digest"),
    )

    profile_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    profile_version: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    subject_namespace: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    credential_kind: Mapped[str] = mapped_column(ExactString(32), nullable=False)
    stage_audience: Mapped[str] = mapped_column(ExactString(256), nullable=False)
    config_schema: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(_json(), nullable=False)
    digest: Mapped[str] = mapped_column(ExactString(64), nullable=False)


class OrganizationRootTable(VersionMixin, StatusMixin, TimestampMixin, Base):
    """The deployment's sole authorization root, independent of legal entities."""

    __tablename__ = "organization_root"
    __table_args__ = (
        CheckConstraint("deployment_slot = 1", name="ck_tenant_deployment_slot"),
        Index("uk_tenant_deployment_slot", "deployment_slot", unique=True),
        CheckConstraint("length(trim(organization_ref)) > 0 AND length(trim(display_name)) > 0", name="ck_tenant_keys"),
        CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="ck_tenant_status"),
        CheckConstraint("version > 0", name="ck_tenant_version"),
    )

    organization_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    deployment_slot: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1, server_default=text("1"))
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)


class OrganizationAuthenticationBindingTable(VersionMixin, StatusMixin, ValidityMixin, TimestampMixin, Base):
    __tablename__ = "tenant_authentication_binding"
    __table_args__ = (
        ForeignKeyConstraint(
            ("tenant_ref",), ("organization_root.organization_ref",), name="fk_tenant_auth_binding_tenant"
        ),
        ForeignKeyConstraint(
            ("profile_ref", "profile_version"),
            ("authentication_profile.profile_ref", "authentication_profile.profile_version"),
            name="fk_tenant_auth_binding_profile",
        ),
        CheckConstraint("length(trim(binding_ref)) > 0 AND length(trim(environment)) > 0", name="ck_tenant_auth_keys"),
        CheckConstraint("status IN ('active', 'suspended', 'revoked', 'expired')", name="ck_tenant_auth_status"),
        CheckConstraint("expires_at IS NULL OR valid_from < expires_at", name="ck_tenant_auth_validity"),
        CheckConstraint("version > 0", name="ck_tenant_auth_version"),
        Index(
            "uk_tenant_auth_active_profile",
            "tenant_ref",
            "profile_ref",
            "profile_version",
            "environment",
            unique=True,
            sqlite_where=text("status IN ('active', 'suspended')"),
            postgresql_where=text("status IN ('active', 'suspended')"),
        ),
    )

    organization_ref: Mapped[str] = mapped_column("tenant_ref", ExactString(128), primary_key=True)
    binding_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    profile_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    profile_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    environment: Mapped[str] = mapped_column(ExactString(64), nullable=False)
    constraints_json: Mapped[dict[str, Any]] = mapped_column(_json(), nullable=False)


class ExternalIdentityTable(VersionMixin, StatusMixin, TimestampMixin, Base):
    __tablename__ = "external_identity"
    __table_args__ = (
        ForeignKeyConstraint(
            ("provider_ref",), ("identity_provider.provider_ref",), name="fk_external_identity_provider"
        ),
        ForeignKeyConstraint(
            ("principal_ref", "principal_type"),
            ("principal.principal_ref", "principal.principal_type"),
            name="fk_external_identity_principal_type",
        ),
        UniqueConstraint("provider_ref", "subject_namespace", "subject_exact", name="uk_external_identity_subject"),
        UniqueConstraint(
            "external_identity_ref", "principal_ref", "principal_type", name="uk_external_identity_principal"
        ),
        CheckConstraint(
            "length(trim(external_identity_ref)) > 0 AND length(trim(subject_namespace)) > 0 "
            "AND length(trim(subject_exact)) > 0 AND length(trim(link_evidence_ref)) > 0",
            name="ck_external_identity_keys",
        ),
        CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="ck_external_identity_status"),
        CheckConstraint("version > 0", name="ck_external_identity_version"),
        Index("ix_external_identity_principal", "principal_ref"),
    )

    external_identity_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    provider_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    subject_namespace: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    subject_exact: Mapped[str] = mapped_column(ExactString(256), nullable=False)
    principal_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    principal_type: Mapped[str] = mapped_column(ExactString(32), nullable=False)
    link_evidence_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)


class OrganizationMembershipTable(VersionMixin, StatusMixin, ValidityMixin, TimestampMixin, Base):
    __tablename__ = "tenant_membership"
    __table_args__ = (
        ForeignKeyConstraint(
            ("tenant_ref",), ("organization_root.organization_ref",), name="fk_tenant_membership_tenant"
        ),
        ForeignKeyConstraint(
            ("principal_ref", "principal_type"),
            ("principal.principal_ref", "principal.principal_type"),
            name="fk_tenant_membership_principal_type",
        ),
        UniqueConstraint("tenant_ref", "membership_ref", "principal_ref", name="uk_tenant_membership_principal"),
        CheckConstraint("length(trim(membership_ref)) > 0 AND length(trim(source_ref)) > 0", name="ck_membership_keys"),
        CheckConstraint("status IN ('active', 'suspended', 'left', 'revoked', 'expired')", name="ck_membership_status"),
        CheckConstraint("expires_at IS NULL OR valid_from < expires_at", name="ck_membership_validity"),
        CheckConstraint(
            "(status IN ('active', 'suspended') AND ended_at IS NULL) OR "
            "(status IN ('left', 'revoked', 'expired') AND ended_at IS NOT NULL)",
            name="ck_membership_ended_at",
        ),
        CheckConstraint("version > 0", name="ck_membership_version"),
        Index(
            "uk_membership_current_principal",
            "tenant_ref",
            "principal_ref",
            unique=True,
            sqlite_where=text("status IN ('active', 'suspended')"),
            postgresql_where=text("status IN ('active', 'suspended')"),
        ),
        Index("ix_membership_principal_tenant", "principal_ref", "tenant_ref"),
    )

    organization_ref: Mapped[str] = mapped_column("tenant_ref", ExactString(128), primary_key=True)
    membership_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    principal_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    principal_type: Mapped[str] = mapped_column(ExactString(32), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    source_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)


class WorkloadRegistrationTable(VersionMixin, StatusMixin, ValidityMixin, TimestampMixin, Base):
    __tablename__ = "workload_registration"
    __table_args__ = (
        ForeignKeyConstraint(
            ("service_principal_ref", "principal_type"),
            ("principal.principal_ref", "principal.principal_type"),
            name="fk_workload_principal_type",
        ),
        ForeignKeyConstraint(
            ("profile_ref", "profile_version"),
            ("authentication_profile.profile_ref", "authentication_profile.profile_version"),
            name="fk_workload_profile",
        ),
        ForeignKeyConstraint(
            ("external_identity_ref", "service_principal_ref", "principal_type"),
            (
                "external_identity.external_identity_ref",
                "external_identity.principal_ref",
                "external_identity.principal_type",
            ),
            name="fk_workload_external_identity",
        ),
        UniqueConstraint(
            "workload_ref", "service_principal_ref", "environment", name="uk_workload_service_environment"
        ),
        CheckConstraint("principal_type = 'service'", name="ck_workload_principal_type"),
        CheckConstraint(
            "length(trim(workload_ref)) > 0 AND length(trim(verified_client_ref)) > 0 "
            "AND length(trim(environment)) > 0",
            name="ck_workload_keys",
        ),
        CheckConstraint("status IN ('active', 'suspended', 'revoked', 'expired')", name="ck_workload_status"),
        CheckConstraint("expires_at IS NULL OR valid_from < expires_at", name="ck_workload_validity"),
        CheckConstraint("version > 0", name="ck_workload_version"),
        Index("ix_workload_principal_environment", "service_principal_ref", "environment", "status"),
    )

    workload_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    service_principal_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    principal_type: Mapped[str] = mapped_column(
        ExactString(32), nullable=False, default="service", server_default="service"
    )
    profile_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    profile_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    external_identity_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    verified_client_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    environment: Mapped[str] = mapped_column(ExactString(64), nullable=False)


class IdentityControlOperationTable(Base):
    __tablename__ = "identity_control_operation"
    __table_args__ = (
        UniqueConstraint(
            "actor_kind", "actor_ref", "operation_type", "idempotency_key", name="uk_identity_operation_idempotency"
        ),
        CheckConstraint(
            "length(trim(operation_ref)) > 0 AND length(trim(operation_type)) > 0 "
            "AND length(trim(idempotency_key)) > 0 AND length(trim(actor_ref)) > 0 "
            "AND length(trim(target_ref)) > 0",
            name="ck_identity_operation_keys",
        ),
        CheckConstraint("actor_kind IN ('human', 'service', 'bootstrap_operator')", name="ck_identity_operation_actor"),
        CheckConstraint("length(input_digest) = 64", name="ck_identity_operation_digest"),
    )

    operation_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    operation_type: Mapped[str] = mapped_column(ExactString(96), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    input_digest: Mapped[str] = mapped_column(ExactString(64), nullable=False)
    actor_kind: Mapped[str] = mapped_column(ExactString(32), nullable=False)
    actor_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    authentication_json: Mapped[dict[str, Any]] = mapped_column(_json(), nullable=False)
    target_ref: Mapped[str] = mapped_column(ExactString(256), nullable=False)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(_json())
    after_json: Mapped[dict[str, Any] | None] = mapped_column(_json())
    result_json: Mapped[dict[str, Any]] = mapped_column(_json(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=utc_now())
    completed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class BootstrapRecordTable(Base):
    __tablename__ = "bootstrap_record"
    __table_args__ = (
        UniqueConstraint("environment", "bootstrap_type", name="uk_bootstrap_environment_type"),
        ForeignKeyConstraint(
            ("initial_identity_ref",),
            ("external_identity.external_identity_ref",),
            name="fk_bootstrap_initial_identity",
        ),
        CheckConstraint(
            "length(trim(bootstrap_ref)) > 0 AND length(trim(environment)) > 0 AND length(trim(bootstrap_type)) > 0",
            name="ck_bootstrap_keys",
        ),
        CheckConstraint("length(configuration_digest) = 64", name="ck_bootstrap_digest"),
    )

    bootstrap_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    environment: Mapped[str] = mapped_column(ExactString(64), nullable=False)
    bootstrap_type: Mapped[str] = mapped_column(ExactString(96), nullable=False)
    configuration_digest: Mapped[str] = mapped_column(ExactString(64), nullable=False)
    initial_identity_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    initial_grant_refs: Mapped[list[str]] = mapped_column(_json(), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
