"""Offline developer-smoke revision and self-evaluation audit probes."""
from __future__ import annotations

import json
from pathlib import Path

import audit_probes as audit


def main() -> None:
    audit.RESULTS = json.loads((audit.OUT / "probes.json").read_text(encoding="utf-8"))
    demo = Path(audit.RESULTS["environment"]["scratch"]) / "demo"
    spec_path = demo / "echo.app.json"
    original = spec_path.read_text(encoding="utf-8")
    spec = json.loads(original)
    spec["revision"] = "audit-revision-two"
    audit.write(spec_path, spec)
    try:
        audit.cli("smoke_changed_revision", ["app", "smoke", "echo.app.json", "--dataset", "fixture-leak.jsonl", "--trust-local-app", "--json"], demo)
    finally:
        spec_path.write_text(original, encoding="utf-8")
    planner = audit.cli("planner_fixture_benchmark", ["plan", "benchmark", "--json"])
    calibration = audit.cli("native_judge_calibration", ["evaluators", "calibrate", "--json"])
    for name, result in (("planner-fixtures", planner), ("native-calibration", calibration)):
        if "json" in result:
            (audit.OUT / f"{name}.json").write_text(json.dumps(result["json"], indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
