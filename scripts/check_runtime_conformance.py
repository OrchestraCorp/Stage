"""Resolve every Core scenario to successful tests in an actual pytest JUnit run."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]


def verify(junit: Path, mapping: Path, output: Path) -> bool:
    document = json.loads(mapping.read_text())
    scenarios: dict[str, list[str]] = document["scenarios"]
    expected = {
        f"C{group}-{i:02d}" for group, count in enumerate((5, 4, 4, 4, 4, 4, 4, 5), 1) for i in range(1, count + 1)
    } | {f"S1-{i:02d}" for i in range(1, 13)}
    if set(scenarios) != expected or any(not nodes for nodes in scenarios.values()):
        raise ValueError("The scenario map must cover exactly 34 Core and 12 supplemental cases")
    tests: dict[str, str] = {}
    tree = ElementTree.parse(junit)
    for case in tree.iter("testcase"):
        nodeid = case.attrib["classname"].replace(".", "/") + ".py::" + case.attrib["name"]
        status = "passed"
        for tag in ("failure", "error", "skipped"):
            if case.find(tag) is not None:
                status = tag
                break
        if nodeid in tests:
            raise ValueError("Ambiguous duplicate test result")
        tests[nodeid] = status
    results: dict[str, Any] = {}
    for scenario, nodes in scenarios.items():
        results[scenario] = {
            "passed": all(tests.get(node) == "passed" for node in nodes),
            "tests": {node: tests.get(node, "not_executed") for node in nodes},
        }
    failures = sum(not case["passed"] for case in results.values())
    sources = sorted((ROOT / "app/runtime").glob("*.py"))
    sources += sorted((ROOT / "app/providers").glob("*.py"))
    sources += sorted((ROOT / "app/bootstrap").glob("*.py"))
    sources += sorted((ROOT / "tests/runtime_core").glob("*.py"))
    record = {
        "specification": document["specification"],
        "scope": "Mapped Python behavior tests; fixed Cordis reference comparison is separate evidence",
        "defined": len(scenarios),
        "passed": len(scenarios) - failures,
        "failed_or_not_run": failures,
        "test_totals": {
            status: sum(value == status for value in tests.values())
            for status in ("passed", "failure", "error", "skipped")
        },
        "junit_sha256": hashlib.sha256(junit.read_bytes()).hexdigest(),
        "mapping_sha256": hashlib.sha256(mapping.read_bytes()).hexdigest(),
        "sources": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        "scenarios": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(f"Core scenario evidence: {record['passed']}/46; incomplete/failed: {failures}")
    return failures == 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, default=ROOT / "tests/runtime_core/scenarios.json")
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/runtime-conformance.json")
    args = parser.parse_args()
    if not verify(args.junit, args.mapping, args.output):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
