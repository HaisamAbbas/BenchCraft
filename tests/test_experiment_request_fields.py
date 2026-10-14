"""An experiment varies settings of an HTTP application. Experiments could vary only the
environment variables of an application BenchCraft starts; LightRAG, connected over HTTP,
takes its retrieval `mode` and `top_k` in each request, so they could not be compared
without editing the connection between runs. An exposed parameter can now name a field of
the request template (`input_field`), sent as its declared JSON type."""

from __future__ import annotations

import asyncio
import json
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from aibench.core.models import ApplicationSpec, ExperimentStatus
from aibench.experiments.service import (
    create_experiment,
    evaluate_protected_holdout,
    execute_experiment,
    experiment_report,
    prepare_experiment,
)
from aibench.security.policy import ExecutionPolicy
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from tests.runner_support import serving

EXAMPLE = Path(__file__).parents[1] / "examples" / "experiments" / "known_objective"


class _RetrievalApp(ThreadingHTTPServer):
    """Answers correctly only with mode "mix", and records every request it is sent."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests: list[dict[str, Any]] = []


class _Handler(BaseHTTPRequestHandler):
    server: _RetrievalApp

    def log_message(self, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        question = str(body.get("query", ""))
        answer = question.removeprefix("answer ") if body.get("mode") == "mix" else "unknown"
        data = json.dumps({"response": answer}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _application(url: str) -> dict[str, Any]:
    return {
        "application_id": "retrieval-http",
        "runner": "http",
        "target": url,
        "revision": "retrieval-http-v1",
        "effects": "none",
        "transport": {"kind": "http", "url": url},
        "input_binding": {
            "template": {"mode": "hybrid", "top_k": 40},
            "fields": {"/query": "/input"},
        },
        "output_binding": {"output": "/response"},
        "exposed_parameters": [
            {
                "name": "mode",
                "input_field": "/mode",
                "default_value": "hybrid",
                "allowed_values": ["hybrid", "mix"],
                "description": "LightRAG's retrieval mode",
            },
            {
                "name": "top_k",
                "input_field": "/top_k",
                "value_type": "integer",
                "default_value": "40",
                "allowed_values": ["40", "60"],
                "description": "Passages retrieved per question",
            },
        ],
    }


def _project(tmp_path: Path, url: str) -> Path:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLE, project)
    (project / "app.py").unlink()
    (project / "application.json").write_text(json.dumps(_application(url)), encoding="utf-8")
    experiment = json.loads((project / "experiment.json").read_text(encoding="utf-8"))
    experiment["experiment_id"] = "retrieval-mode"
    experiment["intended_change"] = "Compare LightRAG retrieval modes."
    experiment["parameters"] = [
        {"name": "mode", "values": ["hybrid", "mix"]},
        {"name": "top_k", "values": ["40", "60"]},
    ]
    experiment["budget"]["max_trials"] = 4
    (project / "experiment.json").write_text(json.dumps(experiment), encoding="utf-8")
    return project


def test_an_experiment_varies_a_request_field_of_an_http_application(tmp_path: Path) -> None:
    app = _RetrievalApp()
    with serving(app) as base:
        project = _project(tmp_path, f"{base}/query")
        workspace = Workspace.at(tmp_path)
        database = Database.open_workspace(workspace)
        storage, artifacts = Storage(database), ArtifactStore(workspace.artifacts_dir)
        try:
            prepared = prepare_experiment(
                project / "experiment.json", policy=ExecutionPolicy(), trusted_local=True
            )
            record = create_experiment(prepared, storage=storage, artifacts=artifacts)
            selected = asyncio.run(
                execute_experiment(record.experiment_id, storage=storage, artifacts=artifacts)
            )
            trials = storage.list_experiment_trials(record.experiment_id)
            assert selected.status is ExperimentStatus.SELECTED, [
                (t.trial_id, t.status.value, t.failure) for t in trials
            ]
            assert dict(trials[0].parameters) == {"mode": "hybrid", "top_k": "40"}  # default first
            assert sorted(tuple(sorted(dict(t.parameters).items())) for t in trials) == sorted(
                (("mode", mode), ("top_k", top_k))
                for mode in ("hybrid", "mix")
                for top_k in ("40", "60")
            )
            winner = storage.get_experiment_trial(selected.selected_trial_id or "")
            assert winner is not None and dict(winner.parameters)["mode"] == "mix"

            # Each trial's requests carried its own value, and top_k as a JSON number.
            modes = {request["mode"] for request in app.requests}
            assert modes == {"hybrid", "mix"}
            assert {type(request["top_k"]) for request in app.requests} == {int}
            assert {request["top_k"] for request in app.requests} == {40, 60}

            done = asyncio.run(
                evaluate_protected_holdout(
                    record.experiment_id, storage=storage, artifacts=artifacts
                )
            )
            assert done.status is ExperimentStatus.COMPLETED
            report = experiment_report(record.experiment_id, storage=storage, artifacts=artifacts)
            assert report["protected_holdout_evaluation"]["comparison"]["claim_qualified"] is True
        finally:
            database.close()


def _spec(**changes: Any) -> dict[str, Any]:
    raw = _application("http://127.0.0.1:9/query")
    raw.update(changes)
    return raw


def test_a_request_field_parameter_is_checked_against_the_template() -> None:
    ApplicationSpec.model_validate(_spec())  # the declaration above is valid

    wrong_default = _spec(
        input_binding={"template": {"mode": "mix", "top_k": 40}, "fields": {"/query": "/input"}}
    )
    with pytest.raises(ValidationError, match="must match its input template default_value"):
        ApplicationSpec.model_validate(wrong_default)

    # "40" in the template is a string; the parameter says the request carries a number.
    as_text = _spec(
        input_binding={
            "template": {"mode": "hybrid", "top_k": "40"},
            "fields": {"/query": "/input"},
        }
    )
    with pytest.raises(ValidationError, match="must match its input template default_value"):
        ApplicationSpec.model_validate(as_text)

    raw = _spec()
    raw["exposed_parameters"][0]["input_field"] = "/query"  # written from the case input
    raw["input_binding"]["template"]["query"] = "hybrid"
    with pytest.raises(ValidationError, match="written from the case input"):
        ApplicationSpec.model_validate(raw)

    raw = _spec()
    raw["exposed_parameters"][1]["allowed_values"] = ["40", "many"]
    with pytest.raises(ValidationError, match="not a valid integer"):
        ApplicationSpec.model_validate(raw)

    raw = _spec()
    raw["exposed_parameters"][0]["environment_key"] = "AIBENCH_TUNABLE_MODE"
    with pytest.raises(ValidationError, match="exactly one of environment_key and input_field"):
        ApplicationSpec.model_validate(raw)


def test_an_environment_parameter_serializes_as_before() -> None:
    """Request fields add no keys to an environment parameter, so an application's identity,
    and an experiment created against it before this release, are unchanged."""
    raw = json.loads((EXAMPLE / "application.json").read_text(encoding="utf-8"))
    spec = ApplicationSpec.model_validate(raw)
    [exposed] = spec.model_dump(mode="json")["exposed_parameters"]
    assert set(exposed) == {
        "name",
        "environment_key",
        "default_value",
        "allowed_values",
        "description",
    }
