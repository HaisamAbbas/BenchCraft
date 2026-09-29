"""Plan changes from the assistant, found blocked in a real session: an objective worded
with "never" read as the user refusing, and "yeah" to the assistant's own question could
not confirm what it offered. Refusals and unoffered values stay blocked."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

from aibench.conversation.agent import ConversationAgent, patch_problems
from aibench.core.plans import PluginEnvironmentRef
from aibench.core.sessions import PlanPatch
from aibench.planning.catalog import concepts_in
from tests.deepeval_support import PLUGIN_ENV, requires_plugin_env
from tests.session_support import ScriptedProvider, SessionHarness, patch_step, say

OBJECTIVE = (
    "Check that the answers are correct compared with the expected answers, relevant to the "
    "question, and never invent fines or rules that aren't in the traffic rules document"
)
OFFER = 'Would you like me to select "correctness" as the evaluation objective?'


def _problems(objective: str, quote: str, message: str, offer: str | None = None) -> list[str]:
    return patch_problems(PlanPatch(add_objectives=(objective,)), quote, message, offer=offer)


def test_negations_inside_the_objective_are_what_to_check() -> None:
    assert _problems(OBJECTIVE, OBJECTIVE[:40], OBJECTIVE + ".") == []
    assert (
        _problems("no hallucinated fines", "no hallucinated fines", "no hallucinated fines") == []
    )


def test_refusals_around_the_objective_still_block_it() -> None:
    blocked = ["the user's words hold back or refuse this change"]
    message = "Add the objective answers are correct. Actually, don't."
    assert _problems("answers are correct", "Add the objective", message) == blocked
    assert _problems("answers are correct", "Don't add", "Don't add answers are correct") == blocked
    assert _problems("answers are correct", "add", "add answers are correct later") == blocked


def test_a_bare_yes_confirms_exactly_what_the_question_offered() -> None:
    assert _problems("correctness", "yeah", "yeah", offer=OFFER) == []
    assert _problems("correctness", "ok", "ok, go ahead", offer=OFFER) == []
    # Not offered, not a question, or not a bare yes: the value must be the user's own.
    assert _problems("latency", "yeah", "yeah", offer=OFFER)
    assert _problems("correctness", "yeah", "yeah", offer="I selected correctness.")
    assert _problems("correctness", "yeah", "yeah", offer=None)
    assert _problems("correctness", "yeah but", "yeah but not now", offer=OFFER)


def test_everyday_wording_maps_to_the_checks_it_means() -> None:
    assert set(concepts_in(OBJECTIVE)) == {"correctness", "groundedness", "relevancy"}
    assert concepts_in("answers match the expected answers") == ("correctness",)
    assert concepts_in("it must not make up facts") == ("groundedness",)
    assert concepts_in("the app never fabricates penalties") == ("groundedness",)
    assert concepts_in("inventory lookups work") == ()
    assert concepts_in("don't care about hallucination") == ()


def test_yes_to_the_assistants_question_applies_its_proposal(tmp_path: Path) -> None:
    """End to end: the assistant asks, the user says "yeah", the offered objective lands."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"})
    provider = ScriptedProvider([say(OFFER)])
    agent = ConversationAgent(ctl, provider)
    try:
        asyncio.run(agent.handle_message("What should I check?"))
        before = ctl.session.revision
        provider.add(patch_step("yeah", add_objectives=["correctness"]), say("Added."))
        outcome = asyncio.run(agent.handle_message("yeah"))
        assert outcome.rejected == [], outcome.rejected
        assert ctl.session.revision == before + 1
        assert "correctness" in ctl.state()["draft"]["objectives"]
    finally:
        ctl.storage.db.close()


GEVAL_MESSAGE = (
    "Add a G-Eval check named traffic correctness with criteria: the answer gives the same "
    "fine amounts, speed limits and conditions as the expected answer, and says the "
    "information is not available when the Tamil Nadu rules do not cover the question. "
    "Evaluation params: input, actual_output, expected_output"
)
CRITERIA = (
    "the answer gives the same fine amounts, speed limits and conditions as the expected "
    "answer, and says the information is not available when the Tamil Nadu rules do not "
    "cover the question"
)


