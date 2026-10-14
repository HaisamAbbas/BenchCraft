"""Deterministic run engine (§15, 06-T1..T4).

Work graph: one execution item per (case, repetition), and one evaluation item per
(execution, metric binding) that depends on it. The orchestration loop owns all scheduling:

- bounded concurrency: at most `concurrency.application` executions and
  `concurrency.evaluation` evaluations in flight; nothing else is materialized as a task;
- single writer: one SQLite connection, used only on this event-loop thread; work-item
  transitions and run events are written by this loop, attempt records by the service
  calls it awaits — commits are synchronous and never interleave;
- crash-consistent ordering: an item is marked `running` (with its attempt number) before
  dispatch, the attempt record is committed, then the item moves on. Every transition is a
  compare-and-set, so a stale or duplicate transition cannot overwrite a newer one;
- budgets are reserved before dispatch and reconciled after (BudgetLedger);
- retries only as `retry.classify_*` allow, with seeded backoff; unknown effects stop;
- run control: pause stops new dispatch and lets in-flight work finish; cancel stops
  dispatch and cancels in-flight work (recorded as cancelled); interrupt stops dispatch,
  records in-flight outcomes and leaves unfinished work pending for `resume`. A second
  interrupt also aborts in-flight work, but unfinished work still stays resumable: an
  aborted effect-free execution returns to pending, an ambiguous effectful one becomes
  `unknown_effect`;
- the session holds the run's lease (see `services.runs`) and heartbeats it; if the lease
  is lost, the session stops dispatching as if interrupted.
"""

from __future__ import annotations

import asyncio
import errno
import heapq
import itertools
import random
import sqlite3
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar

from aibench.core.models import (
    ApplicationSpec,
    BenchmarkCase,
    EffectState,
    ErrorKind,
    ExecutionResult,
    ExecutionStatus,
    UsageEvent,
    UsageRole,
    WorkItem,
    WorkItemState,
)
from aibench.core.plans import ExecutablePlan
from aibench.engine import cache
from aibench.engine.budget import BudgetLedger
from aibench.engine.load_profile import arrival_offset_seconds
from aibench.engine.quota import QuotaGate
from aibench.engine.retry import (
    backoff_delay,
    classify_evaluation,
    classify_execution,
    http_status,
)
from aibench.registry import ResolvedMetric
from aibench.runners.base import BaseRunner, ResetReport, race
from aibench.services.execution import invoke_and_record
from aibench.services.scoring import (
    BindingScorer,
    MissingExecution,
    episode_prefixes,
    worker_budget,
)
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.repositories import RunControlState, Storage

TERMINAL = frozenset(
    {
        WorkItemState.SUCCEEDED,
        WorkItemState.FAILED,
        WorkItemState.BLOCKED,
        WorkItemState.CANCELLED,
        WorkItemState.UNKNOWN_EFFECT,
    }
)
_POLL_SECONDS = 0.1
_CONTROL_POLL_SECONDS = 0.25
_WORK_QUEUE_PAGE_SIZE = 500
# A due retry may join a page that was already prefetched. The delayed heap itself is capped
# at one page, so this is the maximum queue size after promotion.
_WORK_QUEUE_MAX_BUFFER = _WORK_QUEUE_PAGE_SIZE * 2
_CASE_CACHE_MAX_ITEMS = 512
_CASE_CACHE_MAX_BYTES = 4 * 1024 * 1024


class StoredCaseMap(Mapping[str, BenchmarkCase]):
    """A run's Goldens, loaded from SQLite through a small least-recently-used cache.

    Work scheduling needs every case ID, but only a few case bodies are in use at once.
    Keeping the selected IDs plus at most 512 parsed bodies under a 4 MiB serialized-size
    budget avoids rebuilding a full multimodal case map when a run starts or resumes.
    """

    def __init__(
        self,
        storage: Storage,
        dataset_content_hash: str,
        case_ids: Iterator[str] | list[str] | tuple[str, ...],
        *,
        max_cached_items: int = _CASE_CACHE_MAX_ITEMS,
        max_cached_bytes: int = _CASE_CACHE_MAX_BYTES,
    ) -> None:
        if max_cached_items < 1 or max_cached_bytes < 1:
            raise ValueError("case cache limits must be positive")
        self._storage = storage
        self._dataset_content_hash = dataset_content_hash
        self._case_ids = tuple(case_ids)
        self._case_id_set = frozenset(self._case_ids)
        if len(self._case_ids) != len(self._case_id_set):
            raise ValueError("case_ids must be unique")
        self._max_cached_items = max_cached_items
        self._max_cached_bytes = max_cached_bytes
        self._cached_bytes = 0
        self._cache: OrderedDict[str, tuple[BenchmarkCase, int]] = OrderedDict()

    def __getitem__(self, case_id: str) -> BenchmarkCase:
        if case_id not in self._case_id_set:
            raise KeyError(case_id)
        cached = self._cache.pop(case_id, None)
        if cached is None:
            stored = self._storage.get_case_with_size(self._dataset_content_hash, case_id)
            if stored is None:
                raise KeyError(case_id)
            case, size_bytes = stored
            if size_bytes <= self._max_cached_bytes:
                while self._cache and (
                    len(self._cache) >= self._max_cached_items
                    or self._cached_bytes + size_bytes > self._max_cached_bytes
                ):
                    _, (_, evicted_bytes) = self._cache.popitem(last=False)
                    self._cached_bytes -= evicted_bytes
                self._cache[case_id] = (case, size_bytes)
                self._cached_bytes += size_bytes
        else:
            case, size_bytes = cached
            self._cache[case_id] = (case, size_bytes)
        return case

    def __iter__(self) -> Iterator[str]:
        return iter(self._case_ids)

    def __len__(self) -> int:
        return len(self._case_ids)


_ENGINE_CACHE_SIZE = 500
_T = TypeVar("_T")
# Evaluations held by a quota that dispatch looks past per pass (bounded work per pass).
_HELD_SCAN = 256
# The runner bounds a reset by its lifecycle timeout; this only waits for an abort.
_RESET_WAIT_SECONDS = 3600.0
_HEARTBEAT_SECONDS = 2.0


