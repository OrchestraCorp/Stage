"""Persistent RBAC/ReBAC facts for the private deployment.

New authorization columns use organization_ref because Stage has one
organization authorization root per deployment. Legacy role and relation rows
may remain unbound while an installation is being mapped; newly-created bound
rows must provide the complete organization/membership/version tuple.
"""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, IdMixin, TimestampMixin
from app.models.identity_foundation import StatusMixin, VersionMixin
from app.storage.types import ExactString, UTCDateTime


def _bound_or_legacy(*columns: str, name: str) -> CheckConstraint:
    """Allow an untouched historical row, or require all binding columns."""

    legacy = " AND ".join(f"{column} IS NULL" for column in columns)
    bound = " AND ".join(f"{column} IS NOT NULL" for column in columns)
    return CheckConstraint(f"({legacy}) OR ({bound})", name=name)


class PermissionActionTable(VersionMixin, StatusMixin, TimestampMixin, Base):
    """A stable action contract registered by trusted Stage code."""

    __tablename__ = "permission_action"
    __table_args__ = (
        CheckConstraint("length(trim(action_ref)) > 0", name="ck_permission_action_ref"),
        CheckConstraint("length(trim(resource_kind)) > 0", name="ck_permission_action_resource_kind"),
        CheckConstraint("length(trim(contract_version)) > 0", name="ck_permission_action_contract_version"),
        CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="ck_permission_action_status"),
        CheckConstraint("version > 0", name="ck_permission_action_version"),
    )

    action_ref: Mapped[str] = mapped_column(ExactString(96), primary_key=True)
    resource_kind: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    contract_version: Mapped[str] = mapped_column(ExactString(32), nullable=False)


class RoleDefinitionTable(VersionMixin, StatusMixin, TimestampMixin, Base):
    """An organization-owned stable role name; contents live in revisions."""

    __tablename__ = "role_definition"
    __table_args__ = (
        ForeignKeyConstraint(
            ("organization_ref",), ("organization_root.organization_ref",), name="fk_role_definition_organization"
        ),
        UniqueConstraint("organization_ref", "role_code", name="uk_role_definition_code"),
        CheckConstraint("length(trim(organization_ref)) > 0", name="ck_role_definition_organization"),
        CheckConstraint("length(trim(role_ref)) > 0 AND length(trim(role_code)) > 0", name="ck_role_definition_keys"),
        CheckConstraint("length(trim(display_name)) > 0", name="ck_role_definition_display_name"),
        CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="ck_role_definition_status"),
        CheckConstraint("version > 0", name="ck_role_definition_version"),
    )

    organization_ref: Mapped[str] = mapped_column("organization_ref", ExactString(128), primary_key=True)
    role_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    role_code: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    display_name: Mapped[str] = mapped_column(ExactString(256), nullable=False)
    template_source_ref: Mapped[str | None] = mapped_column(ExactString(128))


class RoleVersionTable(VersionMixin, StatusMixin, TimestampMixin, Base):
    """An immutable role capability set, pinned by assignments."""

    __tablename__ = "role_version"
    __table_args__ = (
        ForeignKeyConstraint(
            ("organization_ref", "role_ref"),
            ("role_definition.organization_ref", "role_definition.role_ref"),
            name="fk_role_version_definition",
        ),
        CheckConstraint("role_revision > 0", name="ck_role_version_revision"),
        CheckConstraint("length(trim(content_digest)) = 64", name="ck_role_version_digest"),
        CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="ck_role_version_status"),
        CheckConstraint("version > 0", name="ck_role_version_version"),
        CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL) OR (status <> 'revoked' AND revoked_at IS NULL)",
            name="ck_role_version_revocation",
        ),
    )

    organization_ref: Mapped[str] = mapped_column("organization_ref", ExactString(128), primary_key=True)
    role_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    role_revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    content_digest: Mapped[str] = mapped_column(ExactString(64), nullable=False)
    policy_release_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class RoleVersionActionTable(TimestampMixin, Base):
    """The action members of one immutable role revision."""

    __tablename__ = "role_version_action"
    __table_args__ = (
        ForeignKeyConstraint(
            ("organization_ref", "role_ref", "role_revision"),
            ("role_version.organization_ref", "role_version.role_ref", "role_version.role_revision"),
            name="fk_role_version_action_version",
        ),
        ForeignKeyConstraint(("action_ref",), ("permission_action.action_ref",), name="fk_role_version_action_action"),
        CheckConstraint("length(trim(action_ref)) > 0", name="ck_role_version_action_ref"),
    )

    organization_ref: Mapped[str] = mapped_column("organization_ref", ExactString(128), primary_key=True)
    role_ref: Mapped[str] = mapped_column(ExactString(128), primary_key=True)
    role_revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    action_ref: Mapped[str] = mapped_column(ExactString(96), primary_key=True)


