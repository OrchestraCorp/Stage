import sqlite3
from pathlib import Path
from urllib.parse import unquote

import pytest

from tests.support.migrations import (
    migrate,
)

INITIAL_REVISION = "870845fbe1f0"
PRE_IDENTITY_REVISION = "a1c4e7d9b203"
IDENTITY_REVISION = "b7e1a92c8d40"
SINGLE_ENTERPRISE_REVISION = "c4f2a89d1e63"
ORGANIZATION_ROOT_REVISION = "d6a3c5f8e902"


def test_empty_migration_cycle_and_model_drift(tmp_path: Path) -> None:
    database = tmp_path / "cycle.db"
    migrate(database, "upgrade", "head")
    migrate(database, "check")
    migrate(database, "downgrade", "base")
    migrate(database, "upgrade", "head")
    migrate(database, "check")


def test_migration_config_accepts_percent_encoded_url_components(tmp_path: Path) -> None:
    # SQLite path exercises the same ConfigParser interpolation boundary as an encoded password.
    database = tmp_path / "stage_test%40encoded.db"
    migrate(database, "upgrade", "head")
    assert Path(unquote(str(database))).exists()
    migrate(database, "check")


def test_legacy_naive_data_requires_explicit_utc_provenance(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    migrate(database, "upgrade", INITIAL_REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO principal (principal_ref, principal_type, status) VALUES (?, ?, ?)",
            ("person:1", "human", "active"),
        )
    result = migrate(database, "upgrade", "head", successful=False)
    assert "legacy_timestamps=utc" in result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (INITIAL_REVISION,)
    migrate(database, "-x", "legacy_timestamps=utc", "upgrade", "head")
    migrate(database, "check")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT principal_ref FROM principal").fetchone() == ("person:1",)
    result = migrate(database, "downgrade", INITIAL_REVISION, successful=False)
    assert "Downgrade requires empty Stage tables" in result.stderr


def test_preflight_rejects_invalid_legacy_rows_before_schema_changes(tmp_path: Path) -> None:
    database = tmp_path / "invalid.db"
    migrate(database, "upgrade", INITIAL_REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO principal (principal_ref, principal_type, status) VALUES (?, ?, ?)",
            ("person:1", "Human", "active"),
        )
    result = migrate(database, "-x", "legacy_timestamps=utc", "upgrade", "head", successful=False)
    assert "ck_principal_type" in result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (INITIAL_REVISION,)
        assert (
            "ck_principal_type"
            not in connection.execute("SELECT sql FROM sqlite_master WHERE name='principal'").fetchone()[0]
        )


def test_identity_expansion_preserves_legacy_rows_without_inventing_an_organization_root(tmp_path: Path) -> None:
    database = tmp_path / "identity-expand.db"
    migrate(database, "upgrade", PRE_IDENTITY_REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO principal (id, principal_ref, principal_type, status) VALUES (42, ?, ?, ?)",
            ("human:legacy", "human", "active"),
        )
        connection.execute(
            "INSERT INTO role_assignment (id, principal_ref, role_code, scope_ref, status) VALUES (73, ?, ?, ?, ?)",
            ("human:legacy", "engineering-member", "unmapped:old-scope", "active"),
        )
        connection.execute(
            "INSERT INTO relation_tuple (id, subject_ref, relation, resource_ref, status) VALUES (91, ?, ?, ?, ?)",
            ("human:legacy", "owner_of", "legacy-resource", "active"),
        )
    migrate(database, "upgrade", "head")
    migrate(database, "check")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT id, principal_ref, version, identity_epoch, recovery_epoch, disabled_at FROM principal"
        ).fetchone() == (42, "human:legacy", 1, 0, 0, None)
        assert connection.execute("SELECT id, scope_ref FROM role_assignment").fetchone() == (73, "unmapped:old-scope")
        assert connection.execute("SELECT id, resource_ref FROM relation_tuple").fetchone() == (91, "legacy-resource")
        assert connection.execute("SELECT count(*) FROM organization_root").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM tenant_membership").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM external_identity").fetchone() == (0,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    migrate(database, "downgrade", PRE_IDENTITY_REVISION)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id, principal_ref FROM principal").fetchone() == (42, "human:legacy")
        assert connection.execute("SELECT id FROM role_assignment").fetchone() == (73,)
        assert connection.execute("SELECT id FROM relation_tuple").fetchone() == (91,)


def test_identity_downgrade_refuses_retained_facts_before_removing_any_tables(tmp_path: Path) -> None:
    database = tmp_path / "identity-retained.db"
    migrate(database, "upgrade", IDENTITY_REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO tenant (tenant_ref, display_name) VALUES ('tenant:1', 'Tenant')")
    result = migrate(database, "downgrade", PRE_IDENTITY_REVISION, successful=False)
    assert "Downgrade requires empty identity foundation tables" in result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (IDENTITY_REVISION,)
        assert connection.execute("SELECT tenant_ref FROM tenant").fetchone() == ("tenant:1",)
        assert connection.execute("SELECT count(*) FROM workload_registration").fetchone() == (0,)


def test_identity_downgrade_does_not_erase_principal_revocation_versions(tmp_path: Path) -> None:
    database = tmp_path / "identity-revocation.db"
    migrate(database, "upgrade", IDENTITY_REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO principal (principal_ref, principal_type, status, identity_epoch) "
            "VALUES ('human:1', 'human', 'active', 1)"
        )
    result = migrate(database, "downgrade", PRE_IDENTITY_REVISION, successful=False)
    assert "cannot erase advanced principal identity versions" in result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (IDENTITY_REVISION,)
        assert connection.execute("SELECT identity_epoch FROM principal").fetchone() == (1,)


def test_single_enterprise_migration_preserves_organization_and_membership_history(tmp_path: Path) -> None:
    database = tmp_path / "single-enterprise.db"
    migrate(database, "upgrade", IDENTITY_REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO tenant (tenant_ref, display_name, status, version) VALUES (?, ?, ?, ?)",
            ("existing:enterprise", "Existing private enterprise", "suspended", 3),
        )
        connection.execute(
            "INSERT INTO principal (principal_ref, principal_type, status) VALUES ('human:1', 'human', 'active')"
        )
        connection.execute(
            "INSERT INTO tenant_membership "
            "(tenant_ref, membership_ref, principal_ref, principal_type, valid_from, source_ref) "
            "VALUES ('existing:enterprise', 'membership:1', 'human:1', 'human', '2026-10-25 00:00:00', 'install:1')"
        )
    migrate(database, "upgrade", SINGLE_ENTERPRISE_REVISION)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT tenant_ref, display_name, status, version, deployment_slot FROM tenant"
        ).fetchone() == ("existing:enterprise", "Existing private enterprise", "suspended", 3, 1)
        assert connection.execute(
            "SELECT tenant_ref, membership_ref, principal_ref FROM tenant_membership"
        ).fetchone() == (
            "existing:enterprise",
            "membership:1",
            "human:1",
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    migrate(database, "downgrade", IDENTITY_REVISION)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT tenant_ref FROM tenant").fetchone() == ("existing:enterprise",)
        assert connection.execute("SELECT membership_ref FROM tenant_membership").fetchone() == ("membership:1",)
        assert "deployment_slot" not in {row[1] for row in connection.execute("PRAGMA table_info(tenant)")}
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_single_enterprise_migration_refuses_multiple_organizations_without_altering_data(tmp_path: Path) -> None:
    database = tmp_path / "multiple-organizations.db"
    migrate(database, "upgrade", IDENTITY_REVISION)
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO tenant (tenant_ref, display_name) VALUES (?, ?)",
            (("organization:a", "First enterprise"), ("organization:b", "Second enterprise")),
        )
    result = migrate(database, "upgrade", SINGLE_ENTERPRISE_REVISION, successful=False)
    assert "tenant contains 2 rows" in result.stderr
    assert "No rows were merged or removed" in result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (IDENTITY_REVISION,)
        assert connection.execute("SELECT tenant_ref, display_name FROM tenant ORDER BY tenant_ref").fetchall() == [
            ("organization:a", "First enterprise"),
            ("organization:b", "Second enterprise"),
        ]
        assert "deployment_slot" not in {row[1] for row in connection.execute("PRAGMA table_info(tenant)")}


def test_organization_root_rename_preserves_identity_guard_and_audit_history(tmp_path: Path) -> None:
    database = tmp_path / "organization-root.db"
    migrate(database, "upgrade", SINGLE_ENTERPRISE_REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO tenant (tenant_ref, display_name, status, version) "
            "VALUES ('root:existing', 'Group authorization root', 'revoked', 7)"
        )
        connection.execute(
            "INSERT INTO principal (id, principal_ref, principal_type, status, version, identity_epoch) "
            "VALUES (42, 'human:existing', 'human', 'active', 4, 3)"
        )
        connection.execute(
            "INSERT INTO tenant_membership "
            "(tenant_ref, membership_ref, principal_ref, principal_type, valid_from, source_ref, version) "
            "VALUES ('root:existing', 'membership:existing', 'human:existing', 'human', "
            "'2026-10-25 00:00:00', 'install:existing', 6)"
        )
        connection.execute(
            "INSERT INTO trust_state (trust_ref, purpose, configuration_digest) "
            "VALUES ('trust:existing', 'authentication', ?)",
            ("a" * 64,),
        )
        connection.execute(
            "INSERT INTO identity_provider (provider_ref, issuer_exact, trust_ref) "
            "VALUES ('provider:existing', 'https://issuer.example', 'trust:existing')"
        )
        connection.execute(
            "INSERT INTO authentication_profile "
            "(profile_ref, profile_version, provider_ref, subject_namespace, credential_kind, "
            "stage_audience, config_schema, config_json, digest) "
            "VALUES ('profile:existing', 1, 'provider:existing', 'human/sub', 'human_oidc', "
            "'stage', 'stage.jwt.v1', '{}', ?)",
            ("b" * 64,),
        )
        connection.execute(
            "INSERT INTO tenant_authentication_binding "
            "(tenant_ref, binding_ref, profile_ref, profile_version, environment, constraints_json, "
            "valid_from, version) "
            "VALUES ('root:existing', 'binding:existing', 'profile:existing', 1, 'test', '{}', "
            "'2026-10-25 00:00:00', 2)"
        )
        connection.execute("INSERT INTO execution_tenant_guard (tenant_ref, epoch) VALUES ('root:existing', 11)")
        connection.execute(
            "INSERT INTO command_execution "
            "(execution_id, tenant_ref, actor_ref, effective_subject_ref, command_name, command_version, "
            "idempotency_key, payload_digest, context_digest, target_ref, expected_version, request_id, "
            "status, result_json, completed_at) "
            "VALUES ('execution:existing', 'root:existing', 'human:existing', 'human:existing', "
            "'work.submit', '1', 'request:existing', ?, ?, 'work:existing', 1, 'request:existing', "
            "'succeeded', '{\"version\":2}', '2026-10-25 00:00:00')",
            ("c" * 64, "d" * 64),
        )
        connection.execute(
            "INSERT INTO execution_audit "
            "(audit_id, tenant_ref, execution_id, actor_ref, effective_subject_ref, action, target_ref, "
            "decision_id, policy_release_id, request_id, after_json) "
            "VALUES ('audit:existing', 'root:existing', 'execution:existing', 'human:existing', 'human:existing', "
            "'work.submit', 'work:existing', 'decision:existing', 'policy:existing', 'request:existing', "
            "'{\"version\":2}')"
        )
        retained_tables = (
            "principal",
            "tenant_membership",
            "tenant_authentication_binding",
            "execution_tenant_guard",
            "command_execution",
            "execution_audit",
        )
        before = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in retained_tables}
        root_before = connection.execute("SELECT * FROM tenant").fetchall()

    migrate(database, "upgrade", ORGANIZATION_ROOT_REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (ORGANIZATION_ROOT_REVISION,)
        assert connection.execute("SELECT * FROM organization_root").fetchall() == root_before
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='tenant'").fetchall() == []
        assert {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in retained_tables} == before
        for table in ("tenant_membership", "tenant_authentication_binding"):
            foreign_keys = connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            assert any(row[2:5] == ("organization_root", "tenant_ref", "organization_ref") for row in foreign_keys)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        # A revoked authorization root still occupies the deployment's only slot.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO organization_root (organization_ref, display_name) VALUES ('root:second', 'Second root')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM organization_root")

    migrate(database, "downgrade", SINGLE_ENTERPRISE_REVISION)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM tenant").fetchall() == root_before
        assert {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in retained_tables} == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for table in ("tenant_membership", "tenant_authentication_binding"):
            foreign_keys = connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            assert any(row[2:5] == ("tenant", "tenant_ref", "tenant_ref") for row in foreign_keys)
    migrate(database, "upgrade", "head")
    migrate(database, "check")
