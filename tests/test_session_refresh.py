"""A draft records the limits of the policy it was made under, so raising a limit in the
policy file never reached an existing session: three benchmark runs in a row stopped at the
old 20-call limit ("budget_exhausted") although the policy allowed 100. Reopening a session
now redrafts it under the current policy, and only when that changes the plan."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from aibench.cli.chat import _with_project_plugins
from tests.session_support import SessionHarness


def _budgets(ctl) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    plan = json.loads((ctl.directory / ctl.current_decision().plan_file).read_text("utf-8"))
    return plan["budgets"]["max_application_calls"], plan["budgets"]["max_evaluator_calls"]


def _raise_limits(h: SessionHarness, calls: int) -> None:
    path = h.root / "policy.json"
    policy = json.loads(path.read_text("utf-8"))
    policy["ceilings"] = {"max_application_calls": calls, "max_evaluator_calls": calls}
    path.write_text(json.dumps(policy), encoding="utf-8")


def test_raising_a_policy_limit_reaches_an_existing_session_once(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session(
        {"a": "answer", "b": "answer"},
        objectives=("catch wrong answers",),
        policy={"ceilings": {"max_application_calls": 20, "max_evaluator_calls": 20}},
    )
    try:
        assert _budgets(ctl) == (20, 20)
        revision = ctl.session.revision

        assert ctl.refresh_draft() is None  # nothing changed: same plan, no new revision
        assert ctl.session.revision == revision

        _raise_limits(h, 100)
        result = ctl.refresh_draft()
        assert result is not None and result.status == "applied", result
        assert ctl.session.revision == revision + 1 and _budgets(ctl) == (100, 100)
        assert ctl.current_decision().structured_change == {
            "refreshed": "policy or catalog changed"
        }
        assert ctl.refresh_draft() is None  # and only once
        assert ctl.session.revision == revision + 1
    finally:
        ctl.storage.db.close()


def test_reopening_a_session_refreshes_its_plan_under_the_current_policy(
    tmp_path: Path, capsys
) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session(
        {"a": "answer"},
        objectives=("catch wrong answers",),
        policy={"ceilings": {"max_application_calls": 20, "max_evaluator_calls": 20}},
    )
    try:
        revision = ctl.session.revision
        assert _with_project_plugins(ctl, h.root) is ctl  # policy unchanged: nothing happens
        assert ctl.session.revision == revision and capsys.readouterr().err == ""

        _raise_limits(h, 100)
        assert _with_project_plugins(ctl, h.root) is ctl
        assert ctl.session.revision == revision + 1 and _budgets(ctl) == (100, 100)
        assert "refreshed the plan under the project's current policy" in capsys.readouterr().err

        _with_project_plugins(ctl, h.root)  # reopened again: already current
        assert ctl.session.revision == revision + 1
    finally:
        ctl.storage.db.close()


def test_a_dataset_edited_on_disk_is_noticed_when_the_session_is_reopened(tmp_path: Path) -> None:
    """A case was removed from the dataset file; the reopened session kept saying "3 cases"
    because the plan file names the dataset by path and so hashes the same. The plan said it
    would run cases that were gone."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "x", "b": "y", "c": "z"}, objectives=("catch wrong answers",))
    try:
        dataset = Path(ctl.current_decision().choices.dataset)
        cases = dataset.read_text(encoding="utf-8").splitlines()
        assert ctl.state()["draft"]["coverage"][0]["selected_cases"] == 3
        before = ctl.session.revision
        dataset.write_text("\n".join(cases[:-1]) + "\n", encoding="utf-8")
        reopened = h.reopen(ctl)
        try:
            changed = reopened.refresh_draft()
            assert changed is not None and changed.status == "applied"
            assert reopened.session.revision == before + 1
            assert reopened.state()["draft"]["coverage"][0]["selected_cases"] == 2
            assert reopened.refresh_draft() is None  # nothing further changed
        finally:
            reopened.storage.db.close()
    finally:
        with contextlib.suppress(Exception):
            ctl.storage.db.close()