class AuthorizationScopeTable(VersionMixin, StatusMixin, TimestampMixin, Base):
    """A bounded authorization scope, independent of a domain organization tree."""

    __tablename__ = "authorization_scope"
    __table_args__ = (
        ForeignKeyConstraint(
            ("organization_ref",), ("organization_root.organization_ref",), name="fk_authorization_scope_organization"
        ),
        ForeignKeyConstraint(
            ("organization_ref", "parent_scope_ref"),
            ("authorization_scope.organization_ref", "authorization_scope.scope_ref"),
            name="fk_authorization_scope_parent",
        ),
        CheckConstraint("length(trim(organization_ref)) > 0", name="ck_authorization_scope_organization"),
        CheckConstraint(
            "length(trim(scope_ref)) > 0 AND length(trim(scope_kind)) > 0", name="ck_authorization_scope_keys"
        ),
        CheckConstraint(
            "parent_scope_ref IS NULL OR parent_scope_ref <> scope_ref",
            name="ck_authorization_scope_parent_self",
        ),
        CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="ck_authorization_scope_status"),
        CheckConstraint("version > 0", name="ck_authorization_scope_version"),
    )

    organization_ref: Mapped[str] = mapped_column("organization_ref", ExactString(128), primary_key=True)
    scope_ref: Mapped[str] = mapped_column(ExactString(256), primary_key=True)
    scope_kind: Mapped[str] = mapped_column(ExactString(64), nullable=False)
    parent_scope_ref: Mapped[str | None] = mapped_column(ExactString(256))


class AuthorizationResourceTable(VersionMixin, StatusMixin, TimestampMixin, Base):
    """The security identity/scope registry for a domain resource."""

    __tablename__ = "authorization_resource"
    __table_args__ = (
        ForeignKeyConstraint(
            ("organization_ref",),
            ("organization_root.organization_ref",),
            name="fk_authorization_resource_organization",
        ),
        ForeignKeyConstraint(
            ("organization_ref", "scope_ref"),
            ("authorization_scope.organization_ref", "authorization_scope.scope_ref"),
            name="fk_authorization_resource_scope",
        ),
        CheckConstraint("length(trim(organization_ref)) > 0", name="ck_authorization_resource_organization"),
        CheckConstraint(
            "length(trim(resource_ref)) > 0 AND length(trim(resource_kind)) > 0 AND length(trim(scope_ref)) > 0",
            name="ck_authorization_resource_keys",
        ),
        CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="ck_authorization_resource_status"),
        CheckConstraint("version > 0", name="ck_authorization_resource_version"),
    )

    organization_ref: Mapped[str] = mapped_column("organization_ref", ExactString(128), primary_key=True)
    resource_ref: Mapped[str] = mapped_column(ExactString(256), primary_key=True)
    resource_kind: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    scope_ref: Mapped[str] = mapped_column(ExactString(256), nullable=False)


