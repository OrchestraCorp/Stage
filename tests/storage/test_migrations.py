import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
INITIAL_REVISION = "870845fbe1f0"


def migrate(database: Path, *arguments: str, successful: bool = True) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.update(STAGE_DATABASE_URL=f"sqlite+aiosqlite:///{database}", STAGE_ENV="test")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if successful:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0
    return result


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
