"""Enforce the public composition seams that this architecture review established."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_domains_use_public_control_and_current_session_contracts() -> None:
    violations: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module in {"app.identity.administration", "app.native_auth.oauth"}:
                violations.extend(
                    f"{path.relative_to(ROOT)}:{node.lineno} imports {item.name}"
                    for item in node.names
                    if item.name.startswith("_")
                )
    assert violations == []


def test_features_do_not_call_private_kernel_authorization() -> None:
    violations: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        if path == ROOT / "app/execution/kernel.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr == "_authorize":
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []
