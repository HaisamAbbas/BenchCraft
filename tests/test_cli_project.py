"""Command composition (11-T3, 11-G4): init, doctor, plugins, compare, `run DIR`, report
and the guided `benchmark` workflow, each with its documented exit codes. The project is
the packaged quickstart; its application is a real subprocess."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.quickstart import FILES

cli = CliRunner()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "quickstart"
    result = cli.invoke(app, ["init", str(root)])
    assert result.exit_code == 0, result.output
    return root


def _json(output: str) -> dict:
    return json.loads(output)


# --------------------------------------------------------------------------- init


def test_init_writes_the_quickstart_and_never_overwrites(tmp_path: Path) -> None:
    root = tmp_path / "p"
    created = cli.invoke(app, ["init", str(root), "--json"])
    assert created.exit_code == 0, created.output
    assert sorted(Path(f).name for f in _json(created.output)["files"]) == sorted(FILES)
    config = json.loads((root / "support.app.json").read_text(encoding="utf-8"))
    assert config["transport"]["argv"][0] == sys.executable  # runs without PATH lookups
    lines = (root / "dataset.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 10
    assert not (root / ".aibench").exists()  # init runs nothing and creates no workspace

    (root / "plan.json").write_text('{"mine": true}', encoding="utf-8")
    again = cli.invoke(app, ["init", str(root)])
    assert again.exit_code == 2 and "already exist" in again.output
    assert (root / "plan.json").read_text(encoding="utf-8") == '{"mine": true}'


# --------------------------------------------------------------------------- doctor


def test_doctor_passes_on_the_quickstart_without_running_it(project: Path) -> None:
    result = cli.invoke(app, ["doctor", "--project", str(project), "--json"])
    assert result.exit_code == 0, result.output
    checks = {c["name"]: c for c in _json(result.output)["checks"]}
    for name in (
        "python",
        "workspace",
        "config",
        "policy",
        "application",
        "cli executable",
        "dataset",
        "plan",
    ):
        assert checks[name]["status"] == "ok", checks[name]
    assert "10 valid case(s)" in checks["dataset"]["detail"]
    assert "2 release gate(s)" in checks["plan"]["detail"]
    assert not (project / ".aibench").exists()


def test_doctor_reports_missing_secrets_without_their_values(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = project / "support.app.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["transport"]["secret_env"] = {"SUPPORT_TOKEN": "env:QUICKSTART_TOKEN"}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.delenv("QUICKSTART_TOKEN", raising=False)
    missing = cli.invoke(app, ["doctor", "--project", str(project), "--json"])
    assert missing.exit_code == 3, missing.output
    [secret] = [c for c in _json(missing.output)["checks"] if c["name"].startswith("secret")]
    assert secret["status"] == "missing" and "QUICKSTART_TOKEN is not set" in secret["detail"]

    monkeypatch.setenv("QUICKSTART_TOKEN", "tok-very-secret-value")
    present = cli.invoke(app, ["doctor", "--project", str(project), "--json"])
    assert present.exit_code == 0, present.output
    assert "tok-very-secret-value" not in present.output
    assert "set (value not shown)" in present.output


def test_doctor_flags_invalid_project_files(project: Path) -> None:
    (project / "dataset.jsonl").write_text("{not json\n", encoding="utf-8")
    result = cli.invoke(app, ["doctor", "--project", str(project), "--json"])
    assert result.exit_code == 2, result.output
    [dataset] = [c for c in _json(result.output)["checks"] if c["name"] == "dataset"]
    assert dataset["status"] == "invalid"


# --------------------------------------------------------------------------- plugins, compare


def test_plugins_list_names_installed_kinds() -> None:
    result = cli.invoke(app, ["plugins", "list", "--json"])
    assert result.exit_code == 0, result.output
    data = _json(result.output)
    assert data["runners"] == ["cli", "http"]
    assert "native.exact_match@1.0.0" in data["evaluators"]


def test_compare_reports_that_it_is_not_available() -> None:
    result = cli.invoke(app, ["compare", "run-a", "run-b", "--json"])
    assert result.exit_code == 2
    data = _json(result.output)
    assert data["status"] == "unsupported" and "nothing was compared" in data["message"]


# --------------------------------------------------------------------------- run, report


def test_run_resolves_the_plan_from_the_project_config(project: Path) -> None:
    result = cli.invoke(app, ["run", str(project), "--workspace", str(project), "--json"])
    # the quickstart's order-status case fails in the app: incomplete (3) wins over the
    # failed correctness gate, and both are in the output
    assert result.exit_code == 3, result.output
    data = _json(result.output)
    assert data["exit_code"] == 3
    assert data["outcome"]["unhealthy_work"] == {"failed": 1}
    assert data["outcome"]["gates_failed"] == ["correct-answers"]
    gates = {g["gate_id"]: g for g in data["gates"]}
    assert gates["correct-answers"]["passes"] == 8 and gates["correct-answers"]["selected"] == 10
    assert gates["answers-present"]["status"] == "pass"

    report = cli.invoke(
        app,
        [
            "report",
            data["run_id"],
            "--workspace",
            str(project),
            "--format",
            "markdown",
            "--out",
            "-",
        ],
    )
    assert report.exit_code == 0, report.output
    assert "passes 8/10 = 80.0%" in report.output
    assert "support-004" in report.output and "support-010" in report.output
    assert "retrieved (1 of 1): We ship to over 40 countries" in report.output


def test_run_refuses_a_dataset_its_configured_plan_does_not_use(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other.jsonl"
    other.write_text((project / "dataset.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(project)
    result = cli.invoke(app, ["run", str(other)])
    assert result.exit_code == 2 and "is bound to" in result.output
    empty = tmp_path / "empty"
    empty.mkdir()
    missing = cli.invoke(app, ["run", str(empty)])
    assert missing.exit_code == 2 and "no plan to run" in missing.output
    both = cli.invoke(app, ["run", str(project), "--plan", str(project / "plan.json")])
    assert both.exit_code == 2


def test_report_command_errors_and_options(project: Path) -> None:
    no_workspace = cli.invoke(app, ["report", "run-x", "--workspace", str(project)])
    assert no_workspace.exit_code == 2 and "no aibench workspace" in no_workspace.output
    run = cli.invoke(
        app,
        [
            "run",
            "--workspace",
            str(project),
            "--plan",
            str(project / "plan.json"),
            "--policy",
            str(project / "policy.json"),
            "--json",
        ],
    )
    run_id = _json(run.output)["run_id"]
    unknown = cli.invoke(app, ["report", "run-nope", "--workspace", str(project)])
    assert unknown.exit_code == 2 and "no run committed" in unknown.output
    bad = cli.invoke(app, ["report", run_id, "--workspace", str(project), "--format", "pdf"])
    assert bad.exit_code == 2
    withheld = cli.invoke(
        app,
        [
            "report",
            run_id,
            "--workspace",
            str(project),
            "--format",
            "json",
            "--out",
            "-",
            "--no-content",
        ],
    )
    data = json.loads(withheld.output)
    assert data["evidence"]["content"] == "withheld"
    assert all(i["execution"]["output_excerpt"] is None for i in data["evidence"]["items"])


# --------------------------------------------------------------------------- benchmark


def _benchmark(project: Path, *args: str) -> tuple[int, dict]:
    result = cli.invoke(
        app,
        [
            "benchmark",
            str(project / "support.app.json"),
            "--dataset",
            str(project / "dataset.jsonl"),
            "--out",
            str(project / "bench.plan.json"),
            "--workspace",
            str(project),
            "--json",
            *args,
        ],
    )
    return result.exit_code, _json(result.output)


def test_benchmark_never_invents_objectives_or_runs_without_authorization(project: Path) -> None:
    code, data = _benchmark(project, "--non-interactive", "--policy", str(project / "policy.json"))
    assert code == 2 and data["status"] == "blocked"
    assert data["draft"]["executable"] is False
    assert data["draft"]["questions"][0]["required_fields"] == ["objectives"]
    assert not (project / ".aibench").exists()  # nothing was dispatched

    code, data = _benchmark(
        project,
        "--non-interactive",
        "--objective",
        "answers are correct",
        "--policy",
        str(project / "policy.json"),
        "--revise",
    )
    assert code == 4 and data["status"] == "authorization_required"
    assert data["draft"]["executable"] is True
    assert data["next"].startswith("aibench run --plan")
    assert not (project / ".aibench").exists()


def test_benchmark_auto_runs_only_within_an_explicit_policy(project: Path) -> None:
    code, data = _benchmark(project, "--auto", "--objective", "answers are correct")
    assert code == 2 and "--auto needs --policy" in data["problem"]
    code, data = _benchmark(
        project,
        "--auto",
        "--trust-local-app",
        "--objective",
        "answers are correct",
        "--policy",
        str(project / "policy.json"),
    )
    assert code == 2 and "trusted-local" in data["problem"]

    denying = project / "strict.policy.json"
    denying.write_text(json.dumps({"data_roots": ["."]}), encoding="utf-8")  # no trusted local
    code, data = _benchmark(
        project, "--auto", "--objective", "answers are correct", "--policy", str(denying)
    )
    assert code == 4 and data["status"] == "blocked"
    assert not (project / ".aibench").exists()

    code, data = _benchmark(
        project,
        "--auto",
        "--objective",
        "answers are correct",
        "--policy",
        str(project / "policy.json"),
        "--revise",
    )
    assert code == 3 and data["status"] == "ran" and data["exit_code"] == 3
    assert data["state"] == "completed"
    assert Path(data["reports"]["html"]).is_file() and Path(data["reports"]["json"]).is_file()
    stored = json.loads(Path(data["reports"]["json"]).read_text(encoding="utf-8"))
    assert stored["run"]["approved_by"].startswith("benchmark --auto within policy")
