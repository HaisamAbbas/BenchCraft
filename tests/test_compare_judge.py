"""`/compare BASELINE CURRENT --judge "CRITERIA"`: beside the stored comparison, a judge says
case by case which run's answer is better (DeepEval's ArenaGEval in its worker). Each case is
judged with the answers in both orders, so a judge that leans towards a position cannot decide
a case: a split verdict is a tie. The arena metric is never a plan metric."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any

from rich.console import Console

from aibench.core.plans import PluginEnvironmentRef
from aibench.core.sessions import PlanPatch
from aibench.tui import render
from aibench.tui.commands import Commands
from tests.deepeval_support import JUDGES, PLUGIN_ENV, requires_plugin_env
from tests.session_support import SessionHarness

pytestmark = requires_plugin_env

ARENA_JUDGE = {"kind": "python_factory", "factory": "aibench_test_judges:arena_judge"}

# The harness app, answering from answers.json so two runs of the same cases can differ.
APP = r"""
import json, os, sys, pathlib
request = json.load(sys.stdin)
answers = json.loads(pathlib.Path("answers.json").read_text())
print(json.dumps({"output": answers[request["case_id"]]}))
"""


def _session(tmp_path: Path) -> Any:
    h = SessionHarness(tmp_path)
    (h.root / "app.py").write_text(APP, encoding="utf-8")
    ctl = h.open_session(
        {"a": "How long do refunds take?", "b": "Can I return a used item?", "c": "Hi?"},
        objectives=("catch wrong answers",),
        policy={
            "data_roots": [str(tmp_path)],
            "allowed_evaluators": ["native.*", "deepeval.*"],
            "allowed_plugin_environments": [str(PLUGIN_ENV)],
            "allowed_plugin_paths": [str(JUDGES)],
            "allow_model_evaluators": True,
        },
    )
    environment = PluginEnvironmentRef(python=str(PLUGIN_ENV), paths=(str(JUDGES),))
    loaded = ctl.use_plugin_environments((environment,), {"deepeval.*": {"judge": ARENA_JUDGE}})
    assert loaded.status == "applied", loaded.problems
    return h, ctl


async def _run(h: SessionHarness, ctl: Any, answers: dict[str, str], action: str) -> str:
    (h.root / "answers.json").write_text(json.dumps(answers), encoding="utf-8")
    started = await ctl.start_run(action_id=action, expected_revision=ctl.session.revision)
    done = await ctl.wait_for_run(started.run_id)
    assert done is not None and done.counts["execution"] == {"succeeded": 3}, done
    return started.run_id


def test_a_judge_says_which_runs_answers_are_better_and_a_split_verdict_is_a_tie(
    tmp_path: Path,
) -> None:
    h, ctl = _session(tmp_path)

    async def scenario() -> Any:
        baseline = await _run(
            h, ctl, {"a": "BEST: refunds take 30 days.", "b": "Maybe.", "c": "Hello."}, "run-1"
        )
        current = await _run(
            h, ctl, {"a": "Not sure.", "b": "BEST: no, items must be unused.", "c": "Hi."}, "run-2"
        )
        commands = Commands(ctl)
        return (
            baseline,
            current,
            await commands.run(
                f'/compare {baseline} {current} --judge "the more helpful and correct answer"'
            ),
        )

    try:
        baseline, current, result = asyncio.run(scenario())
        assert result.ok or "arena" in result.data, result.data
        arena = result.data["arena"]
        assert arena["criteria"] == "the more helpful and correct answer"
        assert arena["judge"] == "aibench_test_judges:arena_judge"  # not just "judge"
        verdicts = {row["case_id"]: row["verdict"] for row in arena["rows"]}
        assert "error" not in verdicts.values(), arena["rows"]
        # c: neither answer stands out, and the judge picks whichever it reads first: the
        # two orders disagree, so it is a tie, not a win for one position.
        assert verdicts == {"a": "baseline", "b": "current", "c": "tie"}
        assert arena["counts"] == {
            "current": 1,
            "baseline": 1,
            "tie": 1,
            "not_applicable": 0,
            "error": 0,
        }
        assert "metrics" in result.data  # the stored comparison is still there

        console = Console(file=io.StringIO(), width=160, highlight=False)
        render.comparison(console, result.data)
        shown = " ".join(console.file.getvalue().split())  # type: ignore[attr-defined]
        assert "judged head-to-head" in shown
        assert "current better 1 of 3" in shown and "baseline better 1 of 3" in shown
        assert "tie 1 of 3" in shown
        assert "(aibench_test_judges:arena_judge, each case in both orders)" in shown
        # The verdict once, then the judge's reason with the answers named plainly (it read
        # "a: baseline: baseline: $baseline$ ...").
        assert "a: baseline: baseline and current compared" in shown, shown
        assert "baseline: baseline:" not in shown and "$" not in shown

        plain = asyncio.run(Commands(ctl).run(f"/compare {baseline} {current}"))
        assert "arena" not in plain.data  # no judge unless asked for
    finally:
        ctl.storage.db.close()


def test_compare_with_judge_explains_wrong_use(tmp_path: Path) -> None:
    _, ctl = _session(tmp_path)
    try:
        commands = Commands(ctl)
        missing = asyncio.run(commands.run("/compare run-1 run-2 --judge"))
        assert not missing.ok and "--judge" in missing.data["error"]
    finally:
        ctl.storage.db.close()


def test_the_arena_metric_is_never_a_plan_metric(tmp_path: Path) -> None:
    """It needs a second run's answers, which a run of one plan does not have."""
    _, ctl = _session(tmp_path)
    try:
        option = next(o for o in ctl.inputs().catalog if o.evaluator_id == "deepeval.arena_g_eval")
        assert not option.eligible
        assert any("/compare" in reason for reason in option.reasons)
        result = ctl.apply_patch(
            PlanPatch(
                add_objectives=("judge two runs",),
                params={"deepeval.arena_g_eval": {"criteria": "better"}},
            ),
            expected_revision=ctl.session.revision,
            source="user",
        )
        assert result.status != "applied"
    finally:
        ctl.storage.db.close()
