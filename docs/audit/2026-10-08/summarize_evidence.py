"""Build machine-readable audit summary after the full pytest run has finished."""
from __future__ import annotations

import csv
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).parent
EVIDENCE = ROOT / "evidence"


def test_summary(path: Path) -> dict:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failures = []
    skips = []
    for case in cases:
        node = case.get("classname", "") + "::" + case.get("name", "")
        for tag in ("failure", "error"):
            detail = case.find(tag)
            if detail is not None:
                failures.append({"test": node, "kind": tag, "message": detail.get("message", "")[:1200]})
        skipped = case.find("skipped")
        if skipped is not None:
            skips.append({"test": node, "reason": skipped.get("message", "")})
    return {"tests": len(cases), "passed": len(cases) - len(failures) - len(skips), "failed_or_error": len(failures), "skipped": len(skips), "failures": failures, "skips": skips, "seconds": sum(float(case.get("time", "0")) for case in cases)}


def dependency_summary(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    packages = []
    for package in data["dependencies"]:
        vulnerabilities = {v["id"]: v for v in package.get("vulns", [])}
        if vulnerabilities:
            packages.append({"package": package["name"], "version": package["version"], "advisories": sorted(vulnerabilities), "raw_rows": len(package["vulns"])})
    return {"affected_packages": packages, "unique_package_advisory_pairs": sum(len(p["advisories"]) for p in packages), "raw_advisory_rows": sum(p["raw_rows"] for p in packages)}


def main() -> None:
    report = (ROOT / "REPORT.md").read_text(encoding="utf-8")
    findings = []
    features = []
    for line in report.splitlines():
        if re.match(r"\| [FG]\d\d \|", line):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            entry = {"id": cells[0], "priority": cells[1], "title": cells[2], "detail": cells[3]}
            (findings if cells[0].startswith("F") else features).append(entry)
    assert len(findings) == 19 and len(features) == 22
    for name, rows in (("findings", findings), ("feature-backlog", features)):
        with (ROOT / f"{name}.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["id", "priority", "title", "detail"])
            writer.writeheader()
            writer.writerows(rows)
    coverage = json.loads((EVIDENCE / "coverage.json").read_text(encoding="utf-8"))
    low_coverage = sorted([
        {"file": path, "percent": details["summary"]["percent_covered"], "missing_lines": details["summary"]["missing_lines"]}
        for path, details in coverage["files"].items()
    ], key=lambda row: row["percent"])[:10]
    data = {
        "source_commit": "154fb45a",
        "source_version": "0.1.0rc34",
        "findings": findings,
        "feature_gaps": features,
        "full_suite": test_summary(EVIDENCE / "pytest.xml"),
        "installed_journeys": test_summary(EVIDENCE / "clean-install-junit.xml"),
        "terminal_recheck": test_summary(EVIDENCE / "pty-recheck.xml"),
        "installed_bootstrap": test_summary(EVIDENCE / "clean-bootstrap.xml"),
        "normalized_failure_recheck": test_summary(EVIDENCE / "failure-recheck.xml"),
        "workload_1000": test_summary(EVIDENCE / "workload-1000.xml"),
        "coverage_totals": coverage["totals"],
        "lowest_suite_coverage": low_coverage,
        "dependency_audits": {name: dependency_summary(EVIDENCE / filename) for name, filename in [("core_lock", "dependencies.json"), ("deepeval_environment", "deepeval-dependencies.json"), ("ragas_environment", "ragas-dependencies.json")]},
    }
    (EVIDENCE / "summary.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: data[key] for key in ("full_suite", "coverage_totals", "lowest_suite_coverage")}, indent=2))


if __name__ == "__main__":
    main()