def test_negations_inside_copied_g_eval_criteria_are_what_to_check() -> None:
    patch = PlanPatch(
        add_objectives=("G-Eval check named traffic correctness",),
        params={
            "deepeval.g_eval": {
                "name": "traffic correctness",
                "criteria": CRITERIA,
                "evaluation_params": ["input", "actual_output", "expected_output"],
            }
        },
    )
    assert patch_problems(patch, "Add a G-Eval check", GEVAL_MESSAGE) == []
    # The same criteria followed by a real refusal is still refused.
    refused = GEVAL_MESSAGE + ". Actually, don't add it yet."
    assert patch_problems(patch, "Add a G-Eval check", refused) == [
        "the user's words hold back or refuse this change"
    ]


def test_every_change_the_model_tried_for_the_g_eval_message_is_accepted() -> None:
    """The three proposals from a real session (rc4 still refused all of them): the
    objective alone, with the settings, and the whole message as the objective. The "not"
    words in the criteria are what to check, whether or not the change copies them."""
    quote = "Add a G-Eval check named traffic correctness"
    settings = {
        "deepeval.g_eval": {"name": "traffic correctness", "criteria": CRITERIA},
        "traffic_correctness": {"criteria": CRITERIA},
    }
    for patch in (
        PlanPatch(add_objectives=("traffic correctness",)),
        PlanPatch(
            add_objectives=("traffic correctness",),
            params={"deepeval.g_eval": settings["deepeval.g_eval"]},
        ),
        PlanPatch(
            add_objectives=("traffic correctness",),
            params={"traffic_correctness": settings["traffic_correctness"]},
        ),
        PlanPatch(add_objectives=(GEVAL_MESSAGE,)),
    ):
        assert patch_problems(patch, quote, GEVAL_MESSAGE) == [], patch


def test_taking_a_request_back_still_blocks_a_plan_change() -> None:
    """A real refusal, wherever it is in the message, blocks the change."""
    patch = PlanPatch(add_objectives=("answers are correct",))
    blocked = ["the user's words hold back or refuse this change"]
    for message in (
        "Add answers are correct. Actually, don't.",
        "Add answers are correct. Actually, don't add it.",
        "Add answers are correct. Hold off on that.",
        "Add answers are correct. Not yet.",
        "Add answers are correct, but wait.",
        "Add answers are correct. Never mind.",
        "Add answers are correct. Actually do it later.",
        "Add answers are correct. I don't want that.",
        "Add answers are correct. Please do not change the plan.",
        "Add answers are correct. Let’s not add it.",
        "Don't add answers are correct",
    ):
        assert patch_problems(patch, message.split(".")[0][:14], message) == blocked, message


def test_ordinary_negative_wording_around_a_request_is_not_a_refusal() -> None:
    patch = PlanPatch(add_objectives=("answers are correct",))
    for message in (
        "Add answers are correct. The app should never guess.",
        "Add answers are correct. There is no reference for the last case.",
        "Add answers are correct, and it must not invent fines.",
        "Add answers are correct. I am not sure about latency, so skip that.",
        "Add answers are correct and never make up fines.",
        "Add answers are correct. The answer should not add facts beyond the documents.",
        "Add answers are correct; it does not need to be polite.",
    ):
        assert patch_problems(patch, "Add answers are correct", message) == [], message


def test_a_quote_spanning_several_sentences_is_checked_from_where_it_starts() -> None:
    """The real model quoted the whole two-sentence message (the sentence break before
    "Evaluation params"), and the old rule refused any quote that did not fit inside one
    sentence, with no refusal wording found."""
    patch = PlanPatch(add_objectives=("traffic correctness",))
    assert patch_problems(patch, GEVAL_MESSAGE, GEVAL_MESSAGE) == []
    two = "Add traffic correctness. It compares with the expected answer"
    assert patch_problems(patch, two, two) == []
    later = two + ". Actually, don't add it."
    assert patch_problems(patch, two, later) == ["the user's words hold back or refuse this change"]
    # A refusal before the quoted request does not cancel it.
    earlier = "Don't add latency. Add traffic correctness. It compares with the expected answer"
    assert patch_problems(patch, two, earlier) == []


