"""Regressions for the independent review of Prompt 08. Each test failed on the reviewed
code and asserts the corrected behaviour."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from aibench.conversation.agent import ConversationAgent, authorization_problem, patch_problems
from aibench.core.sessions import ActionKind, ActionState, PlanPatch
from aibench.sessions.controller import reason_code
from tests.session_support import ScriptedProvider, SessionHarness, call, say, start_step

START, PAUSE, RESUME, CANCEL = (
    ActionKind.START_RUN,
    ActionKind.PAUSE_RUN,
    ActionKind.RESUME_RUN,
    ActionKind.CANCEL_RUN,
)


@pytest.mark.parametrize(
    ("message", "quote"),
    [
        ("I don't want you to run the benchmark.", "run the benchmark"),
        ("I'm not sure we should run it yet.", "run it yet"),
        ("Please hold off on running it.", "running it"),
        ("Never run it without asking me.", "run it"),
        ("No, run it later.", "run it later"),
    ],
)
def test_negated_requests_do_not_authorize(message: str, quote: str) -> None:
    """Review #1: negation anywhere before the verb in the sentence, not within 2 words."""
    assert authorization_problem(START, quote, message, offered=True) is not None


def test_a_later_refusal_overrides_an_earlier_action_request() -> None:
    """A later correction in the same message must invalidate an earlier authorization."""
    message = "Start the run. Actually, don't."
    assert authorization_problem(START, "Start the run.", message, offered=False) is not None


def test_a_later_refusal_overrides_an_earlier_plan_patch() -> None:
    message = "Use 2 cases. Actually, don't change the plan."
    assert patch_problems(PlanPatch(sample={"size": 2}), "Use 2 cases.", message)


@pytest.mark.parametrize(
    ("message", "quote"),
    [
        ("I'm not sure about this plan.", "sure"),
        ("Hmm, yes the numbers are low, but let me think.", "yes"),
        ("Ok the metrics look odd, explain coverage first.", "Ok"),
    ],
)
def test_an_affirmation_must_be_the_whole_reply(message: str, quote: str) -> None:
    """Review #2: a 'yes' buried in another sentence is not a reply to the offer."""
    assert authorization_problem(START, quote, message, offered=True) is not None


@pytest.mark.parametrize("message", ["yes", "Yes, please.", "ok", "Sure!", "yes go ahead"])
def test_a_plain_affirmation_accepts_the_offer(message: str) -> None:
    assert authorization_problem(START, message.split(",")[0], message, offered=True) is None
    assert authorization_problem(START, message.split(",")[0], message, offered=False) is not None


@pytest.mark.parametrize(
    ("kind", "message", "quote"),
    [
        (CANCEL, "Stop explaining and list the failures.", "Stop"),
        (RESUME, "Continue with the explanation.", "Continue"),
        (START, "Run through the results with me.", "Run through the results"),
        (PAUSE, "Hold on, what does coverage mean.", "Hold on"),
    ],
)
def test_a_verb_must_be_about_the_run(kind: ActionKind, message: str, quote: str) -> None:
    """Review #4: ordinary chat verbs are not run controls."""
    assert authorization_problem(kind, quote, message, offered=False) is not None


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        (START, "Run it."),
        (START, "Please start the pilot."),
        (START, "Run the benchmark now"),
        (CANCEL, "Stop the run."),
        (CANCEL, "cancel it"),
        (PAUSE, "pause"),
        (PAUSE, "Pause the run please."),
        (RESUME, "Resume the run"),
        (RESUME, "continue it"),
    ],
)
def test_explicit_run_requests_still_authorize(kind: ActionKind, message: str) -> None:
    assert authorization_problem(kind, message.rstrip("."), message, offered=False) is None


def test_every_patch_field_must_be_grounded_in_the_users_words() -> None:
    """Review #5: booleans, parameter names, comparators, removals, concepts, all_cases."""
    patch = PlanPatch(
        params={"native.exact_match": {"case_sensitive": False}},
        rules={"native.exact_match": {"comparator": "is_true"}},
        remove_objectives=("catch wrong answers",),
        objective_concepts={"catch wrong answers": ("groundedness",)},
        all_cases=True,
    )
    problems = patch_problems(patch, "hello there", "hello there")
    text = " ".join(problems)
    for expected in ("case_sensitive", "comparator", "remove", "groundedness", "all cases"):
        assert expected in text, (expected, problems)
    short = PlanPatch(params={"native.json_schema": {"schema_field": "a"}})
    assert patch_problems(short, "make a check", "make a check")  # "a" is not a stated value


def test_grounded_patches_are_accepted() -> None:
    message = (
        "Remove 'catch wrong answers', treat it as correctness, use all cases, case sensitive false"
    )
    patch = PlanPatch(
        remove_objectives=("catch wrong answers",),
        all_cases=True,
    )
    assert patch_problems(patch, "use all cases", message) == []
    sensitive = PlanPatch(params={"native.exact_match": {"case_sensitive": False}})
    assert patch_problems(sensitive, "case sensitive false", message) == []


