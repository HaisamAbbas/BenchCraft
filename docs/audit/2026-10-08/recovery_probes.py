"""Offline recovery, denominator and CLI-inventory audit probes."""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import audit_probes as audit


def main() -> None:
    from typer.main import get_command
    from aibench.cli.main import app
    from aibench.engine.compile import compile_plan, load_policy
    from aibench.engine.engine import RunController
    from aibench.services.runs import create_run, execute_run
    from aibench.storage.artifacts import ArtifactStore
    from aibench.storage.db import Database, Workspace
    from aibench.storage.repositories import Storage

    audit.RESULTS = json.loads((audit.OUT / "probes.json").read_text(encoding="utf-8"))
    audit.SCRATCH = Path(audit.RESULTS["environment"]["scratch"])
    demo = audit.SCRATCH / "demo"
    audit.cli("invalid_utf8_config", ["doctor", "--json"], audit.SCRATCH / "bad-config")
    plan = json.loads((demo / "healthy.plan.json").read_text(encoding="utf-8"))
    plan.update(metrics=[{"metric": "native.exact_match"}], gates=[], concurrency={"application": 1, "evaluation": 1}, budgets={"max_application_calls": 1, "max_wall_seconds": 30})
    audit.write(demo / "partial.plan.json", plan)
    partial = audit.cli("run_partial_budget", ["run", "--plan", "partial.plan.json", "--policy", "policy.json", "--json"], demo)
    partial_id = partial["json"]["run_id"]
    rescored = audit.cli("rescore_partial_denominator", ["evaluate", partial_id, "--plan", "partial.plan.json", "--policy", "policy.json", "--json"], demo)
    report = audit.cli("report_partial_denominator", ["report", partial_id, "--format", "json", "--out", "-"], demo)
    summary = rescored["json"]["summaries"][0]
    pass_summary = next(p for p in report["json"]["scoring_passes"] if p["scoring_id"] == rescored["json"]["scoring_id"])["metrics"][0]["summary"]
    audit.probe("rescore_denominator_mismatch", {"original_planned": 2, "cli_selected": summary["selected"], "cli_completed_coverage": summary["completed_coverage"], "report_selected": pass_summary["selected"], "report_completed_coverage": pass_summary["completed_coverage"]})

    # Check what the actual engine sends after the dataset was frozen through storage.
    echo = demo / "echo_input.py"
    echo.write_text('import json,sys\np=json.load(sys.stdin)\nprint(json.dumps({"output":p["input"]}))\n', encoding="utf-8")
    audit.write(demo / "echo-input.app.json", {"application_id": "audit-nan", "runner": "cli", "target": "echo_input.py", "effects": "none", "transport": {"kind": "cli", "argv": [audit.sys.executable, "echo_input.py"]}})
    audit.write(demo / "nan.plan.json", {"plan_id": "audit-nan", "dataset": "nan.jsonl", "application": "echo-input.app.json", "metrics": [], "budgets": {"max_application_calls": 1, "max_wall_seconds": 30}})
    nan_run = audit.cli("nan_run_e2e", ["run", "--plan", "nan.plan.json", "--policy", "policy.json", "--json"], demo)

    mutable = audit.SCRATCH / "mutable"
    mutable.mkdir(exist_ok=True)
    code = mutable / "mutable.py"
    def app_code(version):
        return f'import json,sys,time\njson.load(sys.stdin)\ntime.sleep(.2)\nprint(json.dumps({{"output":"{version}"}}))\n'
    code.write_text(app_code("before"), encoding="utf-8")
    audit.write(mutable / "app.json", {"application_id": "audit-mutable", "runner": "cli", "target": "mutable.py", "effects": "none", "revision": "fixed-revision", "transport": {"kind": "cli", "argv": [audit.sys.executable, "mutable.py"]}})
    (mutable / "dataset.jsonl").write_text("".join(json.dumps({"case_id": f"mutable-{i}", "input": "Q"}) + "\n" for i in range(4)), encoding="utf-8")
    audit.write(mutable / "policy.json", {"allow_trusted_local": True})
    audit.write(mutable / "plan.json", {"plan_id": "audit-mutable", "dataset": "dataset.jsonl", "application": "app.json", "metrics": [], "concurrency": {"application": 1, "evaluation": 1}, "budgets": {"max_application_calls": 10, "max_wall_seconds": 120}})
    ws = Workspace.at(mutable)
    storage = Storage(Database.open_workspace(ws))
    artifacts = ArtifactStore(ws.artifacts_dir)
    try:
        compiled = compile_plan(mutable / "plan.json", policy=load_policy(mutable / "policy.json"))
        run_id = create_run(compiled, storage=storage, artifacts=artifacts, granted_by="offline audit")
        original_hash = storage.get_run(run_id).manifest.application_hash
        async def first_session():
            control = RunController()
            task = asyncio.create_task(execute_run(run_id, storage=storage, artifacts=artifacts, controller=control))
            while not storage.list_execution_attempts(run_id) and not task.done():
                await asyncio.sleep(.01)
            control.request("interrupt")
            return await task
        first = asyncio.run(first_session())
        code.write_text(app_code("after"), encoding="utf-8")
        second = asyncio.run(execute_run(run_id, storage=storage, artifacts=artifacts))
        outputs = [e.output for e in storage.list_execution_attempts(run_id)]
        audit.probe("application_code_drift_resume", {"first_state": first.state.value, "second_state": second.state.value, "outputs": outputs, "same_frozen_application_hash": original_hash == storage.get_run(run_id).manifest.application_hash, "contains_both_implementations": "before" in outputs and "after" in outputs})
    finally:
        storage.db.close()
    nan_ws = Workspace.at(demo)
    storage = Storage(Database.open_workspace(nan_ws))
    try:
        nan_output = storage.list_execution_attempts(nan_run["json"]["run_id"])[0].output
        audit.probe("nan_mutation_e2e", {"source_input_token": "NaN", "application_received_and_returned": nan_output, "exit_code": nan_run["exit_code"]})
    finally:
        storage.db.close()

    def commands(command, prefix=""):
        rows = []
        for name, child in getattr(command, "commands", {}).items():
            path = (prefix + " " + name).strip()
            rows.append({"command": path, "options": [option for param in child.params for option in getattr(param, "opts", [])]})
            rows.extend(commands(child, path))
        return rows
    files = []
    for root in (audit.REPO / "src", audit.REPO / "plugins"):
        for path in root.rglob("*.py"):
            if ".venv" in path.parts or "__pycache__" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            files.append({"file": path.relative_to(audit.REPO).as_posix(), "lines": len(source.splitlines()), "functions": sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in ast.walk(tree))})
    inventory = {"files": files, "file_count": len(files), "total_lines": sum(row["lines"] for row in files), "commands": commands(get_command(app)), "largest_files": sorted(files, key=lambda row: row["lines"], reverse=True)[:10]}
    (audit.OUT / "inventory.json").write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    audit.probe("source_inventory", {"python_files": len(files), "source_lines": inventory["total_lines"], "command_paths": len(inventory["commands"])})


if __name__ == "__main__":
    main()
