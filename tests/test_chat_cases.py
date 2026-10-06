"""Test cases from documents, in the chat. The assistant's model writes candidates from the
documents the user names; the user reads each beside the source quote it cites, accepts or
rejects it, and saves the accepted ones to a new dataset file. Nothing generated is a case
before that, and the file is an ordinary dataset the benchmark can use."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rich.console import Console

from aibench.core.models import CandidateStatus, ReferenceStatus
from aibench.datasets.ingest import ingest_dataset
from aibench.planning.openai_provider import OpenAICompatibleConfig
from aibench.tui import render
from aibench.tui.commands import CommandResult, Commands
from tests.session_support import SessionHarness
from tests.test_openai_provider import chat_server, completion, tool_call

FIRST = "Refunds may be requested within 30 days of purchase."
SECOND = "Items must be unused and in the original packaging."
DOCUMENT = f"# Refund policy\n\n{FIRST}\n\n{SECOND}\n"


def _reply() -> tuple[int, Any]:
    cases = [
        {
            "input": "How long do I have to request a refund?",
            "expected_answer": FIRST,
            "source_id": "source_1",
            "source_quote": FIRST,
        },
        {  # a paraphrase: the answer is not word for word in the quote it cites
            "input": "What condition must returned items be in?",
            "expected_answer": "They have to be unused, in the box they came in.",
            "source_id": "source_1",
            "source_quote": SECOND,
        },
    ]
    return 200, completion([tool_call("write_candidates", {"cases": cases})])


def _shown(result: CommandResult) -> str:
    console = Console(file=io.StringIO(), width=140, highlight=False)
    {
        "cases": render.cases,
        "cases_decided": render.cases_decided,
        "cases_saved": render.cases_saved,
    }[result.kind](console, result.data)
    return " ".join(console.file.getvalue().split())  # type: ignore[attr-defined]


def test_cases_from_documents_are_generated_reviewed_and_saved(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "refunds.md").write_text(DOCUMENT, encoding="utf-8")
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"})
    try:
        with chat_server([_reply()]) as server:
            host, port = server.server_address[:2]
            config = OpenAICompatibleConfig(base_url=f"http://{host}:{port}/v1", model="writer-1")
            commands = Commands(ctl, provider=SimpleNamespace(config=config))

            unverified: set[ReferenceStatus] = set()

            async def scenario() -> dict[str, CommandResult]:
                out: dict[str, CommandResult] = {}
                out["early_save"] = await commands.run("/cases save")
                out["nothing_yet"] = await commands.run("/cases")
                out["generate"] = await commands.run("/cases generate docs")
                # Straight after generation, before the user has decided anything:
                pool_now = out["generate"].data["pool_id"]
                unverified.update(
                    c.case.reference.status for c in ctl.storage.list_candidates(pool_now)
                )
                out["premature_save"] = await commands.run("/cases save")
                out["accept"] = await commands.run("/cases accept 1")
                out["save"] = await commands.run("/cases save cases-out.jsonl")
                out["again"] = await commands.run("/cases save cases-out.jsonl")
                out["reject"] = await commands.run("/cases reject 2")
                out["show"] = await commands.run("/cases")
                return out

            got = asyncio.run(scenario())
            assert len(server.requests) == 1  # one model call for the whole pool

        assert not got["early_save"].ok and "no cases yet" in got["early_save"].data["error"]
        assert not got["nothing_yet"].ok
        rows = got["generate"].data["rows"]
        assert [r["number"] for r in rows] == [1, 2]
        assert {r["status"] for r in rows} == {"candidate"}
        assert rows[0]["verbatim"] is True and rows[1]["verbatim"] is False
        assert rows[0]["source"] == "refunds.md:3" and rows[0]["quote"] == FIRST
        # The model's answers are unverified until the user says otherwise.
        pool = got["generate"].data["pool_id"]
        assert unverified == {ReferenceStatus.SYNTHETIC_UNVERIFIED}
        shown = _shown(got["generate"])
        assert "1. (to review) How long do I have to request a refund?" in shown
        assert "2 candidate case(s)" in shown and "Nothing is a test case until" in shown
        assert "not word for word in that quote" in shown  # the paraphrase is flagged

        assert not got["premature_save"].ok
        assert "no accepted cases yet" in got["premature_save"].data["error"]
        assert got["accept"].data["done"][0]["number"] == 1
        assert got["accept"].data["undecided"] == 1
        assert "accepted: 1; 1 still to review" in _shown(got["accept"])

        saved = Path(got["save"].data["path"])
        assert got["save"].data["count"] == 1 and saved == tmp_path / "cases-out.jsonl"
        [line] = saved.read_text(encoding="utf-8").splitlines()
        case = json.loads(line)
        assert case["input"] == "How long do I have to request a refund?"
        assert case["reference"]["status"] == "human_reviewed"
        assert "use the dataset" in _shown(got["save"])
        assert not got["again"].ok and "already saved" in got["again"].data["error"]

        # It is an ordinary dataset: the benchmark can ingest it as it is.
        assert ingest_dataset(saved, retain_cases=False).is_valid

        by_status = {c.candidate_id: c.status for c in ctl.storage.list_candidates(pool)}
        assert sorted(s.value for s in by_status.values()) == ["promoted", "rejected"]
        assert CandidateStatus.PROMOTED in by_status.values()
        assert [r["status"] for r in got["show"].data["rows"]] == ["promoted", "rejected"]
    finally:
        ctl.storage.db.close()


def test_documents_and_models_the_policy_does_not_allow_are_refused(tmp_path: Path) -> None:
    (tmp_path / "refunds.md").write_text(DOCUMENT, encoding="utf-8")
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"})
    try:
        remote = OpenAICompatibleConfig(base_url="https://models.example.test/v1", model="m")
        commands = Commands(ctl, provider=SimpleNamespace(config=remote))

        async def scenario() -> list[CommandResult]:
            return [
                await commands.run("/cases generate refunds.md"),  # the model's origin
                await commands.run("/cases generate missing.md"),
                await commands.run("/cases generate"),
                await commands.run("/cases generate refunds.md --max 99"),
                await Commands(ctl, provider=None).run("/cases generate refunds.md"),
                await commands.run("/cases accept 1"),
                await commands.run("/cases sideways"),
            ]

        origin, missing, bare, too_many, no_model, early_accept, unknown = asyncio.run(scenario())
    finally:
        ctl.storage.db.close()
    assert not origin.ok and "models.example.test" in origin.data["error"]
    assert not missing.ok and "no such file" in missing.data["error"]
    assert not bare.ok and "usage: /cases generate" in bare.data["error"]
    assert not too_many.ok and "--max takes a number from 1 to 50" in too_many.data["error"]
    assert not no_model.ok and "no assistant model" in no_model.data["error"]
    assert not early_accept.ok and "no cases yet" in early_accept.data["error"]
    assert not unknown.ok and "usage: /cases generate" in unknown.data["error"]


def _reply_with_quotes() -> tuple[int, Any]:
    wrapped = "Refunds may be requested  within 30 days\nof purchase."  # spacing retyped
    cases = [
        {
            "input": "How long do I have to request a refund?",
            "expected_answer": FIRST,
            "source_id": "source_1",
            "source_quote": FIRST,
        },
        {
            "input": "Within how many days can a refund be asked for?",
            "expected_answer": FIRST,
            "source_id": "source_1",
            "source_quote": wrapped,
        },
        {  # not in the document at all
            "input": "Can I get a refund after a year?",
            "expected_answer": "Yes, refunds are possible for a year.",
            "source_id": "source_1",
            "source_quote": "Refunds are possible for a full year.",
        },
    ]
    return 200, completion([tool_call("write_candidates", {"cases": cases})])


def test_one_invented_quote_leaves_out_that_case_not_the_whole_pool(tmp_path: Path) -> None:
    """A real model's quote for one case did not match the document, and the whole
    generation failed ("source_quote ... is not an exact substring"), discarding the good
    cases. A quote that differs only in spacing is matched to the document's own text; a
    quote that is not there drops only its case, and the chat says so."""
    (tmp_path / "refunds.md").write_text(DOCUMENT, encoding="utf-8")
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"})
    try:
        with chat_server([_reply_with_quotes(), _reply_all_invented()]) as server:
            host, port = server.server_address[:2]
            config = OpenAICompatibleConfig(base_url=f"http://{host}:{port}/v1", model="writer-1")
            commands = Commands(ctl, provider=SimpleNamespace(config=config))

            async def scenario() -> tuple[CommandResult, CommandResult]:
                first = await commands.run("/cases generate refunds.md")
                second = await commands.run("/cases generate refunds.md")
                return first, second

            first, second = asyncio.run(scenario())
    finally:
        ctl.storage.db.close()
    rows = first.data["rows"]
    assert [r["question"] for r in rows] == [
        "How long do I have to request a refund?",
        "Within how many days can a refund be asked for?",
    ]
    # The retyped quote is shown as the document's own words, not the model's spacing.
    assert rows[1]["quote"] == FIRST and rows[1]["verbatim"] is True
    assert first.data["dropped"] == ["Can I get a refund after a year?"]
    assert "left out 1 case(s) whose quoted source text is not in the document" in _shown(first)
    # Nothing usable at all is an error that says what to do.
    assert not second.ok and "none of the cases" in second.data["error"]


def _reply_all_invented() -> tuple[int, Any]:
    cases = [
        {
            "input": "Is shipping free?",
            "expected_answer": "Yes.",
            "source_id": "source_1",
            "source_quote": "Shipping is always free.",
        }
    ]
    return 200, completion([tool_call("write_candidates", {"cases": cases})])


SECTIONED = (
    "# Benefits\n\n"
    "## 9.3.1 Senior Citizens\n"
    "* Medical Exemptions: Relaxed medical fitness requirements.\n"
    "* Priority Service: Preferential treatment at RTOs.\n\n"
    "## 9.3.2 Differently-Abled Persons\n"
    "* Adapted Vehicles: Permission for vehicle modifications.\n"
)


def test_a_quote_is_shown_with_the_heading_and_text_around_it(tmp_path: Path) -> None:
    """A model asked what medical exemptions *differently-abled persons* get and cited
    the senior citizens' line, which sits just above their heading. The quote matched, so the
    case looked fine, and the wrong case was accepted. The review shows the nearest heading
    above the quote and the text on both sides of it."""
    (tmp_path / "benefits.md").write_text(SECTIONED, encoding="utf-8")
    quote = "Medical Exemptions: Relaxed medical fitness requirements."
    cases = [
        {
            "input": "What medical exemptions do differently-abled persons get?",
            "expected_answer": "Relaxed medical fitness requirements.",
            "source_id": "source_1",
            "source_quote": quote,
        }
    ]
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"})
    try:
        with chat_server(
            [(200, completion([tool_call("write_candidates", {"cases": cases})]))]
        ) as server:
            host, port = server.server_address[:2]
            config = OpenAICompatibleConfig(base_url=f"http://{host}:{port}/v1", model="writer-1")
            commands = Commands(ctl, provider=SimpleNamespace(config=config))
            made = asyncio.run(commands.run("/cases generate benefits.md"))
    finally:
        ctl.storage.db.close()
    [row] = made.data["rows"]
    assert row["heading"] == "9.3.1 Senior Citizens"  # not the group the question names
    assert row["after"].startswith("* Priority Service")
    assert row["heading_after"] == "9.3.2 Differently-Abled Persons"  # the quote is above it
    assert "9.3.2 Differently-Abled Persons" in row["after"]
    shown = _shown(made)
    assert "nearest heading above (a guess): 9.3.1 Senior Citizens" in shown
    assert "next heading below the quote: 9.3.2 Differently-Abled Persons" in shown
    assert "around it:" in shown and "Priority Service" in shown
    assert "does the question ask about what the quote is really about" in shown


def test_headings_are_found_in_markdown_and_numbered_text_and_otherwise_absent() -> None:
    from aibench.datasets.candidates import heading_above

    assert heading_above("# Refund policy\n\nSome text.") == "Refund policy"
    assert heading_above("intro 4.2.3 Helmet and Seatbelt Requirements * Motorcycle") == (
        "4.2.3 Helmet and Seatbelt Requirements"
    )
    assert heading_above("# Old\n\n9.3.2 Newer Section ? text") == "9.3.2 Newer Section"
    assert heading_above("plain words with no headings at all") is None


def test_candidate_generation_waits_longer_than_the_chat_does(tmp_path: Path) -> None:
    """DeepSeek V4 Flash took 107 to 202 s to write 15 cases; the saved assistant config's
    120 s timeout would cut it off. Generation uses at least ten minutes, never less."""
    from aibench.planning.openai_provider import OpenAICompatibleProvider
    from aibench.security.policy import ExecutionPolicy
    from aibench.services import case_pools

    (tmp_path / "refunds.md").write_text(DOCUMENT, encoding="utf-8")
    seen: list[float] = []
    original = OpenAICompatibleProvider.__init__

    def spy(self, config, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(config.timeout_seconds)
        original(self, config, **kwargs)

    with chat_server([_reply()]) as server:
        host, port = server.server_address[:2]
        config = OpenAICompatibleConfig(
            base_url=f"http://{host}:{port}/v1", model="writer-1", timeout_seconds=120
        )
        OpenAICompatibleProvider.__init__ = spy  # type: ignore[method-assign]
        try:
            case_pools.draft_pool(
                config, ExecutionPolicy(), (tmp_path / "refunds.md",), max_candidates=5
            )
        finally:
            OpenAICompatibleProvider.__init__ = original  # type: ignore[method-assign]
    assert seen == [case_pools.GENERATION_TIMEOUT_SECONDS] and seen[0] >= 600
