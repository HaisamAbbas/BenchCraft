"""F03: fixture exposure is a validated boolean capability, not truthiness."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.errors import ValidationError
from aibench.core.models import BenchmarkCase, Fixture
from aibench.datasets.normalize import normalize_case
from tests.engine_support import Harness

INVALID_FLAGS = ("false", "False", "0", "true", "yes", 0, 1, None, [], {}, [True])
PRIVATE = "AUDIT_F03_JUDGE_ONLY_SENTINEL"
cli = CliRunner()


@pytest.mark.parametrize("flag", INVALID_FLAGS)
def test_fixture_visibility_rejects_non_booleans_in_models_and_normalization(flag: Any) -> None:
    with pytest.raises(PydanticValidationError):
        Fixture(name="private", content=PRIVATE, app_visible=flag)
    with pytest.raises(ValidationError, match=r"line 7") as error:
        normalize_case(
            {"input": "question", "fixtures": [{"name": "private", "app_visible": flag}]},
            line=7,
            occurrence_index=1,
        )
    assert error.value.line == 7
    assert error.value.field == "fixtures[0]"


def test_projection_fails_closed_for_an_unvalidated_truthy_visibility_flag() -> None:
    private = Fixture(name="private", content=PRIVATE).model_copy(update={"app_visible": "false"})
    case = BenchmarkCase(case_id="private", input="question", fixtures=(private,))
    assert case.application_input_projection()["fixtures"] == {}


@pytest.mark.parametrize("flag", ("false", 1, None))
def test_invalid_visibility_fails_cli_validation_and_run_before_dispatch(
    tmp_path: Path, flag: Any
) -> None:
    h = Harness(tmp_path)
    dataset = tmp_path / "visibility.jsonl"
    dataset.write_text(
        json.dumps({"input": "question", "fixtures": [{"name": "private", "app_visible": flag}]})
        + "\n",
        encoding="utf-8",
    )
    checked = cli.invoke(app, ["dataset", "validate", str(dataset), "--json"])
    assert checked.exit_code == 2, checked.output
    assert json.loads(checked.stdout)["errors"][0]["line"] == 1
    plan = h.plan(dataset=dataset.name, application=h.cli_app())
    executed = cli.invoke(
        app,
        ["run", "--plan", str(plan), "--trust-local-app", "--workspace", str(tmp_path / "run")],
    )
    assert executed.exit_code == 2, executed.output
    assert not h.log.exists()
    assert not (tmp_path / "run" / ".aibench").exists()


def test_real_application_receives_only_explicitly_visible_fixture_content(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    (tmp_path / "app.py").write_text(
        "import json, os, pathlib, sys\n"
        "request = json.load(sys.stdin)\n"
        "pathlib.Path(os.environ['APP_LOG']).write_text(json.dumps(request))\n"
        "print(json.dumps({'output': request['fixtures']}))\n",
        encoding="utf-8",
    )
    dataset = tmp_path / "visibility.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "case_id": "visible-boundary",
                "input": "question",
                "expected_output": PRIVATE,
                "fixtures": [
                    {"name": "default_hidden", "content": PRIVATE},
                    {"name": "explicit_hidden", "content": PRIVATE, "app_visible": False},
                    {"name": "visible", "content": "public", "app_visible": True},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    plan = h.plan(
        dataset=dataset.name,
        application=h.cli_app(),
        metrics=[{"metric": "native.json_schema", "params": {"schema": {"type": "object"}}}],
    )
    executed = cli.invoke(
        app,
        [
            "run",
            "--plan",
            str(plan),
            "--trust-local-app",
            "--workspace",
            str(tmp_path / "run"),
            "--json",
        ],
    )
    assert executed.exit_code == 0, executed.output
    request = h.log.read_text(encoding="utf-8")
    assert json.loads(request)["fixtures"] == {"visible": "public"}
    assert PRIVATE not in request
