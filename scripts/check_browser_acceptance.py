"""Run mandatory Chromium acceptance on an explicitly disposable PostgreSQL database.

Unlike the opt-in local suite, this gate treats missing prerequisites and skipped
browser tests as failures. Only the JUnit summary is a CI artifact; fixtures and
browser storage contain credentials and must never be uploaded.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "test_real_browser_employee_activation_requires_password_and_totp_without_login_or_grants": (
        "tests/integration/test_employee_activation_browser.py"
    ),
    "test_real_browser_factor_reset_requires_rebind_proof_and_new_otp": (
        "tests/integration/test_native_recovery_browser.py"
    ),
    "test_real_browser_sso_consent_form_post_and_confirmed_logout": "tests/integration/test_native_oauth_browser.py",
}


def verify_report(path: Path) -> None:
    """Every selected browser scenario must actually execute and pass."""
    cases = list(ElementTree.parse(path).iter("testcase"))
    if len(cases) != len(CASES) or {case.get("name") for case in cases} != set(CASES):
        raise ValueError("browser_acceptance_cases_missing_or_unexpected")
    if any(case.find(kind) is not None for case in cases for kind in ("skipped", "failure", "error")):
        raise ValueError("browser_acceptance_not_all_passed")


def summary_report(raw: Path, destination: Path) -> None:
    """Keep scenario names/timings/status, never captured output or fixture data."""
    root = ElementTree.Element("testsuites")
    suite = ElementTree.SubElement(root, "testsuite", {"name": "stage-chromium"})
    for case in ElementTree.parse(raw).iter("testcase"):
        result = ElementTree.SubElement(
            suite, "testcase", {key: case.get(key, "") for key in ("name", "classname", "time")}
        )
        for kind in ("skipped", "failure", "error"):
            if case.find(kind) is not None:
                ElementTree.SubElement(result, kind, {"message": f"browser scenario {kind}; inspect CI job log"})
    for kind, tag in (("tests", "testcase"), ("failures", "failure"), ("errors", "error"), ("skipped", "skipped")):
        suite.set(kind, str(sum(1 for _ in suite.iter(tag))))
    ElementTree.ElementTree(root).write(destination, encoding="utf-8", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path, default=ROOT / "tmp/browser-tests.xml")
    args = parser.parse_args()
    database = os.environ.get("TEST_DATABASE_URL", "")
    if not database.startswith("postgresql+asyncpg://") or os.environ.get("STAGE_ALLOW_TEST_DATABASE_RESET") != "1":
        parser.error("explicit disposable PostgreSQL and STAGE_ALLOW_TEST_DATABASE_RESET=1 are required")
    module = Path(os.environ.get("STAGE_PLAYWRIGHT_MODULE", ROOT / "scripts/browser/node_modules/playwright")).resolve()
    if shutil.which("node") is None or not (module / "package.json").is_file():
        parser.error("install Node and the locked scripts/browser dependencies before this gate")
    report = args.junit.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    # A stale passing report must never make a failed subprocess appear successful.
    report.unlink(missing_ok=True)
    raw_report = report.with_suffix(".raw.xml")
    raw_report.unlink(missing_ok=True)
    environment = {**os.environ, "STAGE_PLAYWRIGHT_MODULE": str(module), "STAGE_NATIVE_AUTH_ENABLED": "false"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short", *CASES.values(), f"--junitxml={raw_report}"],
        cwd=ROOT,
        env=environment,
        check=False,
    )
    try:
        summary_report(raw_report, report)
        if result.returncode:
            return result.returncode
        verify_report(report)
    except (OSError, ElementTree.ParseError, ValueError) as error:
        print(f"browser acceptance gate failed: {type(error).__name__}", file=sys.stderr)
        return 1
    finally:
        raw_report.unlink(missing_ok=True)
    print(f"Chromium acceptance: all {len(CASES)} required scenarios executed and passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
