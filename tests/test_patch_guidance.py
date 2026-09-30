"""A rejected plan change has to tell the assistant what to do next. In a real session the
same two mistakes took several tries each time: settings keyed by an objective's name
("traffic_correctness is not available in this session"), and objectives renamed to words the
user never wrote ("the quoted request is not in the user's message")."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aibench.conversation.agent import ConversationAgent, patch_fix
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
