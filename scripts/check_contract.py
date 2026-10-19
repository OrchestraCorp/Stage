import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.export_contract import (  # noqa: E402
    DEFAULT_CONTRACT,
    build_openapi,
    canonicalize,
    parse_contract,
    write_contract,
)


def check_contract(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"contract is missing: {path}")
    expected = canonicalize(build_openapi())
    actual = canonicalize(parse_contract(path))
    if actual != expected:
        raise SystemExit(
            "contract drift detected: generated app.openapi() differs from "
            f"{path}. Run `uv run python scripts/check_contract.py --write` to refresh it."
        )
    print(f"contract check passed: {path}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="compare the committed contract (default)")
    mode.add_argument("--write", action="store_true", help="write the generated contract")
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    args = parser.parse_args()
    path = args.contract if args.contract.is_absolute() else DEFAULT_CONTRACT.parent.parent / args.contract
    if args.write:
        write_contract(path)
        print(f"contract written: {path}")
        return
    check_contract(path)


if __name__ == "__main__":
    main()