def test_a_patch_the_user_said_not_to_make_is_refused() -> None:
    """Review #6."""
    message = "Please don't change repetitions to 3."
    assert patch_problems(PlanPatch(repetitions=3), "repetitions to 3", message)


def test_reading_the_state_is_not_showing_the_plan(tmp_path: Path) -> None:
    """Review #3: only a displayed plan card (show_plan, or a patch result) presents a
    revision; the model merely reading state does not."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "hi", "b": "hi"}, objectives=("catch wrong answers",))
    provider = ScriptedProvider(
        [call("get_session_state"), say("Nothing much."), start_step("Run it"), say("ok")]
    )
    agent = ConversationAgent(ctl, provider)

    async def scenario() -> None:
        quiet = await agent.handle_message("anything new?")
        assert quiet.presented_draft is None and ctl.session.presented_revision is None
        reply = await agent.handle_message("Run it.")
        assert reply.actions == [] and "has not been shown" in reply.rejected[0]["problems"][0]

    asyncio.run(scenario())
    shown = ScriptedProvider([call("show_plan"), say("Here is the plan. Run it?")])
    outcome = asyncio.run(ConversationAgent(ctl, shown).handle_message("show me the plan"))
    assert outcome.presented_draft is not None and outcome.presented_draft["revision"] == 1
    assert ctl.session.presented_revision == 1
    assert h.runs() == []
    ctl.storage.db.close()


def test_free_text_reasons_never_reach_the_assistant_without_permission(tmp_path: Path) -> None:
    """Review #7/#8: run_status's needs_attention reasons and colon-less reasons are
    reduced to codes for the assistant."""
    assert reason_code("not_applicable: missing case.reference.answer") == "not_applicable"
    assert reason_code("The score is 0.4 because the output says SECRET") is None
    h = SessionHarness(tmp_path)
    rows = [{"case_id": "x", "input": "crash", "expected_output": "SECRET-REF"}]
    ctl = h.open_session({}, rows=rows, objectives=("catch wrong answers",))

    async def scenario() -> None:
        start = await ctl.start_run(action_id="act-1", expected_revision=1)
        await ctl.wait_for_run(start.run_id)
        ctl._live[start.run_id].error = "RuntimeError: case output SECRET-OUTPUT"
        provider = ScriptedProvider([call("get_run_status"), say("One case failed.")])
        await ConversationAgent(ctl, provider).handle_message("status?")
        sent = json.dumps(provider.calls)
        user_reasons = [i["reason"] for i in ctl.run_status()["needs_attention"]]
        assert user_reasons and all(r for r in user_reasons)
        for reason in user_reasons:
            assert json.dumps(reason)[1:-1] not in sent  # the free text stayed local
        assert "SECRET-OUTPUT" not in sent
        assistant = ctl.run_status(for_assistant=True)["needs_attention"]
        assert all(i["reason"] is None or " " not in i["reason"] for i in assistant)
        assert ctl.run_status(for_assistant=True)["session_error"] is None
        assert ctl.run_status()["session_error"] == "RuntimeError: case output SECRET-OUTPUT"

    asyncio.run(scenario())
    ctl.storage.db.close()


def test_repeated_pause_or_resume_is_not_reported_as_done(tmp_path: Path) -> None:
    """Review #10."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session({c: "slow 0.3" for c in "abcd"}, objectives=("catch wrong answers",))

    async def scenario() -> None:
        start = await ctl.start_run(action_id="act-start", expected_revision=1)
        await h.wait_for_invocations(1)
        first = await ctl.control_run(PAUSE, action_id="p1")
        second = await ctl.control_run(PAUSE, action_id="p2")
        assert first.state is ActionState.DONE and second.state is ActionState.REJECTED
        assert "already paused" in (second.reason or "")
        resumed = await ctl.control_run(RESUME, action_id="r1")
        again = await ctl.control_run(RESUME, action_id="r2")
        assert resumed.state is ActionState.DONE and again.state is ActionState.REJECTED
        await ctl.wait_for_run(start.run_id)

    asyncio.run(scenario())
    ctl.storage.db.close()


def test_unexpected_start_failures_are_reported_and_free_the_slot(tmp_path: Path) -> None:
    """Review #11: a non-AibenchError from create_run settles the action and frees the
    run slot."""
    import aibench.sessions.controller as controller_module

    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "hi"}, objectives=("catch wrong answers",))
    original = controller_module.create_run

    def failing(*args: object, **kwargs: object) -> str:
        raise OSError("disk full")

    controller_module.create_run = failing  # type: ignore[assignment]
    try:
        action = asyncio.run(ctl.start_run(action_id="act-1", expected_revision=1))
    finally:
        controller_module.create_run = original
    assert action.state is ActionState.REJECTED and "disk full" in (action.reason or "")
    assert ctl.session.active_run_id is None
    ctl.storage.db.close()
