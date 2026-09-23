"""08-T2..T4 through the conversation loop, with a deterministic scripted assistant model
against a real instrumented application, the real engine and a real workspace.

08-G1: a multi-turn dialogue changes the case sample, answers why a metric was selected
       and starts a real fixture run.
08-G2: a follow-up question during execution leaves the run running.
08-G3: repeated delivery cannot start a duplicate run.
08-G4: user corrections persist; a stale model patch cannot overwrite a newer choice.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from aibench.conversation.agent import TOOL_NAMES, ConversationAgent, TurnLimits
from aibench.core.sessions import ActionKind, ActionState, PlanPatch
from aibench.planning.planner import ModelReply
from tests.session_support import (
    ScriptedProvider,
    SessionHarness,
    call,
    patch_step,
    revision_seen,
    say,
    start_step,
)

FOUR = {"a": "hi", "b": "hi", "c": "hi", "d": "hi"}


def test_dialogue_changes_the_sample_explains_a_metric_and_starts_a_real_run(
    tmp_path: Path,
) -> None:
    """08-G1."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session(FOUR)
    provider = ScriptedProvider([])
    agent = ConversationAgent(ctl, provider)

    async def dialogue() -> None:
        # Turn 1: the user states an objective; the model records it verbatim.
        provider.add(
            call("get_session_state"),
            patch_step("catch wrong answers", add_objectives=["catch wrong answers"]),
            say("Drafted an exact-match check for wrong answers. Run it?"),
        )
        first = await agent.handle_message("I want to catch wrong answers.")
        assert first.decisions and first.decisions[-1]["revision"] == 2
        assert first.presented_draft and first.presented_draft["executable"]
        assert first.offer == {"action": "start_run", "revision": 2}

        # Turn 2: a seeded pilot sample.
        provider.add(patch_step("Use 2 cases first", sample={"size": 2}), say("Now 2 cases."))
        second = await agent.handle_message("Use 2 cases first.")
        assert second.decisions[-1]["revision"] == 3
        selection = ctl.state()["choices"]["selection"]
        assert selection["sample_size"] == 2 and isinstance(selection["seed"], int)
        assert second.presented_draft["estimate"]["selected_cases"] == 2

        # Turn 3: why this metric? Answered from the draft's own rationale.
        provider.add(
            call("explain_metric", metric="native.exact_match"),
            say("It measures correctness against your reference answers."),
        )
        third = await agent.handle_message("Why was exact match selected?")
        (explanation,) = third.explained
        draft_rationale = ctl.current_decision().draft["rationale"][0]["rationale"]
        assert explanation["rationale"] == draft_rationale
        assert explanation["serves_objectives"] == ["catch wrong answers"]
        assert not third.decisions and not third.actions
        assert "explained" in third.status_line and "no action taken" in third.status_line

        # Turn 4: run it — the reviewed revision 3.
        provider.add(start_step("Run it"), say("Started."))
        fourth = await agent.handle_message("Run it.")
        (action,) = fourth.actions
        assert action["state"] == "done" and action["expected_revision"] == 3
        assert fourth.status_line.startswith(f"started run {action['run_id']}")
        outcome = await ctl.wait_for_run(action["run_id"])
        assert outcome is not None and outcome.state.value == "completed"

    asyncio.run(dialogue())
    assert h.count() == 2  # the seeded 2-case pilot, really invoked
    record = ctl.storage.get_run(ctl.session.active_run_id)
    assert record.manifest.plan_hash == ctl.store.decision_at(ctl.session_id, 3).plan_hash
    turns = ctl.store.turns(ctl.session_id)
    assert [t.role for t in turns] == ["user", "assistant"] * 4
    assert turns[3].decision_refs == (f"{ctl.session_id}:d3",)
    ctl.storage.db.close()


