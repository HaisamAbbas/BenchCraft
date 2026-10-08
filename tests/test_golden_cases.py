"""From reviewed cases towards a golden dataset, in the chat: `/cases check` lays a saved
case beside the source passage it cites, `/cases verify` marks the ones a person confirmed
(`source_verified`), and `/cases add` appends a case a person wrote (`human_authored`).

Found on a real 15-case LightRAG dataset: every case was model-written and accepted after a
quick read (`human_reviewed`), and the judge gave G-Eval 1.0 on all of them; there was no way
in the chat to check an answer against its source and record that, nor to add a question of
one's own."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from aibench.datasets.ingest import ingest_dataset
from aibench.planning.openai_provider import OpenAICompatibleConfig
from aibench.tui import render
from aibench.tui.commands import CommandResult, Commands
from tests.session_support import SessionHarness
from tests.test_chat_cases import DOCUMENT, FIRST, _reply
from tests.test_openai_provider import chat_server


def _shown(result: CommandResult) -> str:
    console = Console(file=io.StringIO(), width=160, highlight=False)
    getattr(render, result.kind)(console, result.data)
    return " ".join(console.file.getvalue().split())  # type: ignore[attr-defined]


@pytest.fixture
def saved(tmp_path: Path) -> Any:
    """A dataset saved through the chat from generated, accepted cases: case 1 quotes its
    source word for word, case 2 paraphrases it."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "refunds.md").write_text(DOCUMENT, encoding="utf-8")
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"})
    with chat_server([_reply()]) as server:
        host, port = server.server_address[:2]
        config = OpenAICompatibleConfig(base_url=f"http://{host}:{port}/v1", model="writer-1")
        commands = Commands(ctl, provider=SimpleNamespace(config=config))

        async def make() -> None:
            await commands.run("/cases generate docs")
            await commands.run("/cases accept all")
            done = await commands.run("/cases save cases.jsonl")
            assert done.ok, done.data

        asyncio.run(make())
    yield SimpleNamespace(commands=commands, path=tmp_path / "cases.jsonl", root=tmp_path)
    ctl.storage.db.close()


def _run(commands: Commands, text: str) -> CommandResult:
    return asyncio.run(commands.run(text))


def _statuses(path: Path) -> list[str]:
    return [json.loads(line)["reference"]["status"] for line in path.read_text().splitlines()]


def test_check_lays_each_case_beside_the_passage_it_cites(saved: Any) -> None:
    result = _run(saved.commands, "/cases check cases.jsonl all")
    assert result.ok, result.data
    first, second = result.data["rows"]
    assert first["quote"] == FIRST and first["source"] == "refunds.md:3"
    assert first["support"] == 1.0 and second["support"] < 0.5
    assert first["heading"] == "Refund policy"
    shown = _shown(result)
    assert "2 case(s): 2 model-written, accepted after a read" in shown
    assert "only " in shown and "read it closely" in shown  # the paraphrase is flagged
    assert "/cases verify" in shown

    sample = _run(saved.commands, "/cases check cases.jsonl")  # default: unverified, at random
    assert {r["number"] for r in sample.data["rows"]} == {1, 2}


def test_verify_marks_only_the_cases_named_and_leaves_the_rest_byte_for_byte(
    saved: Any,
) -> None:
    before = saved.path.read_bytes().splitlines(keepends=True)
    result = _run(saved.commands, "/cases verify cases.jsonl 1")
    assert result.ok, result.data
    assert result.data["verified"] == [1]
    after = saved.path.read_bytes().splitlines(keepends=True)
    assert after[1] == before[1]  # case 2 untouched
    assert _statuses(saved.path) == ["source_verified", "human_reviewed"]
    first = json.loads(after[0])
    assert first["provenance"]["origin"] == "source_verified"
    assert first["provenance"]["reviewer_identity"]
    assert not ingest_dataset(saved.path).errors  # still a valid dataset

    again = _run(saved.commands, "/cases verify cases.jsonl 1")  # already verified: no change
    assert again.data["verified"] == [] and saved.path.read_bytes().splitlines(True) == after
    rest = _run(saved.commands, "/cases check cases.jsonl")
    assert [r["number"] for r in rest.data["rows"]] == [2]  # only what is left to check


def test_a_case_whose_source_is_gone_is_not_verified(saved: Any) -> None:
    (saved.root / "docs" / "refunds.md").write_text("# Moved\n", encoding="utf-8")
    result = _run(saved.commands, "/cases verify cases.jsonl 1 2")
    assert result.data["verified"] == []
    assert all("shorter than it was" in s["reason"] for s in result.data["skipped"])
    assert _statuses(saved.path) == ["human_reviewed", "human_reviewed"]
    checked = _run(saved.commands, "/cases check cases.jsonl 1")
    assert "cannot be checked here" in _shown(checked)


def test_add_appends_a_case_a_person_wrote(saved: Any) -> None:
    result = _run(
        saved.commands,
        '/cases add cases.jsonl "Can I return a used item?" "No: items must be unused."',
    )
    assert result.ok, result.data
    assert result.data["number"] == 3 and result.data["case_id"].startswith("hand-")
    assert _statuses(saved.path) == ["human_reviewed", "human_reviewed", "human_authored"]
    report = ingest_dataset(saved.path)
    assert not report.errors and report.manifest is not None
    assert report.manifest.case_count == 3
    added = report.cases[-1]
    assert added.input == "Can I return a used item?"
    assert added.reference is not None and added.reference.answer == "No: items must be unused."
    assert "written by" in _shown(result)

    twice = _run(
        saved.commands, '/cases add cases.jsonl "Can I return a used item?" "Something else"'
    )
    assert not twice.ok and "already has this question" in twice.data["error"]
    # A case a person wrote has no source passage to verify against.
    no_source = _run(saved.commands, "/cases verify cases.jsonl 3")
    assert no_source.data["skipped"][0]["reason"] == "it cites no source passage"


def test_add_starts_a_new_dataset_file_and_keeps_windows_line_endings(saved: Any) -> None:
    fresh = _run(saved.commands, '/cases add mine.jsonl "What is covered?" "Refunds only."')
    assert fresh.ok and (saved.root / "mine.jsonl").is_file()
    assert not ingest_dataset(saved.root / "mine.jsonl").errors

    crlf = saved.root / "crlf.jsonl"
    crlf.write_bytes(saved.path.read_bytes().replace(b"\n", b"\r\n"))
    _run(saved.commands, '/cases add crlf.jsonl "A new question?" "A new answer."')
    _run(saved.commands, "/cases verify crlf.jsonl 1")
    raw = crlf.read_bytes()
    assert raw.count(b"\r\n") == 3 and b"\n" not in raw.replace(b"\r\n", b"")


def test_wrong_use_is_explained(saved: Any) -> None:
    assert "usage: /cases check" in _run(saved.commands, "/cases check").data["error"]
    assert (
        "name the cases you checked"
        in _run(saved.commands, "/cases verify cases.jsonl").data["error"]
    )
    assert "no case number 9" in _run(saved.commands, "/cases verify cases.jsonl 9").data["error"]
    assert "quote each" in _run(saved.commands, '/cases add cases.jsonl "only one"').data["error"]
    assert "not a .jsonl" in _run(saved.commands, "/cases check notes.txt").data["error"]
