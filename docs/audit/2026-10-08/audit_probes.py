"""Offline audit reproductions. Run from the repository with its development Python.

Creates fresh scratch projects under .pytest-tmp; makes no external API calls.
Captures both successful workflows and defects without modifying application source.
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OUT = Path(__file__).parent / "evidence"
SCRATCH = Path(tempfile.mkdtemp(prefix="audit-2026-10-08-", dir=REPO / ".pytest-tmp"))
HOME = SCRATCH / "user-settings"
HOME.mkdir()
(HOME / "config.json").write_text('{"provider":null}\n', encoding="utf-8")
ENV = {
    **{key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "PATH"}},
    "BENCHCRAFT_HOME": str(HOME),
    "BENCHCRAFT_NO_USER_ENV": "1",
    "PYTHONUTF8": "1",
    "PYTHONPATH": str(REPO / "src"),
    "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
}
RESULTS: dict = {
    "environment": {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "scratch": str(SCRATCH),
        "packages": {name: importlib.metadata.version(name) for name in ("aibench", "pydantic", "typer", "httpx", "pytest", "ruff", "mypy")},
    },
    "commands": {},
    "probes": {},
}


def save() -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / "probes.json").write_text(json.dumps(RESULTS, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def cli(name: str, args: list[str], cwd: Path = REPO) -> dict:
    try:
        p = subprocess.run([sys.executable, "-m", "aibench", *args], cwd=cwd, env=ENV, text=True, encoding="utf-8", capture_output=True, timeout=180)
        data = {"argv": args, "cwd": str(cwd), "exit_code": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
        try:
            data["json"] = json.loads(p.stdout)
        except ValueError:
            pass
    except subprocess.TimeoutExpired:
        data = {"argv": args, "cwd": str(cwd), "timeout_seconds": 180}
    RESULTS["commands"][name] = data
    save()
    print(name, "exit", data.get("exit_code", "TIMEOUT"), flush=True)
    return data


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def probe(name: str, value: object) -> None:
    RESULTS["probes"][name] = value
    save()
    print(name, json.dumps(value, default=str)[:450], flush=True)


def main() -> None:
    from aibench import __version__
    from aibench.cli.chat import project_settings
    from aibench.config.resolve import resolve_config
    from aibench.core.models import MetricBinding, deep_unfreeze
    from aibench.evaluators.native import ExactMatch
    from aibench.registry import EvaluatorRegistry
    from aibench.reporting.statistics import ExecutionKey, JudgeObservation, summarize_judge_stability
    from aibench.services.scoring import score_recorded_run
    from aibench.storage.artifacts import ArtifactStore
    from aibench.storage.db import Database, Workspace
    from aibench.storage.repositories import Storage

    RESULTS["environment"]["source_version"] = __version__
    demo = SCRATCH / "demo"
    assert cli("init", ["init", str(demo), "--json"])["exit_code"] == 0
    cli("doctor", ["doctor", "--json"], demo)
    cli("dataset_validate", ["dataset", "validate", "dataset.jsonl", "--json"], demo)
    cli("plan_validate", ["plan", "validate", "plan.json", "--policy", "policy.json", "--json"], demo)
    original = cli("run_demo", ["run", "--json"], demo)
    run_id = original["json"]["run_id"]
    for fmt in ("json", "markdown", "html"):
        cli("report_" + fmt, ["report", run_id, "--format", fmt, "--out", str(SCRATCH / ("report." + fmt)), "--json"], demo)
    cli("runs_status", ["runs", "status", run_id, "--json"], demo)
    cli("evaluate_incomplete", ["evaluate", run_id, "--plan", "plan.json", "--policy", "policy.json", "--json"], demo)
    cli("policy_denied", ["run", "--plan", str(demo / "plan.json"), "--workspace", str(SCRATCH / "denied"), "--json"])

    plan = json.loads((demo / "plan.json").read_text(encoding="utf-8"))
    plan["selection"] = {"limit": 2}
    write(demo / "healthy.plan.json", plan)
    healthy = cli("run_healthy", ["run", "--plan", "healthy.plan.json", "--policy", "policy.json", "--json"], demo)
    healthy_id = healthy["json"]["run_id"]
    cli("compare_self", ["compare", healthy_id, healthy_id, "--json", "--bootstrap-replicates", "100"], demo)
    budget_plan = {**plan, "metrics": [{"metric": "native.exact_match"}], "gates": [], "budgets": {"max_evaluator_calls": 1}}
    write(demo / "budget.plan.json", budget_plan)
    policy = json.loads((demo / "policy.json").read_text(encoding="utf-8"))
    policy["ceilings"] = {"max_evaluator_calls": 1}
    write(demo / "budget.policy.json", policy)
    cli("rescore_budget_one", ["evaluate", healthy_id, "--plan", "budget.plan.json", "--policy", "budget.policy.json", "--json"], demo)

    custom = demo / "failing_evaluator.py"
    custom.write_text('''from aibench.core.models import EvaluatorManifest
from aibench.evaluators.protocol import Evaluator, EvaluationOutcome
class Fails(Evaluator):
    manifest = EvaluatorManifest(evaluator_id="audit.failure", version="1.0.0", plugin_id="audit", plugin_version="1.0.0", description="Offline failure reproduction", value_kind="boolean", direction="higher", scope="case", aggregation="rate")
    async def evaluate(self, view, ctx):
        return EvaluationOutcome.error("audit: deterministic evaluator failure")
EVALUATORS = (Fails,)
''', encoding="utf-8")
    write(demo / "failure.metrics.json", {"metrics": [{"metric": "audit.failure"}]})
    cli("score_evaluator_errors", ["score", healthy_id, "--metrics", "failure.metrics.json", "--custom-evaluator", str(custom), "--trust-local-code", "--json"], demo)
    cli("report_after_failed_rescore", ["report", healthy_id, "--format", "json", "--out", "-"], demo)

    nested = SCRATCH / "nested"
    nested.mkdir()
    write(nested / "aibench.json", {"project_root": "subproject", "dataset_path": "dataset.jsonl", "application_target": "support.app.json", "plan_path": "plan.json", "policy_path": "policy.json"})
    cli("init_nested", ["init", str(nested / "subproject"), "--json"])
    expected = resolve_config(config_path=nested / "aibench.json").root
    settings = project_settings(nested, None, None, None)
    probe("project_root_ignored", {"resolved_root": str(expected), "actual_paths": settings, "expected_dataset": str(expected / "dataset.jsonl")})
    cli("doctor_nested_root", ["doctor", "--json"], nested)

    for name, text in (("compare_quote", '/compare "'), ("cases_quote", '/cases add "')):
        cli(name, ["chat", "--new", "--send", text, "--json"], demo)
    provider = demo / "offline.provider.json"
    write(provider, {"base_url": "http://127.0.0.1:9/v1", "model": "audit-offline", "timeout_seconds": 1})
    (demo / "source.md").write_text("The refund period is 30 days.", encoding="utf-8")
    cli("headless_cases_with_provider", ["chat", "--new", "--provider-config", str(provider), "--send", "/cases generate source.md", "--json"], demo)

    key = ExecutionKey("audit-execution", 0)
    stability = summarize_judge_stability([JudgeObservation(key, "pass-1", "ok", 0.8, "pass")], expected_units=[key], expected_repeats=5)
    probe("missing_judge_repeats", {"expected_missing": 4, "reported_missing": stability["denominators"]["missing_repeat_count"], "unit": stability["units"][0]})

    # Plugin implementation identity changed, while semantic metric version/params stayed fixed.
    class ChangedExact(ExactMatch):
        manifest = ExactMatch.manifest.model_copy(update={"plugin_version": "999.0.0"})
        calls = 0
        async def evaluate(self, view, ctx):
            type(self).calls += 1
            return await super().evaluate(view, ctx)
    registry = EvaluatorRegistry()
    registry._add(ChangedExact, allow_native=True)
    ws = Workspace.at(demo)
    storage = Storage(Database.open_workspace(ws))
    try:
        carried = asyncio.run(score_recorded_run(storage=storage, artifacts=ArtifactStore(ws.artifacts_dir), registry=registry, run_id=healthy_id, bindings=[MetricBinding(metric="native.exact_match")], carry_forward=True))
        probe("carry_forward_changed_plugin", {"current_plugin_version": ChangedExact.manifest.plugin_version, "new_evaluator_calls": ChangedExact.calls, "carried": carried.carried, "result_provenance": deep_unfreeze(carried.results[0].provenance)})
    finally:
        storage.db.close()

    bad_json = SCRATCH / "bad-config"
    bad_json.mkdir()
    (bad_json / "aibench.json").write_bytes(b"\xff\xfe\xff")
    cli("invalid_utf8_config", ["doctor", "--json"], bad_json)
    cli("dataset_missing_json", ["dataset", "validate", str(SCRATCH / "missing.jsonl"), "--json"])
    cli("run_missing_json", ["run", "--plan", str(SCRATCH / "missing.plan.json"), "--json"])
    probe("e2e_paths", {"demo_run": run_id, "healthy_run": healthy_id, "scratch": str(SCRATCH)})


if __name__ == "__main__":
    try:
        main()
    finally:
        save()
