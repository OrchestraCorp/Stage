"""Run a fixed Cordis reference in an isolated extraction, without npm installs."""

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REFERENCE_COMMIT = "6f7124baabca582d43ce84fd59d2f48e4b790763"
REFERENCE_PATHS = ("vendor/cordis", "vendor/cosmokit")


def run(repository: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stage-cordis-reference-") as scratch:
        directory = Path(scratch)
        archive = directory / "reference.tar"
        with archive.open("wb") as stream:
            subprocess.run(
                ["git", "-C", str(repository), "archive", REFERENCE_COMMIT, *REFERENCE_PATHS],
                stdout=stream,
                check=True,
            )
        with tarfile.open(archive) as source:
            source.extractall(directory, filter="data")
        manifest = {}
        for path in sorted((directory / "vendor").rglob("*")):
            if path.is_file():
                manifest[str(path.relative_to(directory))] = hashlib.sha256(path.read_bytes()).hexdigest()
        adapter = Path(__file__).with_name("reference.mjs")
        command = ["node", "--experimental-transform-types", str(adapter), str(directory)]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
        (output / "trace.ts.jsonl").write_text(completed.stdout)
        (output / "stderr.txt").write_text(completed.stderr)
        events = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
        results = [event for event in events if event.get("event") == "result"]
        shared = subprocess.run(
            ["node", "--experimental-transform-types", str(adapter.with_name("shared.mjs")), str(directory)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        python = subprocess.run(
            [sys.executable, "-m", "scripts.runtime_reference.shared_python"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=60,
        )
        (output / "shared.ts.jsonl").write_text(shared.stdout)
        (output / "shared.python.jsonl").write_text(python.stdout)
        (output / "shared.stderr.txt").write_text(shared.stderr + python.stderr)
        ts_results = {row["scenario_id"]: row for row in map(json.loads, shared.stdout.splitlines())}
        py_results = {row["scenario_id"]: row for row in map(json.loads, python.stdout.splitlines())}
        comparisons = []
        for case in json.loads(adapter.with_name("shared-cases.json").read_text()):
            left, right = ts_results.get(case["id"]), py_results.get(case["id"])
            equal = bool(left and right and left == right and left.get("outcome") == "observed")
            expected = case.get("expected_observations")
            if case.get("comparison") == "expected-deviation":
                matched = bool(
                    left
                    and right
                    and left.get("outcome") == right.get("outcome") == "observed"
                    and left.get("observations") == expected["ts"]
                    and right.get("observations") == expected["python"]
                )
            else:
                matched = equal
            comparisons.append(
                {
                    "scenario_id": case["id"],
                    "scope": case["scope"],
                    "comparison": case.get("comparison", "shared-semantics"),
                    "deviations": case.get("deviations", []),
                    "adaptation": case.get("adaptation"),
                    "equal": equal,
                    "matched": matched,
                    "expected_observations": expected,
                    "ts": left,
                    "python": right,
                }
            )
        (output / "comparisons.json").write_text(json.dumps(comparisons, indent=2) + "\n")
        deviation_links = {
            "C1-03": ["D-03"],
            "C1-04": ["D-02", "D-04"],
            "C2-04": ["D-09"],
            "C3-01": ["D-05"],
            "C3-02": ["D-05"],
            "C3-03": ["D-05"],
            "C5-01": ["D-01"],
            "C5-02": ["D-01", "D-02"],
            "C5-03": ["D-01.call"],
            "C5-04": ["D-04"],
            "C7-01": ["D-06"],
            "C7-02": ["D-06"],
            "C8-01": ["D-08"],
            "C8-04": ["D-07"],
            "C8-05": ["D-03", "D-04", "D-09"],
        }
        coverage = {
            "scope": "Evidence links, not an additional acceptance count; Stage C/S assertions run separately",
            "cases": [
                {
                    "scenario_id": row["scenario_id"],
                    "fixed_reference_observation": row["coverage"],
                    "shared_comparisons": [
                        comparison["scenario_id"]
                        for comparison in comparisons
                        if comparison["scenario_id"].split(".")[0] == row["scenario_id"]
                    ],
                    "deviation_comparisons": deviation_links.get(row["scenario_id"], []),
                }
                for row in sorted(results, key=lambda item: item["scenario_id"])
            ],
            "remaining_boundaries": [
                "C5-01/02/03: provider drain, inverse ordering and call leases intentionally differ from vendor",
                "C2-04 cycles and C8-05 strict readiness, errors and cancellation require Stage assertions",
                "C6-04 authz belongs to application tests; the shared case only checks resource isolation",
                "ABA, admission, noncooperative tasks and lifecycle wait cycles require S1 assertions",
                "Comparisons cover shared-cases.json operations, not every concurrency schedule",
            ],
        }
        (output / "coverage.json").write_text(json.dumps(coverage, indent=2) + "\n")
        stage_root = Path(__file__).resolve().parents[2]
        stage_patch = subprocess.check_output(["git", "diff", "HEAD"], cwd=stage_root)
        metadata = {
            "spec_id": "stage-runtime-core-v1",
            "scope": "fixed TS reference shared fragments and intentional deviation observations, not Stage acceptance",
            "checked_at": datetime.now(UTC).isoformat(),
            "reference_commit": REFERENCE_COMMIT,
            "reference_worktree_modified": False,
            "reference_repository_dirty": bool(
                subprocess.check_output(["git", "-C", str(repository), "status", "--porcelain"])
            ),
            "reference_scope_paths": list(REFERENCE_PATHS),
            "reference_scope_dirty": bool(
                subprocess.check_output(["git", "-C", str(repository), "status", "--porcelain", "--", *REFERENCE_PATHS])
            ),
            "reference_source_sha256": manifest,
            "adapter_sha256": hashlib.sha256(adapter.read_bytes()).hexdigest(),
            "shared_adapter_sha256": {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (
                    adapter.with_name("shared.mjs"),
                    adapter.with_name("shared_python.py"),
                    adapter.with_name("shared-cases.json"),
                )
            },
            "stage_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=stage_root, text=True).strip(),
            "stage_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=stage_root)),
            "stage_patch_sha256": hashlib.sha256(stage_patch).hexdigest(),
            "stage_runtime_source_sha256": {
                str(path.relative_to(stage_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted((stage_root / "app/runtime").rglob("*.py"))
            },
            "python_version": sys.version,
            "node_version": subprocess.check_output(["node", "--version"], text=True).strip(),
            "command": ["node", "--experimental-transform-types", str(adapter), "<isolated-fixed-source>"],
            "reference_observations_passed": sum(result["outcome"] == "observed" for result in results),
            "failed": sum(result["outcome"] == "failed" for result in results),
            "stage_acceptance_passed": 0,
            "shared_fragments_equal": sum(
                row["equal"] for row in comparisons if row["comparison"] == "shared-semantics"
            ),
            "shared_fragment_count": sum(row["comparison"] == "shared-semantics" for row in comparisons),
            "expected_deviation_comparisons": sum(
                row["matched"] for row in comparisons if row["comparison"] == "expected-deviation"
            ),
            "deviation_comparison_count": sum(row["comparison"] == "expected-deviation" for row in comparisons),
            "comparison_failures": sum(not row["matched"] for row in comparisons),
            "deviations_observed": sorted(
                {deviation for row in comparisons if row["matched"] for deviation in row["deviations"]}
            ),
            "exit_code": completed.returncode
            or shared.returncode
            or python.returncode
            or int(not all(row["matched"] for row in comparisons)),
            "results": results,
        }
        (output / "manifest.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")
        print(
            json.dumps(
                {
                    key: metadata[key]
                    for key in (
                        "reference_observations_passed",
                        "failed",
                        "shared_fragments_equal",
                        "shared_fragment_count",
                        "expected_deviation_comparisons",
                        "deviation_comparison_count",
                        "exit_code",
                    )
                }
            )
        )
        if metadata["exit_code"]:
            raise SystemExit(metadata["exit_code"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-repository", type=Path, default=Path("../Maestro"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.reference_repository.resolve(), args.output.resolve())
