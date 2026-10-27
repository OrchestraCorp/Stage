"""Activation schema preserves enrolled accounts and refuses lifecycle-data loss."""

import sqlite3
from pathlib import Path

import pytest

from tests.support.migrations import migrate

REVISION = "d8e4b6c2a901"
PREVIOUS = "a7d4e2c9f501"


def _account(
    connection: sqlite3.Connection, *, status: str = "active", password: str | None = "synthetic-hash"
) -> None:
    connection.execute(
        "INSERT INTO principal (principal_ref, principal_type, status) VALUES ('person', 'human', 'active')"
    )
    connection.execute(
        "INSERT INTO native_account (account_ref, principal_ref, username, password_hash, status, recovery_hashes) "
        "VALUES ('account', 'person', 'employee', ?, ?, '[]')",
        (password, status),
    )


def test_activation_migration_preserves_accounts_and_referenced_security_history(tmp_path: Path) -> None:
    database = tmp_path / "activation-existing.db"
    migrate(database, "upgrade", PREVIOUS)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _account(connection)
        connection.execute(
            "INSERT INTO native_auth_event (event,account_ref,detail) VALUES ('synthetic.event','account','{}')"
        )
        before = connection.execute("SELECT * FROM native_account").fetchall()
        events = connection.execute("SELECT * FROM native_auth_event").fetchall()
    migrate(database, "upgrade", REVISION)
    migrate(database, "check")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM native_account_activation").fetchone() == (0,)
        assert connection.execute("SELECT * FROM native_account").fetchall() == before
        assert connection.execute("SELECT * FROM native_auth_event").fetchall() == events
        assert connection.execute("SELECT count(*) FROM native_session").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM role_assignment").fetchone() == (0,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    migrate(database, "downgrade", PREVIOUS)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM native_account").fetchall() == before
        assert connection.execute("SELECT * FROM native_auth_event").fetchall() == events
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert "native_account_activation" not in {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    migrate(database, "upgrade", REVISION)
    migrate(database, "check")


@pytest.mark.parametrize("status", ["issued", "enrolling", "completed", "revoked"])
def test_activation_migration_refuses_to_discard_any_activation_history(tmp_path: Path, status: str) -> None:
    database = tmp_path / "activation-retained.db"
    migrate(database, "upgrade", REVISION)
    with sqlite3.connect(database) as connection:
        # Isolate the downgrade guard from domain setup. Foreign-key enforcement
        # and production facts are tested through the real onboarding service.
        connection.execute(
            "INSERT INTO native_account_activation (activation_ref,account_ref,organization_ref,principal_ref,"
            "membership_ref,external_identity_ref,source_pin,status,version,expires_at,failures) "
            "VALUES ('activation','account','org','person','member','identity','{}',?,1,'2026-12-01 00:00:00',0)",
            (status,),
        )
    result = migrate(database, "downgrade", PREVIOUS, successful=False)
    assert "cannot discard employee activation facts" in result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT status FROM native_account_activation").fetchone() == (status,)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION,)
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(native_account)")}
        assert columns["password_hash"][3] == 0


@pytest.mark.parametrize("status,password", [("pending", None), ("disabled", None), ("pending", "synthetic-hash")])
def test_activation_migration_refuses_pending_or_passwordless_account_downgrade(
    tmp_path: Path, status: str, password: str | None
) -> None:
    database = tmp_path / "activation-passwordless.db"
    migrate(database, "upgrade", REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _account(connection, status=status, password=password)
    result = migrate(database, "downgrade", PREVIOUS, successful=False)
    assert "cannot discard pending or passwordless native accounts" in result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT status,password_hash FROM native_account").fetchone() == (status, password)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION,)
        assert connection.execute("SELECT count(*) FROM native_account_activation").fetchone() == (0,)


def test_activation_migration_never_allows_active_accounts_without_passwords(tmp_path: Path) -> None:
    database = tmp_path / "activation-required-password.db"
    migrate(database, "upgrade", REVISION)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _account(connection, status="pending", password=None)
        with pytest.raises(sqlite3.IntegrityError, match="ck_native_account_password"):
            connection.execute("UPDATE native_account SET status='active'")
        connection.execute("UPDATE native_account SET status='active', password_hash='synthetic-replacement'")
        with pytest.raises(sqlite3.IntegrityError, match="ck_native_account_password"):
            connection.execute("UPDATE native_account SET password_hash=NULL")
        with pytest.raises(sqlite3.IntegrityError, match="ck_native_account_status"):
            connection.execute("UPDATE native_account SET status='unknown'")
