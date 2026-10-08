"""A rejected plan change has to tell the assistant what to do next. In a real session the
same two mistakes took several tries each time: settings keyed by an objective's name
("traffic_correctness is not available in this session"), and objectives renamed to words the
user never wrote ("the quoted request is not in the user's message")."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aibench.conversation.agent import ConversationAgent, concept_stated, patch_fix
from aibench.sessions.controller import unavailable_settings
from tests.session_support import ScriptedProvider, SessionHarness, patch_step, say

MESSAGE = (
    "Add these objectives: traffic correctness, answer relevancy. Use the metric "
    "deepeval.g_eval for traffic correctness with these criteria: compare fine amounts"
)


def test_a_name_that_is_not_a_metric_id_is_told_which_ids_exist() -> None:
    ids = ["native.exact_match", "native.regex_match", "native.contains"]
    [text] = unavailable_settings(["traffic_correctness"], ids)
    assert "'traffic_correctness' is not a metric id" in text
    assert "never by an objective's name" in text
    assert "native.exact_match" in text and "native.contains" in text
    # An id of a plugin that is not installed still points at /plugins.
    [text] = unavailable_settings(["deepeval.g_eval"], ids)
    assert "/plugins" in text and "not a metric id" not in text


def test_long_id_lists_are_cut() -> None:
    ids = [f"deepeval.metric_{i:02d}" for i in range(40)]
    [text] = unavailable_settings(["traffic_correctness"], ids)
    assert "deepeval.metric_14" in text and "deepeval.metric_15" not in text and "..." in text


def test_a_paraphrased_quote_gets_the_users_message_back_to_copy_from() -> None:
    fix = patch_fix(["the quoted request is not in the user's message"], MESSAGE)
    assert fix is not None
    assert "copied unchanged" in fix and "Add these objectives: traffic correctness" in fix


def test_a_renamed_objective_is_told_to_keep_the_users_words() -> None:
    fix = patch_fix(["objective 'correctness' does not appear in the user's message"], MESSAGE)
    assert fix is not None
    assert 'keep "traffic correctness"' in fix and "Add these objectives" in fix
    assert patch_fix(["the user's words hold back or refuse this change"], MESSAGE) is None


def test_the_model_receives_how_to_fix_after_a_rejected_patch(tmp_path: Path) -> None:
    """End to end through the agent: the rejection the model reads carries the fix, and the
    retry with the user's own words is applied."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"})
    user = "Check that the answers are correct compared with the expected answers"
    provider = ScriptedProvider(
        [
            patch_step("Check that", add_objectives=["correctness only"]),
            patch_step(user, add_objectives=[user]),
            say("Added."),
        ]
    )
    agent = ConversationAgent(ctl, provider)
    try:
        outcome = asyncio.run(agent.handle_message(user))
        assert len(outcome.rejected) == 1
        seen = _tool_results(provider)
        rejected = next(r for r in seen if r.get("status") == "rejected")
        assert "how_to_fix" in rejected and user[:40] in rejected["how_to_fix"]
        assert user in ctl.state()["draft"]["objectives"]
    finally:
        ctl.storage.db.close()


def _tool_results(provider: ScriptedProvider) -> list[dict[str, Any]]:
    results = []
    for message in provider.calls[-1]:
        if message.get("role") == "tool":
            try:
                results.append(json.loads(message["content"]))
            except (TypeError, ValueError):
                continue
    return results


def test_settings_the_user_never_stated_are_named_so_the_model_drops_them() -> None:
    """G-Eval's evaluation_params were offered by the tool description and then refused as
    not the user's words; in a real session the assistant ran out of calls retrying."""
    problems = [
        "deepeval.g_eval parameter evaluation_params 'input' does not appear in the user's message",
        (
            "deepeval.g_eval parameter evaluation_params 'actual_output' does not appear in the "
            "user's message"
        ),
    ]
    fix = patch_fix(problems, MESSAGE)
    assert fix is not None
    assert "Leave out these settings" in fix and "evaluation_params" in fix
    assert fix.count("evaluation_params") == 1  # listed once, not per value


def test_empty_settings_for_a_metric_are_ignored_not_refused(tmp_path: Path) -> None:
    """The assistant sent params {"native.exact_match@1.0.0": {}} with a change that asked for
    no settings, was refused three ways, and ran out of tokens. Settings that set nothing are
    dropped and `id@version` is read as `id`; real settings are still checked."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"})
    user = "Check that the answers are correct compared with the expected answers"
    provider = ScriptedProvider(
        [
            patch_step(
                user,
                add_objectives=[user],
                params={"native.exact_match@1.0.0": {}, "deepeval.g_eval": {}},
            ),
            say("Added."),
        ]
    )
    agent = ConversationAgent(ctl, provider)
    try:
        outcome = asyncio.run(agent.handle_message(user))
        assert outcome.rejected == []
        assert user in ctl.state()["draft"]["objectives"]
    finally:
        ctl.storage.db.close()


def test_naming_a_metric_states_the_concept_it_serves() -> None:
    """ "Use deepeval.g_eval for that one" was refused twice: the patch named the concept
    `custom_criteria`, a word no user types, and the user had to be told to type it. A metric
    the user names states the concept it serves; the concept in words does too."""
    serving = {"custom_criteria": ("deepeval.g_eval",), "correctness": ("native.exact_match",)}
    assert concept_stated("custom_criteria", "use deepeval.g_eval for that one", serving)
    assert concept_stated("custom_criteria", "check it with g-eval", serving)
    assert concept_stated("custom_criteria", "score it against custom criteria", serving)
    assert not concept_stated("custom_criteria", "make the answers better", serving)
    # A metric the user did not name states nothing, even one that serves the concept.
    assert not concept_stated("correctness", "use deepeval.g_eval for that one", serving)


def test_a_concept_the_user_names_by_its_metric_is_applied(tmp_path: Path) -> None:
    """End to end through the assistant: the user names the metric, the model attaches the
    concept that metric serves, and the change is applied instead of refused."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"})
    user = "Add the objective answers match exactly, and use native.exact_match for it"
    objective = "answers match exactly"
    concept = next(
        c
        for option in ctl.inputs().catalog
        if option.evaluator_id == "native.exact_match"
        for c in option.concepts
    )
    assert concept not in user  # the concept's own name is not in the message
    provider = ScriptedProvider(
        [
            patch_step(
                user,
                add_objectives=[objective],
                objective_concepts={objective: [concept]},
            ),
            say("Added."),
        ]
    )
    agent = ConversationAgent(ctl, provider)
    try:
        outcome = asyncio.run(agent.handle_message(user))
        assert outcome.rejected == [], outcome.rejected
        assert objective in ctl.state()["draft"]["objectives"]
    finally:
        ctl.storage.db.close()


def test_a_concept_the_user_did_not_name_is_not_reworded_but_asked_about() -> None:
    problems = [
        (
            "concept for 'answers match the expected answers' 'custom_criteria' does not "
            "appear in the user's message"
        )
    ]
    fix = patch_fix(problems, "answers match the expected answers")
    assert fix is not None
    assert "Do not retry with other wording" in fix and "custom_criteria" in fix
    assert "use deepeval.g_eval" in fix
