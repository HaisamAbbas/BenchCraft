"""From a reviewed dataset towards a golden one (`/cases check`, `/cases verify`, `/cases add`).

Cases written by a model and accepted after a read are `human_reviewed`: good for comparing
runs, not proof of what is right. Two things make a dataset golden:

- **verify**: a person reads a case's answer beside the source passage it cites and confirms
  the passage says it. The case becomes `source_verified`. `check` lays the case out for
  that: question, expected answer, the cited passage with the text around it, and how much of
  the answer's wording the passage has.
- **add**: a person writes a case, question and answer, themselves (`human_authored`): the
  questions a generator does not think of, such as ones that need two passages, or ones the
  application should answer "not covered".

Both work on the dataset file. Only the lines verified or added change; everything else stays
byte for byte, the write is atomic, and the file is read back as a dataset before it replaces
the original, so a mistake can never leave a broken file.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import random
import tempfile
from pathlib import Path
from typing import Any

from aibench.core.errors import ValidationError
from aibench.core.models import BenchmarkCase, Provenance, ReferenceAnswer, ReferenceStatus
from aibench.datasets.candidates import answer_support, heading_above
from aibench.datasets.ingest import ingest_dataset

DEFAULT_SAMPLE = 5
MAX_TEXT = 4000
_AROUND = 220


def reviewer() -> str:
    return getpass.getuser() or "local user"


def _lines(path: Path) -> list[str]:
    if path.suffix.lower() != ".jsonl":
        raise ValidationError(f"{path.name} is not a .jsonl dataset")
    # Bytes, not text mode: text mode turns "\r\n" into "\n", and the line endings written
    # back must be the file's own.
    try:
        return path.read_bytes().decode("utf-8").splitlines(keepends=True)
    except OSError as exc:
        raise ValidationError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{path.name} is not UTF-8 text") from exc


def _cases(lines: list[str]) -> list[tuple[int, dict[str, Any]]]:
    """(line index, case) for every non-blank line, numbered as the user sees them."""
    found = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"line {index + 1} is not JSON: {exc.msg}") from exc
        if not isinstance(case, dict):
            raise ValidationError(f"line {index + 1} is not a case object")
        found.append((index, case))
    return found


def _passage(case: dict[str, Any]) -> dict[str, Any]:
    """The source passage a case cites (`path#chars=start:end`), with the text around it,
    or why it cannot be shown."""
    refs = (case.get("provenance") or {}).get("source_refs") or []
    ref = next((r for r in refs if isinstance(r, str) and "#chars=" in r), None)
    if ref is None:
        return {"problem": "it cites no source passage"}
    location, _, span = ref.rpartition("#chars=")
    try:
        start, end = (int(n) for n in span.split(":"))
        # The offsets count every character as generation read it, "\r" included: text mode
        # would drop each "\r\n" to "\n" and shift the quote by one character per line.
        text = Path(location).read_bytes().decode("utf-8")
    except (ValueError, OSError, UnicodeDecodeError):
        return {"problem": f"its source {Path(location).name} cannot be read"}
    if not 0 <= start < end <= len(text):
        return {"problem": f"its source {Path(location).name} is shorter than it was"}
    return {
        "source": f"{Path(location).name}:{text.count(chr(10), 0, start) + 1}",
        "quote": " ".join(text[start:end].split()),
        "before": " ".join(text[max(0, start - _AROUND) : start].split()),
        "after": " ".join(text[end : end + _AROUND].split()),
        "heading": heading_above(text[:start]),
    }


def _status(case: dict[str, Any]) -> str:
    return str((case.get("reference") or {}).get("status") or "none")


def _answer(case: dict[str, Any]) -> str:
    return str((case.get("reference") or {}).get("answer") or "")


def _selected(
    cases: list[tuple[int, dict[str, Any]]], selectors: list[str]
) -> list[tuple[int, int, dict[str, Any]]]:
    """(number, line index, case) for `all` or the numbers given."""
    numbered = [(n, index, case) for n, (index, case) in enumerate(cases, start=1)]
    if selectors == ["all"]:
        return numbered
    chosen = []
    for word in selectors:
        if not word.isdigit() or not 1 <= int(word) <= len(numbered):
            raise ValidationError(f"no case number {word}; the file has {len(numbered)}")
        chosen.append(numbered[int(word) - 1])
    return list({n: (n, i, c) for n, i, c in chosen}.values())


def check_rows(path: Path, selectors: list[str], *, seed: int | None = None) -> dict[str, Any]:
    """The cases to read side by side: the numbers given, `all`, or by default a few not yet
    verified, picked at random so a spot-check is not always the first lines."""
    cases = _cases(_lines(path))
    if not cases:
        raise ValidationError(f"{path.name} has no cases")
    if selectors:
        chosen = _selected(cases, selectors)
    else:
        open_ = [
            (n, i, c)
            for n, (i, c) in enumerate(cases, start=1)
            if _status(c) not in (ReferenceStatus.SOURCE_VERIFIED.value, "human_authored")
        ]
        rng = random.Random(seed)
        chosen = sorted(rng.sample(open_, min(DEFAULT_SAMPLE, len(open_))), key=lambda t: t[0])
    rows = []
    for number, _, case in chosen:
        passage = _passage(case)
        answer = _answer(case)
        quote = passage.get("quote")
        rows.append(
            {
                "number": number,
                "case_id": case.get("case_id"),
                "question": case.get("input")
                if isinstance(case.get("input"), str)
                else json.dumps(case.get("input"), ensure_ascii=False),
                "answer": answer,
                "status": _status(case),
                **passage,
                "support": answer_support(answer, quote) if quote else None,
            }
        )
    statuses = [_status(c) for _, c in cases]
    return {
        "path": str(path),
        "rows": rows,
        "total": len(cases),
        "counts": {s: statuses.count(s) for s in sorted(set(statuses))},
    }


def _replace(path: Path, lines: list[str]) -> None:
    """Write the new lines atomically, after reading them back as a dataset."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write("".join(lines))
        report = ingest_dataset(temp)
        if report.errors:
            first = report.errors[0]
            raise ValidationError(f"the change would break the dataset: {first}")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _line(case: dict[str, Any], ending: str) -> str:
    return json.dumps(case, ensure_ascii=False, separators=(",", ":")) + ending


