"""From session choices to a reviewed draft (08-T2, 08-T4).

`apply_patch` turns a typed `PlanPatch` into the next `SessionChoices`, rejecting anything
that does not fit (an unknown objective, an unknown concept, a missing dataset). `build_draft`
derives the draft plan from choices with the same services as `aibench plan`: evidence
(`gather_inputs`), the deterministic template planner and `validate_draft`, whose findings
separate missing information from missing permission. So a conversational draft and a
headless one made from the same choices are the same plan.

Each draft's plan is written to the session directory under a name derived from its hash
(`plan-<hash>.json`). The file is never rewritten, so a run started from a revision uses
exactly the plan that was reviewed, and two turns racing for the same revision cannot
overwrite each other's file.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from aibench.core.errors import AibenchError
from aibench.core.models import deep_unfreeze
from aibench.core.plans import BudgetLimits, CaseSelection, PluginEnvironmentRef
from aibench.core.sessions import BenchmarkSession, PendingQuestion, PlanPatch, SessionChoices
from aibench.engine.compile import freeze_plan, load_policy
from aibench.planning.catalog import CONCEPTS
from aibench.planning.drafts import PlanDraft, PlannerProvenance, draft_document, validate_draft
from aibench.planning.planner import PlanningInputs, PlanningOutcome
from aibench.planning.service import gather_inputs
from aibench.planning.template import template_proposal
from aibench.runners import load_application


class PatchRejected(AibenchError):
    """A patch cannot be applied to the current choices."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def _validation_problems(exc: PydanticValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc']) or 'patch'}: {e['msg']}" for e in exc.errors()]


def apply_patch(
    choices: SessionChoices, patch: PlanPatch, project_root: Path, *, default_seed: int
) -> SessionChoices:
    problems: list[str] = []
    objectives = list(choices.objectives)
    for text in patch.remove_objectives:
        if text in objectives:
            objectives.remove(text)
        else:
            problems.append(f"no stated objective {text!r} to remove")
    for text in patch.add_objectives:
        if text.strip() and text.strip() not in objectives:
            objectives.append(text.strip())
    concepts = {k: v for k, v in choices.objective_concepts.items() if k in objectives}
    for text, chosen in patch.objective_concepts.items():
        if text not in objectives:
            problems.append(f"{text!r} is not a stated objective")
            continue
        unknown = [c for c in chosen if c not in CONCEPTS]
        if unknown:
            problems.append(f"unknown concepts {unknown}; known: {sorted(CONCEPTS)}")
            continue
        concepts[text] = tuple(chosen)

    selection = choices.selection
    if patch.sample is not None:
        selection = CaseSelection(
            case_ids=selection.case_ids,
            where=selection.where,
            sample_size=patch.sample.size,
            seed=next(
                s for s in (patch.sample.seed, selection.seed, default_seed) if s is not None
            ),
        )
    elif patch.limit is not None:
        selection = CaseSelection(
            case_ids=selection.case_ids, where=selection.where, limit=patch.limit
        )
    elif patch.all_cases:
        selection = CaseSelection()

    budgets = choices.budgets
    if patch.budgets is not None:
        changed = patch.budgets.model_dump(exclude_none=True)
        try:
            budgets = BudgetLimits.model_validate({**budgets.model_dump(), **changed})
        except PydanticValidationError as exc:
            problems.extend(_validation_problems(exc))

    params = {k: deep_unfreeze(v) for k, v in choices.params.items()}
    for evaluator_id, value in patch.params.items():
        unfrozen = deep_unfreeze(value)
        if not isinstance(unfrozen, dict):
            problems.append(f"params for {evaluator_id} must be an object")
        elif unfrozen:
            params[evaluator_id] = unfrozen
        else:
            params.pop(evaluator_id, None)  # an empty object clears them
    rules = {**choices.rules, **patch.rules}

    dataset = choices.dataset
    if patch.dataset is not None:
        path = Path(patch.dataset)
        resolved = (path if path.is_absolute() else project_root / path).resolve()
        if not resolved.is_file():
            problems.append(f"dataset {patch.dataset!r} does not exist")
        dataset = str(resolved)

    test_world = None if patch.clear_test_world else choices.test_world
    if patch.test_world is not None:
        declared = _declared_worlds(Path(choices.application))
        if patch.test_world in declared:
            test_world = patch.test_world
        else:
            problems.append(
                f"test world {patch.test_world!r} is not declared by the application "
                f"(declared: {', '.join(declared) or 'none'})"
            )

    if problems:
        raise PatchRejected(problems)
    try:
        return SessionChoices(
            application=choices.application,
            dataset=dataset,
            objectives=tuple(objectives),
            objective_concepts=concepts,
            selection=selection,
            repetitions=patch.repetitions or choices.repetitions,
            budgets=budgets,
            params=params,
            rules=rules,
            test_world=test_world,
        )
    except PydanticValidationError as exc:
        raise PatchRejected(_validation_problems(exc)) from exc


