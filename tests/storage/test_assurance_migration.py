"""MFA schema preserves existing admission and refuses silent assurance removal."""

import json
import sqlite3
from pathlib import Path

from tests.support.migrations import migrate

PRE_ASSURANCE_REVISION = "f4c7b2a1e903"
ASSURANCE_REVISION = "9c2e7a4b6d10"


def test_policy_migration_backfills_disabled_without_claiming_mfa(tmp_path: Path) -> None:
    database = tmp_path / "assurance-existing.db"
    migrate(database, "upgrade", PRE_ASSURANCE_REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO organization_root (organization_ref, display_name) VALUES ('existing:org', 'Existing')"
        )
    migrate(database, "upgrade", "head")
    migrate(database, "check")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("a7d4e2c9f501",)
        row = connection.execute(
            "SELECT organization_ref, mode, version, rules_json, max_auth_age_seconds "
            "FROM organization_human_authentication_policy"
        ).fetchone()
        assert row is not None and row[:3] == ("existing:org", "disabled", 1)
        assert json.loads(row[3]) == [] and row[4] is None
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    migrate(database, "downgrade", PRE_ASSURANCE_REVISION)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT organization_ref FROM organization_root").fetchone() == ("existing:org",)


def test_policy_downgrade_refuses_to_remove_enabled_or_changed_assurance(tmp_path: Path) -> None:
    database = tmp_path / "assurance-protected.db"
    migrate(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO organization_root (organization_ref, display_name) VALUES ('org', 'Org')")
        connection.execute(
            "INSERT INTO organization_human_authentication_policy "
            "(organization_ref, mode, version, rules_json, max_auth_age_seconds) "
            "VALUES ('org', 'required', 1, ?, 600)",
            (json.dumps([{"profile_ref": "human", "profile_version": 1, "required_acr": "2"}]),),
        )
    result = migrate(database, "downgrade", PRE_ASSURANCE_REVISION, successful=False)
    assert "cannot discard configured human authentication policy" in result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (ASSURANCE_REVISION,)
        connection.execute(
            "UPDATE organization_human_authentication_policy "
            "SET mode='disabled', version=2, rules_json='[]', max_auth_age_seconds=NULL"
        )
    result = migrate(database, "downgrade", PRE_ASSURANCE_REVISION, successful=False)
    assert "cannot discard configured human authentication policy" in result.stderr