def _planned_g_eval(ctl) -> dict:
    """The G-Eval settings in the plan file the session would run."""
    plan_path = ctl.directory / ctl.current_decision().plan_file
    bindings = json.loads(plan_path.read_text(encoding="utf-8"))["metrics"]
    return next(b["params"] for b in bindings if b["metric"].split("@")[0] == "deepeval.g_eval")


@requires_plugin_env
def test_g_eval_settings_from_the_assistant_apply_plan_and_survive_reopening(
    tmp_path: Path,
) -> None:
    """Recording a patch with `params` crashed (`mappingproxy` was not serializable), so the
    assistant could never configure G-Eval. Through a real session: the settings apply, the
    draft plans G-Eval with these criteria, and a reopened session still has them."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session(
        {"a": "answer"},
        objectives=("catch wrong answers",),
        policy={
            "data_roots": [str(tmp_path)],
            "allowed_evaluators": ["native.*", "deepeval.*"],
            "allowed_plugin_environments": [str(PLUGIN_ENV)],
            "allow_model_evaluators": True,
        },
    )
    try:
        loaded = ctl.use_plugin_environments((PluginEnvironmentRef(python=str(PLUGIN_ENV)),), {})
        assert loaded.status == "applied", loaded.problems
        judge = {"kind": "python_factory", "factory": "aibench_schema_judges:agreeing_judge"}
        settings = {
            "name": "traffic correctness",
            "criteria": CRITERIA,
            "evaluation_params": ["input", "actual_output", "expected_output"],
            "judge": judge,
        }
        result = ctl.apply_patch(
            PlanPatch(
                add_objectives=("G-Eval check named traffic correctness",),
                params={"deepeval.g_eval": settings},
            ),
            expected_revision=ctl.session.revision,
            source="user",
        )
        assert result.status == "applied", result.problems
        assert _planned_g_eval(ctl)["criteria"] == CRITERIA

        reopened = h.reopen(ctl)
        try:
            assert _planned_g_eval(reopened)["criteria"] == CRITERIA
        finally:
            reopened.storage.db.close()
    finally:
        with contextlib.suppress(Exception):  # reopen() already closed it
            ctl.storage.db.close()


@requires_plugin_env
def test_a_metric_the_user_configured_is_planned_even_if_the_objective_does_not_name_it(
    tmp_path: Path,
) -> None:
    """The real model added the objective "traffic correctness" with G-Eval settings; the
    planner chose metrics by the objective's words alone, so the settings were saved but no
    G-Eval metric ran, while the assistant said it had added one. Metrics the user
    configured are planned; project defaults (the judge) alone do not select anything."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session(
        {"a": "answer"},
        objectives=("catch wrong answers",),
        policy={
            "data_roots": [str(tmp_path)],
            "allowed_evaluators": ["native.*", "deepeval.*"],
            "allowed_plugin_environments": [str(PLUGIN_ENV)],
            "allow_model_evaluators": True,
        },
    )
    try:
        judge = {"kind": "python_factory", "factory": "aibench_schema_judges:agreeing_judge"}
        defaults = {"deepeval.*": {"judge": judge}}
        loaded = ctl.use_plugin_environments(
            (PluginEnvironmentRef(python=str(PLUGIN_ENV)),), defaults
        )
        assert loaded.status == "applied", loaded.problems
        names = {m["metric"].split("@")[0] for m in ctl.state()["draft"]["metrics"]}
        assert not any(n.startswith("deepeval.") for n in names), names  # defaults select nothing

        result = ctl.apply_patch(
            PlanPatch(
                add_objectives=("traffic correctness",),
                params={"deepeval.g_eval": {"name": "traffic correctness", "criteria": CRITERIA}},
            ),
            expected_revision=ctl.session.revision,
            source="user",
        )
        assert result.status == "applied", result.problems
        planned = {m["metric"].split("@")[0]: m for m in ctl.state()["draft"]["metrics"]}
        assert "deepeval.g_eval" in planned
        assert _planned_g_eval(ctl)["criteria"] == CRITERIA
        assert not any(n.startswith("deepeval.") and n != "deepeval.g_eval" for n in planned)
    finally:
        ctl.storage.db.close()
