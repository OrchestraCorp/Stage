"""Management schema is additive and cannot discard an established handoff."""

import sqlite3
from pathlib import Path

import pytest

from tests.support.migrations import migrate

REVISION = "a7d4e2c9f501"
PREVIOUS = "c6f3a19d820b"


def test_management_migration_adds_no_authority_and_matches_models(tmp_path: Path) -> None:
    database = tmp_path / "management-empty.db"
    migrate(database, "upgrade", PREVIOUS)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO organization_root (organization_ref, display_name) VALUES ('org', 'Existing')")
    migrate(database, "upgrade", "head")
    migrate(database, "check")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM management_authority").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM role_assignment").fetchone() == (0,)
        assert connection.execute("SELECT organization_ref FROM organization_root").fetchone() == ("org",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    migrate(database, "downgrade", PREVIOUS)
    migrate(database, "upgrade", "head")
    migrate(database, "check")


@pytest.mark.parametrize(
    "status,accepted_at", [("pending", None), ("active", "2026-01-01 00:00:00"), ("revoked", "2026-01-01 00:00:00")]
)
def test_downgrade_never_discards_nomination_or_persistent_acceptance(
    tmp_path: Path,
    status: str,
    accepted_at: str | None,
) -> None:
    database = tmp_path / "management-populated.db"
    migrate(database, "upgrade", REVISION)
    with sqlite3.connect(database) as connection:
        # Isolate the migration's destructive-removal guard. Foreign-key behavior
        # is covered by ORM schema/nomination tests; no production facts are used.
        connection.execute(
            "INSERT INTO management_authority (organization_ref,authority_ref,principal_ref,"
            "membership_ref,assignment_ref,profile_ref,profile_version,external_identity_ref,"
            "grant_rules_json,nomination_expires_at,status,accepted_at) "
            "VALUES ('org','authority','person','member','assignment','profile',1,'identity','[]',"
            "'2026-01-02 00:00:00',?,?)",
            (status, accepted_at),
        )
    result = migrate(database, "downgrade", PREVIOUS, successful=False)
    assert "cannot discard administrator nomination or acceptance facts" in result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT status,accepted_at FROM management_authority").fetchone() == (
            status,
            accepted_at,
        )
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION,)
