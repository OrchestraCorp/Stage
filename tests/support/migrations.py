"""Shared synthetic builders for migrations."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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
