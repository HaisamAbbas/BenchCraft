"""Strict faithfulness for a whole project. DeepEval's faithfulness counts a claim the
retrieved passages say nothing about as faithful (only contradictions lower it), so invented
detail scored 1.0 on a real LightRAG run; `penalize_ambiguous_claims` counts such claims as
unfaithful. A project can make that its default in `aibench.json`, beside the judge, and every
faithfulness binding the planner makes then uses it, while metrics without the setting are
untouched and a user's own value still wins."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aibench.cli.chat import _with_project_plugins
from aibench.core.sessions import PlanPatch
from aibench.sessions.controller import SessionController
from tests.deepeval_support import PLUGIN_ENV, requires_plugin_env
from tests.planning_support import write_app
from tests.session_support import SessionHarness

pytestmark = requires_plugin_env

JUDGE = {"kind": "python_factory", "factory": "aibench_schema_judges:agreeing_judge"}
POLICY = {
    "allowed_evaluators": ["native.*", "deepeval.*"],
    "allowed_plugin_environments": [str(PLUGIN_ENV)],
    "allow_model_evaluators": True,
}


def _planned(ctl: Any, metric: str) -> dict[str, Any] | None:
    """The settings in the plan file the session would run."""
    plan_path = ctl.directory / ctl.current_decision().plan_file
    for binding in json.loads(plan_path.read_text(encoding="utf-8"))["metrics"]:
        if binding["metric"].split("@")[0] == metric:
            return dict(binding.get("params") or {})
    return None


def _session(tmp_path: Path, defaults: dict[str, Any]) -> Any:
    h = SessionHarness(tmp_path)
    # A RAG app: it declares the passages it retrieved, which faithfulness reads.
    app = write_app(h.root, output_binding={"output": "/answer", "retrieved_context": "/ctx"})
    policy = h.root / "policy.json"
    policy.write_text(
        json.dumps(
            {
                **POLICY,
                "data_roots": [str(tmp_path)],
                "allowed_applications": ["support-bot"],
                "allowed_http_origins": ["http://127.0.0.1:9/"],
                "allowed_egress_origins": ["http://127.0.0.1:9/"],
            }
        ),
        encoding="utf-8",
    )
    storage, artifacts = h.storage()
    ctl = SessionController.create(
        storage=storage,
        artifacts=artifacts,
        workspace_root=h.workspace.root,
        project_root=h.root,
        application=app,
        dataset=h.root / h.dataset({"a": "answer"}),
        objectives=("catch wrong answers",),
        policy_path=policy,
    )
    (h.root / "aibench.json").write_text(
        json.dumps(
            {
                "plugin_environments": [
                    {"name": "deepeval", "python": str(PLUGIN_ENV), "default_params": defaults}
                ]
            }
        ),
        encoding="utf-8",
    )
    _with_project_plugins(ctl, h.root)
    return ctl


def test_a_project_default_makes_every_planned_faithfulness_strict(tmp_path: Path) -> None:
    ctl = _session(
        tmp_path,
        {
            "deepeval.*": {"judge": JUDGE},
            "deepeval.faithfulness": {"penalize_ambiguous_claims": True},
        },
    )
    try:
        result = ctl.apply_patch(
            PlanPatch(
                add_objectives=("faithfulness, and a G-Eval check named polite",),
                params={
                    "deepeval.faithfulness": {"truths_extraction_limit": 5},
                    "deepeval.g_eval": {"criteria": "Is the answer polite?"},
                },
            ),
            expected_revision=ctl.session.revision,
            source="user",
        )
        assert result.status == "applied", result.problems
        faithfulness = _planned(ctl, "deepeval.faithfulness")
        assert faithfulness is not None, ctl.state()["plan"]
        assert faithfulness["penalize_ambiguous_claims"] is True
        assert faithfulness["truths_extraction_limit"] == 5  # the user's own setting is kept
        assert faithfulness["judge"] == JUDGE
        g_eval = _planned(ctl, "deepeval.g_eval")
        assert g_eval is not None and "penalize_ambiguous_claims" not in g_eval
    finally:
        ctl.storage.db.close()


def test_the_users_own_value_wins_over_the_project_default(tmp_path: Path) -> None:
    ctl = _session(
        tmp_path,
        {
            "deepeval.*": {"judge": JUDGE},
            "deepeval.faithfulness": {"penalize_ambiguous_claims": True},
        },
    )
    try:
        result = ctl.apply_patch(
            PlanPatch(
                add_objectives=("faithfulness without penalize_ambiguous_claims",),
                params={"deepeval.faithfulness": {"penalize_ambiguous_claims": False}},
            ),
            expected_revision=ctl.session.revision,
            source="user",
        )
        assert result.status == "applied", result.problems
        faithfulness = _planned(ctl, "deepeval.faithfulness")
        assert faithfulness is not None and faithfulness["penalize_ambiguous_claims"] is False
    finally:
        ctl.storage.db.close()
