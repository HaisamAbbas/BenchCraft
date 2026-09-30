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
