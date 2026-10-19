from pathlib import Path

import pytest

from scripts.check_contract import check_contract
from scripts.export_contract import DEFAULT_CONTRACT


def test_committed_openapi_matches_app() -> None:
    check_contract(DEFAULT_CONTRACT)


def test_contract_drift_is_rejected(tmp_path: Path) -> None:
    drifted = tmp_path / "openapi.yaml"
    drifted.write_text(DEFAULT_CONTRACT.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    # Comments alone are semantically harmless; change a parsed value to exercise the structural check.
    drifted.write_text(
        drifted.read_text(encoding="utf-8").replace("version: 0.1.0", "version: 9.9.9"), encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="contract drift detected"):
        check_contract(drifted)