def verify(path: Path, selectors: list[str], *, who: str | None = None) -> dict[str, Any]:
    """Mark the cases named as checked against their source: `source_verified`. Only a case
    whose cited passage can be read can be verified that way; the others are reported."""
    if not selectors:
        raise ValidationError("name the cases you checked: /cases verify FILE 1 3 5")
    lines = _lines(path)
    cases = _cases(lines)
    who = who or reviewer()
    done, skipped = [], []
    for number, index, case in _selected(cases, selectors):
        if _status(case) == ReferenceStatus.SOURCE_VERIFIED.value:
            continue
        if not _answer(case):
            skipped.append({"number": number, "reason": "it has no expected answer"})
            continue
        passage = _passage(case)
        if "problem" in passage:
            skipped.append({"number": number, "reason": passage["problem"]})
            continue
        case["reference"] = {**case["reference"], "status": ReferenceStatus.SOURCE_VERIFIED.value}
        case["provenance"] = {
            **(case.get("provenance") or {}),
            "origin": ReferenceStatus.SOURCE_VERIFIED.value,
            "reviewer_identity": who,
        }
        ending = "\r\n" if lines[index].endswith("\r\n") else "\n"
        lines[index] = _line(case, ending)
        done.append(number)
    if done:
        _replace(path, lines)
    return {"path": str(path), "verified": done, "skipped": skipped, "reviewer": who}


def add(path: Path, question: str, answer: str, *, who: str | None = None) -> dict[str, Any]:
    """Append a case a person wrote: `human_authored`. The file is created if it is new."""
    question, answer = question.strip(), answer.strip()
    if not question or not answer:
        raise ValidationError('a case needs a question and an answer: /cases add FILE "Q" "A"')
    if len(question) > MAX_TEXT or len(answer) > MAX_TEXT:
        raise ValidationError(f"the question and the answer may have at most {MAX_TEXT} characters")
    lines = _lines(path) if path.exists() else []
    existing = {c.get("case_id") for _, c in _cases(lines)}
    digest = hashlib.sha256(question.encode("utf-8")).hexdigest()
    case_id = f"hand-{digest[:12]}"
    if case_id in existing:
        raise ValidationError(f"{path.name} already has this question ({case_id})")
    who = who or reviewer()
    case = BenchmarkCase(
        case_id=case_id,
        input=question,
        reference=ReferenceAnswer(answer=answer, status=ReferenceStatus.HUMAN_AUTHORED),
        provenance=Provenance(origin=ReferenceStatus.HUMAN_AUTHORED, reviewer_identity=who),
    )
    ending = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += ending
    data = case.model_dump(
        mode="json", exclude={"duplicate_of_line", "source_line"}, exclude_none=True
    )
    lines.append(_line(data, ending))
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    _replace(path, lines)
    return {"path": str(path), "case_id": case_id, "number": len(existing) + 1, "author": who}
