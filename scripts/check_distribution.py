"""Verify that a built wheel imports independently and carries its runtime migration heads."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    from alembic.script import ScriptDirectory

    expected = sorted(ScriptDirectory(str(ROOT / "migrations")).get_heads())
    with tempfile.TemporaryDirectory(prefix="stage-wheel-check-") as temporary:
        destination = Path(temporary)
        with zipfile.ZipFile(args.wheel) as package:
            package.extractall(destination)
        code = """
import json, sys
from pathlib import Path
import app
from app.core.config import Settings
from app.main import create_app
from app.providers.storage import schema_heads
root = Path.cwd()
assert Path(app.__file__).resolve().is_relative_to(root), 'working_tree_import_leaked'
heads = sorted(schema_heads())
assert heads == json.loads(sys.argv[1]), 'packaged_migration_heads_mismatch'
api = create_app(Settings(env='test', native_auth_enabled=False)).openapi()
assert api['paths'], 'packaged_routes_missing'
print(json.dumps({'independent_wheel_import': True, 'migration_heads': heads}))
"""
        subprocess.run(
            [sys.executable, "-c", code, json.dumps(expected)],
            cwd=destination,
            env={**os.environ, "PYTHONPATH": str(destination), "STAGE_NATIVE_AUTH_ENABLED": "false"},
            check=True,
        )


if __name__ == "__main__":
    main()
