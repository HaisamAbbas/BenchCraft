"""Plan changes from the assistant, found blocked in a real session: an objective worded
with "never" read as the user refusing, and "yeah" to the assistant's own question could
not confirm what it offered. Refusals and unoffered values stay blocked."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

from aibench.conversation.agent import ConversationAgent, patch_problems
from aibench.core.plans import PluginEnvironmentRef, ReleaseGate
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


def test_release_gate_patch_requires_user_stated_metric_and_threshold_semantics() -> None:
    gate = ReleaseGate(
        gate_id="correctness-pass-rate", binding=0, min_pass_rate=0.9
    )
    patch = PlanPatch(gates=(gate,))
    message = "Add a 90% pass-rate release gate for correctness."
    bindings = {0: ("native.exact_match", "exact match", "correctness")}

    assert patch_problems(
        patch,
        "Add a 90% pass-rate release gate",
        message,
        metric_bindings=bindings,
    ) == []
    assert patch_problems(
        patch,
        "Add a 90% pass-rate release gate",
        "Add a 90% pass-rate release gate for latency.",
        metric_bindings=bindings,
    ) == [
        (
            "release gate metric 'native.exact_match' and its threshold must appear together in "
            "one unambiguous part of the user's message"
        )
    ]
    assert any(
        "does not specify a passing-rate" in problem
        for problem in patch_problems(
            patch,
            "Add a 90% gate",
            "Add a 90% gate for correctness.",
            metric_bindings=bindings,
        )
    )


def test_release_gate_patch_preserves_existing_gates_and_requires_clear_request() -> None:
    existing = ReleaseGate(gate_id="old", binding=0, min_pass_rate=0.8)
    added = ReleaseGate(gate_id="new", binding=1, min_completed_coverage=1.0)
    patch = PlanPatch(gates=(existing, added))
    message = "Add a 100% completed-coverage gate for relevance."
    assert patch_problems(
        patch,
        message,
        message,
        metric_bindings={1: ("native.non_empty", "relevance")},
        existing_gates=(existing,),
    ) == []

    removal = PlanPatch(gates=())
    assert patch_problems(
        removal,
        "remove all release gates",
        "remove all release gates",
        existing_gates=(existing,),
    ) == []
    assert patch_problems(
        removal,
        "remove all release gates",
        "remove all release gates",
        existing_gates=(existing,),
    ) == []
    assert any(
        "does not ask to remove or replace" in problem
        for problem in patch_problems(
            removal,
            "set the objective",
            "set the objective",
            existing_gates=(existing,),
        )
    )


def test_each_gate_threshold_is_grounded_with_its_own_metric() -> None:
    patch = PlanPatch(
        gates=(
            ReleaseGate(gate_id="correctness", binding=0, min_pass_rate=0.5),
            ReleaseGate(gate_id="format", binding=1, min_completed_coverage=0.9),
        )
    )
    message = "90% pass-rate for correctness and 50% completed coverage for format."
    problems = patch_problems(
        patch,
        message,
        message,
        metric_bindings={
            0: ("native.exact_match", "correctness"),
            1: ("native.json_schema", "format"),
        },
    )
    assert any("0.5 is not stated with its metric" in problem for problem in problems)
    assert any("0.9 is not stated with its metric" in problem for problem in problems)

    swapped = PlanPatch(
        gates=(
            ReleaseGate(
                gate_id="correctness",
                binding=0,
                min_pass_rate=0.5,
                min_completed_coverage=0.9,
            ),
        )
    )
    same_metric = "Require 90% pass-rate with 50% completed coverage for correctness."
    swapped_problems = patch_problems(
        swapped,
        same_metric,
        same_metric,
        metric_bindings={0: ("native.exact_match", "correctness")},
    )
    assert any("minimum pass rate 0.5" in problem for problem in swapped_problems)
    assert any("completed coverage 0.9" in problem for problem in swapped_problems)


def test_retaining_a_gate_id_does_not_implicitly_remove_one_of_its_thresholds() -> None:
    existing = ReleaseGate(gate_id="correctness", binding=0, min_completed_coverage=1.0)
    patch = PlanPatch(
        gates=(
            ReleaseGate(gate_id="correctness", binding=0, min_pass_rate=0.9),
            ReleaseGate(gate_id="relevance", binding=1, min_pass_rate=0.8),
        )
    )
    message = "Add a 90% pass-rate gate for correctness and an 80% pass-rate gate for relevance."
    problems = patch_problems(
        patch,
        message,
        message,
        metric_bindings={0: ("native.exact_match", "correctness"), 1: ("native.non_empty", "relevance")},
        existing_gates=(existing,),
    )
    assert any("removing the existing coverage threshold" in problem for problem in problems)

    kept = PlanPatch(
        gates=(
            ReleaseGate(
                gate_id="correctness",
                binding=0,
                min_pass_rate=0.9,
                min_completed_coverage=1.0,
            ),
        )
    )
    add_threshold = "Add a 90% pass-rate threshold for correctness and keep the current coverage."
    assert patch_problems(
        kept,
        add_threshold,
        add_threshold,
        metric_bindings={0: ("native.exact_match", "correctness")},
        existing_gates=(existing,),
    ) == []


def test_retargeting_a_gate_id_to_another_metric_requires_explicit_retargeting() -> None:
    existing = ReleaseGate(gate_id="correctness", binding=0, min_pass_rate=0.9)
    patch = PlanPatch(
        gates=(ReleaseGate(gate_id="correctness", binding=1, min_pass_rate=0.9),)
    )
    message = "Add the format metric with a 90% pass-rate gate."
    problems = patch_problems(
        patch,
        message,
        message,
        metric_bindings={0: ("native.exact_match", "correctness"), 1: ("native.json_schema", "format")},
        existing_gates=(existing,),
    )
    assert any("retargeting release gate" in problem for problem in problems)


def test_threshold_removal_must_refer_to_the_gate_whose_threshold_is_dropped() -> None:
    existing = (
        ReleaseGate(gate_id="correctness", binding=0, min_pass_rate=0.8, min_completed_coverage=1),
        ReleaseGate(gate_id="format", binding=1, min_pass_rate=0.7, min_completed_coverage=1),
    )
    patch = PlanPatch(
        gates=(
            ReleaseGate(gate_id="correctness", binding=0, min_pass_rate=0.8),
            ReleaseGate(gate_id="format", binding=1, min_pass_rate=0.7),
        )
    )
    message = "Keep the correctness gate and remove the coverage threshold for format."
    problems = patch_problems(
        patch,
        message,
        message,
        metric_bindings={0: ("native.exact_match", "correctness"), 1: ("native.json_schema", "format")},
        existing_gates=existing,
    )
    assert len(problems) == 1
    assert "removing the existing coverage threshold from gate 'correctness'" in problems[0]


def test_threshold_removal_between_gates_on_one_metric_requires_the_gate_id() -> None:
    existing = (
        ReleaseGate(
            gate_id="correctness-pass",
            binding=0,
            min_pass_rate=0.9,
            min_completed_coverage=0.8,
        ),
        ReleaseGate(
            gate_id="correctness-coverage",
            binding=0,
            min_pass_rate=0.9,
            min_completed_coverage=1.0,
        ),
    )
    message = "Remove the coverage threshold from the correctness-coverage gate."
    metrics = {0: ("native.exact_match", "correctness")}
    wrong_gate = PlanPatch(
        gates=(
            ReleaseGate(gate_id="correctness-pass", binding=0, min_pass_rate=0.9),
            existing[1],
        )
    )
    rejected = patch_problems(
        wrong_gate,
        message,
        message,
        metric_bindings=metrics,
        existing_gates=existing,
    )
    assert any(
        "removing the existing coverage threshold from gate 'correctness-pass'" in problem
        for problem in rejected
    )

    requested_gate = PlanPatch(
        gates=(
            existing[0],
            ReleaseGate(
                gate_id="correctness-coverage",
                binding=0,
                min_pass_rate=0.9,
            ),
        )
    )
    assert patch_problems(
        requested_gate,
        message,
        message,
        metric_bindings=metrics,
        existing_gates=existing,
    ) == []


def test_removing_a_gate_requires_naming_that_gate_and_threshold_removal_is_not_gate_removal() -> None:
    existing = ReleaseGate(
        gate_id="correctness",
        binding=0,
        min_pass_rate=0.8,
        min_completed_coverage=1.0,
    )
    removal = PlanPatch(gates=())
    wrong_gate = patch_problems(
        removal,
        "Remove the format gate",
        "Remove the format gate",
        metric_bindings={0: ("native.exact_match", "correctness")},
        existing_gates=(existing,),
    )
    assert any("does not identify release gate 'correctness'" in problem for problem in wrong_gate)

    threshold_only = "Remove the coverage threshold for correctness."
    not_a_gate_removal = patch_problems(
        removal,
        threshold_only,
        threshold_only,
        metric_bindings={0: ("native.exact_match", "correctness")},
        existing_gates=(existing,),
    )
    assert any("does not ask to remove or replace" in problem for problem in not_a_gate_removal)

    threshold_only_gate_removal = "Remove the coverage threshold from the correctness gate."
    rejected_gate_removal = patch_problems(
        removal,
        threshold_only_gate_removal,
        threshold_only_gate_removal,
        metric_bindings={0: ("native.exact_match", "correctness")},
        existing_gates=(existing,),
    )
    assert rejected_gate_removal


def test_gate_removals_are_scoped_to_the_gate_named_in_the_removal_clause() -> None:
    existing = (
        ReleaseGate(gate_id="correctness", binding=0, min_pass_rate=0.9),
        ReleaseGate(gate_id="format", binding=1, min_completed_coverage=1.0),
    )
    message = "Keep the correctness gate and remove the format gate."
    problems = patch_problems(
        PlanPatch(gates=()),
        message,
        message,
        metric_bindings={0: ("native.exact_match", "correctness"), 1: ("native.json_schema", "format")},
        existing_gates=existing,
    )
    assert any("does not identify release gate 'correctness'" in problem for problem in problems)


def test_overlapping_gate_ids_match_only_the_complete_named_gate() -> None:
    existing = (
        ReleaseGate(gate_id="correctness", binding=0, min_pass_rate=0.9),
        ReleaseGate(gate_id="correctness-coverage", binding=0, min_completed_coverage=1.0),
    )
    message = "Remove the correctness-coverage gate."
    retained = PlanPatch(gates=(existing[0],))
    assert patch_problems(
        retained,
        message,
        message,
        metric_bindings={0: ("native.exact_match", "correctness")},
        existing_gates=existing,
    ) == []


def test_gate_retargeting_does_not_confuse_a_source_metric_with_an_overlapping_gate_id() -> None:
    existing = (
        ReleaseGate(gate_id="correctness", binding=0, min_pass_rate=0.9),
        ReleaseGate(gate_id="correctness-coverage", binding=0, min_completed_coverage=1.0),
    )
    message = "Move the correctness-coverage gate from correctness to format."
    wrong_patch = PlanPatch(
        gates=(
            ReleaseGate(gate_id="correctness", binding=1, min_pass_rate=0.9),
            existing[1],
        )
    )
    problems = patch_problems(
        wrong_patch,
        message,
        message,
        metric_bindings={
            0: ("native.exact_match", "correctness"),
            1: ("native.json_schema", "format"),
        },
        existing_gates=existing,
    )
    assert any("retargeting release gate 'correctness'" in problem for problem in problems)


def test_all_gates_in_a_preservation_clause_does_not_authorize_removal() -> None:
    existing = (
        ReleaseGate(gate_id="correctness", binding=0, min_pass_rate=0.9),
        ReleaseGate(gate_id="format", binding=1, min_completed_coverage=1.0),
    )
    message = "Keep all other gates and remove the format gate."
    problems = patch_problems(
        PlanPatch(gates=()),
        message,
        message,
        metric_bindings={
            0: ("native.exact_match", "correctness"),
            1: ("native.json_schema", "format"),
        },
        existing_gates=existing,
    )
    assert any("does not identify release gate 'correctness'" in problem for problem in problems)


def test_remove_all_except_clause_preserves_the_named_gate() -> None:
    existing = (
        ReleaseGate(gate_id="correctness", binding=0, min_pass_rate=0.9),
        ReleaseGate(gate_id="format", binding=1, min_completed_coverage=1.0),
    )
    message = "Remove all gates except the correctness gate."
    metrics = {
        0: ("native.exact_match", "correctness"),
        1: ("native.json_schema", "format"),
    }
    rejected = patch_problems(
        PlanPatch(gates=()),
        message,
        message,
        metric_bindings=metrics,
        existing_gates=existing,
    )
    assert any("does not identify release gate 'correctness'" in problem for problem in rejected)

    remove_only_format = PlanPatch(gates=(existing[0],))
    assert patch_problems(
        remove_only_format,
        message,
        message,
        metric_bindings=metrics,
        existing_gates=existing,
    ) == []


def test_negated_remove_all_request_does_not_authorize_clearing_gates() -> None:
    existing = (ReleaseGate(gate_id="correctness", binding=0, min_pass_rate=0.9),)
    message = "Do not remove all release gates."
    problems = patch_problems(
        PlanPatch(gates=()),
        message,
        message,
        metric_bindings={0: ("native.exact_match", "correctness")},
        existing_gates=existing,
    )
    assert problems


def test_remove_all_scope_is_limited_by_its_metric_or_gate_target() -> None:
    existing = (
        ReleaseGate(gate_id="correctness", binding=0, min_pass_rate=0.9),
        ReleaseGate(gate_id="format", binding=1, min_completed_coverage=1.0),
    )
    metrics = {
        0: ("native.exact_match", "correctness"),
        1: ("native.json_schema", "format"),
    }
    message = "Remove all gates for correctness."
    remove_both = patch_problems(
        PlanPatch(gates=()),
        message,
        message,
        metric_bindings=metrics,
        existing_gates=existing,
    )
    assert any("does not identify release gate 'format'" in problem for problem in remove_both)

    remove_only_correctness = PlanPatch(gates=(existing[1],))
    assert patch_problems(
        remove_only_correctness,
        message,
        message,
        metric_bindings=metrics,
        existing_gates=existing,
    ) == []

    both_scopes = "Remove all gates for correctness. Remove all gates for format."
    assert patch_problems(
        PlanPatch(gates=()),
        both_scopes,
        both_scopes,
        metric_bindings=metrics,
        existing_gates=existing,
    ) == []

    threshold_only = "Remove all thresholds from the correctness gate."
    rejected_threshold_clear = patch_problems(
        PlanPatch(gates=()),
        threshold_only,
        threshold_only,
        metric_bindings=metrics,
        existing_gates=existing,
    )
    assert rejected_threshold_clear


def test_gate_removal_does_not_confuse_shared_metric_or_threshold_words_with_gate_ids() -> None:
    existing = (
        ReleaseGate(
            gate_id="correctness-pass",
            binding=0,
            min_pass_rate=0.9,
            min_completed_coverage=0.8,
        ),
        ReleaseGate(
            gate_id="correctness-coverage",
            binding=0,
            min_pass_rate=0.9,
            min_completed_coverage=1.0,
        ),
    )
    metrics = {0: ("native.exact_match", "correctness")}
    ambiguous = "Remove the correctness gate."
    ambiguous_problems = patch_problems(
        PlanPatch(gates=()),
        ambiguous,
        ambiguous,
        metric_bindings=metrics,
        existing_gates=existing,
    )
    assert ambiguous_problems

    threshold_only = "Drop coverage from the correctness-coverage gate."
    threshold_problems = patch_problems(
        PlanPatch(gates=()),
        threshold_only,
        threshold_only,
        metric_bindings=metrics,
        existing_gates=existing,
    )
    assert threshold_problems


def test_a_shared_objective_alias_does_not_select_an_ambiguous_metric_binding() -> None:
    metrics = {
        0: ("native.exact_match", "correctness", "answers are correct"),
        1: ("deepeval.g_eval", "correctness", "answers are correct"),
    }
    message = "Add a 90% pass-rate gate for correctness."
    for binding in (0, 1):
        patch = PlanPatch(
            gates=(
                ReleaseGate(gate_id=f"correctness-{binding}", binding=binding, min_pass_rate=0.9),
            )
        )
        assert patch_problems(patch, message, message, metric_bindings=metrics)


def test_gate_retargeting_checks_the_requested_direction() -> None:
    existing = (
        ReleaseGate(gate_id="correctness", binding=0, min_pass_rate=0.9),
        ReleaseGate(gate_id="format", binding=1, min_pass_rate=0.7),
    )
    message = "Keep the correctness gate unchanged and move the format gate to correctness."
    metrics = {
        0: ("native.exact_match", "correctness"),
        1: ("native.json_schema", "format"),
    }
    wrong_direction = PlanPatch(
        gates=(
            ReleaseGate(gate_id="correctness", binding=1, min_pass_rate=0.9),
            existing[1],
        )
    )
    wrong = patch_problems(
        wrong_direction,
        message,
        message,
        metric_bindings=metrics,
        existing_gates=existing,
    )
    assert any("requires an explicit retarget request" in problem for problem in wrong)

    requested_direction = PlanPatch(
        gates=(
            existing[0],
            ReleaseGate(gate_id="format", binding=0, min_pass_rate=0.7),
        )
    )
    assert patch_problems(
        requested_direction,
        message,
        message,
        metric_bindings=metrics,
        existing_gates=existing,
    ) == []


def test_explicit_gate_ids_disambiguate_gates_on_the_same_metric() -> None:
    existing = (
        ReleaseGate(gate_id="correctness-pass-rate", binding=0, min_pass_rate=0.9),
        ReleaseGate(gate_id="correctness-coverage", binding=0, min_completed_coverage=1.0),
    )
    metrics = {
        0: ("native.exact_match", "correctness"),
        1: ("native.json_schema", "format"),
    }
    remove_pass_rate = "Remove the correctness-pass-rate gate."
    wrong_removal = patch_problems(
        PlanPatch(gates=()),
        remove_pass_rate,
        remove_pass_rate,
        metric_bindings=metrics,
        existing_gates=existing,
    )
    assert any("does not identify release gate 'correctness-coverage'" in p for p in wrong_removal)

    move_pass_rate = "Move the correctness-pass-rate gate from correctness to format."
    wrong_retarget = patch_problems(
        PlanPatch(
            gates=(
                ReleaseGate(gate_id="correctness-pass-rate", binding=0, min_pass_rate=0.9),
                ReleaseGate(gate_id="correctness-coverage", binding=1, min_completed_coverage=1.0),
            )
        ),
        move_pass_rate,
        move_pass_rate,
        metric_bindings=metrics,
        existing_gates=existing,
    )
    assert any(
        "retargeting release gate 'correctness-coverage'" in p for p in wrong_retarget
    )

    move_coverage = "Move the correctness-coverage gate from correctness to format."
    accepted_retarget = PlanPatch(
        gates=(
            existing[0],
            ReleaseGate(gate_id="correctness-coverage", binding=1, min_completed_coverage=1.0),
        )
    )
    assert patch_problems(
        accepted_retarget,
        move_coverage,
        move_coverage,
        metric_bindings=metrics,
        existing_gates=existing,
    ) == []


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


def test_settings_for_a_metric_this_session_does_not_have_are_reported_not_saved(
    tmp_path: Path,
) -> None:
    """A session without the plugin saved G-Eval settings and reported the change applied,
    although no such metric existed there. The assistant now learns it is unavailable."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"}, objectives=("catch wrong answers",))
    try:
        before = ctl.session.revision
        result = ctl.apply_patch(
            PlanPatch(
                add_objectives=("traffic correctness",),
                params={"deepeval.g_eval": {"name": "traffic correctness", "criteria": CRITERIA}},
            ),
            expected_revision=before,
            source="assistant",
        )
        assert result.status == "rejected"
        assert "deepeval.g_eval is not available in this session" in result.problems[0]
        assert "/plugins" in result.problems[0]
        assert ctl.session.revision == before  # nothing was saved
    finally:
        ctl.storage.db.close()


