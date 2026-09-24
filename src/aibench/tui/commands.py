"""Deterministic slash controls (§13, 09-T3). They bypass natural-language inference and
never call a model, so status, pause and stop work even when the assistant model is slow,
failing or absent. Each command returns structured data (printed by the terminal, or as
JSON by `aibench chat --send`) and is recorded as a command turn with the decisions and
actions it produced.

`/run` starts the revision the user was shown: if the current revision has not been
displayed yet, `/run` displays it and asks for `/run` again (§8: "Run it" authorizes the
displayed plan).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aibench.core.errors import AibenchError
from aibench.core.sessions import ActionKind, PlanPatch
from aibench.services.comparison import comparison_exit_code
from aibench.services.reports import FORMATS
from aibench.sessions.controller import SessionController

COMMANDS: dict[str, str] = {
    "/help": "explain benchmark tasks and commands",
    "/plan": "show the draft: metrics, gaps, missing inputs, coverage and estimate",
    "/run": "start the displayed draft (within the policy you opened the session with)",
    "/status": "committed state of the current run (no model involved)",
    "/pause": "stop new dispatch; in-flight work finishes",
    "/resume": "continue the paused or interrupted run",
    "/stop": "cancel the current run (partial results are kept)",
    "/failures": "failed and errored results of the current run",
    "/case": "/case CASE_ID - one case's evidence",
    "/budget": "ceilings, committed spend and unknown accounting",
    "/app": "the application's runner: what it observes, missing evidence, resets, test worlds",
    "/world": "/world NAME|none - select one of the application's test worlds (a new draft)",
    "/report": "/report [html|markdown|json] - the current run's report, from stored facts",
    "/compare": "/compare BASELINE CURRENT - paired stored-run comparison (no model)",
    "/integrations": "external integrations: modes, data destinations, availability",
    "/sessions": "sessions of this project",
    "/new": "start a fresh session in this project",
    "/exit": "leave; an active run stops dispatching and stays resumable",
}


@dataclass
class CommandResult:
    command: str
    kind: str  # what `data` holds, for rendering
    data: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    exit: bool = False
    switch_to: SessionController | None = None
    exit_code: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "kind": self.kind,
            "ok": self.ok,
            "data": self.data,
            "exit_code": self.exit_code,
        }


NewSession = Callable[[], SessionController]


class Commands:
    def __init__(self, controller: SessionController, new_session: NewSession | None = None):
        self.controller = controller
        self.new_session = new_session

    async def run(self, text: str, *, message_id: str | None = None) -> CommandResult:
        name, _, argument = text.strip().partition(" ")
        name = name.lower()
        handler = getattr(self, "_" + name.lstrip("/"), None) if name in COMMANDS else None
        if handler is None:
            return CommandResult(
                name, "error", {"error": f"unknown command {name}; type /help"}, ok=False
            )
        try:
            result: CommandResult = await handler(argument.strip())
        except AibenchError as exc:
            result = CommandResult(name, "error", {"error": str(exc)}, ok=False)
        self._record(text.strip(), result, message_id)
        return result

    def _record(self, text: str, result: CommandResult, message_id: str | None) -> None:
        if result.switch_to is not None:
            return  # recorded in the new session instead
        data = result.data
        action = data.get("action_id") if result.kind == "action" else None
        decision = data.get("decision_id")
        self.controller.record_command(
            text,
            message_id=message_id,
            action_refs=(action,) if action else (),
            decision_refs=(decision,) if decision else (),
        )

    def _action_id(self) -> str:
        return f"cmd-{uuid.uuid4().hex[:16]}"

    # ------------------------------------------------------------------ commands

    async def _help(self, _: str) -> CommandResult:
        return CommandResult(
            "/help",
            "help",
            {
                "commands": COMMANDS,
                "messages": "Anything else is a message to the assistant: say what to "
                "measure, ask why a metric was chosen, change the sample, or ask to run.",
            },
        )

    async def _plan(self, _: str) -> CommandResult:
        state = self.controller.state()
        self.controller.mark_presented(state["revision"])
        return CommandResult("/plan", "plan", state["draft"])

    async def _run(self, _: str) -> CommandResult:
        session = self.controller.session
        if session.presented_revision != session.revision:
            state = self.controller.state()
            self.controller.mark_presented(state["revision"])
            return CommandResult(
                "/run",
                "confirm",
                {
                    **state["draft"],
                    "note": f"This is revision {state['revision']}; type /run again to start it.",
                },
            )
        action = await self.controller.start_run(
            action_id=self._action_id(), expected_revision=session.revision
        )
        return CommandResult(
            "/run", "action", action.model_dump(mode="json"), ok=action.state.value == "done"
        )

    async def _control(self, name: str, kind: ActionKind) -> CommandResult:
        action = await self.controller.control_run(kind, action_id=self._action_id())
        return CommandResult(
            name, "action", action.model_dump(mode="json"), ok=action.state.value == "done"
        )

    async def _pause(self, _: str) -> CommandResult:
        return await self._control("/pause", ActionKind.PAUSE_RUN)

    async def _resume(self, _: str) -> CommandResult:
        return await self._control("/resume", ActionKind.RESUME_RUN)

    async def _stop(self, _: str) -> CommandResult:
        return await self._control("/stop", ActionKind.CANCEL_RUN)

    async def _status(self, _: str) -> CommandResult:
        return CommandResult("/status", "status", self.controller.run_status())

    async def _failures(self, _: str) -> CommandResult:
        return CommandResult("/failures", "failures", self.controller.failures())

    async def _case(self, argument: str) -> CommandResult:
        if not argument:
            return CommandResult("/case", "error", {"error": "usage: /case CASE_ID"}, ok=False)
        return CommandResult("/case", "case", self.controller.case_evidence(argument))

    async def _budget(self, _: str) -> CommandResult:
        return CommandResult("/budget", "budget", self.controller.budget())

    async def _integrations(self, _: str) -> CommandResult:
        return CommandResult(
            "/integrations", "integrations", {"integrations": self.controller.integrations()}
        )

    async def _app(self, _: str) -> CommandResult:
        return CommandResult("/app", "application", self.controller.describe_application())

    async def _world(self, argument: str) -> CommandResult:
        """Select a test world through the same validated plan change the assistant uses:
        a new draft revision, which the execution gate checks against the policy."""
        name = argument.strip()
        if not name:
            return CommandResult(
                "/world", "error", {"error": "usage: /world NAME (or /world none)"}, ok=False
            )
        patch = (
            PlanPatch(clear_test_world=True)
            if name.lower() == "none"
            else PlanPatch(test_world=name)
        )
        result = self.controller.apply_patch(
            patch, expected_revision=self.controller.session.revision, source="user"
        )
        if result.status != "applied":
            return CommandResult(
                "/world", "error", {"error": "; ".join(result.problems) or result.status}, ok=False
            )
        return CommandResult("/world", "plan", self.controller.state()["draft"])

    async def _report(self, argument: str) -> CommandResult:
        """Render the current run's report from stored facts: a terminal summary, and the
        report files (HTML and JSON by default; `/report markdown` etc. to choose)."""
        formats = tuple(argument.split()) or ("html", "json")
        unknown = [f for f in formats if f not in FORMATS]
        if unknown:
            return CommandResult(
                "/report",
                "error",
                {"error": f"unknown format {' '.join(unknown)}; use html, markdown or json"},
                ok=False,
            )
        exported = self.controller.export_report(formats=formats)
        facts = self.controller.report_facts(exported["run_id"])
        return CommandResult("/report", "report", {**facts, "exported": exported["paths"]})

    async def _compare(self, argument: str) -> CommandResult:
        parts = argument.split()
        if len(parts) != 2:
            return CommandResult(
                "/compare",
                "error",
                {"error": "usage: /compare BASELINE_RUN_ID CURRENT_RUN_ID"},
                ok=False,
                exit_code=2,
            )
        report = self.controller.compare_runs(parts[0], parts[1])
        code = comparison_exit_code(report)
        return CommandResult(
            "/compare",
            "comparison",
            report,
            ok=code == 0,
            exit_code=code,
        )

    async def _sessions(self, _: str) -> CommandResult:
        root = self.controller.session.project_root
        sessions = [
            {
                "session_id": s.session_id,
                "revision": s.revision,
                "active_run_id": s.active_run_id,
                "updated_at": s.updated_at.isoformat(),
                "current": s.session_id == self.controller.session_id,
            }
            for s in self.controller.store.list_sessions()
            if s.project_root == root
        ]
        return CommandResult("/sessions", "sessions", {"sessions": sessions})

    async def _new(self, _: str) -> CommandResult:
        if self.new_session is None:
            return CommandResult("/new", "error", {"error": "no new session here"}, ok=False)
        if self.controller.active_run() is not None:
            return CommandResult(
                "/new",
                "error",
                {"error": "a run is active in this session; /pause, /stop or wait first"},
                ok=False,
            )
        created = self.new_session()
        created.record_command("/new")
        return CommandResult(
            "/new", "switched", {"session_id": created.session_id}, switch_to=created
        )

    async def _exit(self, _: str) -> CommandResult:
        active = self.controller.active_run()
        return CommandResult("/exit", "exit", {"active_run": active}, exit=True)
