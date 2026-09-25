from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from aibench.conversation.agent import ConversationAgent
from aibench.core.models import ExperimentEvent, ExperimentEventKind, ExperimentStatus, utcnow
from aibench.experiments.service import create_experiment, prepare_experiment
from aibench.sessions.controller import SessionController
from tests.session_support import ScriptedProvider, SessionHarness, call, say

EXAMPLE = Path(__file__).parents[1] / "examples" / "experiments" / "known_objective"


def _session(tmp_path: Path, *, slow: bool = True) -> tuple[SessionHarness, SessionController]:
    harness = SessionHarness(tmp_path)
    project = harness.root / "known-objective"
    shutil.copytree(EXAMPLE, project)
    if slow:
        # Make background progress observable while keeping the fixture deterministic.
        app_file = project / "app.py"
        source = app_file.read_text(encoding="utf-8")
        app_file.write_text(
            source.replace("import os\n", "import os\nimport time\n").replace(
                "def respond(payload: dict[str, Any]) -> dict[str, str]:\n",
                "def respond(payload: dict[str, Any]) -> dict[str, str]:\n    time.sleep(0.08)\n",
            ),
            encoding="utf-8",
        )
    storage, artifacts = harness.storage()
    controller = SessionController.create(
        storage=storage,
        artifacts=artifacts,
        workspace_root=harness.workspace.root,
        project_root=project,
        application=project / "application.json",
        dataset=project / "development.jsonl",
        objectives=("correctness",),
        trusted_local=True,
    )
    assert controller.current_decision().executable
    return harness, controller


