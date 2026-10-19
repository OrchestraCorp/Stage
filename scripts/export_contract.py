"""Export the FastAPI OpenAPI document in a deterministic JSON or YAML form."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import create_app  # noqa: E402

DEFAULT_CONTRACT = ROOT / "contracts" / "openapi.yaml"


def canonicalize(value: Any) -> Any:
    """Return a recursively stable representation for contract comparison."""

    if isinstance(value, dict):
        return {str(key): canonicalize(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, list):
        return [canonicalize(item) for item in value]
    return value


def build_openapi() -> dict[str, Any]:
    return create_app().openapi()


def parse_contract(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        document = json.load(handle) if path.suffix.lower() == ".json" else yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise ValueError(f"contract must contain a top-level object: {path}")
    return document


def serialize_contract(document: dict[str, Any], *, format: str) -> str:
    stable = canonicalize(document)
    if format == "json":
        return json.dumps(stable, ensure_ascii=False, indent=2) + "\n"
    if format == "yaml":
        return yaml.safe_dump(stable, allow_unicode=True, sort_keys=False, default_flow_style=False)
    raise ValueError(f"unsupported contract format: {format}")


def format_for_path(path: Path) -> str:
    return "json" if path.suffix.lower() == ".json" else "yaml"


def write_contract(path: Path, *, format: str | None = None) -> None:
    output_format = format or format_for_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_contract(build_openapi(), format=output_format), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--format", choices=("yaml", "json"))
    parser.add_argument("--write", action="store_true", help="write the generated contract to --output")
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    if not args.write:
        print(serialize_contract(build_openapi(), format=args.format or format_for_path(output)), end="")
        return
    write_contract(output, format=args.format)
    print(f"wrote {output.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
