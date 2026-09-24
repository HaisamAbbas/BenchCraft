"""Validation for multi-turn text application episodes (18-T3)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from aibench.core.errors import AibenchError, ValidationError
from aibench.core.models import (
    BenchmarkCase,
    MultiTurnTextEpisode,
    ResetPolicy,
    TextEpisodeManifest,
    deep_unfreeze,
)
from aibench.datasets.ingest import ingest_dataset
from aibench.engine.compile import load_plan
from aibench.runners import load_application


def validate_episode_manifest(
    dataset_path: Path,
    manifest_path: Path,
    plan_path: Path,
) -> tuple[TextEpisodeManifest, tuple[BenchmarkCase, ...]]:
    """Check ordered episode turns, their final-state oracle, and a real reset-capable app.

    This is a static validation: it does not invoke the application. The resulting ordinary
    dataset can then run through the existing `per_episode` engine path and
    `native.final_state` evaluator.
    """
    report = ingest_dataset(dataset_path)
    if not report.is_valid or report.manifest is None:
        errors = "; ".join(str(error) for error in report.errors[:5])
        raise ValidationError(f"dataset {dataset_path} is invalid: {errors}")
    try:
        raw: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = TextEpisodeManifest.model_validate(raw)
    except (OSError, json.JSONDecodeError, PydanticValidationError) as exc:
        raise ValidationError(f"invalid text episode manifest {manifest_path}: {exc}") from exc
    try:
        plan = load_plan(plan_path)
        if not any(
            re.fullmatch(r"native\.final_state(?:@\d+(?:\.\d+){0,2})?", binding.metric)
            for binding in plan.metrics
        ):
            raise ValidationError(
                "episode plan must bind native.final_state to evaluate the declared "
                "independent success criteria"
            )
        plan_dir = plan_path.resolve().parent
        plan_dataset = Path(plan.dataset)
        plan_application = Path(plan.application)
        if not plan_dataset.is_absolute():
            plan_dataset = plan_dir / plan_dataset
        if not plan_application.is_absolute():
            plan_application = plan_dir / plan_application
        if plan_dataset.resolve() != dataset_path.resolve():
            raise ValidationError("episode validation dataset does not match the plan's dataset")
        app = load_application(plan_application).spec
    except AibenchError as exc:
        raise ValidationError(f"invalid episode plan/application {plan_path}: {exc}") from exc
    if app.reset_policy is not ResetPolicy.PER_EPISODE:
        raise ValidationError(
            "multi-turn text episodes require application reset_policy=per_episode"
        )

    case_by_id = {case.case_id: case for case in report.cases}
    index_by_id = {case.case_id: index for index, case in enumerate(report.cases)}
    claimed: set[str] = set()
    for episode in manifest.episodes:
        _validate_episode(episode, case_by_id, index_by_id, claimed, app, plan.test_world)
    return manifest, tuple(report.cases)


def _validate_episode(
    episode: MultiTurnTextEpisode,
    case_by_id: dict[str, BenchmarkCase],
    index_by_id: dict[str, int],
    claimed: set[str],
    app: Any,
    selected_test_world: str | None,
) -> None:
    missing = [case_id for case_id in episode.case_ids if case_id not in case_by_id]
    if missing:
        raise ValidationError(
            f"episode {episode.episode_id!r} references missing case(s): {', '.join(missing)}"
        )
    if episode.test_world_id not in app.test_worlds:
        raise ValidationError(
            f"episode {episode.episode_id!r} names undeclared test world {episode.test_world_id!r}"
        )
    if selected_test_world != episode.test_world_id:
        raise ValidationError(
            f"plan test_world={selected_test_world!r} must match episode "
            f"{episode.episode_id!r} test_world_id={episode.test_world_id!r}"
        )
    indices = [index_by_id[case_id] for case_id in episode.case_ids]
    if indices != list(range(indices[0], indices[0] + len(indices))):
        raise ValidationError(
            f"episode {episode.episode_id!r} turns must be contiguous and listed in dataset order"
        )
    for case_id in episode.case_ids:
        if case_id in claimed:
            raise ValidationError(f"case {case_id!r} belongs to more than one episode")
        claimed.add(case_id)
        case = case_by_id[case_id]
        if case.group_id != episode.episode_id:
            raise ValidationError(f"case {case_id!r} must have group_id={episode.episode_id!r}")

    final_case = case_by_id[episode.case_ids[-1]]
    expectations = deep_unfreeze(final_case.expectations)
    actual_assertions = expectations.get("final_state") if isinstance(expectations, dict) else None
    expected_assertions = episode.success_criterion.model_dump(mode="json")["assertions"]
    if actual_assertions != expected_assertions:
        raise ValidationError(
            f"last turn {final_case.case_id!r} must carry the episode's independent final-state "
            "assertions in expectations.final_state"
        )
