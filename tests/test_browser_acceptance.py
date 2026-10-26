from pathlib import Path

import pytest

from scripts.check_browser_acceptance import CASES, summary_report, verify_report


@pytest.mark.parametrize("marker", ["skipped", "failure", "error"])
def test_browser_gate_rejects_non_passing_scenario(tmp_path: Path, marker: str) -> None:
    report = tmp_path / "report.xml"
    report.write_text(
        "<testsuite>" + "".join(f'<testcase name="{name}"><{marker}/></testcase>' for name in CASES) + "</testsuite>"
    )
    with pytest.raises(ValueError, match="not_all_passed"):
        verify_report(report)


def test_browser_gate_requires_both_scenarios_and_accepts_passing_report(tmp_path: Path) -> None:
    report = tmp_path / "report.xml"
    report.write_text('<testsuite><testcase name="unrelated_passing_test"/></testsuite>')
    with pytest.raises(ValueError, match="cases_missing"):
        verify_report(report)
    report.write_text(
        "<testsuites><testsuite>" + "".join(f'<testcase name="{n}"/>' for n in CASES) + "</testsuite></testsuites>"
    )
    verify_report(report)


def test_ci_artifact_retains_status_without_fixture_output(tmp_path: Path) -> None:
    raw, summary = tmp_path / "raw.xml", tmp_path / "summary.xml"
    raw.write_text(
        '<testsuite><testcase name="example" time="1.2"><failure message="synthetic-secret">'
        "synthetic-secret</failure><system-out>synthetic-secret</system-out></testcase></testsuite>"
    )
    summary_report(raw, summary)
    assert "synthetic-secret" not in summary.read_text()
    assert 'failures="1"' in summary.read_text()
