"""Advanced multi-turn text application evaluation (18-T3)."""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.errors import ValidationError
from aibench.datasets.episodes import validate_episode_manifest
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from tests.runner_support import REPO_ROOT, load_example

cli = CliRunner()
EXAMPLE = REPO_ROOT / "examples" / "multi_turn_text"


def _copied_example(tmp_path: Path, server: Any) -> Path:
    project = tmp_path / "multi_turn_text"
    shutil.copytree(EXAMPLE, project)
    base = f"http://127.0.0.1:{server.server_port}"
    app_config = project / "support.app.json"
    app_config.write_text(
        app_config.read_text(encoding="utf-8").replace("http://127.0.0.1:8769", base),
        encoding="utf-8",
    )
    return project


def test_episode_schema_validates_contiguous_turns_and_declared_reset_world(tmp_path: Path) -> None:
    server = load_example("multi_turn_support").make_server(port=0)
    try:
        project = _copied_example(tmp_path, server)
        manifest, cases = validate_episode_manifest(
            project / "cases.jsonl", project / "episodes.json", project / "plan.json"
        )
        assert len(manifest.episodes) == 2
        assert manifest.episodes[0].simulator.identity.endswith("refund-v1")
        assert len(cases) == 4

        plan_path = project / "plan.json"
        plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
        plan_data["metrics"] = []
        plan_path.write_text(json.dumps(plan_data), encoding="utf-8")
        try:
            validate_episode_manifest(project / "cases.jsonl", project / "episodes.json", plan_path)
        except ValidationError as exc:
            assert "must bind native.final_state" in str(exc)
        else:
            raise AssertionError(
                "episode validation must reject a plan without final-state scoring"
            )
        plan_data["metrics"] = [{"metric": "native.final_state"}]
        plan_path.write_text(json.dumps(plan_data), encoding="utf-8")

        raw = json.loads((project / "episodes.json").read_text(encoding="utf-8"))
        raw["episodes"][0]["case_ids"] = ["refund-turn-2", "refund-turn-1"]
        broken = project / "broken-episodes.json"
        broken.write_text(json.dumps(raw), encoding="utf-8")
        try:
            validate_episode_manifest(project / "cases.jsonl", broken, project / "plan.json")
        except ValidationError as exc:
            assert "contiguous and listed in dataset order" in str(exc)
        else:
            raise AssertionError("reversed episode turns should fail validation")
    finally:
        server.server_close()


def test_multi_turn_text_run_resets_each_episode_and_checks_independent_state(
    tmp_path: Path,
) -> None:
    server = load_example("multi_turn_support").make_server(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        project = _copied_example(tmp_path, server)
        validation = cli.invoke(
            app,
            [
                "dataset",
                "episodes",
                "validate",
                str(project / "cases.jsonl"),
                str(project / "episodes.json"),
                "--plan",
                str(project / "plan.json"),
                "--json",
            ],
        )
        assert validation.exit_code == 0, validation.output
        assert (
            json.loads(validation.stdout)["independent_success_evaluator"] == "native.final_state"
        )

        result = cli.invoke(
            app,
            [
                "run",
                "--plan",
                str(project / "plan.json"),
                "--policy",
                str(project / "policy.json"),
                "--workspace",
                str(project),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        run_id = json.loads(result.stdout)["run_id"]
        storage = Storage(Database.open_workspace(Workspace.at(project)))
        try:
            executions = storage.list_execution_attempts(run_id)
            states = {item.case_id: item.world_state for item in executions}
            assert states["refund-turn-2"] == {
                "active_order": "A17",
                "refund_eligible": True,
                "exchange_denied": False,
                "turn_count": 2,
            }
            assert states["exchange-turn-1"]["turn_count"] == 1
            assert states["exchange-turn-1"]["active_order"] == "B55"
            assert states["exchange-turn-1"]["refund_eligible"] is False
            assert states["exchange-turn-2"]["exchange_denied"] is True
            assert states["exchange-turn-2"]["turn_count"] == 2
            outcomes = storage.list_metric_results(run_id)
            final_state = {item.case_id: item for item in outcomes}
            assert len(final_state) == 4
            assert all(item.value.value is True for item in final_state.values())
            resets = [
                event["payload"]
                for event in storage.list_run_events(run_id)
                if event["event_type"] == "app_reset"
            ]
            assert len(resets) == 2
            assert {item["episode"] for item in resets} == {"refund-episode", "exchange-episode"}
        finally:
            storage.db.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
