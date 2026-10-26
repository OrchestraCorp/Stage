"""Preserve unmapped authorization history during the P1b expansion."""

import sqlite3
from pathlib import Path

from tests.support.migrations import migrate

PRE_AUTHORIZATION_REVISION = "d6a3c5f8e902"
AUTHORIZATION_REVISION = "f4c7b2a1e903"


def test_authorization_expansion_preserves_unmapped_legacy_rows(tmp_path: Path) -> None:
    database = tmp_path / "authorization-legacy.db"
    migrate(database, "upgrade", PRE_AUTHORIZATION_REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO role_assignment (id, principal_ref, role_code, scope_ref, status) "
            "VALUES (17, 'legacy:person', 'legacy:role', 'legacy:scope', 'active')"
        )
        connection.execute(
            "INSERT INTO relation_tuple (id, subject_ref, relation, resource_ref, status) "
            "VALUES (19, 'legacy:person', 'owner_of', 'legacy:resource', 'active')"
        )
    migrate(database, "upgrade", "head")
    migrate(database, "check")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("a7d4e2c9f501",)
        assert connection.execute(
            "SELECT id, principal_ref, role_code, scope_ref, organization_ref, membership_ref FROM role_assignment"
        ).fetchone() == (17, "legacy:person", "legacy:role", "legacy:scope", None, None)
        assert connection.execute(
            "SELECT id, subject_ref, relation, resource_ref, organization_ref, membership_ref FROM relation_tuple"
        ).fetchone() == (19, "legacy:person", "owner_of", "legacy:resource", None, None)
        assert connection.execute("SELECT count(*) FROM organization_root").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM role_version").fetchone() == (0,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    migrate(database, "downgrade", PRE_AUTHORIZATION_REVISION)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id, principal_ref FROM role_assignment").fetchone() == (17, "legacy:person")
        assert connection.execute("SELECT id, subject_ref FROM relation_tuple").fetchone() == (19, "legacy:person")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_authorization_downgrade_preserves_new_facts_by_refusing(tmp_path: Path) -> None:
    database = tmp_path / "authorization-retained.db"
    migrate(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO permission_action (action_ref, resource_kind, contract_version) "
            "VALUES ('task.view', 'work_item', '1')"
        )
    result = migrate(database, "downgrade", PRE_AUTHORIZATION_REVISION, successful=False)
    assert "requires empty new authorization tables" in result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (AUTHORIZATION_REVISION,)
        assert connection.execute("SELECT action_ref FROM permission_action").fetchone() == ("task.view",)


def test_downgrade_never_discards_committed_authorization_evidence(tmp_path: Path) -> None:
    database = tmp_path / "authorization-audit.db"
    migrate(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO command_execution "
            "(execution_id, tenant_ref, actor_ref, effective_subject_ref, command_name, command_version, "
            "idempotency_key, payload_digest, context_digest, target_ref, expected_version, request_id) "
            "VALUES ('execution:1', 'org:1', 'person:1', 'person:1', 'test', '1', 'stable-key', "
            "?, ?, 'work_item:1', 1, 'request:1')",
            ("a" * 64, "b" * 64),
        )
        connection.execute(
            "INSERT INTO execution_audit "
            "(audit_id, tenant_ref, execution_id, actor_ref, effective_subject_ref, action, target_ref, "
            "decision_id, policy_release_id, request_id, authorization_json) "
            "VALUES ('audit:1', 'org:1', 'execution:1', 'person:1', 'person:1', 'task.assign', 'work_item:1', "
            "'decision:1', 'policy:1', 'request:1', '{\"role_revision\":1}')"
        )
    result = migrate(database, "downgrade", PRE_AUTHORIZATION_REVISION, successful=False)
    assert "cannot discard persisted authorization evidence" in result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (AUTHORIZATION_REVISION,)
        assert connection.execute("SELECT authorization_json FROM execution_audit").fetchone() == (
            '{"role_revision":1}',
        )
