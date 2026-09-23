"""Planning services shared by commands now and the conversational layer later (07-T4).

- `gather_inputs` builds everything a planner may see: the application profile, the dataset
  summary and the evaluator catalog (native evaluators plus any plugin environment the
  policy permits).
- `write_draft` writes the executable plan and its draft document, with revisions: an
  existing different plan is never silently overwritten; `revise=True` archives it as
  `<stem>.rev<N>.json` and records the superseded plan hash (§8: "If a pilot exposes missing
  fields, create a new plan revision").

The executable revision a run uses is frozen again by `run` (plan artifact + hash), so a
draft edited after review cannot change a run that already started.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from aibench.core.errors import AibenchError
from aibench.core.models import DecisionRule, ExecutionResult
from aibench.core.plans import BudgetLimits, CaseSelection, PluginEnvironmentRef
from aibench.engine.compile import freeze_plan, load_plan
from aibench.inspection.dataset_summary import summarize_dataset
from aibench.inspection.profile import inspect_application
from aibench.planning.catalog import build_catalog
from aibench.planning.drafts import DraftContext, PlanDraft, draft_document
from aibench.planning.planner import PlanningInputs, PlanningOutcome
from aibench.registry import EvaluatorRegistry, RegistryError
from aibench.security.policy import ExecutionPolicy, plugin_denials


class PlanningError(AibenchError):
    """Planning cannot proceed as asked (e.g. it would overwrite a different plan)."""


_BUDGET_FIELDS = (
    "max_application_calls",
    "max_evaluator_calls",
    "max_judge_tokens",
    "max_wall_seconds",
    "max_cost_usd",
)
_COST_ESTIMATE_FIELDS = (
    "estimated_cost_per_application_call_usd",
    "estimated_cost_per_evaluation_usd",
)


def effective_budgets(policy: ExecutionPolicy, requested: BudgetLimits) -> BudgetLimits:
    """Fill unset plan limits and cost estimates from the policy's approved defaults."""
    updates = {
        name: getattr(policy.ceilings, name)
        for name in _BUDGET_FIELDS
        if getattr(requested, name) is None and getattr(policy.ceilings, name) is not None
    }
    updates.update(
        {
            name: getattr(policy.ceilings, name)
            for name in _COST_ESTIMATE_FIELDS
            if getattr(requested, name) is None and getattr(policy.ceilings, name) is not None
        }
    )
    return requested.model_copy(update=updates) if updates else requested


def draft_paths(out: Path) -> tuple[Path, Path]:
    """(plan file, draft document) for `--out`."""
    return out, out.with_name(f"{out.stem}.draft.json")


def _archived(out: Path) -> list[int]:
    pattern = re.compile(rf"^{re.escape(out.stem)}\.rev(\d+)\.json$")
    numbers = []
    for path in out.parent.glob(f"{out.stem}.rev*.json"):
        match = pattern.match(path.name)
        if match:
            numbers.append(int(match.group(1)))
    return numbers


def current_revision(out: Path) -> int:
    """The revision of the plan now at `out` (0 if none). Taken from the draft document and
    the archives together, so a missing or corrupt draft document cannot reset numbering."""
    plan_file, draft_file = draft_paths(out)
    archived = _archived(out)
    floor = max(archived) + 1 if archived else 0
    if not plan_file.exists():
        return floor - 1 if archived else 0
    recorded = 0
    if draft_file.exists():
        try:
            recorded = int(json.loads(draft_file.read_text(encoding="utf-8"))["revision"])
        except (OSError, ValueError, KeyError, TypeError):
            recorded = 0
    return max(recorded, floor, 1)


def next_revision(out: Path) -> tuple[int, str | None]:
    """The revision a new, different draft at `out` would get, and the plan hash it
    supersedes."""
    plan_file, _ = draft_paths(out)
    old_hash = None
    if plan_file.exists():
        try:
            _, old_hash = freeze_plan(load_plan(plan_file))
        except AibenchError:
            old_hash = None
    return current_revision(out) + 1, old_hash


