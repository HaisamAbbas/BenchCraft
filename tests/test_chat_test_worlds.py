"""Chat integration for runners and test worlds (15-T3).

The conversation explains what the application's runner can observe, what evidence is
missing and how its state is reset, and lets the user choose an approved test world through
a validated plan change: with `/app` and `/world` through `aibench chat --send` (no model),
and with the assistant's `describe_application` and `propose_plan_patch` tools (a scripted
model driving the real agent). A chosen world reaches a run only through the execution
gate, like a hand-written plan."""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.conversation.agent import ConversationAgent
from aibench.sessions.controller import SessionController
from tests.session_support import ScriptedProvider, call, patch_step, say

REPO = Path(__file__).resolve().parents[1]
OBJECTIVE = "check the final world state"
cli = CliRunner()


@pytest.fixture
def project(tmp_path: Path) -> Iterator[tuple[Path, Any]]:
    from tests.runner_support import load_example

    server = load_example("booking_world").make_server(port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = tmp_path / "agent_world"
    shutil.copytree(REPO / "examples" / "agent_world", root)
    config = root / "booking.app.json"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "http://127.0.0.1:8768", f"http://127.0.0.1:{server.server_port}"
        ),
        encoding="utf-8",
    )
    try:
        yield root, server
    finally:
        server.shutdown()
        server.server_close()


def _send(root: Path, text: str, *, new: bool = False) -> tuple[int, dict[str, Any]]:
    args = ["chat", "--project", str(root)]
    if new:
        args += ["--app", str(root / "booking.app.json"), "--dataset", str(root / "cases.jsonl"),
                 "--policy", str(root / "policy.json"), "--new", "--objective", OBJECTIVE]  # fmt: skip
    result = cli.invoke(app, [*args, "--send", text, "--json"])
    return result.exit_code, json.loads(result.stdout)


def test_app_explains_the_runner_resets_and_missing_evidence(project: tuple[Path, Any]) -> None:
    root, server = project
    code, reply = _send(root, "/app", new=True)
    assert code == 0 and reply["kind"] == "application", reply
    data = reply["data"]
    assert data["kind"] == "http" and data["reset"]["mode"] == "per_case"
    assert "before every case, through its reset_url" in data["reset"]["summary"]
    assert {g["capability"] for g in data["missing_evidence"]} == {
        "retrieved_context",
        "usage",
        "cost",
    }
    assert {w["world_id"]: w["approved"] for w in data["test_worlds"]} == {
        "email-allowed": False,
        "two-seats": True,
    }
    assert data["selected_test_world"] is None
    assert sum(server.calls.values()) == 0  # describing starts and invokes nothing


def test_world_selects_an_approved_world_as_a_new_draft_and_the_run_loads_it(
    project: tuple[Path, Any],
) -> None:
    root, server = project
    code, first = _send(root, "/plan", new=True)
    assert code == 0 and first["data"]["test_world"] is None
    revision = first["data"]["revision"]

    code, chosen = _send(root, "/world two-seats")
    assert code == 0 and chosen["kind"] == "plan", chosen
    assert chosen["data"]["revision"] == revision + 1
    assert chosen["data"]["test_world"] == "two-seats" and chosen["data"]["executable"]

    _send(root, "/plan")  # show the revision, then run it
    code, run = _send(root, "/run")
    assert run["kind"] == "action" and run["data"]["state"] == "done", run
    assert code in (0, 1)  # completed; the final-state gate may fail on the faulty agent
    assert server.calls["/reset"] == server.calls["/agent"] == 5  # reset before every case


def test_an_undeclared_world_is_rejected_and_an_unapproved_one_cannot_run(
    project: tuple[Path, Any],
) -> None:
    root, server = project
    _send(root, "/plan", new=True)
    code, undeclared = _send(root, "/world production")
    assert undeclared["ok"] is False and "not declared" in undeclared["data"]["error"]

    code, unapproved = _send(root, "/world email-allowed")
    assert code == 0 and unapproved["data"]["test_world"] == "email-allowed"
    assert unapproved["data"]["executable"] is False  # a valid change the policy refuses
    assert any(
        "email-allowed is not approved" in m for m in unapproved["data"]["missing_permission"]
    )
    _send(root, "/plan")
    code, run = _send(root, "/run")
    assert run["data"]["state"] in ("denied", "blocked"), run
    assert sum(server.calls.values()) == 0

    code, cleared = _send(root, "/world none")
    assert code == 0 and cleared["data"]["test_world"] is None


def test_the_assistant_describes_and_selects_a_world_only_from_the_users_words(
    project: tuple[Path, Any],
) -> None:
    from tests.engine_support import Harness

    root, _ = project
    h = Harness(root / "h")
    storage, artifacts = h.storage()
    controller = SessionController.create(
        storage=storage,
        artifacts=artifacts,
        workspace_root=h.workspace.root,
        project_root=root,
        application=root / "booking.app.json",
        dataset=root / "cases.jsonl",
        objectives=(OBJECTIVE,),
        policy_path=root / "policy.json",
    )
    provider = ScriptedProvider([])
    agent = ConversationAgent(controller, provider)
    try:

        async def dialogue() -> None:
            provider.add(call("describe_application"), say("It is reset before every case."))
            explained = await agent.handle_message("How is the app reset, and what can you see?")
            assert explained.explained[-1]["tool"] == "describe_application"
            assert explained.decisions == []

            # A world the user did not name is refused...
            provider.add(patch_step("use a test world", test_world="two-seats"), say("ok"))
            refused = await agent.handle_message("Please use a test world.")
            assert refused.decisions == []
            assert "test world 'two-seats' does not appear" in refused.rejected[0]["problems"][0]

            # ...the named one becomes a new revision.
            provider.add(
                patch_step("Use the two-seats world", test_world="two-seats"), say("Done.")
            )
            chosen = await agent.handle_message("Use the two-seats world.")
            assert chosen.decisions and chosen.presented_draft["test_world"] == "two-seats"

        asyncio.run(dialogue())
        assert controller.describe_application()["selected_test_world"] == "two-seats"
    finally:
        controller.storage.db.close()