def test_a_question_during_execution_leaves_the_run_running(tmp_path: Path) -> None:
    """08-G2."""
    h = SessionHarness(tmp_path)
    slow = {c: "slow 0.6" for c in ("a", "b", "c", "d")}
    ctl = h.open_session(slow, objectives=("catch wrong answers",))
    provider = ScriptedProvider(
        [
            call("explain_metric", metric="native.exact_match"),
            call("get_run_status"),
            say("Exact match compares the answer to your reference. The run continues."),
        ]
    )
    agent = ConversationAgent(ctl, provider)

    async def scenario() -> None:
        start = await ctl.start_run(action_id="act-start", expected_revision=1)
        run_id = start.run_id
        await h.wait_for_invocations(1)
        before = ctl.run_status(run_id)["status"]
        reply = await agent.handle_message("What does exact match measure?")
        after = ctl.run_status(run_id)
        assert before == "running" and after["status"] == "running"
        assert reply.actions == [] and reply.decisions == []
        assert reply.results[0]["provisional"] is True
        assert f"run {run_id} continues (running)" in reply.status_line
        outcome = await ctl.wait_for_run(run_id)
        assert outcome is not None and outcome.state.value == "completed"
        assert outcome.counts["execution"] == {"succeeded": 4}

    asyncio.run(scenario())
    assert h.count() == 4
    ctl.storage.db.close()


def test_redelivery_and_retried_turns_cannot_start_a_second_run(tmp_path: Path) -> None:
    """08-G3."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session(FOUR, objectives=("catch wrong answers",))
    ctl.mark_presented(1)
    provider = ScriptedProvider([start_step("Run it"), say("Started.")])
    agent = ConversationAgent(ctl, provider)

    async def scenario() -> None:
        first = await agent.handle_message("Run it.", message_id="m-1")
        replay = await agent.handle_message("Run it.", message_id="m-1")  # redelivery
        assert replay.replayed and replay.actions == first.actions
        assert len(provider.calls) == 2  # the model was not called again

        # A turn that crashed after acting is retried: its derived action ID is the same.
        user_turn, _ = ctl.store.append_turn(
            ctl.store.turns(ctl.session_id)[0].model_copy(
                update={"turn_id": "turn-crashed", "message_id": "m-2"}
            )
        )
        action_id = agent.action_id(user_turn, ActionKind.START_RUN, 1, None)
        await ctl.wait_for_run(first.actions[0]["run_id"])
        before_crash = await ctl.start_run(action_id=action_id, expected_revision=1)
        await ctl.wait_for_run(before_crash.run_id)
        provider.add(start_step("Run it"), say("Started."))
        retried = await agent.handle_message("Run it.", message_id="m-2")
        assert retried.actions[0]["run_id"] == before_crash.run_id

        # Concurrent deliveries of one typed action: one run.
        both = await asyncio.gather(
            ctl.start_run(action_id="act-typed", expected_revision=1),
            ctl.start_run(action_id="act-typed", expected_revision=1),
        )
        # The second delivery gets the stored record (still starting); it never acts.
        assert {a.action_id for a in both} == {"act-typed"}
        run_id = ctl.store.get_action("act-typed").run_id
        assert run_id is not None and both[0].run_id == run_id
        await ctl.wait_for_run(run_id)

    asyncio.run(scenario())
    assert len(h.runs()) == 3  # first, the crashed turn's, the typed one — never duplicates
    assert h.count() == 12
    ctl.storage.db.close()


def test_user_corrections_persist_and_stale_model_patches_are_rejected(tmp_path: Path) -> None:
    """08-G4: the model drafts a patch from revision 2 while the user corrects the sample
    with a typed command; the delayed patch arrives stale and changes nothing."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session(FOUR, objectives=("catch wrong answers",))
    ctl.apply_patch(PlanPatch(limit=4), expected_revision=1)  # revision 2
    thinking, corrected = threading.Event(), threading.Event()

    def delayed(messages: list[dict[str, object]]) -> ModelReply:
        seen = revision_seen(messages)  # 2: what the model was briefed with
        thinking.set()
        assert corrected.wait(10)
        return call(
            "propose_plan_patch",
            expected_revision=seen,
            user_quote="use 3 cases",
            patch={"limit": 3},
        )

    provider = ScriptedProvider([delayed, say("Done.")])
    agent = ConversationAgent(ctl, provider)

    async def scenario() -> None:
        turn = asyncio.ensure_future(agent.handle_message("Please use 3 cases."))
        while not thinking.is_set():
            await asyncio.sleep(0.01)
        command = ctl.record_command("/sample 2", message_id="c-1")
        result = ctl.apply_patch(
            PlanPatch(sample={"size": 2, "seed": 7}),
            expected_revision=2,
            source_turn_id=command.turn_id,
        )
        assert result.status == "applied" and result.revision == 3
        corrected.set()
        outcome = await turn
        assert outcome.decisions == []
        (rejected,) = outcome.rejected
        assert rejected["status"] == "stale"
        assert "revision 2" in rejected["problems"][0]

    asyncio.run(scenario())
    reopened = h.reopen(ctl)
    selection = reopened.state()["choices"]["selection"]
    assert selection == {"sample_size": 2, "seed": 7}  # the user's correction persisted
    decision = reopened.store.decision_at(reopened.session_id, 3)
    assert decision.source == "user" and decision.source_turn_id is not None
    reopened.storage.db.close()


