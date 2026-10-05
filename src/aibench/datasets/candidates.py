"""Bounded generation and review rules for development-only dataset candidates (18-T1/2)."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from aibench.core.errors import AibenchError, ValidationError
from aibench.core.hashes import content_hash
from aibench.core.models import (
    BenchmarkCase,
    CandidatePoolManifest,
    CandidateSourceDocument,
    CandidateSourceSpan,
    CandidateStatus,
    CandidateVerification,
    DatasetCandidate,
    Provenance,
    ReferenceAnswer,
    ReferenceStatus,
)
from aibench.planning.planner import ModelReply

MAX_SOURCE_FILES = 8
MAX_SOURCE_BYTES = 128 * 1024
MAX_TOTAL_SOURCE_BYTES = 512 * 1024
MAX_SOURCE_CHARACTERS = 32_000
MAX_CANDIDATES = 50
MAX_CASE_TEXT = 2_000
MAX_QUOTE_CHARACTERS = 2_000
GENERATION_OUTPUT_TOKENS = 6_000

GENERATION_PROMPT = """Create factual question and answer cases from the supplied development sources.
Only use the supplied sources. Do not invent facts. Each answer must be directly supported by
the exact `source_quote` you return. Ask clear questions with enough context to answer. Return
between one and {limit} distinct cases in one `write_candidates` call. Copy `source_id` from
the source labels. Do not include hidden tests, private reasoning, or other fields.