def _last_experiment_id(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "tool":
            result = json.loads(message["content"])
            if isinstance(result, dict) and result.get("experiment_id"):
                return str(result["experiment_id"])
    raise AssertionError("no experiment ID was returned to the model")


def test_conversation_runs_experiment_reports_progress_and_separately_evaluates_holdout(
    tmp_path: Path,
) -> None:
    harness, controller = _session(tmp_path)
    quote = "Run an experiment comparing answer mode baseline and accurate using holdout.jsonl."
    intended = "Compare the accurate fixed-answer mode with the baseline mode."
    provider = ScriptedProvider(
        [
            call(
                "run_controlled_experiment",
                user_quote=quote,
                holdout_dataset="holdout.jsonl",
                intended_change=intended,
                parameters=[{"name": "answer_mode", "values": ["baseline", "accurate"]}],
            ),
            lambda messages: call(
                "get_experiment_report", experiment_id=_last_experiment_id(messages)
            ),
            say("The finite experiment has started; I can check its stored progress."),
        ]
    )
    async def scenario() -> None:
        outcome = await ConversationAgent(controller, provider).handle_message(f"{quote} {intended}")
        assert not outcome.rejected
        assert outcome.experiment_actions[0]["kind"] == "run"
        assert "started controlled experiment" in outcome.status_line
        experiment_id = str(outcome.experiment_actions[0]["experiment_id"])
        progress = next(item for item in outcome.results if item["tool"] == "get_experiment_report")
        assert progress["status"] in {"ready", "running", "selected"}

        record = await controller.wait_for_experiment(experiment_id)
        assert record is not None and record.status is ExperimentStatus.SELECTED
        assert record.selected_trial_id is not None

        holdout_quote = f"Evaluate the protected holdout for experiment {experiment_id}."
        holdout_provider = ScriptedProvider(
            [
                call(
                    "evaluate_experiment_holdout",
                    user_quote=holdout_quote,
                    experiment_id=experiment_id,
                ),
                say("The protected evaluation is running separately from development selection."),
            ]
        )
        holdout_outcome = await ConversationAgent(controller, holdout_provider).handle_message(
            holdout_quote
        )
        assert not holdout_outcome.rejected
        assert holdout_outcome.experiment_actions[0]["kind"] == "holdout"
        completed = await controller.wait_for_experiment(experiment_id)
        assert completed is not None and completed.status is ExperimentStatus.COMPLETED
        report_provider = ScriptedProvider(
            [
                call("get_experiment_report", experiment_id=experiment_id),
                say("The report keeps development selection and protected holdout results separate."),
            ]
        )
        report = await ConversationAgent(controller, report_provider).handle_message(
            "Show the final experiment report."
        )
        final = next(item for item in report.results if item["tool"] == "get_experiment_report")
        assert final["status"] == "completed"
        run_records = controller.storage.list_runs()
        assert len(run_records) == 4  # two development variants plus paired holdout variants
        assert all(run.status == "completed" for run in run_records)
        assert len(harness.runs()) == 4

    asyncio.run(scenario())
    controller.storage.db.close()


def test_conversational_experiment_rejects_question_and_ungrounded_values(tmp_path: Path) -> None:
    _harness, controller = _session(tmp_path)
    bad_calls = [
        (
            "Should I run an experiment using holdout.jsonl?",
            "Should I run an experiment",
            ["baseline", "accurate"],
        ),
        (
            "Run an experiment with answer mode baseline using holdout.jsonl. Compare the accurate fixed-answer mode with the baseline mode.",
            "Run an experiment with answer mode baseline using holdout.jsonl.",
            ["baseline", "imaginary"],
        ),
    ]
    for index, (message, quote, values) in enumerate(bad_calls):
        provider = ScriptedProvider(
            [
                call(
                    "run_controlled_experiment",
                    user_quote=quote,
                    holdout_dataset="holdout.jsonl",
                    intended_change="Compare the accurate fixed-answer mode with the baseline mode.",
                    parameters=[{"name": "answer_mode", "values": values}],
                ),
                say("No experiment was started."),
            ]
        )
        outcome = asyncio.run(
            ConversationAgent(controller, provider).handle_message(message, message_id=f"bad-{index}")
        )
        assert outcome.rejected
        assert outcome.experiment_actions == []
    assert controller.session_experiments() == []
    assert controller.storage.list_runs() == []
    controller.storage.db.close()


def test_conversation_resumes_a_stored_running_experiment(tmp_path: Path) -> None:
    harness, controller = _session(tmp_path, slow=False)
    spec_path = controller.project_root / "experiment.json"
    definition = json.loads(spec_path.read_text(encoding="utf-8"))
    experiment_id = f"{controller.session_id}-exp-recovery"
    definition["experiment_id"] = experiment_id
    definition["budget"]["max_trials"] = 2
    spec_path.write_text(json.dumps(definition), encoding="utf-8")
    prepared = prepare_experiment(
        spec_path,
        policy=controller.policy().with_trusted_local(controller.session.trusted_local),
        trusted_local=controller.session.trusted_local,
    )
    record = create_experiment(
        prepared,
        storage=controller.storage,
        artifacts=controller.artifacts,
        actor="test-recovery-fixture",
    )
    original_trial_ids = [
        trial.trial_id for trial in controller.storage.list_experiment_trials(experiment_id)
    ]
    # Model a process restart after the durable start event but before the first pending
    # trial is dispatched. The normal executor must reuse its stored trial identities.
    running = record.model_copy(update={"status": ExperimentStatus.RUNNING, "updated_at": utcnow()})
    controller.storage.transition_experiment(
        running,
        from_status=ExperimentStatus.READY,
        event=ExperimentEvent(
            event_id=f"{experiment_id}:recovery-started",
            experiment_id=experiment_id,
            kind=ExperimentEventKind.STARTED,
            actor="test-recovery-fixture",
            details={"trial_limit": record.trial_limit},
        ),
    )

    quote = f"Continue experiment {experiment_id}."
    provider = ScriptedProvider(
        [
            call(
                "resume_controlled_experiment",
                user_quote=quote,
                experiment_id=experiment_id,
            ),
            say("I resumed the stored experiment and kept its existing trial identities."),
        ]
    )

    async def scenario() -> None:
        outcome = await ConversationAgent(controller, provider).handle_message(quote)
        assert not outcome.rejected
        assert outcome.experiment_actions == [
            {"kind": "resume", "experiment_id": experiment_id, "status": "running"}
        ]
        assert outcome.results[0]["resume"] is True
        resumed = await controller.wait_for_experiment(experiment_id)
        assert resumed is not None and resumed.status is ExperimentStatus.SELECTED
        resumed_trials = controller.storage.list_experiment_trials(experiment_id)
        assert [trial.trial_id for trial in resumed_trials] == original_trial_ids

    asyncio.run(scenario())
    assert len(harness.runs()) == 2
    controller.storage.db.close()


def test_conversational_experiment_holdout_must_be_inside_configured_data_roots(
    tmp_path: Path,
) -> None:
    harness, controller = _session(tmp_path)
    outside = tmp_path / "private-holdout.jsonl"
    shutil.copyfile(EXAMPLE / "holdout.jsonl", outside)
    policy_path = harness.root / "policy.json"
    policy_path.write_text(json.dumps({"data_roots": [str(controller.project_root / "data")] }))
    controller.store.update_session(controller.session_id, policy_path=str(policy_path))
    quote = (
        "Run an experiment comparing answer mode baseline and accurate using "
        f"{outside.as_posix()}."
    )
    intended = "Compare the accurate fixed-answer mode with the baseline mode."
    provider = ScriptedProvider(
        [
            call(
                "run_controlled_experiment",
                user_quote=quote,
                    holdout_dataset=outside.as_posix(),
                intended_change=intended,
                parameters=[{"name": "answer_mode", "values": ["baseline", "accurate"]}],
            ),
            say("The configured data policy blocked this holdout."),
        ]
    )
    outcome = asyncio.run(ConversationAgent(controller, provider).handle_message(f"{quote} {intended}"))
    assert outcome.rejected
    assert "outside the project's approved data scope" in outcome.rejected[0]["problems"][0]
    assert controller.session_experiments() == []
    assert controller.storage.list_runs() == []
    controller.storage.db.close()


def test_noninteractive_chat_waits_for_a_controlled_experiment_it_started(
    tmp_path: Path, capsys: Any
) -> None:
    from aibench.cli.chat import _send

    _harness, controller = _session(tmp_path, slow=False)
    quote = "Run an experiment comparing answer mode baseline and accurate using holdout.jsonl."
    intended = "Compare the accurate fixed-answer mode with the baseline mode."
    provider = ScriptedProvider(
        [
            call(
                "run_controlled_experiment",
                user_quote=quote,
                holdout_dataset="holdout.jsonl",
                intended_change=intended,
                parameters=[{"name": "answer_mode", "values": ["baseline", "accurate"]}],
            ),
            say("The controlled experiment has started."),
        ]
    )

    async def run_once() -> int:
        return await _send(
            controller,
            provider,
            f"{quote} {intended}",
            json_output=True,
            new_session=lambda: controller,
        )

    exit_code = asyncio.run(run_once())
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["experiments"][0]["status"] == "selected"
    assert controller.session_experiments()[0].status is ExperimentStatus.SELECTED
    controller.storage.db.close()
