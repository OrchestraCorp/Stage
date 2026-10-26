"""Shared helpers must not depend on collected tests or reset a parallel DB."""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.database_safety import require_serial_database_reset

ROOT = Path(__file__).resolve().parents[1]


def test_tests_and_scripts_do_not_import_collected_test_modules() -> None:
    violations: list[str] = []
    for directory in (ROOT / "tests", ROOT / "scripts"):
        for path in sorted(directory.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        package = list(path.relative_to(ROOT).parts[:-1])
                        module = ".".join(package[: len(package) - node.level + 1] + ([module] if module else []))
                    modules = [module] + [f"{module}.{alias.name}" for alias in node.names]
                else:
                    continue
                if any(
                    module.startswith("tests.") and any(part.startswith("test_") for part in module.split("."))
                    for module in modules
                ):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not violations, "Move shared fixtures/builders to tests/support: " + ", ".join(violations)


def test_support_contains_no_collected_test_definitions() -> None:
    for path in (ROOT / "tests" / "support").rglob("*.py"):
        for node in ast.parse(path.read_text()).body:
            assert not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test_"), (
                path,
                node.name,
            )


@pytest.mark.parametrize("workers", [1, 2, "auto", "logical"])
def test_configured_database_rejects_xdist_before_any_connection(workers: int | str) -> None:
    with pytest.raises(ValueError, match="cannot run with pytest-xdist"):
        require_serial_database_reset(
            "postgresql+asyncpg://private@localhost/stage_test", workers=workers, worker_id=None
        )


def test_xdist_worker_cannot_bypass_controller_database_check() -> None:
    with pytest.raises(ValueError, match="cannot run with pytest-xdist"):
        require_serial_database_reset(
            "postgresql+asyncpg://private@localhost/stage_test", workers=None, worker_id="gw0"
        )


def test_serial_shared_database_and_isolated_sqlite_workers_are_allowed() -> None:
    require_serial_database_reset("postgresql+asyncpg://private@localhost/stage_test", workers=0, worker_id=None)
    require_serial_database_reset(None, workers="auto", worker_id="gw0")


def test_pytest_worker_refuses_shared_database_before_collection() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/test_app.py"],
        cwd=ROOT,
        env={
            **os.environ,
            "TEST_DATABASE_URL": "postgresql+asyncpg://fixture@127.0.0.1:1/stage_test",
            "PYTEST_XDIST_WORKER": "gw0",
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR
    assert "cannot run with pytest-xdist" in completed.stderr
    assert "tests collected" not in completed.stdout