def _declared_worlds(application: Path) -> list[str]:
    try:
        return sorted(load_application(application).spec.test_worlds)
    except AibenchError:
        return []


@dataclass
class SessionDraft:
    choices: SessionChoices
    inputs: PlanningInputs
    outcome: PlanningOutcome
    document: PlanDraft
    plan_file: str  # name inside the session directory
    plan_hash: str

    @property
    def executable(self) -> bool:
        return self.outcome.validation.executable

    def questions(self) -> tuple[PendingQuestion, ...]:
        """The draft's clarifications as session records."""
        return self.document.pending_questions


def session_directory(workspace_root: Path, session_id: str) -> Path:
    return workspace_root / "sessions" / session_id


def _write_once(path: Path, text: str) -> None:
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise AibenchError(f"{path} holds a different plan; refusing to overwrite it")
        return
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def plan_environments(session: BenchmarkSession) -> tuple[PluginEnvironmentRef, ...]:
    return tuple(
        PluginEnvironmentRef.model_validate(deep_unfreeze(e)) for e in session.plugin_environments
    )


def planning_inputs(
    session: BenchmarkSession, choices: SessionChoices, *, revision: int, directory: Path
) -> PlanningInputs:
    """The evidence a draft is planned from: profile, dataset counts and the permitted
    evaluator catalog. Raises `AibenchError` when a config or the dataset cannot be read."""
    policy = load_policy(Path(session.policy_path) if session.policy_path else None)
    gathered = gather_inputs(
        application=Path(choices.application),
        dataset=Path(choices.dataset),
        objectives=list(choices.objectives),
        out=directory / "plan.json",
        policy=policy,
        trusted_local=session.trusted_local,
        plan_id=session.session_id,
        selection=choices.selection,
        budgets=choices.budgets,
        plugin_environments=plan_environments(session),
        params={k: deep_unfreeze(v) for k, v in choices.params.items()},
        rules=dict(choices.rules),
        test_world=choices.test_world,
    )
    inputs = gathered.inputs
    inputs.context = replace(inputs.context, revision=revision)
    return inputs


def build_draft(
    session: BenchmarkSession,
    choices: SessionChoices,
    *,
    revision: int,
    directory: Path,
    supersedes: str | None = None,
) -> SessionDraft:
    """Raises `AibenchError` when the choices cannot be planned at all (e.g. the dataset
    or application config cannot be read); a plan that merely is not executable yet is a
    draft with blocking findings."""
    directory.mkdir(parents=True, exist_ok=True)
    inputs = planning_inputs(session, choices, revision=revision, directory=directory)
    ctx = inputs.context
    proposal = template_proposal(
        inputs.objectives,
        inputs.catalog,
        params=ctx.user_params,
        rules=ctx.user_rules,
        concepts=dict(choices.objective_concepts),
    )
    if choices.repetitions != proposal.repetitions:
        proposal = proposal.model_copy(update={"repetitions": choices.repetitions})
    validation = validate_draft(proposal, ctx)
    if validation.plan is None:
        raise PatchRejected(validation.blocking_messages())
    _, plan_hash = freeze_plan(validation.plan)
    plan_file = f"plan-{plan_hash.split(':', 1)[-1][:16]}.json"
    _write_once(
        directory / plan_file,
        json.dumps(validation.plan.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
    )
    outcome = PlanningOutcome(proposal, validation, PlannerProvenance(kind="template"))
    document = draft_document(
        proposal,
        validation,
        ctx,
        planner=outcome.provenance,
        plan_file=plan_file,
        profile_hash=inputs.profile.profile_hash,
        dataset_hash=inputs.dataset.content_hash,
        supersedes=supersedes,
    )
    return SessionDraft(choices, inputs, outcome, document, plan_file, plan_hash)


def draft_summary(document: dict[str, Any], test_world: str | None = None) -> dict[str, Any]:
    """What a person reviews before running (§3 step 4): metrics with rationale, gaps,
    open questions, findings split into missing information and missing permission,
    coverage and the spend estimate."""
    findings = document.get("findings", [])
    by_kind: dict[str, list[str]] = {}
    for finding in findings:
        if finding.get("blocking"):
            by_kind.setdefault(finding["kind"], []).append(finding["message"])
    return {
        "revision": document["revision"],
        "executable": document["executable"],
        "objectives": [o["text"] for o in document.get("objectives", [])],
        "metrics": [
            {"metric": m["metric"], "objectives": m["objective_ids"], "rationale": m["rationale"]}
            for m in document.get("rationale", [])
        ],
        "gaps": document.get("gaps", []),
        "missing_information": by_kind.get("missing_information", []),
        "missing_permission": by_kind.get("missing_permission", []),
        "invalid": by_kind.get("invalid", []),
        "warnings": [f["message"] for f in findings if not f.get("blocking")],
        "coverage": document.get("coverage", []),
        "estimate": document.get("estimate"),
        "test_world": test_world,
    }
