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
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aibench.core.errors import AibenchError
from aibench.core.models import Decision, ExecutionStatus, deep_unfreeze, utcnow
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
from aibench.planning.planner import PlanningInputs
from aibench.security.policy import ExecutionPolicy
from aibench.services.runs import (
    RESUMABLE_STATES,
    RunError,
    create_run,
    execute_run,
    run_budget,
    run_report,
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


# The run slot is claimed with this marker while a run is being created, so two processes
# cannot both start one; a marker older than this belongs to a start that crashed midway.
STARTING = "starting:"
STARTING_TTL_SECONDS = 600.0


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
            "draft": draft_summary(deep_unfreeze(self.decision.draft)) if self.decision else None,
            "problems": self.problems,
            "active_run_unchanged": self.active_run,
        }


@dataclass
class _LiveRun:
    task: asyncio.Task[RunOutcome | None]
    control: RunController
    error: str | None = None


def _stable_seed(session_id: str) -> int:
    return int(hashlib.sha256(session_id.encode()).hexdigest()[:8], 16) % (2**31)


def reason_code(reason: str | None) -> str | None:
    """The machine code at the start of a result reason (e.g. `not_applicable`), or None.
    Free text (a judge's explanation, an application's error) may quote case content, so
    only a code is shown where case content is withheld."""
    if not reason:
        return None
    code = reason.split(":", 1)[0].strip()
    return code if re.fullmatch(r"[a-z][a-z0-9_]{0,40}", code) else None


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

    def inputs(self) -> PlanningInputs:
        """Evidence for the current revision (profile, dataset counts, catalog), cached."""
        decision = self.current_decision()
        if self._inputs is None or self._inputs[0] != decision.revision:
            inputs = planning_inputs(
                self.session, decision.choices, revision=decision.revision, directory=self.directory
            )
            self._inputs = (decision.revision, inputs)
        return self._inputs[1]

    def session_runs(self) -> list[str]:
        """Runs this session started, oldest first."""
        return [
            a.run_id
            for a in self.store.list_actions(self.session_id)
            if a.kind is ActionKind.START_RUN and a.run_id
        ]

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
            "draft": draft_summary(deep_unfreeze(decision.draft)),
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
                content=text,
                message_id=message_id or f"cmd-{uuid.uuid4().hex[:12]}",
                decision_refs=decision_refs,
                action_refs=action_refs,
            )
        )
        return turn

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
        record = self.storage.get_run(run_id)
        if record is not None and record.status in ACTIVE_RUN_STATES:
            return run_id
        if run_id in self._live and not self._live[run_id].task.done():
            return run_id
        return None

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
            return action  # redelivered: never start a second run
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
                granted_by=f"session {self.session_id}, action {action_id} ({action.source})",
            )
        except Exception as exc:  # noqa: BLE001 - any failure frees the slot and is reported
            self.store.claim_active_run(self.session_id, expected=slot, value=session.active_run_id)
            return self.store.settle_action(
                action, ActionState.REJECTED, reason=f"the run could not be created: {exc}"
            )
        self.store.claim_active_run(self.session_id, expected=slot, value=run_id)
        failure = await self._launch(run_id, RunController())
        if failure is not None:
            return self.store.settle_action(
                action,
                ActionState.REJECTED,
                run_id=run_id,
                reason=f"run {run_id} was created but could not start: {failure}",
            )
        return self.store.settle_action(action, ActionState.DONE, run_id=run_id)

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
        return {
            **status,
            "as_of": utcnow().isoformat(),
            "provisional": status["status"] in ACTIVE_RUN_STATES,
            "partial": status["status"] != "completed",
            "session_error": session_error,
        }

    def run_events(self, run_id: str | None = None, after: int = 0) -> list[dict[str, Any]]:
        """Committed run events after a sequence number, so a client can replay what it
        missed without running anything twice (§14)."""
        return self.storage.list_run_events(self._run_id(run_id), after=after)

    def budget(self, run_id: str | None = None) -> dict[str, Any]:
        """Ceilings and committed spend of one of this session's runs (`/budget`), plus the
        conversation's own model usage, which is tracked per turn, not against the run."""
        target = self._run_id(run_id)
        usage: dict[str, int] = {}
        for turn in self.store.turns(self.session_id):
            for name, value in (deep_unfreeze(turn.outcome) or {}).get("usage", {}).items():
                usage[name] = usage.get(name, 0) + int(value)
        return {**run_budget(self.storage, self.artifacts, target), "conversation": usage}

    def report(self, run_id: str | None = None) -> dict[str, Any]:
        """The machine-readable run summary (`/report`); rendered reports are Prompt 11."""
        return run_report(self.storage, self.artifacts, self._run_id(run_id))

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
