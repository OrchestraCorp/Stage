"""Build an immutable source/wheel bundle from a clean, committed Stage revision.

No environment files, working-tree overrides or database contents enter the
bundle. Verification logs can be attached separately by the operator. The
manifest binds the Git revision, lock file, migration heads and artifact hashes.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*arguments: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *arguments], text=True).strip()


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def migration_heads(source: Path) -> list[str]:
    value: ast.expr | None
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in (source / "migrations/versions").glob("*.py"):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name, value = node.targets[0].id, node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name, value = node.target.id, node.value
            else:
                continue
            if name not in {"revision", "down_revision"} or value is None:
                continue
            data = ast.literal_eval(value)
            target = revisions if name == "revision" else parents
            target.update((data,) if isinstance(data, str) else data or ())
    return sorted(revisions - parents)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new directory outside this checkout")
    args = parser.parse_args()
    destination = args.output.resolve()
    if destination.is_relative_to(ROOT) or destination.exists():
        parser.error("output must be a new directory outside the checkout")
    if git("status", "--porcelain", "--untracked-files=normal"):
        parser.error("commit all reviewed changes before freezing this revision")
    revision = git("rev-parse", "HEAD")
    timestamp = git("show", "-s", "--format=%ct", revision)
    destination.mkdir(parents=True)
    archive = destination / "stage-source.tar.gz"
    # Git archive is deterministic for a fixed commit. Omit gzip's filename and
    # wall-clock timestamp, which would otherwise make repeated bundles differ.
    with archive.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        result = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", revision], capture_output=True)
        result.check_returncode()
        compressed.write(result.stdout)
    with tempfile.TemporaryDirectory(prefix="stage-release-") as temporary:
        source = Path(temporary)
        with tarfile.open(archive) as package:
            package.extractall(source, filter="data")
        subprocess.run(
            ["uv", "build", "--no-sources", "--directory", str(source), "--out-dir", str(destination / "dist")],
            env={**os.environ, "SOURCE_DATE_EPOCH": timestamp},
            check=True,
        )
        for wheel in (destination / "dist").glob("*.whl"):
            subprocess.run(
                [sys.executable, str(source / "scripts/check_distribution.py"), str(wheel)],
                cwd=source,
                check=True,
            )
        artifacts = {
            str(path.relative_to(destination)): checksum(path)
            for path in sorted(destination.rglob("*"))
            if path.is_file()
        }
        manifest = {
            "schema": "stage-release-v1",
            "git_revision": revision,
            "source_date_epoch": int(timestamp),
            "python": platform.python_version(),
            "uv": subprocess.check_output(["uv", "--version"], text=True).strip(),
            "uv_lock_sha256": checksum(source / "uv.lock"),
            "browser_lock_sha256": checksum(source / "scripts/browser/package-lock.json"),
            "migration_heads": migration_heads(source),
            "distribution_checked": True,
            "artifacts": artifacts,
        }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"revision": revision, "bundle": str(destination), "artifacts": len(artifacts)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
