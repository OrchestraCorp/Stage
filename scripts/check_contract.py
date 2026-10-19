from pathlib import Path


def main() -> None:
    contract = Path("contracts/openapi.yaml")
    if not contract.exists():
        raise SystemExit("contracts/openapi.yaml is missing")
    text = contract.read_text(encoding="utf-8")
    for marker in ("openapi: 3.1.0", "/health:", "HealthResponse"):
        if marker not in text:
            raise SystemExit(f"contract marker missing: {marker}")
    print("contract check passed")


if __name__ == "__main__":
    main()
