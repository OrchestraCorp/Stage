"""Add versioned RBAC and resource/scope authorization facts.

Revision ID: f4c7b2a1e903
Revises: d6a3c5f8e902

This is an additive P1b schema slice. Historical role_assignment and
relation_tuple rows remain readable but unmapped. Their organization and
membership columns stay nullable until an explicit reviewed mapping is done;
the new bound shape is enforced by foreign keys and a legacy-or-bound check.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4c7b2a1e903"
down_revision: str | None = "d6a3c5f8e902"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _exact(length: int, dialect: str) -> sa.String:
    return sa.String(length, collation="C" if dialect == "postgresql" else "BINARY")


def _timestamp(dialect: str) -> sa.DateTime:
    return sa.DateTime(timezone=dialect == "postgresql")


def _now(dialect: str) -> sa.TextClause:
    return sa.text("CURRENT_TIMESTAMP" if dialect == "postgresql" else "(STRFTIME('%Y-%m-%d %H:%M:%f', 'now'))")


def _created_updated(dialect: str) -> tuple[sa.Column, sa.Column]:
    now = _now(dialect)
    return (
        sa.Column("created_at", _timestamp(dialect), server_default=now, nullable=False),
        sa.Column("updated_at", _timestamp(dialect), server_default=now, nullable=False),
    )


def _create_authorization_tables(dialect: str) -> None:
    def exact(n: int) -> sa.String:
        return _exact(n, dialect)

    op.create_table(
        "permission_action",
        sa.Column("action_ref", exact(96), nullable=False),
        sa.Column("resource_kind", exact(128), nullable=False),
        sa.Column("contract_version", exact(32), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", exact(32), server_default="active", nullable=False),
        *_created_updated(dialect),
        sa.CheckConstraint("length(trim(action_ref)) > 0", name="ck_permission_action_ref"),
        sa.CheckConstraint("length(trim(resource_kind)) > 0", name="ck_permission_action_resource_kind"),
        sa.CheckConstraint("length(trim(contract_version)) > 0", name="ck_permission_action_contract_version"),
        sa.CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="ck_permission_action_status"),
        sa.CheckConstraint("version > 0", name="ck_permission_action_version"),
        sa.PrimaryKeyConstraint("action_ref"),
    )
    op.create_table(
        "role_definition",
        sa.Column("organization_ref", exact(128), nullable=False),
        sa.Column("role_ref", exact(128), nullable=False),
        sa.Column("role_code", exact(128), nullable=False),
        sa.Column("display_name", exact(256), nullable=False),
        sa.Column("template_source_ref", exact(128), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", exact(32), server_default="active", nullable=False),
        *_created_updated(dialect),
        sa.ForeignKeyConstraint(
            ["organization_ref"], ["organization_root.organization_ref"], name="fk_role_definition_organization"
        ),
        sa.UniqueConstraint("organization_ref", "role_code", name="uk_role_definition_code"),
        sa.CheckConstraint("length(trim(organization_ref)) > 0", name="ck_role_definition_organization"),
        sa.CheckConstraint(
            "length(trim(role_ref)) > 0 AND length(trim(role_code)) > 0", name="ck_role_definition_keys"
        ),
        sa.CheckConstraint("length(trim(display_name)) > 0", name="ck_role_definition_display_name"),
        sa.CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="ck_role_definition_status"),
        sa.CheckConstraint("version > 0", name="ck_role_definition_version"),
        sa.PrimaryKeyConstraint("organization_ref", "role_ref"),
    )
    op.create_table(
        "authorization_scope",
        sa.Column("organization_ref", exact(128), nullable=False),
        sa.Column("scope_ref", exact(256), nullable=False),
        sa.Column("scope_kind", exact(64), nullable=False),
        sa.Column("parent_scope_ref", exact(256), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", exact(32), server_default="active", nullable=False),
        *_created_updated(dialect),
        sa.ForeignKeyConstraint(
            ["organization_ref"], ["organization_root.organization_ref"], name="fk_authorization_scope_organization"
        ),
        sa.ForeignKeyConstraint(
            ["organization_ref", "parent_scope_ref"],
            ["authorization_scope.organization_ref", "authorization_scope.scope_ref"],
            name="fk_authorization_scope_parent",
        ),
        sa.CheckConstraint("length(trim(organization_ref)) > 0", name="ck_authorization_scope_organization"),
        sa.CheckConstraint(
            "length(trim(scope_ref)) > 0 AND length(trim(scope_kind)) > 0", name="ck_authorization_scope_keys"
        ),
        sa.CheckConstraint(
            "parent_scope_ref IS NULL OR parent_scope_ref <> scope_ref",
            name="ck_authorization_scope_parent_self",
        ),
        sa.CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="ck_authorization_scope_status"),
        sa.CheckConstraint("version > 0", name="ck_authorization_scope_version"),
        sa.PrimaryKeyConstraint("organization_ref", "scope_ref"),
    )
    op.create_table(
        "authorization_resource",
        sa.Column("organization_ref", exact(128), nullable=False),
        sa.Column("resource_ref", exact(256), nullable=False),
        sa.Column("resource_kind", exact(128), nullable=False),
        sa.Column("scope_ref", exact(256), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", exact(32), server_default="active", nullable=False),
        *_created_updated(dialect),
        sa.ForeignKeyConstraint(
            ["organization_ref"], ["organization_root.organization_ref"], name="fk_authorization_resource_organization"
        ),
        sa.ForeignKeyConstraint(
            ["organization_ref", "scope_ref"],
            ["authorization_scope.organization_ref", "authorization_scope.scope_ref"],
            name="fk_authorization_resource_scope",
        ),
        sa.CheckConstraint("length(trim(organization_ref)) > 0", name="ck_authorization_resource_organization"),
        sa.CheckConstraint(
            "length(trim(resource_ref)) > 0 AND length(trim(resource_kind)) > 0 AND length(trim(scope_ref)) > 0",
            name="ck_authorization_resource_keys",
        ),
        sa.CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="ck_authorization_resource_status"),
        sa.CheckConstraint("version > 0", name="ck_authorization_resource_version"),
        sa.PrimaryKeyConstraint("organization_ref", "resource_ref"),
    )
    op.create_table(
        "role_version",
        sa.Column("organization_ref", exact(128), nullable=False),
        sa.Column("role_ref", exact(128), nullable=False),
        sa.Column("role_revision", sa.BigInteger(), nullable=False),
        sa.Column("content_digest", exact(64), nullable=False),
        sa.Column("policy_release_ref", exact(128), nullable=False),
        sa.Column("revoked_at", _timestamp(dialect), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", exact(32), server_default="active", nullable=False),
        *_created_updated(dialect),
        sa.ForeignKeyConstraint(
            ["organization_ref", "role_ref"],
            ["role_definition.organization_ref", "role_definition.role_ref"],
            name="fk_role_version_definition",
        ),
        sa.CheckConstraint("role_revision > 0", name="ck_role_version_revision"),
        sa.CheckConstraint("length(trim(content_digest)) = 64", name="ck_role_version_digest"),
        sa.CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="ck_role_version_status"),
        sa.CheckConstraint("version > 0", name="ck_role_version_version"),
        sa.CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL) OR (status <> 'revoked' AND revoked_at IS NULL)",
            name="ck_role_version_revocation",
        ),
        sa.PrimaryKeyConstraint("organization_ref", "role_ref", "role_revision"),
    )
    op.create_table(
        "role_version_action",
        sa.Column("organization_ref", exact(128), nullable=False),
        sa.Column("role_ref", exact(128), nullable=False),
        sa.Column("role_revision", sa.BigInteger(), nullable=False),
        sa.Column("action_ref", exact(96), nullable=False),
        *_created_updated(dialect),
        sa.ForeignKeyConstraint(
            ["organization_ref", "role_ref", "role_revision"],
            ["role_version.organization_ref", "role_version.role_ref", "role_version.role_revision"],
            name="fk_role_version_action_version",
        ),
        sa.ForeignKeyConstraint(["action_ref"], ["permission_action.action_ref"], name="fk_role_version_action_action"),
        sa.CheckConstraint("length(trim(action_ref)) > 0", name="ck_role_version_action_ref"),
        sa.PrimaryKeyConstraint("organization_ref", "role_ref", "role_revision", "action_ref"),
    )


def _extend_legacy_tables(dialect: str) -> None:
    def exact(n: int) -> sa.String:
        return _exact(n, dialect)

    with op.batch_alter_table("role_assignment", recreate="always" if dialect == "sqlite" else "auto") as batch:
        batch.add_column(sa.Column("organization_ref", exact(128), nullable=True))
        batch.add_column(sa.Column("assignment_ref", exact(128), nullable=True))
        batch.add_column(sa.Column("membership_ref", exact(128), nullable=True))
        batch.add_column(sa.Column("principal_type", exact(32), nullable=True))
        batch.add_column(sa.Column("role_ref", exact(128), nullable=True))
        batch.add_column(sa.Column("role_revision", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("revoked_at", _timestamp(dialect), nullable=True))
        batch.add_column(sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False))
        batch.add_column(sa.Column("grant_source_ref", exact(128), nullable=True))
        batch.create_foreign_key(
            "fk_role_assignment_membership_principal",
            "tenant_membership",
            ["organization_ref", "membership_ref", "principal_ref"],
            ["tenant_ref", "membership_ref", "principal_ref"],
        )
        batch.create_foreign_key(
            "fk_role_assignment_role_version",
            "role_version",
            ["organization_ref", "role_ref", "role_revision"],
            ["organization_ref", "role_ref", "role_revision"],
        )
        batch.create_foreign_key(
            "fk_role_assignment_scope",
            "authorization_scope",
            ["organization_ref", "scope_ref"],
            ["organization_ref", "scope_ref"],
        )
        batch.create_foreign_key(
            "fk_role_assignment_organization", "organization_root", ["organization_ref"], ["organization_ref"]
        )
        batch.create_foreign_key(
            "fk_role_assignment_principal_type",
            "principal",
            ["principal_ref", "principal_type"],
            ["principal_ref", "principal_type"],
        )
        batch.drop_constraint("uk_role_assignment", type_="unique")
        batch.create_unique_constraint("uk_role_assignment_ref", ["organization_ref", "assignment_ref"])
        batch.create_unique_constraint(
            "uk_role_assignment_bound_ref", ["organization_ref", "assignment_ref", "principal_ref"]
        )
        batch.create_check_constraint(
            "ck_role_assignment_legacy_or_bound",
            "(organization_ref IS NULL AND assignment_ref IS NULL AND membership_ref IS NULL "
            "AND principal_type IS NULL AND role_ref IS NULL AND role_revision IS NULL "
            "AND grant_source_ref IS NULL) OR "
            "(organization_ref IS NOT NULL AND assignment_ref IS NOT NULL AND membership_ref IS NOT NULL "
            "AND principal_type IS NOT NULL AND role_ref IS NOT NULL AND role_revision IS NOT NULL "
            "AND grant_source_ref IS NOT NULL)",
        )
        batch.create_check_constraint(
            "ck_role_assignment_bound_fields",
            "organization_ref IS NULL OR (principal_type = 'human' AND role_revision > 0 "
            "AND length(trim(assignment_ref)) > 0 AND length(trim(grant_source_ref)) > 0 "
            "AND valid_from IS NOT NULL AND status IN ('active', 'suspended', 'revoked', 'expired'))",
        )
        batch.create_check_constraint(
            "ck_role_assignment_bound_revocation",
            "organization_ref IS NULL OR (status = 'revoked' AND revoked_at IS NOT NULL) "
            "OR (status <> 'revoked' AND revoked_at IS NULL)",
        )
        batch.create_check_constraint("ck_role_assignment_version", "version > 0")
    with op.batch_alter_table("relation_tuple", recreate="always" if dialect == "sqlite" else "auto") as batch:
        batch.add_column(sa.Column("organization_ref", exact(128), nullable=True))
        batch.add_column(sa.Column("relation_ref", exact(128), nullable=True))
        batch.add_column(sa.Column("membership_ref", exact(128), nullable=True))
        batch.add_column(sa.Column("subject_type", exact(32), nullable=True))
        batch.add_column(sa.Column("relation_type", exact(128), nullable=True))
        batch.add_column(sa.Column("revoked_at", _timestamp(dialect), nullable=True))
        batch.add_column(sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False))
        batch.add_column(sa.Column("source_kind", exact(32), nullable=True))
        batch.add_column(sa.Column("source_ref", exact(128), nullable=True))
        batch.add_column(sa.Column("source_version", sa.BigInteger(), nullable=True))
        batch.create_foreign_key(
            "fk_relation_tuple_membership_subject",
            "tenant_membership",
            ["organization_ref", "membership_ref", "subject_ref"],
            ["tenant_ref", "membership_ref", "principal_ref"],
        )
        batch.create_foreign_key(
            "fk_relation_tuple_resource",
            "authorization_resource",
            ["organization_ref", "resource_ref"],
            ["organization_ref", "resource_ref"],
        )
        batch.create_foreign_key(
            "fk_relation_tuple_organization", "organization_root", ["organization_ref"], ["organization_ref"]
        )
        batch.create_foreign_key(
            "fk_relation_tuple_subject_type",
            "principal",
            ["subject_ref", "subject_type"],
            ["principal_ref", "principal_type"],
        )
        batch.drop_constraint("uk_relation_tuple", type_="unique")
        batch.create_unique_constraint("uk_relation_tuple_bound_ref", ["organization_ref", "relation_ref"])
        batch.create_check_constraint(
            "ck_relation_tuple_legacy_or_bound",
            "(organization_ref IS NULL AND relation_ref IS NULL AND relation_type IS NULL "
            "AND membership_ref IS NULL AND subject_type IS NULL "
            "AND source_kind IS NULL AND source_ref IS NULL AND source_version IS NULL) OR "
            "(organization_ref IS NOT NULL AND relation_ref IS NOT NULL AND relation_type IS NOT NULL "
            "AND membership_ref IS NOT NULL AND subject_type IS NOT NULL "
            "AND source_kind IS NOT NULL AND source_ref IS NOT NULL AND source_version IS NOT NULL)",
        )
        batch.create_check_constraint(
            "ck_relation_tuple_bound_fields",
            "organization_ref IS NULL OR (relation = relation_type AND source_version > 0 "
            "AND length(trim(relation_ref)) > 0 AND length(trim(source_kind)) > 0 "
            "AND length(trim(source_ref)) > 0 AND valid_from IS NOT NULL "
            "AND status IN ('active', 'suspended', 'revoked', 'expired'))",
        )
        batch.create_check_constraint(
            "ck_relation_tuple_bound_revocation",
            "organization_ref IS NULL OR (status = 'revoked' AND revoked_at IS NOT NULL) "
            "OR (status <> 'revoked' AND revoked_at IS NULL)",
        )
        batch.create_check_constraint("ck_relation_tuple_version", "version > 0")


def _create_indexes() -> None:
    # Partial uniqueness prevents duplicate active bindings while allowing
    # explicitly terminated history. Expiry is checked by the loader, not an
    # index expression involving now().
    op.create_index(
        "uk_role_assignment_legacy",
        "role_assignment",
        ["principal_ref", "role_code", "scope_ref"],
        unique=True,
        postgresql_where=sa.text("organization_ref IS NULL"),
        sqlite_where=sa.text("organization_ref IS NULL"),
    )
    op.create_index(
        "uk_relation_tuple_legacy",
        "relation_tuple",
        ["subject_ref", "relation", "resource_ref"],
        unique=True,
        postgresql_where=sa.text("organization_ref IS NULL"),
        sqlite_where=sa.text("organization_ref IS NULL"),
    )
    op.create_index(
        "uk_relation_tuple_current",
        "relation_tuple",
        ["organization_ref", "membership_ref", "relation_type", "resource_ref"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'suspended') AND organization_ref IS NOT NULL"),
        sqlite_where=sa.text("status IN ('active', 'suspended') AND organization_ref IS NOT NULL"),
    )
    op.create_index(
        "uk_role_assignment_current",
        "role_assignment",
        ["organization_ref", "membership_ref", "role_ref", "role_revision", "scope_ref"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'suspended') AND organization_ref IS NOT NULL"),
        sqlite_where=sa.text("status IN ('active', 'suspended') AND organization_ref IS NOT NULL"),
    )
    op.create_index(
        "ix_relation_tuple_bound_resource",
        "relation_tuple",
        ["organization_ref", "resource_ref", "relation_type", "status"],
    )


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in ("postgresql", "sqlite"):
        raise RuntimeError(f"Unsupported Stage authorization database: {dialect}")
    _create_authorization_tables(dialect)
    _extend_legacy_tables(dialect)
    op.add_column("execution_audit", sa.Column("authorization_json", sa.JSON(none_as_null=True), nullable=True))
    _create_indexes()


def _has_bound_rows(connection: sa.Connection, table: str, key: str) -> bool:
    return connection.scalar(sa.text(f"SELECT 1 FROM {table} WHERE {key} IS NOT NULL LIMIT 1")) is not None


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name not in ("postgresql", "sqlite"):
        raise RuntimeError(f"Unsupported Stage authorization database: {connection.dialect.name}")
    if connection.scalar(sa.text("SELECT 1 FROM execution_audit WHERE authorization_json IS NOT NULL LIMIT 1")):
        raise RuntimeError("Authorization downgrade cannot discard persisted authorization evidence")
    for table in (
        "permission_action",
        "role_version_action",
        "role_version",
        "authorization_resource",
        "authorization_scope",
        "role_definition",
    ):
        if connection.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) is not None:
            raise RuntimeError("Authorization downgrade requires empty new authorization tables")
    if _has_bound_rows(connection, "role_assignment", "organization_ref") or _has_bound_rows(
        connection, "relation_tuple", "organization_ref"
    ):
        raise RuntimeError("Authorization downgrade requires unmapped legacy role and relation rows")
    op.drop_column("execution_audit", "authorization_json")
    op.drop_index("uk_role_assignment_current", table_name="role_assignment")
    op.drop_index("uk_role_assignment_legacy", table_name="role_assignment")
    op.drop_index("uk_relation_tuple_current", table_name="relation_tuple")
    op.drop_index("uk_relation_tuple_legacy", table_name="relation_tuple")
    op.drop_index("ix_relation_tuple_bound_resource", table_name="relation_tuple")
    with op.batch_alter_table("role_assignment", recreate="auto") as batch:
        for name in (
            "fk_role_assignment_membership_principal",
            "fk_role_assignment_role_version",
            "fk_role_assignment_scope",
            "fk_role_assignment_organization",
            "fk_role_assignment_principal_type",
        ):
            batch.drop_constraint(name, type_="foreignkey")
        batch.drop_constraint("uk_role_assignment_bound_ref", type_="unique")
        batch.drop_constraint("uk_role_assignment_ref", type_="unique")
        batch.create_unique_constraint("uk_role_assignment", ["principal_ref", "role_code", "scope_ref"])
        batch.drop_constraint("ck_role_assignment_legacy_or_bound", type_="check")
        batch.drop_constraint("ck_role_assignment_version", type_="check")
        batch.drop_constraint("ck_role_assignment_bound_fields", type_="check")
        batch.drop_constraint("ck_role_assignment_bound_revocation", type_="check")
        for column in (
            "organization_ref",
            "assignment_ref",
            "membership_ref",
            "principal_type",
            "role_ref",
            "role_revision",
            "revoked_at",
            "version",
            "grant_source_ref",
        ):
            batch.drop_column(column)
    with op.batch_alter_table("relation_tuple", recreate="auto") as batch:
        for name in (
            "fk_relation_tuple_membership_subject",
            "fk_relation_tuple_resource",
            "fk_relation_tuple_organization",
            "fk_relation_tuple_subject_type",
        ):
            batch.drop_constraint(name, type_="foreignkey")
        batch.drop_constraint("uk_relation_tuple_bound_ref", type_="unique")
        batch.create_unique_constraint("uk_relation_tuple", ["subject_ref", "relation", "resource_ref"])
        batch.drop_constraint("ck_relation_tuple_legacy_or_bound", type_="check")
        batch.drop_constraint("ck_relation_tuple_version", type_="check")
        batch.drop_constraint("ck_relation_tuple_bound_fields", type_="check")
        batch.drop_constraint("ck_relation_tuple_bound_revocation", type_="check")
        for column in (
            "organization_ref",
            "relation_ref",
            "membership_ref",
            "subject_type",
            "relation_type",
            "revoked_at",
            "version",
            "source_kind",
            "source_ref",
            "source_version",
        ):
            batch.drop_column(column)
    op.drop_table("role_version_action")
    op.drop_table("role_version")
    op.drop_table("authorization_resource")
    op.drop_table("authorization_scope")
    op.drop_table("role_definition")
    op.drop_table("permission_action")
