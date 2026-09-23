"""The quickstart as a new user follows it (11-T4, 11-G3), and conversational analysis of
its results (11-T2).

- Conversational planning uses a deterministic scripted assistant model (a fake provider:
  it proves the harness's side of the protocol, not any live model's behaviour). The
  application, engine, storage and reports are real: the quickstart's fixture app runs as
  a subprocess.
- The model-free path uses the real CLI (`aibench chat --send`) with slash commands only.
- Command/chat parity: `aibench run` and `chat --send /run` return the same exit codes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.conversation.agent import ConversationAgent, check_claims
from aibench.sessions.controller import SessionController
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from tests.session_support import ScriptedProvider, call, patch_step, say, start_step

cli = CliRunner()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "quickstart"
    assert cli.invoke(app, ["init", str(root)]).exit_code == 0
    return root


def _open(project: Path) -> SessionController:
    workspace = Workspace.at(project)
    workspace.ensure_directories()
    return SessionController.create(
        storage=Storage(Database.open_workspace(workspace)),
        artifacts=ArtifactStore(workspace.artifacts_dir),
        workspace_root=workspace.root,
        project_root=project,
        application=project / "support.app.json",
        dataset=project / "dataset.jsonl",
        policy_path=project / "policy.json",
    )


def _tool_result(messages: list[dict[str, Any]], name_hint: str) -> dict[str, Any]:
    """The latest tool result the model received (scripted steps read real data)."""
    for message in reversed(messages):
        if message.get("role") == "tool":
            data = json.loads(message["content"])
            if name_hint in data or name_hint == "":
                return data
    raise AssertionError(f"no tool result with {name_hint!r}")


def test_a_new_user_plans_runs_and_discusses_the_quickstart_in_conversation(
    project: Path,
) -> None:
    ctl = _open(project)

    def summarize(messages: list[dict[str, Any]]) -> Any:
        facts = _tool_result(messages, "metrics")
        [metric] = facts["metrics"]
        passes, selected = metric["decisions"]["pass"], metric["selected"]
        failed = facts["application"]["failed"]
        cases = ", ".join(c.split()[0] for c in facts["non_passing_cases"]["first"])
        return say(
            f"This is a final report of run {facts['run_id']}: {passes} of {selected} cases "
            f"passed ({100 * passes / selected:.0f}%), and {failed} case failed in the "
            f"application itself. Cases to look at: {cases}."
        )

    provider = ScriptedProvider(
        [
            # 1. the user states the goal in their own words; the draft changes
            patch_step("check that answers are correct", add_objectives=["answers are correct"]),
            call("show_plan"),
            say("I drafted an exact-match correctness check over the 10 cases. Run it?"),
            # 2. the user authorizes the run
            start_step("Run it"),
            say("Started the run."),
            # 3. conversational analysis from stored facts
            call("list_failures"),
            call("get_report"),
            summarize,
            # 4. one case's evidence; the explanation is labelled a hypothesis
            call("get_case_evidence", case_id="support-004"),
            say(
                "Hypothesis, based on support-004 only: the answer is the shipping passage, "
                "so retrieval likely picked the wrong passage."
            ),
            # 5. an invented statistic is flagged, never presented as verified
            call("get_report"),
            say("About 42% of all failures are retrieval problems."),
            # 6. export at the user's request
            call("export_report", user_quote="export the report as html", formats=["html"]),
            say("Exported the report."),
        ]
    )
    agent = ConversationAgent(ctl, provider)

    async def conversation() -> list[Any]:
        outcomes = [
            await agent.handle_message(
                "Benchmark this support app and check that answers are correct."
            )
        ]
        outcomes.append(await agent.handle_message("Run it"))
        run_id = outcomes[-1].actions[0]["run_id"]
        finished = await ctl.wait_for_run(run_id)
        assert finished is not None and finished.state.value == "completed"
        for message in (
            "Show me the failures.",
            "Explain case support-004.",
            "Why did the cases fail overall?",
            "Please export the report as html.",
        ):
            outcomes.append(await agent.handle_message(message))
        return outcomes

    planned, started, failures, explained, invented, exported = asyncio.run(conversation())
    try:
        assert planned.decisions and "changed the draft" in planned.status_line
        assert started.actions[0]["state"] == "done"
        run_id = started.actions[0]["run_id"]

        # every number in the summary traces to a query made in that turn
        assert failures.unverified_numbers == []
        numbers = {c["number"]: c["source"] for c in failures.claims}
        for number in ("8", "10", "80%"):
            assert numbers[number] in ("list_failures", "get_report"), numbers
        assert {r["tool"] for r in failures.results} == {"list_failures", "get_report"}
        assert "8 of 10 cases passed (80%)" in failures.text

        assert "Hypothesis" in explained.text
        assert explained.results[0]["case_id"] == "support-004"
        # the quickstart policy does not share case content with the assistant model
        seen = [
            json.loads(m["content"])
            for batch in provider.calls
            for m in batch
            if m.get("role") == "tool" and '"case_id": "support-004"' in m["content"]
        ]
        assert seen and all(e.get("case_content") == "withheld by policy" for e in seen)
        evidence = [e for e in seen if "executions" in e]  # get_case_evidence results
        assert evidence and all("output" not in x for e in evidence for x in e["executions"])

        assert invented.unverified_numbers == ["42%"]
        assert "not found in any result queried this turn: 42%" in invented.status_line

        [export] = exported.exports
        path = Path(export["paths"]["html"])
        assert path == project / ".aibench" / "reports" / run_id / "report.html"
        assert "8/10 = 80.0%" in path.read_text(encoding="utf-8")  # passes / selected
        assert "exported the report" in exported.status_line
        assert "no action taken" not in exported.status_line
    finally:
        ctl.storage.db.close()


def test_the_assistant_cannot_export_a_report_the_user_did_not_ask_for(project: Path) -> None:
    ctl = _open(project)
    provider = ScriptedProvider(
        [
            call("export_report", user_quote="what went wrong"),
            call("get_report"),
            say("Nothing exported."),
        ]
    )
    agent = ConversationAgent(ctl, provider)

    async def go() -> Any:
        from aibench.tui.commands import Commands

        ctl_commands = Commands(ctl)
        await ctl_commands.run("/plan")
        return await agent.handle_message("what went wrong")

    # no run yet: get_report errors, export is refused (the user did not ask to export)
    outcome = asyncio.run(go())
    try:
        assert outcome.exports == []
        assert outcome.rejected[0]["tool"] == "export_report"
        assert not (project / ".aibench" / "reports").exists()
    finally:
        ctl.storage.db.close()


def test_claims_are_linked_to_their_query_and_identifiers_are_not_claims() -> None:
    sources = [
        ("get_report", json.dumps({"passes": 8, "selected": 10, "rate": 0.888889})),
        ("get_case_evidence", json.dumps({"wall_ms": 1583.09})),
    ]
    claims, unverified = check_claims(
        "8/10 passed (80.0%); 88.9% of completed; support-004 took 1583.09 ms under "
        "native.exact_match@1.0.0 in run-3f2a; 12 cases were retrieval errors.",
        sources,
    )
    assert {c["number"]: c["source"] for c in claims} == {
        "8": "get_report",
        "10": "get_report",
        "80.0%": "get_report",
        "88.9%": "get_report",
        "1583.09": "get_case_evidence",
    }
    assert unverified == ["12"]


# --------------------------------------------------------------------------- model-free


def _send(project: Path, *args: str) -> Any:
    return cli.invoke(app, ["chat", "--project", str(project), *args])


def test_the_quickstart_works_in_conversation_without_a_model(project: Path) -> None:
    opened = _send(project, "--new", "--objective", "answers are correct", "--send", "/plan")
    assert opened.exit_code == 0, opened.output
    assert "ready to run" in opened.output

    ran = _send(project, "--send", "/run", "--json")
    data = json.loads(ran.stdout.strip().splitlines()[-1])
    assert ran.exit_code == 3 == data["exit_code"], ran.output  # parity with `aibench run`
    [run] = data["runs"]
    assert run["state"] == "completed" and run["outcome"]["unhealthy_work"] == {"failed": 1}

    message = _send(project, "--send", "why did support-004 fail?", "--json")
    reply = json.loads(message.stdout.strip().splitlines()[-1])
    assert message.exit_code == 0
    assert "Slash commands still work" in reply["outcome"]["text"]

    report = _send(project, "--send", "/report")
    assert report.exit_code == 0, report.output
    assert "exact_match" in report.output and "pass 8/10 (80.0%) of selected" in report.output
    html = project / ".aibench" / "reports" / run["run_id"] / "report.html"
    assert html.is_file() and (html.parent / "report.json").is_file()
    case = _send(project, "--send", "/case support-004")
    assert "We ship to over 40 countries" in case.output


def test_command_and_chat_exit_codes_agree(project: Path) -> None:
    """Headless commands and the conversation map outcomes to the same §13 codes through
    one function (`run_exit_code`): 3 for the quickstart (previous test), 0 for a clean
    run, 4 for a policy denial. A failed gate (1) is shown headless; a session's own draft
    cannot declare gates yet, so its clean run exits 0 by the same rule."""
    dataset = project / "dataset.jsonl"
    rows = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines()]
    dataset.write_text(
        "\n".join(json.dumps(r) for r in rows if r["case_id"] != "support-010") + "\n",
        encoding="utf-8",
    )  # no application failure left: only the correctness gate can fail (8/9 < 0.9)
    plan_path = project / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["metrics"] = plan["metrics"][:1]
    plan["gates"] = [{"gate_id": "correct-answers", "binding": 0, "min_pass_rate": 0.9}]
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    headless = cli.invoke(app, ["run", str(project), "--workspace", str(project), "--json"])
    assert headless.exit_code == 1, headless.output
    _send(project, "--new", "--objective", "answers are correct", "--send", "/plan")
    chat_run = _send(project, "--send", "/run", "--json")
    data = json.loads(chat_run.stdout.strip().splitlines()[-1])
    # the session draft has no gates of its own, so a finished run with a failed item is 3
    # and a clean one is 0; the draft here has no failed items and no gates
    assert chat_run.exit_code == data["exit_code"] == 0, chat_run.output

    denying = project / "deny.policy.json"
    denying.write_text(json.dumps({"data_roots": ["."]}), encoding="utf-8")
    denied_headless = cli.invoke(
        app, ["run", str(project), "--workspace", str(project), "--policy", str(denying)]
    )
    assert denied_headless.exit_code == 4
    _send(
        project,
        "--new",
        "--policy",
        str(denying),
        "--objective",
        "answers are correct",
        "--send",
        "/plan",
    )
    sessions = json.loads(
        cli.invoke(app, ["sessions", "list", "--workspace", str(project), "--json"]).output
    )
    newest = max(sessions, key=lambda s: s["updated_at"])["session_id"]
    denied_chat = _send(project, "--resume", newest, "--send", "/run", "--json")
    denied = json.loads(denied_chat.stdout.strip().splitlines()[-1])
    assert denied_chat.exit_code == 4 == denied["exit_code"], denied_chat.output
    assert denied["data"]["state"] == "denied"


def test_status_lines_label_partial_snapshots_and_unverified_numbers() -> None:
    from aibench.conversation.agent import TurnOutcome, _status_line

    outcome = TurnOutcome(turn_id="t", replies_to="u")
    outcome.results = [
        {"tool": "get_report", "run_id": "run-1", "status": "running", "provisional": True}
    ]
    outcome.unverified_numbers = ["42%"]
    line = _status_line(outcome, revision=3)
    assert "results are a partial snapshot of run run-1 (running)" in line
    assert "1 number(s) in the reply were not found in any result queried this turn: 42%" in line
    assert line.endswith("no action taken")
