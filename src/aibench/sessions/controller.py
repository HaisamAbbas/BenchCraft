"""The benchmark session controller (§4, §8, 08-T1..T4).

It owns a session's decisions, pending questions, typed action dispatch and run
subscriptions, and calls the same services as the headless commands:

- drafts: `planning.service.gather_inputs` + the template planner + `validate_draft`
  (see `sessions.drafting`);
- runs: `engine.compile.compile_plan` (the execution gate), `services.runs.create_run`,
  `execute_run` and `run_status`; runs execute as asyncio tasks on the caller's event loop,
  so the conversation keeps answering while a run executes (§15);
- results: stored metric results and execution attempts.

Boundaries (08-T4):
- every patch names the revision it was made against, and applies only if that is still
  current (`StaleRevision`); answers to questions asked against an older revision are
  stale too;
- `start_run` names the reviewed revision it runs; a redelivered action ID returns the
  original record instead of acting again; one active run per session (§15);
- a patch while a run is active creates a new draft revision and never touches the run,
  whose plan is frozen; pause/resume/cancel are recorded as action requests and run
  events, not benchmark changes.

The controller never grants permissions: the policy file and the user's explicit grant
when the session was opened (`trusted_local`) decide what a run may do.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from aibench.core.errors import AibenchError, ConflictError
from aibench.core.models import (
    Decision,
    ExecutionStatus,
    WorkItemState,
    deep_unfreeze,
    utcnow,
)
from aibench.core.plans import PluginEnvironmentRef
from aibench.core.sessions import (
    ActionKind,
    ActionRequest,
    ActionState,
    BenchmarkSession,
    ConversationTurn,
    DecisionRecord,
    PendingQuestion,
    PlanPatch,
    SessionChoices,
)
from aibench.engine.compile import PlanInvalid, PolicyDenied, compile_plan, load_policy
from aibench.engine.engine import RunController, RunOutcome
from aibench.planning.opportunities import discover_opportunities
from aibench.planning.planner import PlanningInputs
from aibench.reporting.aggregation import reason_code
from aibench.runners import load_application
from aibench.security.policy import ExecutionPolicy
from aibench.security.redaction import sanitize
from aibench.services.applications import describe_application
from aibench.services.comparison import compare_runs as compare_stored_runs
from aibench.services.reports import build_report, export_report, report_dir, report_facts
from aibench.services.runs import (
    RESUMABLE_STATES,
    RunError,
    create_run,
    evaluate_run,
    execute_run,
    lease_state,
    run_budget,
    run_status,
)
from aibench.services.scoring import select_final_executions
from aibench.sessions.drafting import (
    PatchRejected,
    apply_patch,
    build_draft,
    draft_summary,
    planning_inputs,
    session_directory,
)
from aibench.sessions.store import SessionStore, StaleRevision
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.repositories import Storage

# A run in one of these states is the session's active run (§15: one per session).
ACTIVE_RUN_STATES = frozenset(
    {"created", "running", "pausing", "paused", "cancelling", "interrupting"}
)


# Conditions in which a run occupies the session's single run slot.
LIVE_CONDITIONS = frozenset({"running_here", "paused_here", "active_elsewhere", "starting"})
# A run just created, before its session takes the lease, is starting — not interrupted.
CREATED_GRACE_SECONDS = 60.0

# The run slot is claimed with this marker while a run is being created, so two processes
# cannot both start one; a marker older than this belongs to a start that crashed midway.
STARTING = "starting:"
STARTING_TTL_SECONDS = 600.0


_COMPARISON_DROP_KEYS = frozenset(
    {
        "cases",
        "groups",
        "units",
        "missing_repeats",
        "identity_checks",
        "identities",
        "evidence",
        "artifacts",
        "raw",
        "baseline_only_keys",
        "current_only_keys",
    }
)
_COMPARISON_HIDDEN_KEYS = frozenset(
    {
        "application_hash",
        "binding_hash",
        "case_content_hash",
        "compatibility_hash",
        "semantic_digest",
        "parameters_hash",
        "rule_digest",
        "selection_digest",
        "input_binding_digest",
        "output_binding_digest",
        "environment_digest",
        "instrumentation_digest",
        "changed_case_ids",
        "dataset_hash",
        "digest",
        "execution_id",
        "group_id",
        "case_id",
        "case_ids",
        "judge",
        "rubric",
        "params",
        "reason",
    }
)


def _comparison_key_hidden(name: str) -> bool:
    lowered = name.lower()
    return (
        name in _COMPARISON_DROP_KEYS
        or name in _COMPARISON_HIDDEN_KEYS
        or "case_id" in lowered
        or "group_id" in lowered
        or lowered.endswith(("_hash", "_digest"))
    )


def _comparison_for_assistant(value: Any, *, key: str = "") -> Any:
    """Remove identity hashes, case/group IDs and raw ledger rows from chat facts."""
    if _comparison_key_hidden(key):
        return None
    if isinstance(value, dict):
        return {
            str(name): _comparison_for_assistant(item, key=str(name))
            for name, item in value.items()
            if not _comparison_key_hidden(str(name))
        }
    if isinstance(value, list):
        return [_comparison_for_assistant(item, key=key) for item in value]
    if isinstance(value, str):
        lowered = value.lower()
        if value.startswith("sha256:") or "case_id" in lowered or "group_id" in lowered:
            return None
    return value


class SessionError(AibenchError):
    """A session cannot be created or opened as asked."""


@dataclass
class PatchResult:
    status: str  # "applied" | "unchanged" | "stale" | "rejected"
    revision: int  # the session's revision after this call
    decision: DecisionRecord | None = None
    problems: list[str] = field(default_factory=list)
    active_run: str | None = None  # a run that keeps executing its frozen plan

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "revision": self.revision,
            "decision_id": self.decision.decision_id if self.decision else None,
            "changes": deep_unfreeze(self.decision.structured_change) if self.decision else None,
            "draft": draft_summary(
                deep_unfreeze(self.decision.draft), self.decision.choices.test_world
            )
            if self.decision
            else None,
            "problems": self.problems,
            "active_run_unchanged": self.active_run,
        }


@dataclass
class _LiveRun:
    task: asyncio.Task[RunOutcome | None]
    control: RunController
    error: str | None = None


def _age(record: Any) -> float:
    created = datetime.fromisoformat(record.created_at)
    return (utcnow() - created).total_seconds()


def _stable_seed(session_id: str) -> int:
    return int(hashlib.sha256(session_id.encode()).hexdigest()[:8], 16) % (2**31)


def unavailable_settings(names: list[str], available: list[str]) -> list[str]:
    """Why settings keyed by `names` were not saved. A name that is not a metric id at all (an
    objective's name, "traffic_correctness") gets the ids that exist: telling a model it is
    "not available" sent it round in circles between plugins and paraphrases."""
    problems = []
    for name in names:
        prefix = name.split(".", 1)[0] if "." in name else None
        if prefix and not any(i.startswith(prefix + ".") for i in available):
            problems.append(
                f"{name} is not available in this session, so its settings were not saved; "
                "/plugins shows which optional plugins this project has installed (a metric "
                "from a plugin needs it installed and allowed by the policy)"
            )
            continue
        shown = ", ".join(available[:15]) + (" ..." if len(available) > 15 else "")
        problems.append(
            f"{name!r} is not a metric id, so its settings were not saved. Settings are keyed "
            f"by a metric id, never by an objective's name. Metric ids in this session: {shown}"
        )
    return problems


def unplanned_settings(patch: PlanPatch, draft: Any) -> list[str]:
    """Metrics the patch configures that did not make it into the plan, and why. A metric
    configured with settings it cannot run on (a required setting missing) was left out of
    the plan as a pending question while the plan still read "ready to run": the run had no
    correctness check, and nobody was told. The change is refused instead, with the reason."""
    planned = {choice.metric.rsplit("@", 1)[0] for choice in draft.document.rationale}
    available = {option.evaluator_id for option in draft.inputs.catalog}
    problems = []
    for evaluator_id, settings in patch.params.items():
        if not settings or evaluator_id not in available or evaluator_id in planned:
            continue
        reasons = [
            question.prompt
            for question in draft.document.pending_questions
            if evaluator_id in question.prompt
        ] + [gap.reason for gap in draft.document.gaps if evaluator_id in gap.reason]
        problems.append(
            f"{evaluator_id} would not be in the plan with these settings, so nothing was "
            f"changed: {'; '.join(reasons) or 'its settings are incomplete'}"
        )
    return problems


class SessionController:
    def __init__(
        self,
        session_id: str,
        *,
        storage: Storage,
        artifacts: ArtifactStore,
        workspace_root: Path,
        environ: dict[str, str] | None = None,
    ) -> None:
        self.session_id = session_id
        self.storage = storage
        self.artifacts = artifacts
        self.store = SessionStore(storage)
        self.workspace_root = workspace_root
        self.environ = environ
        self._live: dict[str, _LiveRun] = {}
        self._live_experiments: dict[str, asyncio.Task[Any]] = {}
        self._inputs: tuple[int, PlanningInputs] | None = None
        if self.store.get_session(session_id) is None:
            raise SessionError(f"no session {session_id!r}")

    # ------------------------------------------------------------------ lifecycle

    @classmethod
    def create(
        cls,
        *,
        storage: Storage,
        artifacts: ArtifactStore,
        workspace_root: Path,
        project_root: Path,
        application: Path,
        dataset: Path,
        objectives: tuple[str, ...] = (),
        policy_path: Path | None = None,
        trusted_local: bool = False,
        plugin_environments: tuple[PluginEnvironmentRef, ...] = (),
        evaluator_defaults: dict[str, dict[str, Any]] | None = None,
        environ: dict[str, str] | None = None,
    ) -> SessionController:
        """Open a new session with its first draft (revision 1)."""
        session_id = f"ses-{uuid.uuid4().hex[:12]}"
        session = BenchmarkSession(
            session_id=session_id,
            project_root=str(project_root.resolve()),
            policy_path=str(policy_path.resolve()) if policy_path else None,
            trusted_local=trusted_local,
            plugin_environments=tuple(e.model_dump(mode="json") for e in plugin_environments),
            evaluator_defaults=evaluator_defaults or {},
            revision=1,
        )
        choices = SessionChoices(
            application=str(application.resolve()),
            dataset=str(dataset.resolve()),
            objectives=tuple(o.strip() for o in objectives if o.strip()),
        )
        directory = session_directory(workspace_root, session_id)
        draft = build_draft(session, choices, revision=1, directory=directory)
        decision = DecisionRecord(
            decision_id=f"{session_id}:d1",
            session_id=session_id,
            source_turn_id=None,
            source="session",
            revision=1,
            choices=choices,
            plan_file=draft.plan_file,
            plan_hash=draft.plan_hash,
            executable=draft.executable,
            draft=draft.document.model_dump(mode="json"),
        )
        session = session.model_copy(update={"decision_id": decision.decision_id})
        store = SessionStore(storage)
        store.create_session(session, decision, draft.questions())
        controller = cls(
            session_id,
            storage=storage,
            artifacts=artifacts,
            workspace_root=workspace_root,
            environ=environ,
        )
        controller._inputs = (1, draft.inputs)
        return controller

    async def close(self) -> None:
        """Leaving the session: live runs stop new dispatch, record in-flight outcomes and
        stay resumable (§13: exiting safely pauses; reopening never restarts a run)."""
        for live in list(self._live.values()):
            live.control.interrupt()
        for live in list(self._live.values()):
            with contextlib.suppress(Exception):
                await live.task
        experiments = list(self._live_experiments.values())
        for task in experiments:
            if not task.done():
                task.cancel()
        if experiments:
            await asyncio.gather(*experiments, return_exceptions=True)

    # ------------------------------------------------------------------ state

    @property
    def session(self) -> BenchmarkSession:
        session = self.store.get_session(self.session_id)
        assert session is not None
        return session

    def current_decision(self) -> DecisionRecord:
        session = self.session
        decision = self.store.get_decision(session.decision_id or "")
        assert decision is not None, "a session always has a current draft"
        return decision

    @property
    def directory(self) -> Path:
        return session_directory(self.workspace_root, self.session_id)

    @property
    def project_root(self) -> Path:
        return Path(self.session.project_root)

    def policy(self) -> ExecutionPolicy:
        path = self.session.policy_path
        return load_policy(Path(path) if path else None)

    def describe_application(self) -> dict[str, Any]:
        """The application's runner: what it observes, what evidence is missing, how its
        state is reset, and the test worlds it declares with the session policy's approval.
        Starts nothing."""
        choices = self.current_decision().choices
        data = describe_application(load_application(Path(choices.application)), self.policy())
        data["selected_test_world"] = choices.test_world
        return data

    def integrations(self) -> list[dict[str, Any]]:
        """External integrations: supported modes, data destinations and whether the
        session policy lets them run now (17-T4). Starts nothing, contacts nothing."""
        from aibench.services.integrations import integrations

        return integrations(self.policy())

    def optional_plugins(self) -> list[dict[str, Any]]:
        """Optional metric plugins, their state in this project and how to enable them.
        Reads files only."""
        from aibench.services.plugins import plugin_status

        return plugin_status(self.project_root, self.policy())

    def inputs(self) -> PlanningInputs:
        """Evidence for the current revision (profile, dataset counts, catalog), cached."""
        decision = self.current_decision()
        if self._inputs is None or self._inputs[0] != decision.revision:
            inputs = planning_inputs(
                self.session, decision.choices, revision=decision.revision, directory=self.directory
            )
            self._inputs = (decision.revision, inputs)
        return self._inputs[1]

    def opportunities(self) -> dict[str, Any]:
        """Current objective-to-metric opportunities from the shared planner inputs.

        The report is read-only and contains field counts and evidence references, never
        dataset values. Unsupported or unobserved requirements stay unavailable/unknown.
        """
        return discover_opportunities(self.inputs()).model_dump(mode="json")

    def session_runs(self) -> list[str]:
        """Runs this session started, oldest first."""
        return [
            a.run_id
            for a in self.store.list_actions(self.session_id)
            if a.kind is ActionKind.START_RUN and a.run_id
        ]

    def session_experiments(self) -> list[Any]:
        """Controlled experiments created through this session only."""
        prefix = f"{self.session_id}-exp-"
        return [
            record
            for record in self.storage.list_experiments()
            if record.experiment_id.startswith(prefix)
        ]

    def has_active_experiment_task(self) -> bool:
        """Whether this controller is currently dispatching any experiment work."""
        return any(not task.done() for task in self._live_experiments.values())

    def experiment_task_keys(self) -> set[str]:
        """Task keys created by this controller, including tasks that have finished."""
        return set(self._live_experiments)

    def start_experiment(self, experiment_id: str) -> dict[str, Any]:
        """Start or resume one experiment owned by this conversation, without blocking the
        turn. The immutable experiment record is the progress source if the process exits.
        """
        prefix = f"{self.session_id}-exp-"
        if not experiment_id.startswith(prefix):
            raise SessionError("this conversation can start only its own controlled experiments")
        record = self.storage.get_experiment(experiment_id)
        if record is None:
            raise SessionError(f"no experiment {experiment_id!r} belongs to this session")
        current = self._live_experiments.get(experiment_id)
        if current is not None and not current.done():
            return {"experiment_id": experiment_id, "status": "running", "already_running": True}
        from aibench.experiments.service import execute_experiment

        task = asyncio.create_task(
            execute_experiment(
                experiment_id,
                storage=self.storage,
                artifacts=self.artifacts,
                environ=self.environ,
            )
        )
        self._live_experiments[experiment_id] = task
        return {
            "experiment_id": experiment_id,
            "status": "running",
            "already_running": False,
            # execute_experiment starts READY records and continues RUNNING records.
            # Both entry paths are valid, but only the latter is a resume.
            "resume": record.status.value != "ready",
        }

    def start_experiment_holdout(self, experiment_id: str) -> dict[str, Any]:
        """Start the selected experiment's separately authorized protected evaluation."""
        prefix = f"{self.session_id}-exp-"
        if not experiment_id.startswith(prefix):
            raise SessionError("this conversation can evaluate only its own experiments")
        record = self.storage.get_experiment(experiment_id)
        if record is None:
            raise SessionError(f"no experiment {experiment_id!r} belongs to this session")
        current = self._live_experiments.get(experiment_id)
        if current is not None and not current.done():
            raise SessionError("the development experiment is still running")
        task_key = f"{experiment_id}:holdout"
        current = self._live_experiments.get(task_key)
        if current is not None and not current.done():
            return {"experiment_id": experiment_id, "status": "holdout_running", "already_running": True}
        from aibench.experiments.service import evaluate_protected_holdout

        task = asyncio.create_task(
            evaluate_protected_holdout(
                experiment_id,
                storage=self.storage,
                artifacts=self.artifacts,
                environ=self.environ,
            )
        )
        self._live_experiments[task_key] = task
        return {
            "experiment_id": experiment_id,
            "status": "holdout_running",
            "already_running": False,
            "resume": record.status.value == "holdout_running",
        }

    async def wait_for_experiment(self, experiment_id: str) -> Any:
        """Wait for an in-process experiment task, then return its committed record."""
        tasks = [
            self._live_experiments[key]
            for key in (experiment_id, f"{experiment_id}:holdout")
            if key in self._live_experiments
        ]
        task = next((item for item in tasks if not item.done()), tasks[-1] if tasks else None)
        if task is not None:
            await task
        return self.storage.get_experiment(experiment_id)

    def trace_evidence(self, run_id: str | None = None) -> dict[str, Any]:
        """Return imported trace totals for a run owned by this session.

        This is deliberately a read-only summary. Raw trace artifacts stay in the restricted
        artifact store and never enter the assistant briefing. Importing additional spans
        enriches the same stored run; it does not restart the application.
        """
        runs = self.session_runs()
        selected = run_id or (self.session.active_run_id if self.session.active_run_id in runs else None)
        if selected is None and runs:
            selected = runs[-1]
        if selected is None:
            return {
                "session_id": self.session_id,
                "run_id": None,
                "available": False,
                "reason": "this session has no stored application run yet",
                "trace_summary": None,
            }
        if selected not in runs:
            raise SessionError("trace evidence can only be read for a run started in this session")
        from aibench.services.traces import traces_summary

        summary = traces_summary(self.storage, selected)
        return {
            "session_id": self.session_id,
            "run_id": selected,
            "available": summary is not None,
            "reason": None if summary is not None else "no trace import is stored for this run",
            "trace_summary": summary,
        }

    def import_traces(self, file: str, run_id: str | None = None) -> dict[str, Any]:
        """Attach an OTLP/JSON trace export to a run this session started (the latest by
        default), as `aibench traces import` does: nothing is re-executed. The file must be
        inside the project or one of the policy's data roots; a relative path is read from
        the project."""
        from aibench.observations.otel import TraceFormatError
        from aibench.services.traces import import_traces

        runs = self.session_runs()
        selected = run_id or (runs[-1] if runs else None)
        if selected is None:
            raise SessionError("this session has no stored application run yet")
        if selected not in runs:
            raise SessionError("traces can only be imported into a run started in this session")
        path = Path(file)
        path = (path if path.is_absolute() else self.project_root / path).resolve()
        roots = (
            self.project_root.resolve(),
            *(Path(r).resolve() for r in self.policy().data_roots),
        )
        if not any(path.is_relative_to(root) for root in roots):
            raise SessionError(
                f"{file} is outside the project and the policy's data roots; "
                "copy the export into the project or add its folder to data_roots"
            )
        if not path.is_file():
            raise SessionError(f"no trace file at {path}")
        try:
            summary = import_traces(self.storage, self.artifacts, selected, path)
        except (TraceFormatError, OSError) as exc:
            raise SessionError(f"could not import {path.name}: {exc}") from exc
        return {"run_id": selected, **summary}

    def state(self, *, for_assistant: bool = False) -> dict[str, Any]:
        """The session as a person (or the assistant) reviews it; read from storage every
        time, so a resumed session shows actual project and run state."""
        session = self.session
        decision = self.current_decision()
        choices = decision.choices
        active = None
        if session.active_run_id:
            with contextlib.suppress(RunError):
                active = self.run_status(session.active_run_id, for_assistant=for_assistant)
        return {
            "session_id": session.session_id,
            "revision": session.revision,
            "presented_revision": session.presented_revision,
            "choices": {
                "application": choices.application,
                "dataset": choices.dataset,
                "objectives": list(choices.objectives),
                "objective_concepts": {k: list(v) for k, v in choices.objective_concepts.items()},
                "selection": choices.selection.model_dump(mode="json", exclude_defaults=True),
                "repetitions": choices.repetitions,
                "budgets": choices.budgets.model_dump(mode="json", exclude_none=True),
                "params": {k: deep_unfreeze(v) for k, v in choices.params.items()},
                "rules": {k: v.model_dump(mode="json") for k, v in choices.rules.items()},
            },
            "draft": draft_summary(deep_unfreeze(decision.draft), decision.choices.test_world),
            "open_questions": [
                q.model_dump(mode="json") for q in self.store.questions(self.session_id, "open")
            ],
            "active_run": active,
            "runs": self.session_runs(),
            "permissions": {
                "policy": session.policy_path or "built-in conservative default",
                "trusted_local": session.trusted_local,
            },
        }

    def record_command(
        self,
        text: str,
        *,
        message_id: str | None = None,
        decision_refs: tuple[str, ...] = (),
        action_refs: tuple[str, ...] = (),
    ) -> ConversationTurn:
        """Store a typed command (e.g. `/run`) as a user turn with the decisions and actions
        it produced, so the history keeps both the request and its interpreted effect."""
        turn, _ = self.store.append_turn(
            ConversationTurn(
                turn_id=f"turn-{uuid.uuid4().hex[:12]}",
                session_id=self.session_id,
                sequence=1,
                role="user",
                kind="command",
                content=sanitize(text),
                message_id=message_id or f"cmd-{uuid.uuid4().hex[:12]}",
                decision_refs=decision_refs,
                action_refs=action_refs,
            )
        )
        return turn

    def delete(self) -> dict[str, Any]:
        """Delete this conversation (§14). Refused while one of its runs is active. The
        runs it started — manifests, attempts, results, artifacts, events — are kept."""
        active = self.active_run()
        if active is not None:
            raise SessionError(f"run {active} is active; pause, stop or let it finish first")
        runs = self.session_runs()
        counts = self.store.delete_session(self.session_id)
        shutil.rmtree(self.directory, ignore_errors=True)  # draft plan files only
        return {"session_id": self.session_id, "deleted": counts, "runs_kept": runs}

    def mark_presented(self, revision: int) -> None:
        """The terminal showed this revision's draft to the user."""
        if revision == self.session.revision:
            self.store.update_session(self.session_id, presented_revision=revision)

    # ------------------------------------------------------------------ drafts

    def apply_patch(
        self,
        patch: PlanPatch,
        *,
        expected_revision: int,
        source: str = "user",
        source_turn_id: str | None = None,
    ) -> PatchResult:
        """Apply a typed change made against `expected_revision`, producing the next draft
        revision; never touches a run (an active run keeps its frozen plan)."""
        session = self.session
        active = self.active_run(session)
        if session.revision != expected_revision:
            return PatchResult(
                "stale",
                session.revision,
                problems=[str(StaleRevision(expected_revision, session.revision))],
                active_run=active,
            )
        stale = self._stale_answers(patch.answers, expected_revision)
        if stale:
            return PatchResult("stale", session.revision, problems=stale, active_run=active)
        current = self.current_decision()
        try:
            choices = apply_patch(
                current.choices,
                patch,
                Path(session.project_root),
                default_seed=_stable_seed(session.session_id),
            )
        except PatchRejected as exc:
            return PatchResult("rejected", session.revision, problems=exc.problems)
        if choices == current.choices:
            problems = ["the patch changes nothing in the current choices"]
            return PatchResult("unchanged", session.revision, problems=problems, active_run=active)
        revision = expected_revision + 1
        try:
            draft = build_draft(
                session,
                choices,
                revision=revision,
                directory=self.directory,
                supersedes=current.plan_hash,
            )
        except AibenchError as exc:
            problems = exc.problems if isinstance(exc, PatchRejected) else [str(exc)]
            return PatchResult("rejected", session.revision, problems=problems)
        # Settings for a metric that is not in this session's catalog would be saved and
        # then ignored, and the change reported as applied: say so instead.
        available = {option.evaluator_id for option in draft.inputs.catalog}
        unavailable = sorted(set(patch.params) - available)
        if unavailable:
            return PatchResult(
                "rejected",
                session.revision,
                problems=unavailable_settings(unavailable, sorted(available)),
            )
        dropped = unplanned_settings(patch, draft)
        if dropped:
            return PatchResult("rejected", session.revision, problems=dropped)
        decision = DecisionRecord(
            decision_id=f"{self.session_id}:d{revision}",
            session_id=self.session_id,
            source_turn_id=source_turn_id,
            source="assistant" if source == "assistant" else "user",
            revision=revision,
            supersedes=current.decision_id,
            structured_change=patch.model_dump(mode="json", exclude_defaults=True),
            choices=choices,
            plan_file=draft.plan_file,
            plan_hash=draft.plan_hash,
            executable=draft.executable,
            draft=draft.document.model_dump(mode="json"),
        )
        try:
            updated = self.store.commit_decision(
                decision,
                expected_revision=expected_revision,
                questions=draft.questions(),
                answered=patch.answers,
            )
        except StaleRevision as exc:
            return PatchResult("stale", exc.current, problems=[str(exc)], active_run=active)
        self._inputs = (revision, draft.inputs)
        return PatchResult("applied", updated.revision, decision=decision, active_run=active)

    def use_plugin_environments(
        self,
        environments: tuple[PluginEnvironmentRef, ...],
        defaults: dict[str, dict[str, Any]],
    ) -> PatchResult:
        """Make these plugin environments (and their parameter defaults) the session's, then
        redraft the current choices as the next revision, so the catalog, questions and
        metrics reflect what is now installed. Never touches a run."""
        self.store.update_session(
            self.session_id,
            plugin_environments=tuple(e.model_dump(mode="json") for e in environments),
            evaluator_defaults=defaults,
        )
        result = self._redraft({"plugin_environments": [e.python for e in environments]})
        assert result is not None  # a forced redraft always commits or reports why not
        return result

    def refresh_draft(self) -> PatchResult | None:
        """Redraft the current choices under the policy and catalog as they are now, as the
        next revision; None when that gives the same plan. A draft records the limits of the
        policy it was made under, so a policy edited afterwards (a raised call limit) would
        never reach a session that already exists. Never touches a run."""
        return self._redraft({"refreshed": "policy or catalog changed"}, only_if_different=True)

    def _redraft(
        self, change: dict[str, Any], *, only_if_different: bool = False
    ) -> PatchResult | None:
        session = self.session
        active = self.active_run(session)
        current = self.current_decision()
        revision = session.revision + 1
        try:
            draft = build_draft(
                session,
                current.choices,
                revision=revision,
                directory=self.directory,
                supersedes=current.plan_hash,
            )
        except AibenchError as exc:
            problems = exc.problems if isinstance(exc, PatchRejected) else [str(exc)]
            return PatchResult("rejected", session.revision, problems=problems)
        # The plan file names the dataset by path, so its hash is the same after the file's
        # cases changed; the draft's case counts and estimate are not. Compare the dataset too.
        unchanged_dataset = draft.document.dataset_hash == (current.draft or {}).get(
            "dataset_hash"
        )
        if only_if_different and draft.plan_hash == current.plan_hash and unchanged_dataset:
            return None
        decision = DecisionRecord(
            decision_id=f"{self.session_id}:d{revision}",
            session_id=self.session_id,
            source_turn_id=None,
            source="user",
            revision=revision,
            supersedes=current.decision_id,
            structured_change=change,
            choices=current.choices,
            plan_file=draft.plan_file,
            plan_hash=draft.plan_hash,
            executable=draft.executable,
            draft=draft.document.model_dump(mode="json"),
        )
        try:
            updated = self.store.commit_decision(
                decision,
                expected_revision=session.revision,
                questions=draft.questions(),
                answered=(),
            )
        except StaleRevision as exc:
            return PatchResult("stale", exc.current, problems=[str(exc)], active_run=active)
        self._inputs = (revision, draft.inputs)
        return PatchResult("applied", updated.revision, decision=decision, active_run=active)

    def _stale_answers(self, answers: tuple[str, ...], revision: int) -> list[str]:
        problems = []
        for question_id in answers:
            question = self.store.get_question(self.session_id, question_id)
            if question is None:
                problems.append(f"no question {question_id!r} was asked in this session")
            elif question.status != "open" or question.draft_revision != revision:
                problems.append(
                    f"question {question_id} was asked against revision "
                    f"{question.draft_revision} and is {question.status}; the draft is now at "
                    f"revision {self.session.revision}"
                )
        return problems

    def ask(self, questions: list[PendingQuestion]) -> list[PendingQuestion]:
        """Record clarifying questions asked in conversation, against the current revision."""
        revision = self.session.revision
        asked = [q.model_copy(update={"draft_revision": revision}) for q in questions]
        self.store.ask(self.session_id, asked)
        return asked

    def explain_metric(self, metric: str) -> dict[str, Any]:
        """Why a metric is (or is not) in the current draft, from the draft's own rationale,
        coverage and gaps — never from a model."""
        decision = self.current_decision()
        draft = deep_unfreeze(decision.draft)
        objectives = {o["objective_id"]: o["text"] for o in draft.get("objectives", [])}
        for choice in draft.get("rationale", []):
            if metric in (choice["metric"], choice["metric"].split("@", 1)[0]):
                coverage = next(
                    (c for c in draft.get("coverage", []) if c["metric"] == choice["metric"]),
                    None,
                )
                return {
                    "metric": choice["metric"],
                    "selected": True,
                    "revision": decision.revision,
                    "rationale": choice["rationale"],
                    "serves_objectives": [objectives.get(i, i) for i in choice["objective_ids"]],
                    "coverage": coverage,
                }
        option = next(
            (o for o in self.inputs().catalog if metric in (o.metric, o.evaluator_id)), None
        )
        return {
            "metric": metric,
            "selected": False,
            "revision": decision.revision,
            "installed": option is not None,
            "eligible": option.eligible if option else False,
            "reasons": list(option.reasons) if option else ["no installed evaluator by that id"],
            "gaps": draft.get("gaps", []),
        }

    # ------------------------------------------------------------------ actions

    def _new_action(
        self,
        kind: ActionKind,
        action_id: str,
        *,
        source: str,
        source_turn_id: str | None,
        expected_revision: int | None = None,
        run_id: str | None = None,
        authorization: str | None = None,
    ) -> tuple[ActionRequest, bool]:
        return self.store.record_action(
            ActionRequest(
                action_id=action_id,
                session_id=self.session_id,
                source_turn_id=source_turn_id,
                source="assistant" if source == "assistant" else "user",
                kind=kind,
                expected_revision=expected_revision,
                run_id=run_id,
                authorization=authorization,
            )
        )

    def active_run(self, session: BenchmarkSession | None = None) -> str | None:
        """The session's active run (§15: at most one), if any."""
        session = session or self.session
        run_id = session.active_run_id
        if run_id is None:
            return None
        if run_id.startswith(STARTING):
            action = self.store.get_action(run_id[len(STARTING) :])
            if action is None or action.state is not ActionState.REQUESTED:
                return None
            age = (utcnow() - action.created_at).total_seconds()
            return run_id if age < STARTING_TTL_SECONDS else None
        condition = self.run_condition(run_id)["condition"]
        return run_id if condition in LIVE_CONDITIONS else None

    def run_condition(self, run_id: str) -> dict[str, Any]:
        """What a run is really doing, from its stored status and its lease (10-T1). A run
        stored as `running` whose session died (killed, crashed, disconnected) is not
        active: it is `interrupted` and resumable. Work left in `unknown_effect` needs the
        user to reconcile the application's state; it is never repeated automatically."""
        record = self.storage.get_run(run_id)
        stored = record.status if record else "missing"
        live = self._live.get(run_id)
        if live is not None and not live.task.done():
            condition = "paused_here" if live.control.paused else "running_here"
        elif stored in ACTIVE_RUN_STATES:
            lease = lease_state(self.storage, run_id)
            if lease == "live":
                condition = "active_elsewhere"
            elif stored == "created" and lease is None and _age(record) < CREATED_GRACE_SECONDS:
                condition = "starting"  # its session takes the lease in its next step
            else:
                condition = "interrupted"
        else:
            condition = stored
        unknown = [
            w.task_key
            for w in self.storage.list_work_items(run_id)
            if w.state is WorkItemState.UNKNOWN_EFFECT
        ]
        return {
            "run_id": run_id,
            "stored_status": stored,
            "condition": condition,
            "resumable": condition == "interrupted"
            or (condition == stored and stored in RESUMABLE_STATES),
            "unknown_effect": unknown,
        }

    async def start_run(
        self,
        *,
        action_id: str,
        expected_revision: int,
        source: str = "user",
        source_turn_id: str | None = None,
        authorization: str | None = None,
    ) -> ActionRequest:
        """Run the reviewed draft at `expected_revision`. The run executes in the
        background; this returns once it is created and dispatching."""
        action, new = self._new_action(
            ActionKind.START_RUN,
            action_id,
            source=source,
            source_turn_id=source_turn_id,
            expected_revision=expected_revision,
            authorization=authorization,
        )
        if not new:
            return self._redelivered_start(action)  # never a second run
        session = self.session
        if session.revision != expected_revision:
            return self.store.settle_action(
                action,
                ActionState.REJECTED,
                reason=str(StaleRevision(expected_revision, session.revision))
                + "; review the current draft before running it",
            )
        active = self.active_run(session)
        if active is not None:
            return self.store.settle_action(
                action,
                ActionState.REJECTED,
                reason=f"run {active} is still active; pause, cancel or let it finish first "
                "(one active run per session)",
            )
        decision = self.current_decision()
        draft = deep_unfreeze(decision.draft)
        blocking = tuple(f for f in draft.get("findings", []) if f.get("blocking"))
        if any(f["kind"] == "missing_permission" for f in blocking):
            return self.store.settle_action(
                action,
                ActionState.DENIED,
                reason="the policy does not permit this plan; nothing was dispatched",
                findings=tuple(f for f in blocking if f["kind"] == "missing_permission"),
            )
        if blocking:
            return self.store.settle_action(
                action,
                ActionState.BLOCKED,
                reason="the draft is not executable yet; nothing was dispatched",
                findings=blocking,
            )
        try:
            compiled = compile_plan(
                self.directory / decision.plan_file,
                policy=self.policy(),
                trusted_local=session.trusted_local,
            )
        except PolicyDenied as exc:
            return self.store.settle_action(
                action,
                ActionState.DENIED,
                reason="the policy does not permit this plan; nothing was dispatched",
                findings=tuple({"kind": "missing_permission", "message": d} for d in exc.denials),
            )
        except PlanInvalid as exc:
            return self.store.settle_action(
                action,
                ActionState.BLOCKED,
                reason="the plan did not pass the execution gate; nothing was dispatched",
                findings=tuple({"kind": "invalid", "message": p} for p in exc.problems),
            )
        if compiled.plan_hash != decision.plan_hash:
            return self.store.settle_action(
                action,
                ActionState.REJECTED,
                reason="the plan file no longer matches the reviewed revision; nothing was "
                "dispatched",
            )
        # Claim the session's single run slot before creating anything, so a second
        # process starting a different action cannot also start a run (§15).
        slot = f"{STARTING}{action_id}"
        if not self.store.claim_active_run(
            self.session_id, expected=session.active_run_id, value=slot
        ):
            return self.store.settle_action(
                action, ActionState.REJECTED, reason="another run is starting in this session"
            )
        try:
            run_id = create_run(
                compiled,
                storage=self.storage,
                artifacts=self.artifacts,
                granted_by=self._granted_by(action_id, action.source),
            )
        except Exception as exc:  # noqa: BLE001 - any failure frees the slot and is reported
            self.store.claim_active_run(self.session_id, expected=slot, value=session.active_run_id)
            return self.store.settle_action(
                action, ActionState.REJECTED, reason=f"the run could not be created: {exc}"
            )
        if not self.store.claim_active_run(self.session_id, expected=slot, value=run_id):
            holder = self.session.active_run_id
            if holder != run_id:  # the starting window expired and another start took it
                return self._settle(
                    action,
                    ActionState.REJECTED,
                    run_id=run_id,
                    reason=f"run {run_id} was created but not started: the session's run "
                    f"slot was taken by {holder} meanwhile (/resume {run_id} later)",
                )
        failure = await self._launch(run_id, RunController())
        if failure is not None:
            return self._settle(
                action,
                ActionState.REJECTED,
                run_id=run_id,
                reason=f"run {run_id} was created but could not start: {failure}",
            )
        return self._settle(action, ActionState.DONE, run_id=run_id)

    def _settle(self, action: ActionRequest, state: ActionState, **fields: Any) -> ActionRequest:
        """Settle an action, or return how it was already settled: a concurrent
        redelivery may have recorded the same outcome first (10-G2)."""
        try:
            return self.store.settle_action(action, state, **fields)
        except ConflictError:
            stored = self.store.get_action(action.action_id)
            assert stored is not None
            return stored

    def _granted_by(self, action_id: str, source: str) -> str:
        return f"session {self.session_id}, action {action_id} ({source})"

    def _redelivered_start(self, action: ActionRequest) -> ActionRequest:
        """A start_run action delivered again (10-G2). Settled: its stored record. Still
        `requested` means its first delivery is starting now, or crashed midway: if that
        crash came after the run was created, the run's approval names this action, and
        the run is adopted instead of starting another; if no run was created and the start
        is older than the starting window, the request is closed so the user can start
        again. Nothing is ever dispatched twice."""
        if action.state is not ActionState.REQUESTED:
            return action
        if self.session.active_run_id in self._live:
            return action  # its first delivery is starting in this process right now
        marker = f"{STARTING}{action.action_id}"
        granted = self._granted_by(action.action_id, action.source)
        for row in self.storage.conn.execute("SELECT approval_id, data FROM approvals"):
            if json.loads(row["data"]).get("granted_by") == granted:
                run_id = str(row["approval_id"]).removesuffix(":approval")
                self.store.claim_active_run(self.session_id, expected=marker, value=run_id)
                return self._settle(
                    action,
                    ActionState.DONE,
                    run_id=run_id,
                    reason="recovered: the run was created before the previous session "
                    "ended; it was not started again (/resume continues it)",
                )
        age = (utcnow() - action.created_at).total_seconds()
        if age < STARTING_TTL_SECONDS:
            return action  # possibly still starting in another process
        self.store.claim_active_run(self.session_id, expected=marker, value=None)
        return self._settle(
            action,
            ActionState.REJECTED,
            reason="the start was interrupted before a run was created; start it again",
        )

    async def _launch(self, run_id: str, control: RunController) -> str | None:
        """Start executing a run in the background. `execute_run` takes the run's lease and
        verifies its frozen identities before its first suspension, so after one scheduler
        step a refusal (another session holds the lease, the run is not resumable, a
        frozen artifact fails verification) is known; it is returned instead of claiming
        the run started."""

        async def execute() -> RunOutcome | None:
            try:
                return await execute_run(
                    run_id,
                    storage=self.storage,
                    artifacts=self.artifacts,
                    controller=control,
                    environ=self.environ,
                )
            except Exception as exc:  # noqa: BLE001 - recorded, never lost with the task
                self._live[run_id].error = f"{type(exc).__name__}: {exc}"
                return None

        live = _LiveRun(asyncio.ensure_future(execute()), control)
        self._live[run_id] = live
        await asyncio.sleep(0)
        if live.task.done() and live.error is not None:
            return live.error
        return None

    async def control_run(
        self,
        kind: ActionKind,
        *,
        action_id: str,
        run_id: str | None = None,
        source: str = "user",
        source_turn_id: str | None = None,
        authorization: str | None = None,
    ) -> ActionRequest:
        """Pause, resume or cancel one of this session's runs through the engine's run
        control. Recorded as an action request and a run event; never a plan change."""
        assert kind is not ActionKind.START_RUN
        target = run_id or self.session.active_run_id
        action, new = self._new_action(
            kind,
            action_id,
            source=source,
            source_turn_id=source_turn_id,
            run_id=target,
            authorization=authorization,
        )
        if not new:
            return action
        if target is None or target not in self.session_runs():
            return self.store.settle_action(
                action, ActionState.REJECTED, reason="no run of this session to control"
            )
        record = self.storage.get_run(target)
        status = record.status if record else "missing"
        live = self._live.get(target)
        running_here = live is not None and not live.task.done()
        applied = False
        if kind is not ActionKind.CANCEL_RUN and running_here:
            assert live is not None
            if live.control.stopping:
                reason = f"run {target} is already stopping"
            elif (kind is ActionKind.PAUSE_RUN) == live.control.paused:
                reason = f"run {target} is already {'paused' if live.control.paused else 'running'}"
            else:
                reason = None
            if reason is not None:
                return self.store.settle_action(action, ActionState.REJECTED, reason=reason)
            (live.control.pause if kind is ActionKind.PAUSE_RUN else live.control.resume)()
            applied = True
        elif kind is ActionKind.CANCEL_RUN and running_here:
            assert live is not None
            live.control.cancel()
            applied = True
        elif kind in (ActionKind.RESUME_RUN, ActionKind.CANCEL_RUN) and status in RESUMABLE_STATES:
            # Not executing in this process (e.g. the session was reopened): continue the
            # run under its frozen identities, or settle its unstarted work as cancelled.
            current = self.session.active_run_id
            other = self.active_run()
            if other not in (None, target) or not self.store.claim_active_run(
                self.session_id, expected=current, value=target
            ):
                return self.store.settle_action(
                    action,
                    ActionState.REJECTED,
                    reason=f"run {other or current} is active in this session",
                )
            control = RunController()
            if kind is ActionKind.CANCEL_RUN:
                control.request("cancel")
            failure = await self._launch(target, control)
            if failure is not None:
                return self.store.settle_action(
                    action, ActionState.REJECTED, reason=f"run {target} did not continue: {failure}"
                )
            applied = True
        if not applied:
            return self.store.settle_action(
                action,
                ActionState.REJECTED,
                reason=f"run {target} is {status}; cannot {kind.value.split('_')[0]} it",
            )
        self.storage.append_run_event(
            target,
            "control_requested",
            {
                "action": kind.value,
                "action_id": action_id,
                "session_id": self.session_id,
                "source": action.source,
            },
        )
        return self.store.settle_action(action, ActionState.DONE)

    async def wait_for_run(self, run_id: str) -> RunOutcome | None:
        live = self._live.get(run_id)
        return await live.task if live else None

    def live_runs(self) -> list[str]:
        """Runs executing in this process right now."""
        return [run_id for run_id, live in self._live.items() if not live.task.done()]

    def run_error(self, run_id: str) -> str | None:
        live = self._live.get(run_id)
        return live.error if live else None

    # ------------------------------------------------------------------ results

    def _run_id(self, run_id: str | None) -> str:
        target = run_id or self.session.active_run_id
        if target is None:
            raise RunError("this session has not started a run")
        if target not in self.session_runs():
            raise RunError(f"run {target} was not started in this session")
        return target

    def run_status(
        self, run_id: str | None = None, *, for_assistant: bool = False
    ) -> dict[str, Any]:
        """Committed engine state as a timestamped snapshot; partial while the run is
        active (§15: label partial aggregates provisional). Needs no model. For the
        assistant, free-text reasons are reduced to codes unless the policy shares case
        content."""
        target = self._run_id(run_id)
        status = run_status(self.storage, target)
        share = not for_assistant or self.policy().share_case_content_with_assistant
        if not share:
            status["needs_attention"] = [
                {**item, "reason": reason_code(item["reason"])}
                for item in status["needs_attention"]
            ]
        session_error = self.run_error(target)
        if not share:
            session_error = reason_code(session_error)
        condition = self.run_condition(target)
        return {
            **status,
            "condition": condition["condition"],
            "resumable": condition["resumable"],
            "as_of": utcnow().isoformat(),
            "provisional": condition["condition"] in LIVE_CONDITIONS,
            "partial": status["status"] != "completed",
            "session_error": session_error,
        }

    def run_events(self, run_id: str | None = None, after: int = 0) -> list[dict[str, Any]]:
        """Committed run events after a sequence number, so a client can replay what it
        missed without running anything twice (§14)."""
        return self.storage.list_run_events(self._run_id(run_id), after=after)

    def missed_events(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Events committed since the user last saw this run (the session's cursor).
        Replaying them only displays history; no action is repeated (10-T1)."""
        target = self._run_id(run_id)
        cursor = self.session.event_cursors.get(target, 0)
        return self.storage.list_run_events(target, after=cursor)

    def acknowledge_events(self, run_id: str, sequence: int) -> None:
        """Record that the user has seen this run's events up to `sequence`."""
        self.store.advance_event_cursor(self.session_id, run_id, sequence)

    def reconcile(self, *, recent_turns: int = 6) -> dict[str, Any]:
        """The authoritative picture on reopening a session (10-T1): conversation, current
        draft, open questions and each run's real condition from storage — with the events
        missed since last seen. It reads only: nothing is dispatched or repeated, and an
        interrupted run stays stopped until a new resume action (10-G1)."""
        session = self.session
        runs, notes, attention = [], [], []
        for run_id in self.session_runs():
            condition = self.run_condition(run_id)
            missed = self.missed_events(run_id)
            condition["missed_events"] = len(missed)
            condition["last_sequence"] = missed[-1]["sequence"] if missed else None
            runs.append(condition)
            if condition["condition"] == "interrupted":
                if condition["stored_status"] == "cancelling":
                    notes.append(
                        f"run {run_id} was being cancelled when its session ended; /stop "
                        "finishes the cancellation (nothing restarted it)."
                    )
                else:
                    notes.append(
                        f"run {run_id} was {condition['stored_status']} when its session "
                        "ended; nothing restarted it. /resume continues it under its frozen "
                        "plan."
                    )
            elif condition["condition"] == "active_elsewhere":
                notes.append(f"run {run_id} is being executed by another live session")
            for task_key in condition["unknown_effect"]:
                attention.append({"run_id": run_id, "task_key": task_key})
        if attention:
            notes.append(
                f"{len(attention)} item(s) may have reached the application before a crash "
                "(unknown effect); check the application's state before repeating them."
            )
        state = self.state()
        return {
            "session_id": session.session_id,
            "revision": session.revision,
            "draft": state["draft"],
            "open_questions": state["open_questions"],
            "active_run": self.active_run(session),
            "runs": runs,
            "unknown_effect": attention,
            "notes": notes,
            "recent_turns": [
                {"role": t.role, "kind": t.kind, "content": t.content}
                for t in self.store.turns(self.session_id, last=recent_turns)
            ],
        }

    def budget(self, run_id: str | None = None) -> dict[str, Any]:
        """Ceilings and committed spend of one of this session's runs (`/budget`), plus the
        conversation's own model usage, which is tracked per turn, not against the run."""
        target = self._run_id(run_id)
        usage: dict[str, int] = {}
        for turn in self.store.turns(self.session_id):
            for name, value in (deep_unfreeze(turn.outcome) or {}).get("usage", {}).items():
                usage[name] = usage.get(name, 0) + int(value)
        return {**run_budget(self.storage, self.artifacts, target), "conversation": usage}

    async def rescore(
        self, run_id: str | None = None, *, carry_forward: bool = True
    ) -> dict[str, Any]:
        """Rescore this session's stored executions with its current validated draft.

        This delegates to the same policy-checked service as `aibench evaluate`; that
        service reads stored executions and cannot invoke the application runner. By
        default results the run already finished are carried forward and only what is
        missing or failed is evaluated (a rate-limited judge is asked about the failed
        cases, not all of them); `carry_forward=False` evaluates everything again.
        """
        target = run_id
        if target is None:
            runs = self.session_runs()
            if not runs:
                raise RunError("this session has not started a run")
            target = runs[-1]
        target = self._run_id(target)
        condition = self.run_condition(target)["condition"]
        if condition in LIVE_CONDITIONS:
            raise RunError(f"run {target} is still active; rescore it after execution stops")
        decision = self.current_decision()
        report = await evaluate_run(
            target,
            self.directory / decision.plan_file,
            storage=self.storage,
            artifacts=self.artifacts,
            policy=self.policy().with_trusted_local(self.session.trusted_local),
            carry_forward=carry_forward,
        )
        return {
            "run_id": report.run_id,
            "scoring_id": report.scoring_id,
            "summaries": [summary.as_dict() for summary in report.summaries],
            "warnings": report.warnings,
            "application_invoked": False,
            "carried_forward": report.carried,
            "budget": report.budget,
            "quotas": report.quotas,
            "stop_reason": report.stop_reason,
            "evaluated_now": len(report.results) - report.carried,
        }

    def report(self, run_id: str | None = None, *, for_assistant: bool = False) -> dict[str, Any]:
        """The run's report document, from stored facts only (services.reports): nothing
        is rerun. The assistant gets case excerpts only when the policy shares content."""
        share = not for_assistant or self.policy().share_case_content_with_assistant
        return build_report(
            self.storage, self.artifacts, self._run_id(run_id), include_content=share
        )

    def report_facts(
        self, run_id: str | None = None, *, for_assistant: bool = False
    ) -> dict[str, Any]:
        """The report's aggregates (`/report`, the assistant's `get_report`)."""
        return report_facts(self.report(run_id, for_assistant=for_assistant))

    def export_report(
        self, run_id: str | None = None, formats: tuple[str, ...] = ("html", "json")
    ) -> dict[str, Any]:
        """Write the report files under `.aibench/reports/RUN_ID/` (sanitized content)."""
        document = self.report(run_id)
        run = document["run"]
        paths = export_report(
            document, list(formats), report_dir(self.workspace_root, run["run_id"])
        )
        return {
            "run_id": run["run_id"],
            "status": run["status"],
            "provisional": run["provisional"],
            "partial": run["partial"],
            "as_of_event_sequence": document["as_of_event_sequence"],
            "paths": paths,
            "outcome": document["outcome"],
        }

    def compare_runs(
        self,
        baseline_run_id: str,
        current_run_id: str,
        *,
        baseline_scoring_id: str | None = None,
        current_scoring_id: str | None = None,
        mode: str = "strict",
        min_paired_coverage: float = 0.95,
        bootstrap_seed: int = 0,
        bootstrap_replicates: int = 2_000,
        for_assistant: bool = False,
    ) -> dict[str, Any]:
        """Compare two runs started by this session through the shared stored-only service."""
        baseline = self._run_id(baseline_run_id)
        current = self._run_id(current_run_id)
        if mode not in {"strict", "exploratory"}:
            raise SessionError("comparison mode must be 'strict' or 'exploratory'")
        report = compare_stored_runs(
            self.storage,
            self.artifacts,
            baseline,
            current,
            baseline_scoring_id=baseline_scoring_id,
            current_scoring_id=current_scoring_id,
            mode=cast(Literal["strict", "exploratory"], mode),
            min_paired_coverage=min_paired_coverage,
            bootstrap_seed=bootstrap_seed,
            bootstrap_replicates=bootstrap_replicates,
        )
        return _comparison_for_assistant(report) if for_assistant else report

    async def judge_runs(
        self, baseline_run_id: str, current_run_id: str, criteria: str
    ) -> dict[str, Any]:
        """Which run's answers a judge prefers, case by case, by the user's `criteria`
        (DeepEval's ArenaGEval, each case judged in both orders). Reads recorded answers only.
        The judge, its plugin environment and the policy are the session plan's: the plan is
        compiled through the execution gate first, so a judge the policy would not permit in
        a run is not used here either."""
        from aibench.core.models import MetricBinding
        from aibench.security.policy import evaluator_denials
        from aibench.services import arena

        criteria = criteria.strip()
        if not criteria:
            raise SessionError('state what makes an answer better: --judge "CRITERIA"')
        baseline = self._run_id(baseline_run_id)
        current = self._run_id(current_run_id)
        decision = self.current_decision()
        try:
            compiled = compile_plan(
                self.directory / decision.plan_file,
                policy=self.policy(),
                trusted_local=self.session.trusted_local,
            )
        except (PolicyDenied, PlanInvalid) as exc:
            raise SessionError(f"the current plan cannot load its judge: {exc}") from exc
        judge = next(
            (
                dict(deep_unfreeze(m.binding.params)).get("judge")
                for m in compiled.metrics
                if m.manifest.evaluator_id.startswith("deepeval.")
                and dict(deep_unfreeze(m.binding.params) or {}).get("judge")
            ),
            None,
        ) or ((self.session.evaluator_defaults or {}).get("deepeval.*") or {}).get("judge")
        if not judge:
            raise SessionError(
                "no DeepEval judge in this session: /plugins install deepeval sets one up"
            )
        try:
            metric = compiled.registry.resolve_binding(
                MetricBinding(metric=arena.ARENA, params={"judge": judge, "criteria": criteria})
            )
        except AibenchError as exc:
            raise SessionError(f"the head-to-head judge is not available: {exc}") from exc
        denials = evaluator_denials(compiled.policy, [metric.manifest])
        if denials:
            raise SessionError("; ".join(denials))
        found = arena.pairs(self.storage, baseline, current)
        if not found:
            raise SessionError("the two runs have no case both answered")
        return await arena.judge(metric, found)

    def _scoring_id(self, run_id: str) -> str:
        record = self.storage.get_run(run_id)
        if record is None:
            raise RunError(f"no run committed with run_id={run_id!r}")
        return str(record.manifest.parameters["scoring_id"])

    def failures(
        self, run_id: str | None = None, *, for_assistant: bool = False, limit: int = 20
    ) -> dict[str, Any]:
        """Failed, indeterminate and errored results, plus application failures."""
        target = self._run_id(run_id)
        share = not for_assistant or self.policy().share_case_content_with_assistant
        results = self.storage.list_metric_results(target, scoring_id=self._scoring_id(target))
        failed = [
            r
            for r in results
            if r.decision in (Decision.FAIL, Decision.INDETERMINATE)
            or r.status is ExecutionStatus.ERROR
        ]
        failed.sort(key=lambda r: (r.case_id, r.repetition_id, r.metric_id))
        executions = select_final_executions(self.storage.list_execution_attempts(target))
        app_failures = [e for e in executions if e.status is not ExecutionStatus.OK]
        status = self.run_status(target)
        return {
            "run_id": target,
            "as_of": status["as_of"],
            "run_status": status["status"],
            "provisional": status["provisional"],
            "metric_failures": [
                {
                    "case_id": r.case_id,
                    "repetition": r.repetition_id,
                    "metric": f"{r.metric_id}@{r.metric_version}",
                    "decision": r.decision.value,
                    "status": r.status.value,
                    "value": deep_unfreeze(r.value.value) if r.value else None,
                    "reason": r.reason if share else reason_code(r.reason),
                }
                for r in failed[:limit]
            ],
            "application_failures": [
                {
                    "case_id": e.case_id,
                    "repetition": e.repetition_id,
                    "status": e.status.value,
                    "error_kind": e.error_kind.value if e.error_kind else None,
                    "error": e.error if share else None,
                }
                for e in app_failures[:limit]
            ],
            "total_metric_failures": len(failed),
            "total_application_failures": len(app_failures),
            "case_content": "included" if share else "withheld by policy",
        }

    def case_evidence(
        self, case_id: str, run_id: str | None = None, *, for_assistant: bool = False
    ) -> dict[str, Any]:
        """One case's evidence: the app's recorded executions and each metric's result.
        For the user this includes the Golden; the assistant never receives reference
        answers or other judge-only fields, and sees inputs and outputs only when the
        policy allows it."""
        target = self._run_id(run_id)
        share = not for_assistant or self.policy().share_case_content_with_assistant
        executions = select_final_executions(self.storage.list_execution_attempts(target, case_id))
        if not executions and not self.storage.list_metric_results(target, case_id):
            raise RunError(f"run {target} has no recorded work for case {case_id!r}")
        results = self.storage.list_metric_results(
            target, case_id, scoring_id=self._scoring_id(target)
        )
        evidence: dict[str, Any] = {
            "run_id": target,
            "case_id": case_id,
            "run_status": self.run_status(target)["status"],
            "executions": [
                {
                    "repetition": e.repetition_id,
                    "attempt": e.attempt_id,
                    "status": e.status.value,
                    "error_kind": e.error_kind.value if e.error_kind else None,
                    "timing": deep_unfreeze(e.timing),
                    **(
                        {
                            "output": deep_unfreeze(e.output),
                            "error": e.error,
                            "retrieved_context": list(e.retrieved_context or ()),
                            "tool_events": deep_unfreeze(e.tool_events),
                        }
                        if share
                        else {}
                    ),
                }
                for e in executions
            ],
            "results": [
                {
                    "metric": f"{r.metric_id}@{r.metric_version}",
                    "repetition": r.repetition_id,
                    "decision": r.decision.value,
                    "status": r.status.value,
                    "value": deep_unfreeze(r.value.value) if r.value else None,
                    "reason": r.reason if share else reason_code(r.reason),
                    "evidence_refs": list(r.evidence_refs),
                }
                for r in sorted(results, key=lambda r: (r.repetition_id, r.metric_id))
            ],
            "case_content": "included" if share else "withheld by policy",
        }
        if not for_assistant:
            record = self.storage.get_run(target)
            assert record is not None
            golden = next(
                (
                    c
                    for c in self.storage.list_cases(record.manifest.dataset_hash)
                    if c.case_id == case_id
                ),
                None,
            )
            evidence["golden"] = golden.model_dump(mode="json") if golden else None
        return evidence