def test_stale_answers_to_questions_cannot_apply(tmp_path: Path) -> None:
    """08-G4: a question asked against revision 1 cannot be answered once the draft moved."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session(FOUR)
    (question,) = ctl.store.questions(ctl.session_id, "open")
    assert question.draft_revision == 1
    ctl.apply_patch(PlanPatch(limit=2), expected_revision=1)  # the question is re-asked at r2
    stale = ctl.apply_patch(
        PlanPatch(add_objectives=("catch wrong answers",), answers=(question.question_id,)),
        expected_revision=1,
    )
    assert stale.status == "stale"
    reasked = ctl.store.get_question(ctl.session_id, question.question_id)
    assert reasked.status == "open" and reasked.draft_revision == 2
    answered = ctl.apply_patch(
        PlanPatch(add_objectives=("catch wrong answers",), answers=(question.question_id,)),
        expected_revision=2,
    )
    assert answered.status == "applied"
    assert ctl.store.get_question(ctl.session_id, question.question_id).status == "answered"
    ctl.storage.db.close()


def _one_turn(h: SessionHarness, message: str, steps: list[object], **session: object):
    ctl = h.open_session(FOUR, objectives=("catch wrong answers",), **session)
    provider = ScriptedProvider(steps)  # type: ignore[arg-type]
    agent = ConversationAgent(ctl, provider)
    outcome = asyncio.run(agent.handle_message(message))
    return ctl, provider, outcome


def test_vague_negated_or_questioning_words_do_not_start_a_run(tmp_path: Path) -> None:
    for index, message in enumerate(
        ["Looks interesting.", "Don't run it yet.", "Should I run it now?", "yes"]
    ):
        h = SessionHarness(tmp_path / str(index))
        quote = message.rstrip(".?")
        ctl, _, outcome = _one_turn(
            h,
            message,
            [
                lambda m, q=quote: call(
                    "request_action",
                    action="start_run",
                    user_quote=q,
                    expected_revision=revision_seen(m),
                ),
                say("ok"),
            ],
        )
        assert outcome.actions == [], message
        assert outcome.rejected[0]["tool"] == "request_action"
        assert h.runs() == [] and h.count() == 0
        ctl.storage.db.close()


def test_yes_starts_only_the_plan_the_previous_reply_offered(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session(FOUR, objectives=("catch wrong answers",))
    provider = ScriptedProvider(
        [call("show_plan"), say("Here is the plan. Run it?"), start_step("yes"), say("ok")]
    )
    agent = ConversationAgent(ctl, provider)

    async def scenario() -> None:
        offer = await agent.handle_message("Show me the plan.")
        assert offer.offer == {"action": "start_run", "revision": 1}
        started = await agent.handle_message("yes")
        assert started.actions[0]["state"] == "done"
        await ctl.wait_for_run(started.actions[0]["run_id"])

    asyncio.run(scenario())
    assert len(h.runs()) == 1
    ctl.storage.db.close()


def test_a_run_needs_a_revision_the_user_was_shown(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl, _, outcome = _one_turn(h, "Run it.", [start_step("Run it"), say("ok")])
    assert outcome.actions == []
    assert "has not been shown" in outcome.rejected[0]["problems"][0]
    assert h.runs() == []
    ctl.storage.db.close()


def test_patch_values_must_come_from_the_users_words(tmp_path: Path) -> None:
    cases = [
        ("Use a small pilot.", {"sample": {"size": 50}}, "sample size 50"),
        (
            "Make it strict.",
            {"rules": {"native.exact_match": {"comparator": ">=", "threshold": 0.9}}},
            "threshold 0.9",
        ),
        ("Also check tone.", {"add_objectives": ["be polite and friendly"]}, "objective"),
    ]
    for index, (message, patch, problem) in enumerate(cases):
        h = SessionHarness(tmp_path / str(index))
        quote = message.rstrip(".")
        ctl, _, outcome = _one_turn(h, message, [patch_step(quote, **patch), say("ok")])
        assert outcome.decisions == [], message
        assert problem in outcome.rejected[0]["problems"][0]
        assert ctl.session.revision == 1
        ctl.storage.db.close()


def test_a_run_the_policy_does_not_permit_dispatches_nothing(tmp_path: Path) -> None:
    """Policy-negative: the session was opened without trusted-local permission."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session(FOUR, objectives=("catch wrong answers",), trusted=False)
    ctl.mark_presented(1)
    provider = ScriptedProvider([start_step("Run it"), say("ok")])
    outcome = asyncio.run(ConversationAgent(ctl, provider).handle_message("Run it."))
    (action,) = outcome.actions
    assert action["state"] == ActionState.DENIED.value
    assert any("trusted" in f["message"] for f in action["findings"])
    assert h.runs() == [] and h.count() == 0
    ctl.storage.db.close()


