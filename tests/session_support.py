"""Helpers for session and conversation tests: a real instrumented CLI application (from
`engine_support`), sessions opened on a real workspace, and a deterministic scripted
assistant model — no mocks of the controller, engine or storage."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from aibench.planning.planner import ModelReply, ToolCall
from aibench.sessions.controller import SessionController
from tests.engine_support import Harness

Step = ModelReply | Callable[[list[dict[str, Any]]], ModelReply]


class ScriptedProvider:
    """A deterministic assistant model. Each `complete` call returns the next step; a step
    may be a function of the messages it was sent, so a script can react to the actual
    session state. Calls beyond the script fail the test."""

    name = "scripted"
    model = "scripted-1"

    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.calls: list[list[dict[str, Any]]] = []

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        self.calls.append(json.loads(json.dumps(messages, default=str)))
        if not self.steps:
            raise AssertionError("the assistant model was called more often than scripted")
        step = self.steps.pop(0)
        return step(messages) if callable(step) else step

    def add(self, *steps: Step) -> None:
        self.steps.extend(steps)


_counter = iter(range(1, 1_000_000))


def call(name: str, **arguments: Any) -> ModelReply:
    return ModelReply(
        text=None,
        tool_calls=(ToolCall(f"call-{next(_counter)}", name, json.dumps(arguments)),),
        prompt_tokens=10,
        completion_tokens=5,
    )


def say(text: str) -> ModelReply:
    return ModelReply(text=text, prompt_tokens=10, completion_tokens=5)


class SessionHarness(Harness):
    def open_session(
        self,
        inputs: dict[str, str],
        *,
        objectives: tuple[str, ...] = (),
        trusted: bool = True,
        policy: dict[str, Any] | None = None,
        rows: list[dict[str, Any]] | None = None,
        environment_digest: str | None = None,
    ) -> SessionController:
        if rows is not None:
            data = "data.jsonl"
            (self.root / data).write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
            )
        else:
            data = self.dataset(inputs)
        app = self.cli_app(environment_digest=environment_digest)
        policy_path = None
        if policy is not None:
            policy_path = self.root / "policy.json"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
        storage, artifacts = self.storage()
        return SessionController.create(
            storage=storage,
            artifacts=artifacts,
            workspace_root=self.workspace.root,
            project_root=self.root,
            application=self.root / app,
            dataset=self.root / data,
            objectives=objectives,
            policy_path=policy_path,
            trusted_local=trusted,
        )

    def reopen(self, controller: SessionController) -> SessionController:
        """The same session on a fresh connection, as a new process would open it."""
        controller.storage.db.close()
        storage, artifacts = self.storage()
        return SessionController(
            controller.session_id,
            storage=storage,
            artifacts=artifacts,
            workspace_root=self.workspace.root,
        )

    def runs(self) -> list[str]:
        storage, _ = self.storage()
        try:
            return [r.manifest.run_id for r in storage.list_runs()]
        finally:
            storage.db.close()


def revision_seen(messages: list[dict[str, Any]]) -> int:
    """The latest draft revision the model has been told about: from the most recent tool
    result that reports one, else the session state it was briefed with."""
    for message in reversed(messages):
        if message.get("role") == "tool":
            data = json.loads(message["content"])
            if isinstance(data, dict) and isinstance(data.get("revision"), int):
                return data["revision"]
    state = next(m for m in messages if str(m.get("content", "")).startswith("Session state"))
    return int(json.loads(state["content"].split("\n", 1)[1])["revision"])


def patch_step(quote: str, **patch: Any) -> Step:
    """A model step proposing `patch` against the revision the model last saw."""
    return lambda messages: call(
        "propose_plan_patch",
        expected_revision=revision_seen(messages),
        user_quote=quote,
        patch=patch,
    )


def start_step(quote: str) -> Step:
    """A model step asking to run the revision the model last saw."""
    return lambda messages: call(
        "request_action",
        action="start_run",
        user_quote=quote,
        expected_revision=revision_seen(messages),
    )
