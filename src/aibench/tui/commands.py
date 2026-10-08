"""Deterministic slash controls (§13, 09-T3). They bypass natural-language inference and
never call a model, so status, pause and stop work even when the assistant model is slow,
failing or absent. Each command returns structured data (printed by the terminal, or as
JSON by `aibench chat --send`) and is recorded as a command turn with the decisions and
actions it produced.

`/run` is an explicit request to start the current validated draft under the session's
existing policy. It displays that draft as a preview in the same response and does not ask
the user to repeat the command.
"""

from __future__ import annotations

import asyncio
import shlex
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aibench.core.errors import AibenchError
from aibench.core.sessions import ActionKind, PlanPatch
from aibench.services.comparison import comparison_exit_code
from aibench.services.reports import FORMATS
from aibench.sessions.controller import SessionController

COMMANDS: dict[str, str] = {
    "/help": "explain benchmark tasks and commands",
    "/plan": "show the draft: metrics, gaps, missing inputs, coverage and estimate",
    "/run": "start the current validated draft (within the policy you opened the session with)",
    "/status": "committed state of the current run (no model involved)",
    "/pause": "stop new dispatch; in-flight work finishes",
    "/resume": "continue the paused or interrupted run",
    "/stop": "cancel the current run (partial results are kept)",
    "/failures": "failed and errored results of the current run",
    "/traces": "/traces [import FILE] [RUN_ID] - a run's imported traces, or attach an export",
    "/rescore": "/rescore [all] [RUN_ID] - score stored outputs; only what failed or is missing",
    "/case": "/case CASE_ID - one case's evidence",
    "/cases": (
        "/cases generate FILE... | accept N... | reject N... | save - test cases from documents; "
        "/cases check|verify|add FILE.jsonl - check cases against their source, write your own"
    ),
    "/budget": "ceilings, committed spend and unknown accounting",
    "/app": "the application's runner: what it observes, missing evidence, resets, test worlds",
    "/world": "/world NAME|none - select one of the application's test worlds (a new draft)",
    "/report": "/report [html|markdown|json] - the current run's report, from stored facts",
    "/compare": "/compare BASELINE CURRENT - paired stored-run comparison (no model)",
    "/integrations": "external integrations: modes, data destinations, availability",
    "/plugins": "/plugins [install NAME [--yes]] - optional metric plugins (DeepEval, Ragas)",
    "/sessions": "sessions of this project",
    "/new": "start a fresh session in this project",
    "/exit": "leave; an active run stops dispatching and stays resumable",
}


def _unquote(word: str) -> str:
    """A word as `shlex.split(posix=False)` keeps it, without its surrounding quotes."""
    if len(word) >= 2 and word[0] == word[-1] and word[0] in "\"'":
        return word[1:-1]
    return word


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
# The judge a plugin install configures (the assistant's own model), and its secret_env.
JudgeSource = tuple[dict[str, Any], dict[str, str]]


