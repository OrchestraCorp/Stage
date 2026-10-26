"""Verify retained IDs and serial sequences in an explicitly empty disposable PG database.

Uses TEST_DATABASE_URL and STAGE_ALLOW_TEST_DATABASE_RESET=1, like check_database.
Leaves the retained synthetic rows at the current schema head for inspection.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.db import create_engine  # noqa: E402
from scripts.check_database import ensure_empty_database, validate_test_database  # noqa: E402

BASE_REVISION = "d6a3c5f8e902"


def migrate(url: str, *arguments: str) -> None:
    environment = dict(os.environ, STAGE_DATABASE_URL=url, STAGE_ENV="test")
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        diagnostic = (completed.stdout + completed.stderr).replace(url, "[database URL]")
        password = make_url(url).password
        if password:
            diagnostic = diagnostic.replace(password, "[redacted]")
        raise RuntimeError("Authorization migration check failed: " + diagnostic)


async def append_and_check(url: str, expected: int) -> None:
    engine = create_engine(url)
    try:
        async with engine.begin() as connection:
            values = {"principal": f"probe:person:{expected}", "scope": f"probe:scope:{expected}"}
            assignment = await connection.scalar(
                text(
                    "INSERT INTO role_assignment(principal_ref, role_code, scope_ref, status) "
                    "VALUES (:principal, 'probe-role', :scope, 'active') RETURNING id"
                ),
                values,
            )
            relation = await connection.scalar(
                text(
                    "INSERT INTO relation_tuple(subject_ref, relation, resource_ref, status) "
                    "VALUES (:principal, 'viewer_of', :scope, 'active') RETURNING id"
                ),
                values,
            )
            assert assignment == relation == expected, "Migration reset the retained serial sequence"
            for table in ("role_assignment", "relation_tuple"):
                ids = (await connection.scalars(text(f"SELECT id FROM {table} ORDER BY id"))).all()
                assert ids == list(range(1, expected + 1)), "Migration changed retained IDs"
    finally:
        await engine.dispose()


def main() -> None:
    url = os.environ.get("TEST_DATABASE_URL", "")
    validate_test_database(url, os.environ.get("STAGE_ALLOW_TEST_DATABASE_RESET"))
    if make_url(url).get_backend_name() != "postgresql":
        raise ValueError("Serial-sequence validation requires PostgreSQL")
    asyncio.run(ensure_empty_database(url))
    migrate(url, "upgrade", BASE_REVISION)
    asyncio.run(append_and_check(url, 1))
    migrate(url, "upgrade", "head")
    migrate(url, "check")
    asyncio.run(append_and_check(url, 2))
    migrate(url, "downgrade", BASE_REVISION)
    asyncio.run(append_and_check(url, 3))
    migrate(url, "upgrade", "head")
    migrate(url, "check")
    asyncio.run(append_and_check(url, 4))
    print("PASS: PostgreSQL authorization upgrade/downgrade preserves legacy rows and serial sequences")


if __name__ == "__main__":
    main()
