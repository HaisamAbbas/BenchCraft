"""Offline comparison-selection denominator reproduction."""
from __future__ import annotations

import json
from pathlib import Path

import audit_probes as audit


def main() -> None:
    from aibench.storage.db import Database, Workspace
    from aibench.storage.repositories import Storage
    audit.RESULTS = json.loads((audit.OUT / "probes.json").read_text(encoding="utf-8"))
    demo = Path(audit.RESULTS["environment"]["scratch"]) / "demo"
    baseline_id = audit.RESULTS["commands"]["run_healthy"]["json"]["run_id"]
    current = audit.cli("run_subset_current", ["run", "--plan", "healthy.plan.json", "--policy", "policy.json", "--json"], demo)
    current_id = current["json"]["run_id"]
    compared = audit.cli("compare_subset_distinct", ["compare", baseline_id, current_id, "--json", "--bootstrap-replicates", "100"], demo)
    storage = Storage(Database.open_readonly(Workspace.at(demo).db_path))
    try:
        record = storage.get_run(current_id)
        items = storage.list_work_items(current_id)
        planned = sum(item.kind == "execution" for item in items)
        cached_cases = len(storage.list_cases(record.manifest.dataset_hash))
        gates = [{"metric": item["metric_id"], "gate": item["coverage_gate"]} for item in compared["json"]["metrics"]]
        audit.probe("comparison_selection_denominator", {"baseline_id": baseline_id, "current_id": current_id, "current_planned_execution_items": planned, "current_successful_execution_items": current["json"]["counts"]["execution"]["succeeded"], "dataset_cases_in_workspace": cached_cases, "work_keys": [item.task_key for item in items], "binding_hashes": list(record.manifest.parameters["binding_hashes"]), "metric_gates": gates, "comparison_exit": compared["exit_code"]})
    finally:
        storage.db.close()


if __name__ == "__main__":
    main()