class RoleAssignmentTable(IdMixin, TimestampMixin, Base):
    """A role assignment, retaining the original unmapped storage shape."""

    __tablename__ = "role_assignment"
    __table_args__ = (
        ForeignKeyConstraint(
            ("organization_ref", "membership_ref", "principal_ref"),
            ("tenant_membership.tenant_ref", "tenant_membership.membership_ref", "tenant_membership.principal_ref"),
            name="fk_role_assignment_membership_principal",
        ),
        ForeignKeyConstraint(
            ("organization_ref", "role_ref", "role_revision"),
            ("role_version.organization_ref", "role_version.role_ref", "role_version.role_revision"),
            name="fk_role_assignment_role_version",
        ),
        ForeignKeyConstraint(
            ("organization_ref", "scope_ref"),
            ("authorization_scope.organization_ref", "authorization_scope.scope_ref"),
            name="fk_role_assignment_scope",
        ),
        ForeignKeyConstraint(
            ("organization_ref",), ("organization_root.organization_ref",), name="fk_role_assignment_organization"
        ),
        ForeignKeyConstraint(
            ("principal_ref", "principal_type"),
            ("principal.principal_ref", "principal.principal_type"),
            name="fk_role_assignment_principal_type",
        ),
        UniqueConstraint("organization_ref", "assignment_ref", name="uk_role_assignment_ref"),
        UniqueConstraint("organization_ref", "assignment_ref", "principal_ref", name="uk_role_assignment_bound_ref"),
        CheckConstraint(
            "length(trim(principal_ref)) > 0 AND length(trim(role_code)) > 0 AND length(trim(scope_ref)) > 0",
            name="ck_role_assignment_keys",
        ),
        CheckConstraint("length(trim(status)) > 0", name="ck_role_assignment_status"),
        CheckConstraint(
            "valid_from IS NULL OR expires_at IS NULL OR valid_from < expires_at", name="ck_role_assignment_window"
        ),
        _bound_or_legacy(
            "organization_ref",
            "assignment_ref",
            "membership_ref",
            "principal_type",
            "role_ref",
            "role_revision",
            "grant_source_ref",
            name="ck_role_assignment_legacy_or_bound",
        ),
        CheckConstraint(
            "organization_ref IS NULL OR (principal_type = 'human' AND role_revision > 0 "
            "AND length(trim(assignment_ref)) > 0 AND length(trim(grant_source_ref)) > 0 "
            "AND valid_from IS NOT NULL AND status IN ('active', 'suspended', 'revoked', 'expired'))",
            name="ck_role_assignment_bound_fields",
        ),
        CheckConstraint(
            "organization_ref IS NULL OR (status = 'revoked' AND revoked_at IS NOT NULL) "
            "OR (status <> 'revoked' AND revoked_at IS NULL)",
            name="ck_role_assignment_bound_revocation",
        ),
        CheckConstraint("version > 0", name="ck_role_assignment_version"),
        Index(
            "uk_role_assignment_legacy",
            "principal_ref",
            "role_code",
            "scope_ref",
            unique=True,
            sqlite_where=text("organization_ref IS NULL"),
            postgresql_where=text("organization_ref IS NULL"),
        ),
        Index(
            "uk_role_assignment_current",
            "organization_ref",
            "membership_ref",
            "role_ref",
            "role_revision",
            "scope_ref",
            unique=True,
            sqlite_where=text("status IN ('active', 'suspended') AND organization_ref IS NOT NULL"),
            postgresql_where=text("status IN ('active', 'suspended') AND organization_ref IS NOT NULL"),
        ),
    )

    principal_ref: Mapped[str] = mapped_column(ExactString(128), nullable=False, index=True)
    role_code: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    scope_ref: Mapped[str] = mapped_column(ExactString(256), nullable=False)
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(ExactString(32), nullable=False, default="active")
    organization_ref: Mapped[str | None] = mapped_column("organization_ref", ExactString(128))
    assignment_ref: Mapped[str | None] = mapped_column(ExactString(128))
    membership_ref: Mapped[str | None] = mapped_column(ExactString(128))
    principal_type: Mapped[str | None] = mapped_column(ExactString(32))
    role_ref: Mapped[str | None] = mapped_column(ExactString(128))
    role_revision: Mapped[int | None] = mapped_column(BigInteger)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    grant_source_ref: Mapped[str | None] = mapped_column(ExactString(128))