class RunState(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    INTERRUPTING = "interrupting"
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


def execution_key(case_id: str, repetition: int) -> str:
    return f"exec:{case_id}:r{repetition}"


def evaluation_key(case_id: str, repetition: int, binding_hash: str) -> str:
    return f"eval:{case_id}:r{repetition}:{binding_hash[7:23]}"


def parse_work_item_key(task_key: str, kind: str) -> tuple[str, int, str | None]:
    """Decode a work key while allowing colons in a dataset's case ID.

    The repetition and binding suffixes are generated by the harness, so parse them from
    the right edge instead of treating every colon as a delimiter.
    """
    prefix = {"execution": "exec:", "evaluation": "eval:"}.get(kind)
    if prefix is None or not task_key.startswith(prefix):
        raise ValueError(f"invalid {kind} work-item key: {task_key!r}")
    body = task_key[len(prefix) :]
    binding_key: str | None = None
    if kind == "evaluation":
        try:
            body, binding_key = body.rsplit(":", 1)
        except ValueError as exc:
            raise ValueError(f"invalid {kind} work-item key: {task_key!r}") from exc
    try:
        case_id, repetition_text = body.rsplit(":r", 1)
        if not case_id or not repetition_text.isdecimal():
            raise ValueError
        return case_id, int(repetition_text), binding_key
    except ValueError as exc:
        raise ValueError(f"invalid {kind} work-item key: {task_key!r}") from exc


class RunController:
    """Internal run control (06-T4) for commands now and the chat layer later. Methods are
    called on the event loop; `request(...)` is safe from other threads (signal handlers),
    and requests made before the engine starts are applied when it does.

    `cancelled` means the run is cancelled: unstarted work is recorded as cancelled.
    `abort_event` aborts in-flight invocations and evaluations; cancel sets it, and so does
    a second interrupt, which still leaves unstarted work pending for resume."""

    def __init__(self) -> None:
        self.state = RunState.CREATED
        self.abort_event = asyncio.Event()
        self._wake = asyncio.Event()
        self.cancelled = False
        self.interrupting = False
        self.paused = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._early: list[str] = []

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        early, self._early = self._early, []
        for action in early:
            getattr(self, action)()

    def pause(self) -> None:
        if not self.cancelled and not self.abort_event.is_set():
            self.interrupting = False
            self.paused = True
            self._wake.set()

    def resume(self) -> None:
        if self.cancelled or self.abort_event.is_set():
            return
        self.interrupting = False
        self.paused = False
        self._wake.set()

    def cancel(self) -> None:
        self.cancelled = True
        self.abort_event.set()
        self.paused = False
        self._wake.set()

    def interrupt(self) -> None:
        """First call: stop dispatch, let in-flight work finish, keep the rest resumable.
        A second call also aborts in-flight work; unfinished work stays resumable."""
        if self.interrupting:
            self.abort_event.set()
        self.interrupting = True
        self.paused = False
        self._wake.set()

    @property
    def stopping(self) -> bool:
        return self.cancelled or self.interrupting

    def request(self, action: str) -> None:
        if self._loop is None:
            self._early.append(action)  # a signal during setup: applied on bind
        else:
            self._loop.call_soon_threadsafe(getattr(self, action))

    def next_change(self) -> asyncio.Task[bool]:
        """A task that completes at the next control change (pause, resume, cancel,
        interrupt)."""
        self._wake.clear()
        return asyncio.ensure_future(self._wake.wait())

    async def wait_for_change(self, timeout: float) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except TimeoutError:
            pass


@dataclass
class _Item:
    task_key: str
    kind: str  # "execution" | "evaluation"
    case_id: str
    repetition: int
    attempt: int
    binding_hash: str | None = None
    warmup: bool = False
    arrival_ordinal: int | None = None
    arrival_offset_seconds: float | None = None


class ResetFailed(Exception):
    """The application could not be reset to the state a case needs (or the run was
    aborted while it was being reset)."""

    def __init__(self, detail: str, *, cancelled: bool = False) -> None:
        super().__init__(detail)
        self.cancelled = cancelled


@dataclass
class RunOutcome:
    state: RunState
    counts: dict[str, dict[str, int]]
    budget: dict[str, Any]
    stop_reason: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class RunEngine:
    storage: Storage
    artifacts: ArtifactStore
    run_id: str
    plan: ExecutablePlan
    application: ApplicationSpec
    runner: BaseRunner | None  # None when no execution work remains (e.g. resume)
    cases: Mapping[str, BenchmarkCase]
    metrics: list[ResolvedMetric]
    scoring_id: str
    ledger: BudgetLedger
    controller: RunController
    rng: random.Random
    heartbeat: Callable[[], bool] = lambda: True  # refresh the run lease; False if lost
    run_control: Callable[[], RunControlState | None] = lambda: None
    # State between cases (§7): "none", "per_case" (reset before every attempt) or
    # "per_episode" (reset before an episode's first turn; turns run in order and share
    # state). The engine resets with the selected test world's seed.
    reset_mode: str = "none"
    world_id: str | None = None
    world_seed: Any = None
    # The dependency identity frozen by the run; every engine result must carry it.
    dependency_lock_hash: str | None = None
    # Opt-in caches (16-T3): the key parts that are fixed for the run, or None when off.
    execution_cache: dict[str, Any] | None = None
    evaluation_cache_policy: str | None = None
    _exec_cache_keys: dict[str, str] = field(default_factory=dict)
    # Provider-aware quotas (16-T4): consulted before a task is created.
    _gates: list[QuotaGate] = field(default_factory=list)
    _task_gates: dict[asyncio.Task[Any], list[QuotaGate]] = field(default_factory=dict)
    _throttled: bool = False
    _episodes: dict[str, list[str]] = field(default_factory=dict)  # episode -> turn case IDs
    _episode_of: dict[str, str] = field(default_factory=dict)
    _episode_success_key: dict[tuple[str, int], str] = field(default_factory=dict)
    _exec_queue: deque[_Item] = field(default_factory=deque)
    _warmup_queue: deque[_Item] = field(default_factory=deque)
    _eval_queue: deque[_Item] = field(default_factory=deque)
    _exec_source: Iterator[_Item] | None = None
    _warmup_source: Iterator[_Item] | None = None
    _eval_after_row_id: int = 0
    # IDs reserved across the queue, quota-held scans, delayed retries, and in-flight calls.
    _queued_evaluations: set[str] = field(default_factory=set)
    _binding_by_key: dict[str, str] = field(default_factory=dict)
    _delayed: list[tuple[float, int, _Item]] = field(default_factory=list)
    _in_flight: dict[asyncio.Task[Any], _Item] = field(default_factory=dict)
    _executions: OrderedDict[str, ExecutionResult | None] = field(default_factory=OrderedDict)
    _exec_states: OrderedDict[str, WorkItemState] = field(default_factory=OrderedDict)
    _scorers: dict[str, BindingScorer] = field(default_factory=dict)
    _reserved: set[str] = field(default_factory=set)
    _seq: itertools.count[int] = field(default_factory=itertools.count)
    _stop_reason: str | None = None
    _last_heartbeat: float = field(default_factory=time.monotonic)
    _last_control_sequence: int = 0
    _next_arrival_ordinal: int = 0
    _profile_started_at: float | None = None
    _profile_pause_started_at: float | None = None
    _profile_paused_seconds: float = 0.0
    # Start the reported wall time with the budget ledger, before resume identity and
    # runner setup, so replayed session time matches what hard wall limits consumed.
    _session_started: float = field(default_factory=time.monotonic)
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ setup

    def _event(self, event_type: str, **payload: Any) -> None:
        self.storage.append_run_event(self.run_id, event_type, payload)

    def _item(self, work_item: WorkItem) -> _Item:
        case_id, repetition, binding_key = parse_work_item_key(work_item.task_key, work_item.kind)
        arrival_ordinal: int | None = None
        arrival_offset: float | None = None
        profile = self.plan.load_profile
        if (
            work_item.kind == "execution"
            and not work_item.warmup
            and work_item.attempt == 0
            and profile.mode != "closed_loop"
        ):
            arrival_ordinal = self._next_arrival_ordinal
            arrival_offset = arrival_offset_seconds(profile, arrival_ordinal)
            self._next_arrival_ordinal += 1
        return _Item(
            work_item.task_key,
            work_item.kind,
            case_id,
            repetition,
            work_item.attempt,
            self._binding_by_key.get(binding_key) if binding_key else None,
            work_item.warmup,
            arrival_ordinal,
            arrival_offset,
        )

    def _execution_task_keys(self, *, warmup: bool) -> Iterator[str]:
        if warmup:
            repetitions = range(
                self.plan.repetitions,
                self.plan.repetitions + self.plan.warmup_repetitions,
            )
        else:
            repetitions = range(self.plan.repetitions)
        if self.reset_mode == "per_episode":
            for turns in self._episodes.values():
                for repetition in repetitions:
                    for case_id in turns:
                        yield execution_key(case_id, repetition)
            return
        for case_id in self.cases:
            for repetition in repetitions:
                yield execution_key(case_id, repetition)

    def _iter_pending_items(self, task_keys: Iterator[str], *, kind: str) -> Iterator[_Item]:
        while batch := list(itertools.islice(task_keys, _WORK_QUEUE_PAGE_SIZE)):
            found = {
                item.task_key: item
                for item in self.storage.list_work_items_by_task_keys(self.run_id, batch)
            }
            for task_key in batch:
                work_item = found.get(task_key)
                if work_item is None or work_item.kind != kind:
                    raise RuntimeError(
                        f"run {self.run_id!r} is missing {kind} work item {task_key!r}"
                    )
                if work_item.state is WorkItemState.PENDING:
                    yield self._item(work_item)

    def _refill_queue(
        self, queue: deque[_Item], source: Iterator[_Item] | None
    ) -> Iterator[_Item] | None:
        if source is None or len(queue) >= _WORK_QUEUE_PAGE_SIZE:
            return source
        while len(queue) < _WORK_QUEUE_PAGE_SIZE:
            try:
                queue.append(next(source))
            except StopIteration:
                return None
        return source

    def _refill_execution_queues(self) -> None:
        self._warmup_source = self._refill_queue(self._warmup_queue, self._warmup_source)
        self._exec_source = self._refill_queue(self._exec_queue, self._exec_source)

    def _refill_evaluation_queue(self) -> None:
        if len(self._eval_queue) >= _WORK_QUEUE_PAGE_SIZE:
            return
        wrapped = False
        while (
            len(self._eval_queue) < _WORK_QUEUE_PAGE_SIZE
            and len(self._queued_evaluations) < _WORK_QUEUE_MAX_BUFFER
        ):
            capacity = min(
                _WORK_QUEUE_PAGE_SIZE - len(self._eval_queue),
                _WORK_QUEUE_MAX_BUFFER - len(self._queued_evaluations),
            )
            if capacity <= 0:
                return
            page = self.storage.list_ready_evaluation_work_items(
                self.run_id,
                after_row_id=self._eval_after_row_id,
                limit=capacity,
            )
            if not page:
                if self._eval_after_row_id and not wrapped:
                    self._eval_after_row_id = 0
                    wrapped = True
                    continue
                return
            for row_id, work_item in page:
                self._eval_after_row_id = row_id
                if work_item.task_key in self._queued_evaluations:
                    continue
                item = self._item(work_item)
                if item.binding_hash is None and not self.controller.cancelled:
                    raise RuntimeError(
                        f"evaluation work item {item.task_key!r} has no known metric binding"
                    )
                self._eval_queue.append(item)
                self._queued_evaluations.add(item.task_key)

    def _transition(
        self,
        item: _Item,
        from_states: set[WorkItemState],
        to: WorkItemState,
        *,
        attempt: int | None = None,
        error: str | None = None,
    ) -> bool:
        updated = self.storage.transition_work_item(
            self.run_id,
            item.task_key,
            from_states=from_states,
            to_state=to,
            attempt=attempt,
            last_error=error,
        )
        if updated is None:
            return False
        self._event(
            "item_state",
            task_key=item.task_key,
            state=to.value,
            attempt=updated.attempt,
            reason=error,
        )
        return True

    def _load(self) -> None:
        self._binding_by_key = {
            metric.binding_hash[7:23]: metric.binding_hash for metric in self.metrics
        }
        if self.reset_mode == "per_episode":
            # An episode is the cases sharing a group_id, in dataset order; a case without
            # one is an episode of its own. Each (episode, repetition) runs contiguously.
            for case_id, case in self.cases.items():
                episode = case.group_id or case_id
                self._episode_of[case_id] = episode
                self._episodes.setdefault(episode, []).append(case_id)
        self._warmup_source = self._iter_pending_items(
            self._execution_task_keys(warmup=True), kind="execution"
        )
        self._exec_source = self._iter_pending_items(
            self._execution_task_keys(warmup=False), kind="execution"
        )
        self._refill_execution_queues()

    # ------------------------------------------------------------------ main loop

    async def execute(self) -> RunOutcome:
        self.controller.bind(asyncio.get_running_loop())
        self._gates = [QuotaGate(q) for q in self.plan.quotas]
        self._load()
        self._set_state(RunState.RUNNING)
        self._event(
            "run_session_started",
            pending_executions=self.storage.count_work_items(
                self.run_id, kind="execution", state=WorkItemState.PENDING
            ),
            pending_evaluations=self.storage.count_work_items(
                self.run_id, kind="evaluation", state=WorkItemState.PENDING
            ),
        )
        needs_runner = (
            self.storage.count_work_items(
                self.run_id, kind="execution", state=WorkItemState.PENDING
            )
            > 0
            and not self.controller.cancelled
        )
        try:
            if needs_runner:
                assert self.runner is not None, "execution work remains but no runner was given"
                await self._while_starting(self.runner.prepare())
            await self._while_starting(self._open_scorers())
            await self._loop()
        finally:
            await self._shutdown(needs_runner)
        return self._finish()

    async def _while_starting(self, work: Awaitable[_T]) -> _T:
        """Await start-up work (runner preparation, evaluator start-up) while keeping the
        committed state in step with controls. A pause or cancel requested meanwhile shows
        as `pausing`/`cancelling` at once (section 15), not only once start-up has ended;
        nothing is dispatched until it has."""
        task = asyncio.ensure_future(work)
        try:
            while True:
                self._sync_run_control()
                # Armed first: a change before this line is in the state recorded next,
                # and one after it wakes the wait.
                change = self.controller.next_change()
                self._set_state(self._busy_state())
                done, _ = await asyncio.wait(
                    {task, change},
                    timeout=_CONTROL_POLL_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if change not in done:
                    change.cancel()
                if task.done():
                    return task.result()
        except BaseException:
            task.cancel()
            raise

    async def _open_scorers(self) -> None:
        wanted = {
            self._binding_by_key[key]
            for key in self.storage.pending_evaluation_binding_keys(self.run_id)
            if key in self._binding_by_key
        }
        # Worker processes beyond each metric's first, shared by all of them: a metric's
        # cases are scored at once instead of one after another (see `WorkerBudget`).
        workers = worker_budget(self.plan.concurrency.evaluation)
        for metric in self.metrics:
            if metric.binding_hash in wanted:
                scorer = BindingScorer(
                    self.storage,
                    self.artifacts,
                    self.scoring_id,
                    metric,
                    self.plan.model_evaluation_timeout_seconds
                    if metric.manifest.uses_models
                    else self.plan.evaluation_timeout_seconds,
                    self.controller.abort_event,
                    application=self.application,
                    dependency_lock_hash=self.dependency_lock_hash,
                    episodes=episode_prefixes(self.cases.values()),
                    workers=workers,
                )
                scorer.cache_policy_hash = self.evaluation_cache_policy
                await scorer.open()
                self._scorers[metric.binding_hash] = scorer

    def _keep_lease(self) -> None:
        self._sync_run_control()
        now = time.monotonic()
        if now - self._last_heartbeat < _HEARTBEAT_SECONDS:
            return
        self._last_heartbeat = now
        if not self.heartbeat() and not self.controller.interrupting:
            self.warnings.append("run lease lost to another session; stopped dispatching")
            self.controller.interrupt()

    def _sync_run_control(self) -> None:
        state = self.run_control()
        if state is None or state.sequence <= self._last_control_sequence:
            return
        action = {"running": "resume", "paused": "pause", "cancelled": "cancel"}[
            state.desired_state
        ]
        if self.controller.cancelled and action != "cancel":
            return
        if self.controller.abort_event.is_set() and action != "cancel":
            # A second interrupt has already aborted in-flight work. Keep the durable
            # request for the next resumable session instead of claiming it was applied.
            return
        getattr(self.controller, action)()
        self._last_control_sequence = state.sequence
        self._event(
            "run_control_applied",
            control_sequence=state.sequence,
            desired_state=state.desired_state,
        )

    async def _loop(self) -> None:
        ctl = self.controller
        while True:
            self._keep_lease()
            self._update_profile_pause_clock()
            self._throttled = False
            if ctl.cancelled:
                self._drain_delayed()  # waiting retries are cancelled, not stranded
            self._release_due_retries()
            stopping = ctl.stopping
            if not stopping and not ctl.paused:
                self._dispatch_executions()
            if not ctl.paused:
                await self._dispatch_evaluations(stopping)
            if self._in_flight:
                self._set_state(self._busy_state())
                done, _ = await asyncio.wait(
                    set(self._in_flight),
                    timeout=self._next_wait(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    await self._complete(task, self._in_flight.pop(task))
                continue
            # Nothing in flight.
            if ctl.paused and not stopping:
                self._set_state(RunState.PAUSED)
                await ctl.wait_for_change(_POLL_SECONDS * 5)
                continue
            if self._throttled:
                # A quota's rate or cooldown holds the next work back: it waits as a queue
                # entry, and the loop stays responsive to controls meanwhile. (A cancelled
                # run consults no quota, so this never delays a cancel.)
                await ctl.wait_for_change(_POLL_SECONDS)
                continue
            if self._delayed and not stopping:
                await ctl.wait_for_change(self._next_wait())
                continue
            if self._profile_arrival_wait() is not None and not stopping:
                await ctl.wait_for_change(self._next_wait())
                continue
            if self._settle_undispatchable(stopping):
                continue  # newly unblocked evaluations to record
            if ctl.interrupting and not ctl.cancelled:
                return  # unstarted evaluations remain pending for resume
            self._refill_evaluation_queue()
            if not self._eval_queue:
                return

    def _busy_state(self) -> RunState:
        ctl = self.controller
        if ctl.cancelled:
            return RunState.CANCELLING
        if ctl.interrupting:
            return RunState.INTERRUPTING
        return RunState.PAUSING if ctl.paused else RunState.RUNNING

    def _next_wait(self) -> float:
        waits = [_POLL_SECONDS]
        if self._delayed:
            waits.append(max(0.0, self._delayed[0][0] - time.monotonic()))
        profile_wait = self._profile_arrival_wait()
        if profile_wait is not None:
            waits.append(profile_wait)
        return min(waits)

    def _update_profile_pause_clock(self) -> None:
        if self._profile_started_at is None:
            return
        now = time.monotonic()
        if self.controller.paused:
            if self._profile_pause_started_at is None:
                self._profile_pause_started_at = now
        elif self._profile_pause_started_at is not None:
            self._profile_paused_seconds += now - self._profile_pause_started_at
            self._profile_pause_started_at = None

    def _start_profile_clock(self) -> None:
        if self._profile_started_at is None:
            self._profile_started_at = time.monotonic()

    def _profile_elapsed(self, now: float | None = None) -> float:
        if self._profile_started_at is None:
            return 0.0
        current = time.monotonic() if now is None else now
        if self._profile_pause_started_at is not None:
            current = min(current, self._profile_pause_started_at)
        return max(0.0, current - self._profile_started_at - self._profile_paused_seconds)

    def _profile_arrival_wait(self) -> float | None:
        """Time until a queued measured first attempt's scheduled arrival."""
        if (
            self.plan.load_profile.mode == "closed_loop"
            or self._profile_started_at is None
            or self.controller.paused
            or self.controller.stopping
            or self._stop_reason is not None
            or self._running("execution") >= self.plan.concurrency.application
            or self._warmups_pending()
            or not self._exec_queue
        ):
            return None
        item = self._exec_queue[0]
        if item.attempt != 0 or item.arrival_offset_seconds is None:
            return None
        return max(0.0, item.arrival_offset_seconds - self._profile_elapsed())

    def _drain_delayed(self) -> None:
        while self._delayed:
            _, _, item = heapq.heappop(self._delayed)
            self._queue_for(item).appendleft(item)
            if item.kind != "execution":
                self._queued_evaluations.add(item.task_key)

    def _release_due_retries(self) -> None:
        now = time.monotonic()
        while self._delayed and self._delayed[0][0] <= now:
            _, _, item = heapq.heappop(self._delayed)
            queue = self._queue_for(item)
            if len(queue) >= _WORK_QUEUE_MAX_BUFFER:
                raise RuntimeError("work retry buffer exceeded its bounded capacity")
            queue.appendleft(item)
            if item.kind != "execution":
                self._queued_evaluations.add(item.task_key)

    def _queue_for(self, item: _Item) -> deque[_Item]:
        if item.kind != "execution":
            return self._eval_queue
        return self._warmup_queue if item.warmup else self._exec_queue

    def _warmups_pending(self) -> bool:
        return (
            self._warmup_source is not None
            or bool(self._warmup_queue)
            or any(item.kind == "execution" and item.warmup for _, _, item in self._delayed)
            or any(item.kind == "execution" and item.warmup for item in self._in_flight.values())
        )

    # ------------------------------------------------------------------ dispatch

    def _dispatch_executions(self) -> None:
        while self._running("execution") < self.plan.concurrency.application:
            self._refill_execution_queues()
            if self._warmup_queue:
                queue = self._warmup_queue
            elif self._warmups_pending():
                return  # global warmup barrier: no measured call starts before warmups settle
            else:
                queue = self._exec_queue
            if not queue:
                return
            if queue is self._exec_queue and self.plan.load_profile.mode != "closed_loop":
                # The profile starts after the warmup barrier and does not wait for
                # application completion, retry backoff, quotas, or evaluator progress.
                self._start_profile_clock()
                head = queue[0]
                if (
                    head.attempt == 0
                    and head.arrival_offset_seconds is not None
                    and head.arrival_offset_seconds > self._profile_elapsed()
                ):
                    return
            if len(self._delayed) + len(self._in_flight) >= _WORK_QUEUE_PAGE_SIZE:
                return
            if self._stop_reason is not None:
                return
            gates = self._gates_for("execution")
            if self._held(gates):
                return
            denial = self.ledger.reserve_application()
            if denial is not None:
                self._stop_reason = denial
                self._event("budget_exhausted", reason=denial)
                return
            gate = self._episode_gate(queue[0])
            if gate is not None:
                self.ledger.settle_application(dispatched=False, cost=None)
                self._block_undispatched(queue.popleft(), gate)
                continue
            item = queue.popleft()
            item.attempt += 1
            if not self._transition(
                item,
                {WorkItemState.PENDING},
                WorkItemState.RUNNING,
                attempt=item.attempt,
            ):
                self.ledger.settle_application(dispatched=False, cost=None)
                continue
            task = asyncio.ensure_future(self._reset_then_invoke(item))
            self._in_flight[task] = item
            self._start(task, gates)

    def _arrival_dispatch_details(self, item: _Item) -> dict[str, Any] | None:
        if item.attempt != 1 or item.arrival_ordinal is None or item.arrival_offset_seconds is None:
            return None
        elapsed = self._profile_elapsed()
        return {
            "offered_load_arrival": True,
            "arrival_ordinal": item.arrival_ordinal,
            "scheduled_arrival_offset_seconds": round(item.arrival_offset_seconds, 9),
            "dispatch_active_seconds": round(elapsed, 9),
            "dispatch_lag_seconds": round(max(0.0, elapsed - item.arrival_offset_seconds), 9),
        }

    def _is_first_turn(self, item: _Item) -> bool:
        return self._episodes[self._episode_of[item.case_id]][0] == item.case_id

    def _remember_execution_state(self, task_key: str, state: WorkItemState) -> None:
        self._exec_states[task_key] = state
        self._exec_states.move_to_end(task_key)
        while len(self._exec_states) > _ENGINE_CACHE_SIZE:
            self._exec_states.popitem(last=False)

    def _execution_state(self, task_key: str) -> WorkItemState | None:
        state = self._exec_states.get(task_key)
        if state is None:
            state = self.storage.get_work_item_state_by_task_key(self.run_id, task_key)
            if state is not None:
                self._remember_execution_state(task_key, state)
        else:
            self._exec_states.move_to_end(task_key)
        return state

    def _remember_execution(self, task_key: str, result: ExecutionResult | None) -> None:
        self._executions[task_key] = result
        self._executions.move_to_end(task_key)
        while len(self._executions) > _ENGINE_CACHE_SIZE:
            self._executions.popitem(last=False)

    def _execution_result(self, item: _Item) -> ExecutionResult | None:
        if item.task_key in self._executions:
            self._executions.move_to_end(item.task_key)
            return self._executions[item.task_key]
        result = self.storage.get_latest_execution_attempt(
            self.run_id, item.case_id, item.repetition
        )
        self._remember_execution(item.task_key, result)
        return result

    def _episode_gate(self, item: _Item) -> str | None:
        """Why a later episode turn must not run: the turn before it did not succeed in
        this session, so the application's state is not what the episode built."""
        if self.reset_mode != "per_episode" or self._is_first_turn(item):
            return None
        episode = self._episode_of[item.case_id]
        turns = self._episodes[episode]
        previous = execution_key(turns[turns.index(item.case_id) - 1], item.repetition)
        if self._episode_success_key.get((episode, item.repetition)) == previous:
            return None
        state = self._execution_state(previous)
        if state is WorkItemState.SUCCEEDED:
            return (
                f"episode_interrupted: episode {episode!r} stopped before this turn; the state "
                "its earlier turns built cannot be restored"
            )
        return (
            f"episode_broken: an earlier turn of episode {episode!r} did not succeed "
            f"({state.value if state else 'not run'})"
        )

    async def _reset_or_abort(self) -> ResetReport:
        """The runner's reset, abandoned (and its process cleaned up) as soon as the run is
        aborted, like an invocation."""
        assert self.runner is not None
        reset = asyncio.ensure_future(self.runner.reset(self.world_seed))
        if await race(reset, timeout=_RESET_WAIT_SECONDS, cancel=self.controller.abort_event) != (
            "cancelled"
        ):
            return await reset
        reset.cancel()
        await asyncio.gather(reset, return_exceptions=True)
        return ResetReport("aborted", "the run was aborted during the reset")

    def _cached_execution(self, item: _Item) -> ExecutionResult | None:
        assert self.execution_cache is not None
        key = cache.execution_key(self.cases[item.case_id], item.repetition, **self.execution_cache)
        self._exec_cache_keys[item.task_key] = key
        entry = self.storage.get_cache_entry("execution", key)
        source = self.storage.get_execution_attempt(entry[1]) if entry else None
        if source is None or source.status is not ExecutionStatus.OK:
            return None
        copy = cache.execution_from_cache(
            source, run_id=self.run_id, repetition=item.repetition, attempt=item.attempt, key=key
        )
        self.storage.commit_execution_attempt(copy)
        self._event(
            "cache_hit",
            task_key=item.task_key,
            kind="execution",
            source=entry[1] if entry else None,
        )
        return copy

    def _block_undispatched(self, item: _Item, reason: str) -> None:
        if self._transition(item, {WorkItemState.PENDING}, WorkItemState.BLOCKED, error=reason):
            self._remember_execution_state(item.task_key, WorkItemState.BLOCKED)

    async def _reset_then_invoke(self, item: _Item) -> ExecutionResult:
        """Reset the application first when this attempt needs a known state, then invoke.
        A reset that fails raises ResetFailed: the case is not run on unknown state. With
        the execution cache on, a stored result for the same key is copied instead: nothing
        is reset or invoked."""
        assert self.runner is not None
        is_warmup = item.warmup
        if self.execution_cache is not None and not is_warmup:
            cached = self._cached_execution(item)
            if cached is not None:
                return cached
        if self.reset_mode == "per_case" or (
            self.reset_mode == "per_episode" and self._is_first_turn(item)
        ):
            report = await self._reset_or_abort()
            self._event(
                "app_reset",
                task_key=item.task_key,
                episode=self._episode_of.get(item.case_id),
                world=self.world_id,
                status=report.status,
                detail=report.detail,
            )
            if report.status == "aborted":
                raise ResetFailed(report.detail, cancelled=True)
            if report.status not in ("reset", "not_needed"):
                raise ResetFailed(report.detail)
        self._record_profile_arrival(item)
        return await invoke_and_record(
            self.runner,
            self.cases[item.case_id],
            storage=self.storage,
            artifacts=self.artifacts,
            run_id=self.run_id,
            repetition_id=item.repetition,
            attempt_id=item.attempt,
            warmup=is_warmup,
            cancel=self.controller.abort_event,
        )

    def _record_profile_arrival(self, item: _Item) -> None:
        """Record just before handing a fresh measured request to its runner."""
        details = self._arrival_dispatch_details(item)
        if details is not None:
            self._event("load_profile_dispatch", task_key=item.task_key, **details)

    async def _dispatch_evaluations(self, stopping: bool) -> None:
        if stopping and not self.controller.cancelled:
            return  # interrupt: unstarted evaluations stay pending for resume
        self._refill_evaluation_queue()
        # Work held by one evaluator's quota does not hold back other evaluators or
        # records that need no evaluator: up to _HELD_SCAN held entries are looked past,
        # and put back in order.
        held_back: list[_Item] = []
        held_evaluators: set[str] = set()
        try:
            await self._dispatch_evaluations_past(held_back, held_evaluators)
        finally:
            self._eval_queue.extendleft(reversed(held_back))
            self._queued_evaluations.update(item.task_key for item in held_back)

    async def _dispatch_evaluations_past(
        self, held_back: list[_Item], held_evaluators: set[str]
    ) -> None:
        while self._running("evaluation") < self.plan.concurrency.evaluation:
            if not self._eval_queue:
                self._refill_evaluation_queue()
                if not self._eval_queue:
                    return
            if len(held_back) >= _HELD_SCAN:
                return
            item = self._eval_queue.popleft()
            if self.controller.cancelled and (
                item.binding_hash is None or item.binding_hash not in self._scorers
            ):
                self._transition(
                    item,
                    {WorkItemState.PENDING},
                    WorkItemState.CANCELLED,
                    error="run cancelled before evaluation",
                )
                self._queued_evaluations.discard(item.task_key)
                continue
            assert item.binding_hash is not None
            scorer = self._scorers[item.binding_hash]
            exec_key = execution_key(item.case_id, item.repetition)
            execution = self._execution_result(item)
            exec_state = self._execution_state(exec_key)
            needs_evaluator = exec_state is WorkItemState.SUCCEEDED and execution is not None
            evaluator_id = scorer.manifest.evaluator_id
            gates = (
                self._gates_for("evaluation", evaluator_id)
                if needs_evaluator and not self.controller.cancelled
                else []
            )
            if gates and (evaluator_id in held_evaluators or self._held(gates)):
                self._throttled = True
                held_evaluators.add(evaluator_id)
                self._queued_evaluations.add(item.task_key)
                held_back.append(item)
                continue
            if needs_evaluator and self.controller.cancelled:
                self._record_without_evaluator(item, scorer, "cancelled", ExecutionStatus.CANCELLED)
                self._queued_evaluations.discard(item.task_key)
                continue
            if needs_evaluator:
                denial = self.ledger.reserve_evaluation()
                if denial is not None:
                    if self._stop_reason is None:
                        self._stop_reason = denial
                        self._event("budget_exhausted", reason=denial)
                    self._record_without_evaluator(item, scorer, f"not_evaluated:{denial}")
                    self._queued_evaluations.discard(item.task_key)
                    continue
                self._reserved.add(item.task_key)
            if len(self._delayed) + len(self._in_flight) >= _WORK_QUEUE_PAGE_SIZE:
                self._release_evaluation(item)
                self._queued_evaluations.add(item.task_key)
                held_back.append(item)
                continue
            item.attempt += 1
            if not self._transition(
                item, {WorkItemState.PENDING}, WorkItemState.RUNNING, attempt=item.attempt
            ):
                self._release_evaluation(item)
                self._queued_evaluations.discard(item.task_key)
                continue
            if execution is None:
                self._finish_missing(
                    item, scorer, f"not_executed:{(exec_state or WorkItemState.BLOCKED).value}"
                )
                self._queued_evaluations.discard(item.task_key)
                continue
            task = asyncio.ensure_future(scorer.score(execution, [self.cases[item.case_id]]))
            self._in_flight[task] = item
            self._start(task, gates)

    def _gates_for(self, kind: str, evaluator_id: str | None = None) -> list[QuotaGate]:
        return [g for g in self._gates if g.applies_to(kind, evaluator_id)]

    def _held(self, gates: list[QuotaGate]) -> bool:
        """Whether a quota holds the next work back (rate, cap or cooldown)."""
        if any(g.wait_seconds() > 0 for g in gates):
            self._throttled = True
            return True
        return False

    def _start(self, task: asyncio.Task[Any], gates: list[QuotaGate]) -> None:
        for gate in gates:
            gate.start()
        if gates:
            self._task_gates[task] = gates

    def _running(self, kind: str) -> int:
        return sum(1 for i in self._in_flight.values() if i.kind == kind)

    def _release_evaluation(self, item: _Item) -> None:
        if item.task_key in self._reserved:
            self._reserved.discard(item.task_key)
            self.ledger.settle_evaluation(None)

    def _record_without_evaluator(
        self,
        item: _Item,
        scorer: BindingScorer,
        reason: str,
        status: ExecutionStatus = ExecutionStatus.SKIPPED,
    ) -> None:
        item.attempt += 1
        if self._transition(
            item, {WorkItemState.PENDING}, WorkItemState.RUNNING, attempt=item.attempt
        ):
            self._finish_missing(item, scorer, reason, status)

    def _finish_missing(
        self,
        item: _Item,
        scorer: BindingScorer,
        reason: str,
        status: ExecutionStatus = ExecutionStatus.SKIPPED,
    ) -> None:
        execution = self._execution_result(item)
        target = MissingExecution(
            self.run_id,
            item.case_id,
            item.repetition,
            execution.execution_id if execution else None,
        )
        result = scorer.score_missing(target, reason, status=status)
        scorer.finalize(result)
        final = (
            WorkItemState.CANCELLED
            if status is ExecutionStatus.CANCELLED
            else WorkItemState.SUCCEEDED
        )
        self._transition(
            item,
            {WorkItemState.RUNNING},
            final,
            error=None if final is WorkItemState.SUCCEEDED else reason,
        )

    # ------------------------------------------------------------------ completion

    async def _complete(self, task: asyncio.Task[Any], item: _Item) -> None:
        gates = self._task_gates.pop(task, [])
        for gate in gates:
            gate.finish()
        if item.kind == "execution":
            await self._complete_execution(task, item)
            self._backpressure(task, gates)
        else:
            self._complete_evaluation(task, item)

    def _backpressure(self, task: asyncio.Task[Any], gates: list[QuotaGate]) -> None:
        """A provider's "slow down" (HTTP 429 or 503) pauses every quota the call ran
        under, for its Retry-After or the quota's backoff."""
        if not gates or task.cancelled() or task.exception() is not None:
            return
        result = task.result()
        status, retry_after = http_status(result)
        if status not in (429, 503):
            return
        for gate in gates:
            pause = gate.backpressure(retry_after)
            self._event("backpressure", quota=gate.quota.name, status=status, pause_seconds=pause)

    async def _complete_execution(self, task: asyncio.Task[Any], item: _Item) -> None:
        key = self._exec_cache_keys.pop(item.task_key, None)  # released on every path
        try:
            result: ExecutionResult = task.result()
        except ResetFailed as exc:
            # Never invoked: the application was not in the state the case needs.
            self.ledger.settle_application(dispatched=False, cost=None)
            if exc.cancelled:
                self._final_execution(
                    item, WorkItemState.CANCELLED, "cancelled before the case ran", None
                )
            else:
                self._final_execution(
                    item, WorkItemState.BLOCKED, f"reset_failed: {exc}"[:500], None
                )
            return
        except Exception as exc:  # noqa: BLE001 - an engine-side failure, recorded not raised
            self.ledger.settle_application(dispatched=True, cost=None)
            if is_storage_failure(exc):
                self._stop_for_storage(item, exc)
                return
            self._final_execution(
                item, WorkItemState.FAILED, f"engine_error:{type(exc).__name__}: {exc}"[:500], None
            )
            return
        self.ledger.settle_application(dispatched=was_dispatched(result), cost=result.cost)
        if key is not None and result.status is ExecutionStatus.OK and not result.cache:
            self.storage.put_cache_entry("execution", key, self.run_id, result.execution_id)
        if not result.cache and (result.cost is not None or result.usage is not None):
            # A cache hit spent nothing now: its source run already recorded the spend.
            self.storage.commit_usage_event(
                UsageEvent(
                    usage_event_id=f"{result.execution_id}:usage",
                    run_id=self.run_id,
                    role=UsageRole.APPLICATION,
                    provider=None,
                    tokens=result.usage,
                    calls=1,
                    cost=result.cost,
                )
            )
        verdict = classify_execution(result)
        ctl = self.controller
        stopping = ctl.stopping
        if (
            ctl.interrupting
            and not ctl.cancelled
            and verdict.final_state is WorkItemState.CANCELLED
        ):
            # Aborted by a second interrupt with no effect possible: effect-free work stays
            # resumable. (An ambiguous effectful abort is UNKNOWN_EFFECT, handled below.)
            self._transition(
                item,
                {WorkItemState.RUNNING},
                WorkItemState.PENDING,
                error="aborted by interrupt; safe to repeat (no effects)",
            )
            return
        if verdict.retry and item.attempt < self.plan.retry.max_attempts:
            if stopping and ctl.cancelled:
                self._final_execution(
                    item,
                    WorkItemState.CANCELLED,
                    f"cancelled before retrying: {verdict.reason}",
                    result,
                )
                return
            delay = backoff_delay(
                self.plan.retry, item.attempt, self.rng, verdict.retry_after_seconds
            )
            if self._transition(
                item, {WorkItemState.RUNNING}, WorkItemState.PENDING, error=verdict.reason
            ):
                self._event(
                    "retry_scheduled",
                    task_key=item.task_key,
                    attempt=item.attempt,
                    delay_seconds=round(delay, 3),
                )
                if stopping:  # interrupt: leave it pending for resume
                    return
                heapq.heappush(self._delayed, (time.monotonic() + delay, next(self._seq), item))
            return
        reason = verdict.reason if verdict.final_state is not WorkItemState.SUCCEEDED else None
        if verdict.retry:
            reason = f"{verdict.reason} (retries exhausted after {item.attempt} attempts)"
        self._final_execution(item, verdict.final_state, reason, result)

    def _final_execution(
        self, item: _Item, state: WorkItemState, reason: str | None, result: ExecutionResult | None
    ) -> None:
        self._transition(item, {WorkItemState.RUNNING}, state, error=reason)
        if self.reset_mode == "per_episode":
            episode = self._episode_of[item.case_id]
            key = (episode, item.repetition)
            if state is WorkItemState.SUCCEEDED and item.case_id != self._episodes[episode][-1]:
                self._episode_success_key[key] = item.task_key
            else:
                self._episode_success_key.pop(key, None)
        self._remember_execution_state(item.task_key, state)
        self._remember_execution(item.task_key, result)

    def _stop_for_storage(self, item: _Item, exc: BaseException) -> None:
        """The workspace can't take writes (disk full, I/O error, ...). That is not a result
        of this item: leave it `running`, exactly as a crash would, and stop dispatching.
        Resume's recovery then counts a possibly dispatched call and repeats it only if the
        application declares no effects."""
        warning = (
            f"workspace storage failed ({type(exc).__name__}: {exc}); stopped dispatching. "
            f"Free space or fix the workspace, then resume the run"
        )
        if warning not in self.warnings:
            self.warnings.append(warning[:500])
        if not self.controller.interrupting:
            # A second interrupt means "abort what is in flight"; another storage failure,
            # or one after the user's first Ctrl+C, must not escalate to that.
            self.controller.interrupt()

    def _complete_evaluation(self, task: asyncio.Task[Any], item: _Item) -> None:
        assert item.binding_hash is not None
        scorer = self._scorers[item.binding_hash]
        try:
            result = task.result()
        except BaseException as exc:  # noqa: BLE001 - includes cancellation of the task
            self._release_evaluation(item)
            if is_storage_failure(exc):
                self._stop_for_storage(item, exc)
                self._queued_evaluations.discard(item.task_key)
                return
            status = (
                ExecutionStatus.CANCELLED
                if isinstance(exc, asyncio.CancelledError)
                else ExecutionStatus.ERROR
            )
            self._finish_missing(item, scorer, f"engine_error:{type(exc).__name__}", status)
            self._queued_evaluations.discard(item.task_key)
            return
        if item.task_key in self._reserved:
            self._reserved.discard(item.task_key)
            self.ledger.settle_evaluation(dict(result.resources))
        verdict = classify_evaluation(result, scorer.manifest)
        ctl = self.controller
        if (
            ctl.interrupting
            and not ctl.cancelled
            and (verdict.retry or result.status is ExecutionStatus.CANCELLED)
        ):
            # Interrupted: a transient or aborted evaluation is not a final result; it runs
            # again on resume (its spend stays recorded as an attempt).
            self._transition(
                item, {WorkItemState.RUNNING}, WorkItemState.PENDING, error=verdict.reason
            )
            return
        if verdict.retry and item.attempt < self.plan.retry.max_attempts and not ctl.stopping:
            delay = backoff_delay(self.plan.retry, item.attempt, self.rng)
            if self._transition(
                item, {WorkItemState.RUNNING}, WorkItemState.PENDING, error=verdict.reason
            ):
                self._event(
                    "retry_scheduled",
                    task_key=item.task_key,
                    attempt=item.attempt,
                    delay_seconds=round(delay, 3),
                )
                self._queued_evaluations.add(item.task_key)
                heapq.heappush(self._delayed, (time.monotonic() + delay, next(self._seq), item))
            return
        scorer.finalize(result)
        reason = None if verdict.final_state is WorkItemState.SUCCEEDED else verdict.reason
        if verdict.retry:
            reason = f"{verdict.reason} (retries exhausted after {item.attempt} attempts)"
        self._transition(item, {WorkItemState.RUNNING}, verdict.final_state, error=reason)
        self._queued_evaluations.discard(item.task_key)

    # ------------------------------------------------------------------ stopping

    def _settle_undispatchable(self, stopping: bool) -> bool:
        """Nothing is in flight. Decide what happens to work that can no longer be
        dispatched; returns True if evaluations were released for recording."""
        if self.controller.interrupting and not self.controller.cancelled:
            self._exec_queue.clear()  # unstarted work stays pending for resume
            self._warmup_queue.clear()
            self._eval_queue.clear()
            self._queued_evaluations.clear()
            self._exec_source = None
            self._warmup_source = None
            self._delayed.clear()
            return False
        if self.controller.cancelled:
            state, reason = WorkItemState.CANCELLED, "run cancelled before dispatch"
        elif self._stop_reason is not None:
            state, reason = WorkItemState.BLOCKED, f"budget: {self._stop_reason}"
        else:
            return False
        changed = False
        for _, _, item in self._delayed:
            if item.kind == "execution" and self._transition(
                item, {WorkItemState.PENDING}, state, error=reason
            ):
                self._remember_execution_state(item.task_key, state)
                changed = True
        self._delayed.clear()
        while self._exec_queue or self._warmup_queue or self._exec_source or self._warmup_source:
            self._refill_execution_queues()
            for queue in (self._warmup_queue, self._exec_queue):
                while queue:
                    item = queue.popleft()
                    if self._transition(item, {WorkItemState.PENDING}, state, error=reason):
                        self._remember_execution_state(item.task_key, state)
                        changed = True
        return changed

    async def _shutdown(self, needs_runner: bool) -> None:
        for task in list(self._in_flight):
            task.cancel()
        if self._in_flight:
            await asyncio.gather(*self._in_flight, return_exceptions=True)
        for scorer in self._scorers.values():
            await scorer.close(self.warnings)
        if needs_runner and self.runner is not None:
            await self.runner.close()

    def _set_state(self, state: RunState) -> None:
        if self.controller.state is not state:
            self.controller.state = state
            self.storage.update_run_status(self.run_id, state.value)

    def _finish(self) -> RunOutcome:
        ctl = self.controller
        pending = any(
            self.storage.count_work_items(self.run_id, state=state) > 0
            for state in (WorkItemState.PENDING, WorkItemState.RUNNING)
        )
        if ctl.cancelled:
            state = RunState.CANCELLED
        elif ctl.interrupting or pending:
            state = RunState.INTERRUPTED
        elif self._stop_reason is not None:
            state = RunState.BUDGET_EXHAUSTED
        else:
            state = RunState.COMPLETED
        self._set_state(state)
        counts = work_counts(self.storage, self.run_id)
        budget = self.ledger.summary()
        self._event(
            "run_session_ended",
            state=state.value,
            counts=counts,
            session_elapsed_seconds=round(time.monotonic() - self._session_started, 3),
            budget=budget,
            stop_reason=self._stop_reason,
            warnings=self.warnings,
            quotas=[g.summary() for g in self._gates],
        )
        return RunOutcome(state, counts, budget, self._stop_reason, list(self.warnings))


_STORAGE_ERRNOS = frozenset(
    code
    for code in (
        errno.ENOSPC,
        getattr(errno, "EDQUOT", None),
        errno.EIO,
        errno.EROFS,
    )
    if code is not None
)


# Windows codes whose errno mapping is too generic to use: ERROR_WRITE_PROTECT (19, maps to
# EACCES), ERROR_HANDLE_DISK_FULL (39, EINVAL), ERROR_DISK_FULL (112, ENOSPC).
_STORAGE_WINERRORS = frozenset({19, 39, 112})
# SQLite primary result codes: READONLY, IOERR, FULL. Not BUSY/LOCKED (contention) and not
# CONSTRAINT (a bug): those are not the workspace running out of room.
_STORAGE_SQLITE_CODES = frozenset({8, 10, 13})


def is_storage_failure(exc: BaseException) -> bool:
    """Whether `exc` means the workspace itself can't take writes, rather than something
    wrong with one work item: a full disk or quota, an I/O error, or a read-only
    filesystem, whether reported by the OS or by the run database."""
    if isinstance(exc, sqlite3.DatabaseError):
        code = getattr(exc, "sqlite_errorcode", None)
        return code is not None and code & 0xFF in _STORAGE_SQLITE_CODES
    if not isinstance(exc, OSError):
        return False
    return exc.errno in _STORAGE_ERRNOS or getattr(exc, "winerror", None) in _STORAGE_WINERRORS


_NEVER_DISPATCHED = frozenset(
    {ErrorKind.BINDING, ErrorKind.REQUEST_LIMIT, ErrorKind.POLICY_DENIED, ErrorKind.SPAWN_FAILED}
)


def was_dispatched(result: ExecutionResult) -> bool:
    """Whether the call reached the application (and so counts against the call budget).
    `effect_state` alone cannot say: effect-free apps report `none_declared` either way."""
    if result.effect_state is EffectState.NOT_DISPATCHED:
        return False
    return result.error_kind not in _NEVER_DISPATCHED


def work_counts(storage: Storage, run_id: str) -> dict[str, dict[str, int]]:
    return storage.work_item_counts(run_id)
