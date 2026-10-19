"""Verify migrations against an explicitly authorized, empty test database."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import validate_database_url  # noqa: E402
from app.core.db import create_engine  # noqa: E402


def validate_test_database(url: str, allow_reset: str | None) -> None:
    validate_database_url(url)
    database = make_url(url).database or ""
    name = Path(database).name
    if allow_reset != "1" or not re.match(r"^stage_(?:test|ci)(?:[_.]|$)", name):
        raise ValueError("Use a disposable stage_test*/stage_ci* database and set STAGE_ALLOW_TEST_DATABASE_RESET=1")


async def ensure_empty_database(url: str) -> None:
    engine = create_engine(url)
    try:
        for attempt in range(30):
            try:
                async with engine.connect() as connection:
                    tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
                if tables:
                    raise ValueError("Refusing to run migration downgrade against a nonempty database")
                return
            except (SQLAlchemyError, OSError):
                if attempt == 29:
                    raise RuntimeError("Test database did not become available") from None
                await asyncio.sleep(1)
    finally:
        await engine.dispose()


def run_migration_cycle(url: str) -> None:
    environment = dict(os.environ, STAGE_DATABASE_URL=url, STAGE_ENV="test")
    for args in (("upgrade", "head"), ("check",), ("downgrade", "base"), ("upgrade", "head"), ("check",)):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            # Preserve useful migration diagnostics while stripping the supplied credentials.
            diagnostic = result.stdout + result.stderr
            diagnostic = diagnostic.replace(url, "[redacted database URL]")
            password = make_url(url).password
            if password:
                diagnostic = diagnostic.replace(password, "[redacted]")
            raise RuntimeError(f"Migration {' '.join(args)} failed:\n{diagnostic}")
        print(f"PASS: migration {' '.join(args)}")


def main() -> None:
    url = os.environ.get("TEST_DATABASE_URL", "")
    validate_test_database(url, os.environ.get("STAGE_ALLOW_TEST_DATABASE_RESET"))
    asyncio.run(ensure_empty_database(url))
    run_migration_cycle(url)


if __name__ == "__main__":
    main()