class Commands:
    def __init__(
        self,
        controller: SessionController,
        new_session: NewSession | None = None,
        *,
        judge: JudgeSource | None = None,
        progress: Callable[[str], None] | None = None,
        provider: Any = None,
    ):
        self.controller = controller
        self.new_session = new_session
        self.judge = judge
        self.provider = provider  # the assistant's model: it writes candidate cases
        self.progress = progress or (lambda _line: None)

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
        preview = self.controller.state()["draft"]
        self.controller.mark_presented(session.revision)
        action = await self.controller.start_run(
            action_id=self._action_id(), expected_revision=session.revision
        )
        return CommandResult(
            "/run",
            "action",
            {**action.model_dump(mode="json"), "plan_preview": preview},
            ok=action.state.value == "done",
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

    async def _plugins(self, argument: str) -> CommandResult:
        """`/plugins`: optional plugins and whether this project can use them.
        `/plugins install NAME` shows what installing it changes; adding `--yes` does it:
        the plugin's own environment, the project config and the policy lines it needs.
        The user typing this is the approval; the assistant can only suggest it."""
        from aibench.services.plugins import install, plan_install, plugin_status, session_plugins

        root = self.controller.project_root
        words = shlex.split(argument, posix=False)
        if not words:
            rows = plugin_status(root, self.controller.policy())
            return CommandResult("/plugins", "plugins", {"plugins": rows})
        usage = "usage: /plugins install NAME [--use-env PYTHON] [--yes]"
        if words[0] != "install" or len(words) < 2:
            return CommandResult("/plugins", "error", {"error": usage}, ok=False)
        name, options = words[1], words[2:]
        existing: Path | None = None
        if "--use-env" in options:
            index = options.index("--use-env")
            if index + 1 >= len(options):
                return CommandResult("/plugins", "error", {"error": usage}, ok=False)
            existing = Path(options[index + 1].strip('"'))
        judge, secrets = self.judge if self.judge else (None, {})
        policy = self.controller.session.policy_path
        plan = plan_install(
            name,
            root,
            policy_path=Path(policy) if policy else None,
            judge=judge,
            secret_env=secrets,
            existing_python=existing,
        )
        if "--yes" not in options:
            confirm = f"/plugins install {argument.split(maxsplit=1)[1]} --yes"
            return CommandResult(
                "/plugins", "plugin_preview", {**plan.summary(), "confirm": confirm}
            )
        if self.controller.active_run() is not None:
            return CommandResult(
                "/plugins",
                "error",
                {"error": "a run is active in this session; /pause, /stop or wait first"},
                ok=False,
            )
        done = await asyncio.to_thread(install, plan, self.progress)
        environments, defaults = session_plugins(root)
        result = self.controller.use_plugin_environments(environments, defaults)
        return CommandResult(
            "/plugins",
            "plugin_installed",
            {
                **done,
                "revision": result.revision,
                "status": result.status,
                "problems": result.problems,
            },
            ok=result.status == "applied",
        )

    async def _cases(self, argument: str) -> CommandResult:
        """`/cases`: test cases from documents. `generate FILE...` has the assistant's model
        write candidate cases from the documents (they go to that model's provider, so the
        policy must allow it); `accept`/`reject` are the user's review of each one, shown
        beside the source quote it cites; `save` writes the accepted ones to a new dataset
        file. Nothing generated is a case until it is accepted and saved."""
        from aibench.services import case_pools

        usage = (
            "usage: /cases generate FILE_OR_FOLDER... [--max N]  |  /cases  |  "
            "/cases accept N... | all  |  /cases reject N...  |  /cases save [FILE.jsonl]  |  "
            "/cases check FILE.jsonl [N... | all]  |  /cases verify FILE.jsonl N...  |  "
            '/cases add FILE.jsonl "QUESTION" "ANSWER"'
        )
        words = shlex.split(argument, posix=False)
        sub = words[0].lower() if words else "show"
        rest = words[1:]
        storage = self.controller.storage
        root = self.controller.project_root

        def failed(message: str) -> CommandResult:
            return CommandResult("/cases", "error", {"error": message}, ok=False)

        if sub == "generate":
            limit = 20
            if "--max" in rest:
                at = rest.index("--max")
                if (
                    at + 1 >= len(rest)
                    or not rest[at + 1].isdigit()
                    or not 1 <= int(rest[at + 1]) <= 50
                ):
                    return failed("--max takes a number from 1 to 50")
                limit = int(rest[at + 1])
                rest = rest[:at] + rest[at + 2 :]
            if not rest:
                return failed(usage)
            config = getattr(self.provider, "config", None)
            if config is None:
                return failed(
                    "no assistant model is configured to write the cases: run "
                    "`benchcraft setup` first"
                )
            sources = case_pools.source_files(root, rest)
            # Only the model call runs off this thread: the database is not shared with it.
            manifest, candidates, dropped = await asyncio.to_thread(
                case_pools.draft_pool,
                config,
                self.controller.policy(),
                sources,
                max_candidates=limit,
            )
            made = case_pools.store_pool(storage, manifest, candidates, dropped)
            return CommandResult(
                "/cases",
                "cases",
                {
                    "pool_id": made.pool_id,
                    "rows": made.rows,
                    "duplicate_sources": made.duplicate_sources,
                    "dropped": list(made.dropped),
                    "generated": True,
                },
            )
        if sub in ("check", "verify", "add"):
            return self._golden(sub, rest, root)
        pool_id = case_pools.newest_pool_id(storage)
        if sub == "show":
            if pool_id is None:
                return failed("no cases yet: /cases generate FILE_OR_FOLDER")
            return CommandResult(
                "/cases",
                "cases",
                {
                    "pool_id": pool_id,
                    "rows": case_pools.pool_rows(storage, pool_id),
                    "duplicate_sources": [],
                    "generated": False,
                },
            )
        if sub in ("accept", "reject"):
            if pool_id is None:
                return failed("no cases yet: /cases generate FILE_OR_FOLDER")
            if not rest:
                return failed(f"which cases? /cases {sub} 1 2 3  (or /cases {sub} all)")
            done = case_pools.decide(storage, pool_id, rest, accept=sub == "accept")
            left = sum(
                1 for row in case_pools.pool_rows(storage, pool_id) if row["status"] == "candidate"
            )
            return CommandResult(
                "/cases",
                "cases_decided",
                {"accepted": sub == "accept", "done": done, "undecided": left},
            )
        if sub == "save":
            if pool_id is None:
                return failed("no cases yet: /cases generate FILE_OR_FOLDER")
            target = Path(rest[0].strip('"')) if rest else Path(case_pools.DEFAULT_OUTPUT)
            target = target if target.is_absolute() else root / target
            path, count = case_pools.save_accepted(storage, pool_id, target)
            return CommandResult("/cases", "cases_saved", {"path": str(path), "count": count})
        return failed(usage)

    def _golden(self, sub: str, words: list[str], root: Path) -> CommandResult:
        """`/cases check|verify|add FILE ...`: work on a saved dataset towards a golden one
        (see `aibench.services.golden`). They read and write only that file."""
        from aibench.services import golden

        examples = {
            "check": "/cases check FILE.jsonl [N... | all]",
            "verify": "/cases verify FILE.jsonl N...",
            "add": '/cases add FILE.jsonl "QUESTION" "ANSWER"',
        }
        if not words:
            return CommandResult("/cases", "error", {"error": f"usage: {examples[sub]}"}, ok=False)
        target = Path(_unquote(words[0]))
        path = target if target.is_absolute() else root / target
        rest = [_unquote(w) for w in words[1:]]
        if sub == "check":
            return CommandResult("/cases", "cases_check", golden.check_rows(path, rest))
        if sub == "verify":
            return CommandResult("/cases", "cases_verified", golden.verify(path, rest))
        if len(rest) != 2:
            return CommandResult(
                "/cases",
                "error",
                {"error": f"usage: {examples['add']} (quote each)"},
                ok=False,
            )
        return CommandResult("/cases", "cases_added", golden.add(path, rest[0], rest[1]))

    async def _traces(self, argument: str) -> CommandResult:
        """`/traces [RUN_ID]`: what the run's imported traces add. `/traces import FILE
        [RUN_ID]`: attach an OpenTelemetry (OTLP/JSON) export to the run, so metrics that
        read traces (DeepEval's agent metrics) can score it with `/rescore`."""
        words = shlex.split(argument, posix=False)
        if not words or words[0] != "import":
            if len(words) > 1:
                return CommandResult(
                    "/traces", "error", {"error": "usage: /traces [import FILE] [RUN_ID]"}, ok=False
                )
            evidence = self.controller.trace_evidence(words[0] if words else None)
            return CommandResult("/traces", "traces", evidence)
        if len(words) not in (2, 3):
            return CommandResult(
                "/traces", "error", {"error": "usage: /traces import FILE [RUN_ID]"}, ok=False
            )
        file = words[1].strip('"')
        summary = self.controller.import_traces(file, words[2] if len(words) == 3 else None)
        return CommandResult("/traces", "traces_imported", summary)

    async def _rescore(self, argument: str) -> CommandResult:
        """Score the run's stored outputs with the current draft: the same policy-checked
        path as the assistant's rescore. The application is never called. Results already
        finished are carried forward and only what failed or is missing is evaluated, so a
        judge that hit a rate limit on 2 of 15 cases is asked about those 2; `/rescore all`
        evaluates everything again."""
        words = argument.split()
        everything = bool(words) and words[0].lower() == "all"
        if everything:
            words = words[1:]
        if len(words) > 1:
            return CommandResult(
                "/rescore", "error", {"error": "usage: /rescore [all] [RUN_ID]"}, ok=False
            )
        report = await self.controller.rescore(
            words[0] if words else None, carry_forward=not everything
        )
        return CommandResult("/rescore", "rescored", report)

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