Generation prompt version: aibench.dataset-generation.v1
"""
GENERATION_PROMPT_HASH = content_hash(GENERATION_PROMPT)


class _QuoteNotFound(Exception):
    """One generated case cites text that is not in its source."""


class CandidateGenerationError(AibenchError):
    """The provider response or selected development sources cannot form candidates."""


class CandidateProvider(Protocol):
    name: str
    model: str

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply: ...


class _GeneratedCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    input: str = Field(min_length=1, max_length=MAX_CASE_TEXT)
    expected_answer: str = Field(min_length=1, max_length=MAX_CASE_TEXT)
    source_id: str = Field(pattern=r"^source_[1-8]$")
    source_quote: str = Field(min_length=8, max_length=MAX_QUOTE_CHARACTERS)


class _GeneratedCases(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    cases: list[_GeneratedCase] = Field(min_length=1, max_length=MAX_CANDIDATES)


class _Source:
    def __init__(self, path: Path, text: str, digest: str, source_id: str) -> None:
        self.path = path
        self.text = text
        self.digest = digest
        self.source_id = source_id


def _read_sources(
    paths: Sequence[Path],
) -> tuple[list[_Source], tuple[CandidateSourceDocument, ...]]:
    if not paths:
        raise CandidateGenerationError("select at least one development source file")
    if len(paths) > MAX_SOURCE_FILES:
        raise CandidateGenerationError(f"at most {MAX_SOURCE_FILES} source files are allowed")

    total_bytes = 0
    unique: list[_Source] = []
    documents: list[CandidateSourceDocument] = []
    first_by_digest: dict[str, str] = {}
    seen_paths: set[str] = set()
    for raw_path in paths:
        path = raw_path.resolve(strict=True)
        if path.suffix.lower() not in {".txt", ".md", ".markdown"}:
            raise CandidateGenerationError(
                f"source {path.name!r} must be UTF-8 text (.txt, .md, or .markdown); "
                "case datasets are not generation sources"
            )
        if not path.is_file():
            raise CandidateGenerationError(f"source {path} is not a regular file")
        # Preserve distinct paths on case-sensitive filesystems while collapsing aliases on
        # Windows, where path identity is case-insensitive.
        path_key = os.path.normcase(str(path))
        if path_key in seen_paths:
            raise CandidateGenerationError(f"source path was selected more than once: {path}")
        seen_paths.add(path_key)
        raw = path.read_bytes()
        if not raw or len(raw) > MAX_SOURCE_BYTES:
            raise CandidateGenerationError(
                f"source {path.name!r} must contain 1 to {MAX_SOURCE_BYTES} bytes"
            )
        total_bytes += len(raw)
        if total_bytes > MAX_TOTAL_SOURCE_BYTES:
            raise CandidateGenerationError(
                f"selected sources exceed the {MAX_TOTAL_SOURCE_BYTES}-byte total limit"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CandidateGenerationError(f"source {path.name!r} is not valid UTF-8") from exc
        if len(text) > MAX_SOURCE_CHARACTERS:
            raise CandidateGenerationError(
                f"source {path.name!r} exceeds the {MAX_SOURCE_CHARACTERS}-character prompt limit"
            )
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        duplicate_of = first_by_digest.get(digest)
        documents.append(
            CandidateSourceDocument(
                source_ref=str(path),
                digest=digest,
                character_count=len(text),
                line_count=max(1, text.count("\n") + (not text.endswith("\n"))),
                duplicate_of=duplicate_of,
            )
        )
        if duplicate_of is None:
            source_id = f"source_{len(unique) + 1}"
            unique.append(_Source(path, text, digest, source_id))
            first_by_digest[digest] = str(path)

    if not unique:
        raise CandidateGenerationError("all selected source documents were exact duplicates")
    return unique, tuple(documents)


def _tool_spec(limit: int) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "write_candidates",
                "description": "Return the bounded candidate cases supported by the sources.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "cases": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": limit,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "input": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": MAX_CASE_TEXT,
                                    },
                                    "expected_answer": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": MAX_CASE_TEXT,
                                    },
                                    "source_id": {
                                        "type": "string",
                                        "enum": [f"source_{i}" for i in range(1, 9)],
                                    },
                                    "source_quote": {
                                        "type": "string",
                                        "minLength": 8,
                                        "maxLength": MAX_QUOTE_CHARACTERS,
                                    },
                                },
                                "required": [
                                    "input",
                                    "expected_answer",
                                    "source_id",
                                    "source_quote",
                                ],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["cases"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def _source_message(sources: Sequence[_Source]) -> str:
    blocks = [f"<{source.source_id}>\n{source.text}\n</{source.source_id}>" for source in sources]
    return (
        "Development sources follow. Cite one exact, contiguous quote per case.\n\n"
        + "\n\n".join(blocks)
    )


def _locate_quote(text: str, quote: str) -> tuple[int, int] | None:
    """Where `quote` is in `text`: exactly, or else differing only in white space (a model
    retypes line breaks and runs of spaces freely). The span is always the source's own
    characters, so the evidence stays verbatim; anything else is not a match."""
    start = text.find(quote)
    if start >= 0:
        return start, start + len(quote)
    words = quote.split()
    if not words:
        return None
    pattern = r"\s+".join(re.escape(word) for word in words)
    found = re.search(pattern, text)
    return (found.start(), found.end()) if found else None


def _line_at(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _candidate_from_generated(
    item: _GeneratedCase,
    *,
    pool_id: str,
    generator_identity: str,
    sources: Mapping[str, _Source],
) -> DatasetCandidate:
    source = sources.get(item.source_id)
    if source is None:
        raise CandidateGenerationError(f"unknown source_id {item.source_id!r}")
    located = _locate_quote(source.text, item.source_quote)
    if located is None:
        raise _QuoteNotFound(
            f"source_quote for {item.source_id!r} is not in that source"
        )
    start, end = located
    quote = source.text[start:end]
    candidate_id = (
        "candidate-"
        + content_hash(
            {
                "pool_id": pool_id,
                "source_digest": source.digest,
                "source_quote": quote,
                "input": item.input,
                "expected_answer": item.expected_answer,
            }
        ).split(":", 1)[1][:24]
    )
    span = CandidateSourceSpan(
        source_ref=str(source.path),
        source_digest=source.digest,
        start_offset=start,
        end_offset=end,
        start_line=_line_at(source.text, start),
        end_line=_line_at(source.text, end - 1),
    )
    case = BenchmarkCase(
        case_id=candidate_id,
        input=item.input,
        reference=ReferenceAnswer(
            answer=item.expected_answer,
            status=ReferenceStatus.SYNTHETIC_UNVERIFIED,
        ),
        provenance=Provenance(
            origin=ReferenceStatus.SYNTHETIC_UNVERIFIED,
            source_refs=(f"{span.source_ref}#chars={start}:{end}",),
            generator_identity=generator_identity,
            prompt_hash=GENERATION_PROMPT_HASH,
        ),
    )
    return DatasetCandidate(
        candidate_id=candidate_id,
        pool_id=pool_id,
        case=case,
        source_spans=(span,),
    )


def generate_candidate_pool(
    paths: Sequence[Path],
    provider: CandidateProvider,
    *,
    pool_id: str,
    source_split: str,
    max_candidates: int = 20,
    dropped: list[str] | None = None,
) -> tuple[CandidatePoolManifest, tuple[DatasetCandidate, ...]]:
    """Make one bounded provider call from explicit development-only text sources.

    The function rejects every other split before reading a source or calling a provider.
    Candidate rows stay separate from regular datasets and keep synthetic references marked
    unverified until a recorded human or executable check changes that status.
    """
    if source_split != "development":
        raise CandidateGenerationError(
            "candidate generation is restricted to the development split; holdout data is not read"
        )
    if isinstance(max_candidates, bool) or not 1 <= max_candidates <= MAX_CANDIDATES:
        raise CandidateGenerationError(f"max_candidates must be between 1 and {MAX_CANDIDATES}")
    sources, documents = _read_sources(paths)
    system = GENERATION_PROMPT.format(limit=max_candidates)
    request = [
        {"role": "system", "content": system},
        {"role": "user", "content": _source_message(sources)},
    ]
    reply = provider.complete(request, _tool_spec(max_candidates))
    calls = [call for call in reply.tool_calls if call.name == "write_candidates"]
    if len(reply.tool_calls) != 1 or len(calls) != 1:
        raise CandidateGenerationError(
            "provider must return exactly one write_candidates tool call"
        )
    try:
        raw = json.loads(calls[0].arguments)
        generated = _GeneratedCases.model_validate(raw)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise CandidateGenerationError(f"provider returned invalid candidates: {exc}") from exc
    if len(generated.cases) > max_candidates:
        raise CandidateGenerationError("provider exceeded the requested candidate limit")

    identity = f"{provider.name}:{provider.model}"
    source_map = {source.source_id: source for source in sources}
    made: list[DatasetCandidate] = []
    for item in generated.cases:
        try:
            made.append(
                _candidate_from_generated(
                    item, pool_id=pool_id, generator_identity=identity, sources=source_map
                )
            )
        except _QuoteNotFound:
            # A case whose quote is not in the document has no evidence: leave out that
            # case, not the good ones beside it.
            if dropped is not None:
                dropped.append(item.input)
    if not made:
        raise CandidateGenerationError(
            "none of the cases the model wrote quote text that is in the document; "
            "try again, or a smaller document"
        )
    candidates = tuple(made)
    if len({candidate.candidate_id for candidate in candidates}) != len(candidates):
        raise CandidateGenerationError("provider returned an exact duplicate candidate")
    manifest = CandidatePoolManifest(
        pool_id=pool_id,
        generator_identity=identity,
        prompt_hash=GENERATION_PROMPT_HASH,
        sources=documents,
        candidate_ids=tuple(c.candidate_id for c in candidates),
    )
    return manifest, candidates


def source_text_for_span(span: CandidateSourceSpan) -> str:
    """Read an unchanged source span. Fail closed when a file moved or changed."""
    path = Path(span.source_ref)
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError(f"source span is no longer readable: {path.name}") from exc
    actual = "sha256:" + hashlib.sha256(raw).hexdigest()
    if actual != span.source_digest:
        raise ValidationError(
            f"source {path.name!r} changed since generation; candidate evidence is stale"
        )
    return text[span.start_offset : span.end_offset]


def verify_source_quote(candidate: DatasetCandidate) -> CandidateVerification:
    """Strict executable oracle: the candidate answer must be an exact source substring."""
    if candidate.case.reference is None or not candidate.case.reference.answer:
        raise ValidationError("candidate has no answer reference to verify")
    answer = candidate.case.reference.answer
    snippets = [source_text_for_span(span) for span in candidate.source_spans]
    passed = any(answer in snippet for snippet in snippets)
    detail = (
        "the expected answer is an exact substring of the recorded source span"
        if passed
        else "the expected answer is not an exact substring of the recorded source span"
    )
    return CandidateVerification(
        method="executable",
        outcome="passed" if passed else "failed",
        verifier_id="aibench.source_quote_presence.v1",
        detail=detail,
    )


def apply_verification(
    candidate: DatasetCandidate,
    verification: CandidateVerification,
    *,
    review_status: ReferenceStatus | None = None,
) -> DatasetCandidate:
    """Return a lifecycle-consistent immutable candidate after recording one check."""
    if candidate.status in (CandidateStatus.REJECTED, CandidateStatus.PROMOTED):
        raise ValidationError(f"candidate in state {candidate.status.value!r} cannot be verified")
    verifications = (*candidate.verifications, verification)
    if verification.outcome == "failed":
        return candidate.model_copy(update={"verifications": verifications})
    origin: ReferenceStatus
    if verification.method == "human" and verification.outcome == "passed":
        if review_status not in (ReferenceStatus.SOURCE_VERIFIED, ReferenceStatus.HUMAN_REVIEWED):
            raise ValidationError(
                "a passed human review must specify source_verified or human_reviewed"
            )
        next_status = CandidateStatus.REVIEWED
        origin = review_status
    elif verification.method == "executable" and verification.outcome == "passed":
        next_status = CandidateStatus.VERIFIED
        origin = ReferenceStatus.EXECUTABLE_ORACLE
    else:
        next_status = CandidateStatus.CANDIDATE
        origin = ReferenceStatus.SYNTHETIC_UNVERIFIED

    reference = candidate.case.reference
    assert reference is not None
    updated_case = candidate.case.model_copy(
        update={
            "reference": reference.model_copy(update={"status": origin}),
            "provenance": candidate.case.provenance.model_copy(
                update={
                    "origin": origin,
                    "reviewer_identity": verification.actor
                    if verification.method == "human"
                    else candidate.case.provenance.reviewer_identity,
                }
            ),
        }
    )
    return candidate.model_copy(
        update={"case": updated_case, "status": next_status, "verifications": verifications}
    )


def candidate_for_promotion(candidate: DatasetCandidate) -> DatasetCandidate:
    """Require a recorded non-synthetic decision before a candidate becomes a Golden."""
    if candidate.status not in (CandidateStatus.REVIEWED, CandidateStatus.VERIFIED):
        raise ValidationError(
            f"candidate {candidate.candidate_id} is {candidate.status.value}; explicit "
            "human review or successful executable verification is required"
        )
    if (
        candidate.case.reference is None
        or candidate.case.reference.status is ReferenceStatus.SYNTHETIC_UNVERIFIED
    ):
        raise ValidationError("unreviewed synthetic references cannot be promoted")
    return candidate.model_copy(update={"status": CandidateStatus.PROMOTED})