def _registry(
    policy: ExecutionPolicy, plan_dir: Path, environments: Iterable[PluginEnvironmentRef]
) -> tuple[EvaluatorRegistry, list[str]]:
    """Native evaluators plus each plugin environment the policy permits (checked by the
    caller through `plan_denials`); load errors are reported, not fatal."""
    registry = EvaluatorRegistry.with_native()
    notes = []
    for env in environments:
        python = Path(env.python) if Path(env.python).is_absolute() else plan_dir / env.python
        try:
            for load in registry.load_plugin_environment(
                python,
                secret_env=dict(env.secret_env),
                extra_paths=[Path(p) if Path(p).is_absolute() else plan_dir / p for p in env.paths],
            ):
                if load.error:
                    notes.append(f"plugin {load.plugin.name}: {load.error}")
        except RegistryError as exc:
            notes.append(str(exc))
    return registry, notes


@dataclass
class GatheredInputs:
    inputs: PlanningInputs
    notes: list[str]


def gather_inputs(
    *,
    application: Path,
    dataset: Path,
    objectives: list[str],
    out: Path,
    policy: ExecutionPolicy,
    trusted_local: bool = False,
    plan_id: str | None = None,
    selection: CaseSelection | None = None,
    budgets: BudgetLimits | None = None,
    plugin_environments: tuple[PluginEnvironmentRef, ...] = (),
    params: dict[str, dict[str, object]] | None = None,
    rules: dict[str, DecisionRule] | None = None,
    executions: Iterable[ExecutionResult] = (),
    run_ids: Iterable[str] = (),
) -> GatheredInputs:
    effective = policy.with_trusted_local(trusted_local)
    out_dir = out.resolve().parent
    revision, _ = next_revision(out)
    context = DraftContext(
        plan_id=plan_id or out.stem,
        out_dir=out_dir,
        dataset=dataset,
        application=application,
        policy=policy,
        trusted_local=trusted_local,
        budgets=effective_budgets(effective, budgets or BudgetLimits()),
        plugin_environments=plugin_environments,
        selection=selection,
        revision=revision,
        user_objectives=tuple(objectives),
        user_params=dict(params or {}),
        user_rules=dict(rules or {}),
    )
    profile = inspect_application(application, executions=executions, run_ids=run_ids)
    summary = summarize_dataset(dataset)
    env_denials = plugin_denials(effective, plugin_environments, out_dir)
    notes = list(env_denials)
    if env_denials:  # never start a plugin the policy does not permit
        registry = EvaluatorRegistry.with_native()
    else:
        registry, load_notes = _registry(effective, out_dir, plugin_environments)
        notes.extend(load_notes)
        context.plugin_problems = tuple(load_notes)  # blocking, as in the execution gate
    catalog = build_catalog(registry, profile, summary, effective)
    context.registry = registry
    return GatheredInputs(PlanningInputs(objectives, profile, summary, catalog, context), notes)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_draft(
    outcome: PlanningOutcome, inputs: PlanningInputs, out: Path, *, revise: bool = False
) -> PlanDraft:
    validation = outcome.validation
    if validation.plan is None:
        raise PlanningError(
            "the planner produced no plan: " + "; ".join(validation.blocking_messages())
        )
    plan_file, draft_file = draft_paths(out)
    ctx = inputs.context
    _, new_hash = freeze_plan(validation.plan)
    supersedes = None
    if plan_file.exists():
        current = current_revision(out)
        _, old_hash = next_revision(out)
        if old_hash == new_hash:
            ctx = replace(ctx, revision=current)  # same plan: same revision
        elif not revise:
            raise PlanningError(
                f"{plan_file} already holds a different plan; pass --revise to write a "
                "new revision (the current one is archived)"
            )
        else:
            archive = plan_file.with_name(f"{plan_file.stem}.rev{current}.json")
            if archive.exists():
                raise PlanningError(f"{archive} already exists; refusing to overwrite history")
            _write_atomic(archive, plan_file.read_text(encoding="utf-8"))
            supersedes = old_hash
            ctx = replace(ctx, revision=current + 1)
    document = draft_document(
        outcome.proposal,
        validation,
        ctx,
        planner=outcome.provenance,
        plan_file=plan_file.name,
        profile_hash=inputs.profile.profile_hash,
        dataset_hash=inputs.dataset.content_hash,
        supersedes=supersedes,
    )
    plan_json = json.dumps(validation.plan.model_dump(mode="json"), indent=2, sort_keys=True)
    _write_atomic(plan_file, plan_json + "\n")
    _write_atomic(draft_file, document.model_dump_json(indent=2) + "\n")
    return document
