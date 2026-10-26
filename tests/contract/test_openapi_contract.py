import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_contract import check_contract
from scripts.export_contract import DEFAULT_CONTRACT, parse_contract


def test_committed_openapi_matches_app() -> None:
    check_contract(DEFAULT_CONTRACT)


def test_work_contract_exposes_only_selected_assignment_and_metadata_inputs() -> None:
    contract = parse_contract(DEFAULT_CONTRACT)
    paths = contract["paths"]
    operations = (
        paths["/api/v1/work-items/{work_item_id}"]["get"],
        paths["/api/v1/work-items/{work_item_id}/details"]["put"],
        paths["/api/v1/work-items/{work_item_id}/operations/{idempotency_key}"]["get"],
    )
    for operation in operations:
        assert {item["name"] for item in operation["parameters"] if item["in"] == "query"} == {
            "profile_ref",
            "profile_version",
            "assignment_ref",
        }
        assert operation["security"] == [{"HTTPBearer": []}]
        assert all(code in operation["responses"] for code in ("401", "403", "422", "503"))
    update = operations[1]
    assert any(
        parameter["name"] == "Idempotency-Key" and parameter["in"] == "header" and parameter["required"]
        for parameter in update["parameters"]
    )
    assert "Location" in update["responses"]["503"]["headers"]
    body = contract["components"]["schemas"]["UpdateWorkDetailsRequest"]
    assert body["additionalProperties"] is False
    assert set(body["properties"]) == set(body["required"]) == {"title", "description", "expected_version"}


def test_assignment_discovery_contract_does_not_require_or_expose_caller_supplied_identity() -> None:
    contract = parse_contract(DEFAULT_CONTRACT)
    operation = contract["paths"]["/api/v1/identity/assignments"]["get"]
    parameters = {item["name"]: item for item in operation["parameters"] if item["in"] == "query"}
    assert set(parameters) == {"profile_ref", "profile_version", "limit", "after"}
    assert parameters["limit"]["schema"]["default"] == 20
    assert parameters["limit"]["schema"]["maximum"] == 50
    assert operation["security"] == [{"HTTPBearer": []}]
    assert operation["operationId"] == "identity_list_self_assignments"
    view = contract["components"]["schemas"]["AssignmentView"]
    assert view["additionalProperties"] is False
    assert "assignment_ref" in view["properties"]
    assert not {"actor_ref", "principal_ref", "permission_actions", "token"}.intersection(view["properties"])
    assert set(contract["components"]["schemas"]["AssignmentList"]["properties"]) == {"items", "next_after"}


def test_contract_drift_is_rejected(tmp_path: Path) -> None:
    drifted = tmp_path / "openapi.yaml"
    drifted.write_text(DEFAULT_CONTRACT.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    # Comments alone are semantically harmless; change a parsed value to exercise the structural check.
    drifted.write_text(
        drifted.read_text(encoding="utf-8").replace("version: 1.0.0", "version: 9.9.9"), encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="contract drift detected"):
        check_contract(drifted)


def test_export_contract_can_write_an_absolute_external_path(tmp_path: Path) -> None:
    output = tmp_path / "exported.json"
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "scripts/export_contract.py", "--write", "--output", str(output)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    check_contract(output)