def test_the_assistant_has_no_terminal_file_or_network_tool(tmp_path: Path) -> None:
    assert TOOL_NAMES == {
        "get_session_state",
        "show_plan",
        "read_profile",
        "summarize_dataset",
        "list_evaluators",
        "describe_evaluator",
        "explain_metric",
        "propose_plan_patch",
        "ask_user",
        "get_run_status",
        "list_failures",
        "get_case_evidence",
        "request_action",
    }
    h = SessionHarness(tmp_path)
    ctl, _, outcome = _one_turn(
        h, "list my files", [call("run_shell", command="ls"), say("I can't do that.")]
    )
    assert outcome.rejected == [{"tool": "run_shell", "status": "unknown tool"}]
    ctl.storage.db.close()


def test_the_assistant_never_receives_reference_answers(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    rows = [{"case_id": c, "input": "hi", "expected_output": f"SECRET-REFERENCE-{c}"} for c in "ab"]
    ctl = h.open_session({}, rows=rows, objectives=("catch wrong answers",))

    async def scenario() -> None:
        start = await ctl.start_run(action_id="act-1", expected_revision=1)
        await ctl.wait_for_run(start.run_id)
        provider = ScriptedProvider(
            [
                call("list_failures"),
                call("get_case_evidence", case_id="a"),
                call("read_profile"),
                call("summarize_dataset"),
                say("Both cases failed exact match."),
            ]
        )
        outcome = await ConversationAgent(ctl, provider).handle_message("Show the failures.")
        sent = json.dumps(provider.calls)
        assert "SECRET-REFERENCE" not in sent
        tool_results = [json.loads(m["content"]) for m in provider.calls[-1] if m["role"] == "tool"]
        evidence = next(r for r in tool_results if r.get("case_id") == "a")
        assert "output" not in evidence["executions"][0]  # outputs withheld by default
        assert evidence["case_content"] == "withheld by policy" and "golden" not in evidence
        assert [r["tool"] for r in outcome.results] == ["list_failures", "get_case_evidence"]
        # The user's own view has the Golden and the output.
        user_view = ctl.case_evidence("a")
        assert user_view["golden"]["reference"]["answer"] == "SECRET-REFERENCE-a"
        assert user_view["executions"][0]["output"] == "yes"

    asyncio.run(scenario())
    ctl.storage.db.close()


def test_case_content_reaches_the_assistant_only_when_policy_allows(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    rows = [{"case_id": "a", "input": "hi", "expected_output": "SECRET-REFERENCE"}]
    policy = {"allow_trusted_local": True, "share_case_content_with_assistant": True}
    ctl = h.open_session({}, rows=rows, objectives=("catch wrong answers",), policy=policy)

    async def scenario() -> None:
        start = await ctl.start_run(action_id="act-1", expected_revision=1)
        await ctl.wait_for_run(start.run_id)

    asyncio.run(scenario())
    evidence = ctl.case_evidence("a", for_assistant=True)
    assert evidence["executions"][0]["output"] == "yes"
    assert "golden" not in evidence and "SECRET" not in json.dumps(evidence)
    ctl.storage.db.close()


def test_turn_limits_and_provider_failures_end_the_turn_not_the_session(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session(FOUR, objectives=("catch wrong answers",))
    looping = ScriptedProvider([call("get_session_state") for _ in range(3)])
    agent = ConversationAgent(ctl, looping, TurnLimits(max_model_calls=2))
    outcome = asyncio.run(agent.handle_message("hello"))
    assert outcome.stopped == "model call limit reached (2)"
    assert outcome.text.startswith("The assistant stopped")

    def boom(_: object) -> ModelReply:
        raise RuntimeError("provider down")

    failing = ConversationAgent(ctl, ScriptedProvider([boom]))
    outcome = asyncio.run(failing.handle_message("hello again"))
    assert outcome.stopped == "assistant model failed: RuntimeError: provider down"
    assert len(ctl.store.turns(ctl.session_id)) == 4  # both turns and replies stored
    ctl.storage.db.close()


def test_without_a_model_messages_get_guidance_and_commands_still_work(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session(FOUR, objectives=("catch wrong answers",))
    agent = ConversationAgent(ctl, None)

    async def scenario() -> None:
        outcome = await agent.handle_message("run it")
        assert outcome.actions == [] and "Commands still work" in outcome.text
        action = await ctl.start_run(action_id="act-1", expected_revision=1)
        assert action.state is ActionState.DONE
        await ctl.wait_for_run(action.run_id)

    asyncio.run(scenario())
    assert h.count() == 4
    ctl.storage.db.close()


def test_credentials_in_a_message_are_not_stored_or_sent(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl, provider, _ = _one_turn(
        h,
        "my key is sk-abcdefghijklmnop1234567890 and api_key=hunter2secret",
        [say("Please use a secret reference instead.")],
    )
    stored = ctl.store.turns(ctl.session_id)[0].content
    assert "sk-abcdef" not in stored and "hunter2secret" not in stored
    assert "sk-abcdef" not in json.dumps(provider.calls)
    ctl.storage.db.close()


def test_at_most_two_questions_and_only_about_benchmark_fields(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ask = lambda n: call("ask_user", prompt=f"question {n}?", required_fields=["selection"])
    ctl, _, outcome = _one_turn(
        h,
        "help me",
        [
            call("ask_user", prompt="What is your name?", required_fields=["user.name"]),
            ask(1),
            ask(2),
            ask(3),
            say("Two questions above."),
        ],
    )
    assert [q["prompt"] for q in outcome.questions] == ["question 1?", "question 2?"]
    assert len(outcome.rejected) == 2
    assert all(q.draft_revision == 1 for q in ctl.store.questions(ctl.session_id, "open")[-2:])
    ctl.storage.db.close()


def test_yes_after_a_different_question_is_not_authorization(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session(FOUR, objectives=("catch wrong answers",))
    provider = ScriptedProvider(
        [
            call("show_plan"),
            say("Should I also track latency?"),
            start_step("yes"),
            say("ok"),
        ]
    )
    agent = ConversationAgent(ctl, provider)

    async def scenario() -> None:
        asked = await agent.handle_message("Show me the plan.")
        assert asked.offer is None
        reply = await agent.handle_message("yes")
        assert reply.actions == [] and reply.rejected[0]["tool"] == "request_action"

    asyncio.run(scenario())
    assert h.runs() == []
    ctl.storage.db.close()


def test_a_failing_tool_is_reported_and_the_turn_completes(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session(FOUR, objectives=("catch wrong answers",))

    def broken(metric: str) -> dict[str, object]:
        raise ValueError("boom")

    ctl.explain_metric = broken  # type: ignore[method-assign]
    provider = ScriptedProvider(
        [call("explain_metric", metric="native.exact_match"), say("I could not explain that.")]
    )
    outcome = asyncio.run(ConversationAgent(ctl, provider).handle_message("why exact match?"))
    assert outcome.rejected[0]["status"] == "error"
    assert "ValueError: boom" in outcome.rejected[0]["problems"][0]
    assert outcome.text == "I could not explain that."
    ctl.storage.db.close()
