"""Plan changes from the assistant, found blocked in a real session: an objective worded
with "never" read as the user refusing, and "yeah" to the assistant's own question could
not confirm what it offered. Refusals and unoffered values stay blocked."""

from __future__ import annotations

import asyncio
from pathlib import Path

from aibench.conversation.agent import ConversationAgent, patch_problems
from aibench.core.sessions import PlanPatch
from aibench.planning.catalog import concepts_in
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