class RelationTupleTable(IdMixin, TimestampMixin, Base):
    """A directly-owned ReBAC tuple; derived relations remain domain facts."""

    __tablename__ = "relation_tuple"
    __table_args__ = (
        ForeignKeyConstraint(
            ("organization_ref", "membership_ref", "subject_ref"),
            ("tenant_membership.tenant_ref", "tenant_membership.membership_ref", "tenant_membership.principal_ref"),
            name="fk_relation_tuple_membership_subject",
        ),
        ForeignKeyConstraint(
            ("organization_ref", "resource_ref"),
            ("authorization_resource.organization_ref", "authorization_resource.resource_ref"),
            name="fk_relation_tuple_resource",
        ),
        ForeignKeyConstraint(
            ("organization_ref",), ("organization_root.organization_ref",), name="fk_relation_tuple_organization"
        ),
        ForeignKeyConstraint(
            ("subject_ref", "subject_type"),
            ("principal.principal_ref", "principal.principal_type"),
            name="fk_relation_tuple_subject_type",
        ),
        UniqueConstraint("organization_ref", "relation_ref", name="uk_relation_tuple_bound_ref"),
        CheckConstraint(
            "length(trim(subject_ref)) > 0 AND length(trim(relation)) > 0 AND length(trim(resource_ref)) > 0",
            name="ck_relation_tuple_keys",
        ),
        CheckConstraint("length(trim(status)) > 0", name="ck_relation_tuple_status"),
        CheckConstraint(
            "valid_from IS NULL OR expires_at IS NULL OR valid_from < expires_at", name="ck_relation_tuple_window"
        ),
        _bound_or_legacy(
            "organization_ref",
            "relation_ref",
            "membership_ref",
            "subject_type",
            "relation_type",
            "source_kind",
            "source_ref",
            "source_version",
            name="ck_relation_tuple_legacy_or_bound",
        ),
        CheckConstraint(
            "organization_ref IS NULL OR (relation = relation_type AND source_version > 0 "
            "AND length(trim(relation_ref)) > 0 AND length(trim(source_kind)) > 0 "
            "AND length(trim(source_ref)) > 0 AND valid_from IS NOT NULL "
            "AND status IN ('active', 'suspended', 'revoked', 'expired'))",
            name="ck_relation_tuple_bound_fields",
        ),
        CheckConstraint(
            "organization_ref IS NULL OR (status = 'revoked' AND revoked_at IS NOT NULL) "
            "OR (status <> 'revoked' AND revoked_at IS NULL)",
            name="ck_relation_tuple_bound_revocation",
        ),
        CheckConstraint("version > 0", name="ck_relation_tuple_version"),
        Index(
            "uk_relation_tuple_legacy",
            "subject_ref",
            "relation",
            "resource_ref",
            unique=True,
            sqlite_where=text("organization_ref IS NULL"),
            postgresql_where=text("organization_ref IS NULL"),
        ),
        Index(
            "uk_relation_tuple_current",
            "organization_ref",
            "membership_ref",
            "relation_type",
            "resource_ref",
            unique=True,
            sqlite_where=text("status IN ('active', 'suspended') AND organization_ref IS NOT NULL"),
            postgresql_where=text("status IN ('active', 'suspended') AND organization_ref IS NOT NULL"),
        ),
        Index("ix_relation_tuple_bound_resource", "organization_ref", "resource_ref", "relation_type", "status"),
    )

    subject_ref: Mapped[str] = mapped_column(ExactString(256), nullable=False, index=True)
    relation: Mapped[str] = mapped_column(ExactString(128), nullable=False)
    resource_ref: Mapped[str] = mapped_column(ExactString(256), nullable=False, index=True)
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(ExactString(32), nullable=False, default="active")
    organization_ref: Mapped[str | None] = mapped_column("organization_ref", ExactString(128))
    relation_ref: Mapped[str | None] = mapped_column(ExactString(128))
    membership_ref: Mapped[str | None] = mapped_column(ExactString(128))
    subject_type: Mapped[str | None] = mapped_column(ExactString(32))
    relation_type: Mapped[str | None] = mapped_column(ExactString(128))
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    source_kind: Mapped[str | None] = mapped_column(ExactString(32))
    source_ref: Mapped[str | None] = mapped_column(ExactString(128))
    source_version: Mapped[int | None] = mapped_column(BigInteger)