@requires_plugin_env
def test_reopening_an_older_session_loads_the_plugins_installed_since(tmp_path: Path) -> None:
    """The reported case: a session created before `plugins install deepeval` was reopened
    afterwards, and the G-Eval change was accepted but planned nothing. Reopening now loads
    the project's plugins; the G-Eval change then plans a G-Eval metric."""
    from aibench.cli.chat import _with_project_plugins

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
        assert not ctl.session.plugin_environments  # created before the install
        judge = {"kind": "python_factory", "factory": "aibench_schema_judges:agreeing_judge"}
        (h.root / "aibench.json").write_text(
            json.dumps(
                {
                    "plugin_environments": [
                        {
                            "name": "deepeval",
                            "python": str(PLUGIN_ENV),
                            "default_params": {"deepeval.*": {"judge": judge}},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        before = ctl.session.revision
        assert _with_project_plugins(ctl, h.root) is ctl
        assert ctl.session.revision == before + 1
        assert len(ctl.session.plugin_environments) == 1

        again = ctl.session.revision
        _with_project_plugins(ctl, h.root)  # already loaded: nothing changes
        assert ctl.session.revision == again

        result = ctl.apply_patch(
            PlanPatch(
                add_objectives=("traffic correctness",),
                params={"deepeval.g_eval": {"name": "traffic correctness", "criteria": CRITERIA}},
            ),
            expected_revision=ctl.session.revision,
            source="assistant",
        )
        assert result.status == "applied", result.problems
        assert _planned_g_eval(ctl)["criteria"] == CRITERIA
    finally:
        ctl.storage.db.close()


@requires_plugin_env
def test_g_eval_needs_criteria_not_a_name(tmp_path: Path) -> None:
    """The real model sent G-Eval's criteria without a `name`. G-Eval then needed a name "that
    only you can supply", the metric was left out of the plan, and the run had no correctness
    check although the plan read "ready to run". The name is only a label."""
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
        loaded = ctl.use_plugin_environments(
            (PluginEnvironmentRef(python=str(PLUGIN_ENV)),), {"deepeval.*": {"judge": judge}}
        )
        assert loaded.status == "applied", loaded.problems
        result = ctl.apply_patch(
            PlanPatch(
                add_objectives=("traffic correctness",),
                params={"deepeval.g_eval": {"criteria": CRITERIA}},
            ),
            expected_revision=ctl.session.revision,
            source="assistant",
        )
        assert result.status == "applied", result.problems
        assert _planned_g_eval(ctl)["criteria"] == CRITERIA
    finally:
        ctl.storage.db.close()


@requires_plugin_env
def test_a_change_that_would_leave_a_configured_metric_out_of_the_plan_is_refused(
    tmp_path: Path,
) -> None:
    """A metric configured with settings that leave it unable to run used to be dropped from
    the plan quietly. The change is refused instead, saying what is missing, so the assistant
    (or the user) can fix it before the plan reads "ready to run" without it."""
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
        loaded = ctl.use_plugin_environments((PluginEnvironmentRef(python=str(PLUGIN_ENV)),), {})
        assert loaded.status == "applied", loaded.problems
        before = ctl.session.revision
        result = ctl.apply_patch(
            PlanPatch(
                add_objectives=("check for misuse",),
                params={"deepeval.misuse": {"judge": judge}},  # no `domain`
            ),
            expected_revision=before,
            source="assistant",
        )
        assert result.status == "rejected"
        assert "deepeval.misuse would not be in the plan" in result.problems[0]
        assert "domain" in result.problems[0]
        assert ctl.session.revision == before  # nothing was saved
    finally:
        ctl.storage.db.close()
