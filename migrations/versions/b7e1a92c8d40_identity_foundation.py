"""Add explicit identity sources, tenant admission and member tenures.

Revision ID: b7e1a92c8d40
Revises: a1c4e7d9b203

Existing principal IDs and legacy authorization rows are preserved. No tenant or
membership is inferred from historical scopes. Storage definitions are frozen
here, independently of the application's current models.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "b7e1a92c8d40"
down_revision: str | None = "a1c4e7d9b203"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "identity_control_operation",
    "tenant",
    "trust_state",
    "identity_provider",
    "tenant_membership",
    "authentication_profile",
    "external_identity",
    "bootstrap_record",
    "tenant_authentication_binding",
    "workload_registration",
)


def _exact_string(length: int, dialect: str) -> sa.String:
    return sa.String(length, collation="C" if dialect == "postgresql" else "BINARY")


def _timestamp(dialect: str) -> sa.DateTime:
    return sa.DateTime(timezone=dialect == "postgresql")


def _now(dialect: str) -> sa.TextClause:
    return sa.text("CURRENT_TIMESTAMP" if dialect == "postgresql" else "(STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))")


def _json(dialect: str) -> sa.JSON:
    return JSONB(none_as_null=True) if dialect == "postgresql" else sa.JSON(none_as_null=True)


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in ("postgresql", "sqlite"):
        raise RuntimeError(f"Unsupported Stage identity database: {dialect}")
    with op.batch_alter_table("principal", schema=None) as batch_op:
        batch_op.add_column(sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False))
        batch_op.add_column(sa.Column("identity_epoch", sa.BigInteger(), server_default=sa.text("0"), nullable=False))
        batch_op.add_column(sa.Column("recovery_epoch", sa.BigInteger(), server_default=sa.text("0"), nullable=False))
        batch_op.add_column(sa.Column("disabled_at", _timestamp(dialect), nullable=True))
        batch_op.create_unique_constraint("uk_principal_ref_type", ["principal_ref", "principal_type"])

        batch_op.create_check_constraint("ck_principal_version", "version > 0")
        batch_op.create_check_constraint("ck_principal_epochs", "identity_epoch >= 0 AND recovery_epoch >= 0")

    op.create_table(
        "identity_control_operation",
        sa.Column("operation_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("operation_type", _exact_string(96, dialect), nullable=False),
        sa.Column("idempotency_key", _exact_string(128, dialect), nullable=False),
        sa.Column("input_digest", _exact_string(64, dialect), nullable=False),
        sa.Column("actor_kind", _exact_string(32, dialect), nullable=False),
        sa.Column("actor_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("authentication_json", _json(dialect), nullable=False),
        sa.Column("target_ref", _exact_string(256, dialect), nullable=False),
        sa.Column("before_json", _json(dialect), nullable=True),
        sa.Column("after_json", _json(dialect), nullable=True),
        sa.Column("result_json", _json(dialect), nullable=False),
        sa.Column("created_at", _timestamp(dialect), server_default=_now(dialect), nullable=False),
        sa.Column("completed_at", _timestamp(dialect), nullable=False),
        sa.CheckConstraint(
            "actor_kind IN ('human', 'service', 'bootstrap_operator')", name="ck_identity_operation_actor"
        ),
        sa.CheckConstraint("length(input_digest) = 64", name="ck_identity_operation_digest"),
        sa.CheckConstraint(
            "length(trim(operation_ref)) > 0 AND length(trim(operation_type)) > 0 "
            "AND length(trim(idempotency_key)) > 0 AND length(trim(actor_ref)) > 0 AND length(trim(target_ref)) > 0",
            name="ck_identity_operation_keys",
        ),
        sa.PrimaryKeyConstraint("operation_ref"),
        sa.UniqueConstraint(
            "actor_kind", "actor_ref", "operation_type", "idempotency_key", name="uk_identity_operation_idempotency"
        ),
    )
    op.create_table(
        "tenant",
        sa.Column("tenant_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", _exact_string(32, dialect), server_default="active", nullable=False),
        sa.Column("created_at", _timestamp(dialect), server_default=_now(dialect), nullable=False),
        sa.Column("updated_at", _timestamp(dialect), server_default=_now(dialect), nullable=False),
        sa.CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="ck_tenant_status"),
        sa.CheckConstraint("length(trim(tenant_ref)) > 0 AND length(trim(display_name)) > 0", name="ck_tenant_keys"),
        sa.CheckConstraint("version > 0", name="ck_tenant_version"),
        sa.PrimaryKeyConstraint("tenant_ref"),
    )
    op.create_table(
        "trust_state",
        sa.Column("trust_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("purpose", _exact_string(32, dialect), nullable=False),
        sa.Column("trust_config_revision", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("key_status_revision", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("configuration_digest", _exact_string(64, dialect), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", _timestamp(dialect), server_default=_now(dialect), nullable=False),
        sa.Column("updated_at", _timestamp(dialect), server_default=_now(dialect), nullable=False),
        sa.CheckConstraint("purpose IN ('authentication', 'release')", name="ck_trust_state_purpose"),
        sa.CheckConstraint("length(configuration_digest) = 64", name="ck_trust_state_digest"),
        sa.CheckConstraint("length(trim(trust_ref)) > 0", name="ck_trust_state_ref"),
        sa.CheckConstraint("trust_config_revision > 0 AND key_status_revision > 0", name="ck_trust_state_revisions"),
        sa.CheckConstraint("version > 0", name="ck_trust_state_version"),
        sa.PrimaryKeyConstraint("trust_ref"),
    )
    op.create_table(
        "identity_provider",
        sa.Column("provider_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("issuer_exact", _exact_string(512, dialect), nullable=False),
        sa.Column("trust_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", _exact_string(32, dialect), server_default="active", nullable=False),
        sa.Column("created_at", _timestamp(dialect), server_default=_now(dialect), nullable=False),
        sa.Column("updated_at", _timestamp(dialect), server_default=_now(dialect), nullable=False),
        sa.CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="ck_identity_provider_status"),
        sa.CheckConstraint(
            "length(trim(provider_ref)) > 0 AND length(trim(issuer_exact)) > 0", name="ck_identity_provider_keys"
        ),
        sa.CheckConstraint("version > 0", name="ck_identity_provider_version"),
        sa.ForeignKeyConstraint(["trust_ref"], ["trust_state.trust_ref"], name="fk_identity_provider_trust"),
        sa.PrimaryKeyConstraint("provider_ref"),
        sa.UniqueConstraint("issuer_exact", name="uk_identity_provider_issuer"),
    )
    op.create_table(
        "tenant_membership",
        sa.Column("tenant_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("membership_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("principal_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("principal_type", _exact_string(32, dialect), nullable=False),
        sa.Column("ended_at", _timestamp(dialect), nullable=True),
        sa.Column("source_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", _exact_string(32, dialect), server_default="active", nullable=False),
        sa.Column("valid_from", _timestamp(dialect), nullable=False),
        sa.Column("expires_at", _timestamp(dialect), nullable=True),
        sa.Column("created_at", _timestamp(dialect), server_default=_now(dialect), nullable=False),
        sa.Column("updated_at", _timestamp(dialect), server_default=_now(dialect), nullable=False),
        sa.CheckConstraint(
            "(status IN ('active', 'suspended') AND ended_at IS NULL) OR "
            "(status IN ('left', 'revoked', 'expired') AND ended_at IS NOT NULL)",
            name="ck_membership_ended_at",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'left', 'revoked', 'expired')", name="ck_membership_status"
        ),
        sa.CheckConstraint("expires_at IS NULL OR valid_from < expires_at", name="ck_membership_validity"),
        sa.CheckConstraint(
            "length(trim(membership_ref)) > 0 AND length(trim(source_ref)) > 0", name="ck_membership_keys"
        ),
        sa.CheckConstraint("version > 0", name="ck_membership_version"),
        sa.ForeignKeyConstraint(
            ["principal_ref", "principal_type"],
            ["principal.principal_ref", "principal.principal_type"],
            name="fk_tenant_membership_principal_type",
        ),
        sa.ForeignKeyConstraint(["tenant_ref"], ["tenant.tenant_ref"], name="fk_tenant_membership_tenant"),
        sa.PrimaryKeyConstraint("tenant_ref", "membership_ref"),
        sa.UniqueConstraint("tenant_ref", "membership_ref", "principal_ref", name="uk_tenant_membership_principal"),
    )
    with op.batch_alter_table("tenant_membership", schema=None) as batch_op:
        batch_op.create_index("ix_membership_principal_tenant", ["principal_ref", "tenant_ref"], unique=False)
        batch_op.create_index(
            "uk_membership_current_principal",
            ["tenant_ref", "principal_ref"],
            unique=True,
            sqlite_where=sa.text("status IN ('active', 'suspended')"),
            postgresql_where=sa.text("status IN ('active', 'suspended')"),
        )

    op.create_table(
        "authentication_profile",
        sa.Column("profile_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("profile_version", sa.BigInteger(), nullable=False),
        sa.Column("provider_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("subject_namespace", _exact_string(128, dialect), nullable=False),
        sa.Column("credential_kind", _exact_string(32, dialect), nullable=False),
        sa.Column("stage_audience", _exact_string(256, dialect), nullable=False),
        sa.Column("config_schema", _exact_string(128, dialect), nullable=False),
        sa.Column("config_json", _json(dialect), nullable=False),
        sa.Column("digest", _exact_string(64, dialect), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", _exact_string(32, dialect), server_default="active", nullable=False),
        sa.Column("created_at", _timestamp(dialect), server_default=_now(dialect), nullable=False),
        sa.Column("updated_at", _timestamp(dialect), server_default=_now(dialect), nullable=False),
        sa.CheckConstraint("credential_kind IN ('human_oidc', 'workload_oauth2')", name="ck_auth_profile_kind"),
        sa.CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="ck_auth_profile_status"),
        sa.CheckConstraint("length(digest) = 64", name="ck_auth_profile_digest"),
        sa.CheckConstraint(
            "length(trim(profile_ref)) > 0 AND length(trim(subject_namespace)) > 0 "
            "AND length(trim(stage_audience)) > 0 AND length(trim(config_schema)) > 0",
            name="ck_auth_profile_keys",
        ),
        sa.CheckConstraint("profile_version > 0 AND version > 0", name="ck_auth_profile_versions"),
        sa.ForeignKeyConstraint(["provider_ref"], ["identity_provider.provider_ref"], name="fk_auth_profile_provider"),
        sa.PrimaryKeyConstraint("profile_ref", "profile_version"),
    )
    op.create_table(
        "external_identity",
        sa.Column("external_identity_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("provider_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("subject_namespace", _exact_string(128, dialect), nullable=False),
        sa.Column("subject_exact", _exact_string(256, dialect), nullable=False),
        sa.Column("principal_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("principal_type", _exact_string(32, dialect), nullable=False),
        sa.Column("link_evidence_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", _exact_string(32, dialect), server_default="active", nullable=False),
        sa.Column("created_at", _timestamp(dialect), server_default=_now(dialect), nullable=False),
        sa.Column("updated_at", _timestamp(dialect), server_default=_now(dialect), nullable=False),
        sa.CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="ck_external_identity_status"),
        sa.CheckConstraint(
            "length(trim(external_identity_ref)) > 0 AND length(trim(subject_namespace)) > 0 "
            "AND length(trim(subject_exact)) > 0 AND length(trim(link_evidence_ref)) > 0",
            name="ck_external_identity_keys",
        ),
        sa.CheckConstraint("version > 0", name="ck_external_identity_version"),
        sa.ForeignKeyConstraint(
            ["principal_ref", "principal_type"],
            ["principal.principal_ref", "principal.principal_type"],
            name="fk_external_identity_principal_type",
        ),
        sa.ForeignKeyConstraint(
            ["provider_ref"], ["identity_provider.provider_ref"], name="fk_external_identity_provider"
        ),
        sa.PrimaryKeyConstraint("external_identity_ref"),
        sa.UniqueConstraint(
            "external_identity_ref", "principal_ref", "principal_type", name="uk_external_identity_principal"
        ),
        sa.UniqueConstraint("provider_ref", "subject_namespace", "subject_exact", name="uk_external_identity_subject"),
    )
    with op.batch_alter_table("external_identity", schema=None) as batch_op:
        batch_op.create_index("ix_external_identity_principal", ["principal_ref"], unique=False)

    op.create_table(
        "bootstrap_record",
        sa.Column("bootstrap_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("environment", _exact_string(64, dialect), nullable=False),
        sa.Column("bootstrap_type", _exact_string(96, dialect), nullable=False),
        sa.Column("configuration_digest", _exact_string(64, dialect), nullable=False),
        sa.Column("initial_identity_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("initial_grant_refs", _json(dialect), nullable=False),
        sa.Column("completed_at", _timestamp(dialect), nullable=False),
        sa.CheckConstraint("length(configuration_digest) = 64", name="ck_bootstrap_digest"),
        sa.CheckConstraint(
            "length(trim(bootstrap_ref)) > 0 AND length(trim(environment)) > 0 AND length(trim(bootstrap_type)) > 0",
            name="ck_bootstrap_keys",
        ),
        sa.ForeignKeyConstraint(
            ["initial_identity_ref"], ["external_identity.external_identity_ref"], name="fk_bootstrap_initial_identity"
        ),
        sa.PrimaryKeyConstraint("bootstrap_ref"),
        sa.UniqueConstraint("environment", "bootstrap_type", name="uk_bootstrap_environment_type"),
    )
    op.create_table(
        "tenant_authentication_binding",
        sa.Column("tenant_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("binding_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("profile_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("profile_version", sa.BigInteger(), nullable=False),
        sa.Column("environment", _exact_string(64, dialect), nullable=False),
        sa.Column("constraints_json", _json(dialect), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", _exact_string(32, dialect), server_default="active", nullable=False),
        sa.Column("valid_from", _timestamp(dialect), nullable=False),
        sa.Column("expires_at", _timestamp(dialect), nullable=True),
        sa.Column("created_at", _timestamp(dialect), server_default=_now(dialect), nullable=False),
        sa.Column("updated_at", _timestamp(dialect), server_default=_now(dialect), nullable=False),
        sa.CheckConstraint("status IN ('active', 'suspended', 'revoked', 'expired')", name="ck_tenant_auth_status"),
        sa.CheckConstraint("expires_at IS NULL OR valid_from < expires_at", name="ck_tenant_auth_validity"),
        sa.CheckConstraint(
            "length(trim(binding_ref)) > 0 AND length(trim(environment)) > 0", name="ck_tenant_auth_keys"
        ),
        sa.CheckConstraint("version > 0", name="ck_tenant_auth_version"),
        sa.ForeignKeyConstraint(
            ["profile_ref", "profile_version"],
            ["authentication_profile.profile_ref", "authentication_profile.profile_version"],
            name="fk_tenant_auth_binding_profile",
        ),
        sa.ForeignKeyConstraint(["tenant_ref"], ["tenant.tenant_ref"], name="fk_tenant_auth_binding_tenant"),
        sa.PrimaryKeyConstraint("tenant_ref", "binding_ref"),
    )
    with op.batch_alter_table("tenant_authentication_binding", schema=None) as batch_op:
        batch_op.create_index(
            "uk_tenant_auth_active_profile",
            ["tenant_ref", "profile_ref", "profile_version", "environment"],
            unique=True,
            sqlite_where=sa.text("status IN ('active', 'suspended')"),
            postgresql_where=sa.text("status IN ('active', 'suspended')"),
        )

    op.create_table(
        "workload_registration",
        sa.Column("workload_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("service_principal_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("principal_type", _exact_string(32, dialect), server_default="service", nullable=False),
        sa.Column("profile_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("profile_version", sa.BigInteger(), nullable=False),
        sa.Column("external_identity_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("verified_client_ref", _exact_string(128, dialect), nullable=False),
        sa.Column("environment", _exact_string(64, dialect), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", _exact_string(32, dialect), server_default="active", nullable=False),
        sa.Column("valid_from", _timestamp(dialect), nullable=False),
        sa.Column("expires_at", _timestamp(dialect), nullable=True),
        sa.Column("created_at", _timestamp(dialect), server_default=_now(dialect), nullable=False),
        sa.Column("updated_at", _timestamp(dialect), server_default=_now(dialect), nullable=False),
        sa.CheckConstraint("principal_type = 'service'", name="ck_workload_principal_type"),
        sa.CheckConstraint("status IN ('active', 'suspended', 'revoked', 'expired')", name="ck_workload_status"),
        sa.CheckConstraint("expires_at IS NULL OR valid_from < expires_at", name="ck_workload_validity"),
        sa.CheckConstraint(
            "length(trim(workload_ref)) > 0 AND length(trim(verified_client_ref)) > 0 "
            "AND length(trim(environment)) > 0",
            name="ck_workload_keys",
        ),
        sa.CheckConstraint("version > 0", name="ck_workload_version"),
        sa.ForeignKeyConstraint(
            ["external_identity_ref", "service_principal_ref", "principal_type"],
            [
                "external_identity.external_identity_ref",
                "external_identity.principal_ref",
                "external_identity.principal_type",
            ],
            name="fk_workload_external_identity",
        ),
        sa.ForeignKeyConstraint(
            ["profile_ref", "profile_version"],
            ["authentication_profile.profile_ref", "authentication_profile.profile_version"],
            name="fk_workload_profile",
        ),
        sa.ForeignKeyConstraint(
            ["service_principal_ref", "principal_type"],
            ["principal.principal_ref", "principal.principal_type"],
            name="fk_workload_principal_type",
        ),
        sa.PrimaryKeyConstraint("workload_ref"),
        sa.UniqueConstraint(
            "workload_ref", "service_principal_ref", "environment", name="uk_workload_service_environment"
        ),
    )
    with op.batch_alter_table("workload_registration", schema=None) as batch_op:
        batch_op.create_index(
            "ix_workload_principal_environment", ["service_principal_ref", "environment", "status"], unique=False
        )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("This downgrade requires an online connection to inspect retained identity facts")
    connection = op.get_bind()
    for table_name in TABLES:
        if connection.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first() is not None:
            raise RuntimeError(
                f"Downgrade requires empty identity foundation tables; {table_name} contains retained data. "
                "No identity tables have been removed."
            )
    advanced = connection.execute(
        sa.text(
            "SELECT 1 FROM principal WHERE version <> 1 OR identity_epoch <> 0 "
            "OR recovery_epoch <> 0 OR disabled_at IS NOT NULL LIMIT 1"
        )
    ).first()
    if advanced is not None:
        raise RuntimeError("Downgrade cannot erase advanced principal identity versions or revocation state")
    for table_name in reversed(TABLES):
        op.drop_table(table_name)
    with op.batch_alter_table("principal") as batch_op:
        batch_op.drop_constraint("uk_principal_ref_type", type_="unique")
        batch_op.drop_constraint("ck_principal_version", type_="check")
        batch_op.drop_constraint("ck_principal_epochs", type_="check")
        batch_op.drop_column("disabled_at")
        batch_op.drop_column("recovery_epoch")
        batch_op.drop_column("identity_epoch")
        batch_op.drop_column("version")
