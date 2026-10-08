"""Making test cases from documents, for the chat's `/cases` commands.

The model writes candidate cases from documents the user names; nothing it writes is a case
until the user has looked at it beside the exact quote it cites and accepted it, and saved the
accepted ones to a new dataset file. The rules live in `datasets.candidates` and
`services.candidates` (the same ones `benchcraft candidates ...` uses): development data only,
every generated reference starts unverified, a changed source file fails closed, and a saved
dataset never replaces an existing file.
"""

from __future__ import annotations

import getpass
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from aibench.core.errors import ValidationError
from aibench.core.models import CandidatePoolManifest, CandidateStatus, DatasetCandidate
from aibench.datasets.candidates import (
    GENERATION_OUTPUT_TOKENS,
    MAX_SOURCE_FILES,
    CandidateGenerationError,
    answer_support,
    generate_candidate_pool,
    source_surroundings,
    verify_source_quote,
)
from aibench.planning.openai_provider import (
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
    provider_denials,
)
from aibench.security.policy import ExecutionPolicy
from aibench.services.candidates import path_denials, promote_candidates, record_candidate_review
from aibench.storage.repositories import Storage

SOURCE_SUFFIXES = (".txt", ".md")
GENERATION_TIMEOUT_SECONDS = 600.0  # the most a provider config allows
DEFAULT_OUTPUT = "cases-from-documents.jsonl"
_ACCEPTED = (CandidateStatus.REVIEWED, CandidateStatus.VERIFIED)


@dataclass(frozen=True)
class GeneratedPool:
    pool_id: str
    rows: list[dict[str, Any]]
    duplicate_sources: list[dict[str, str]]
    dropped: tuple[str, ...] = ()  # questions whose quoted source text was not in the document


def source_files(root: Path, words: list[str]) -> tuple[Path, ...]:
    """The documents named, relative to the project; a folder stands for its .txt and .md files."""
    found: list[Path] = []
    for word in words:
        path = Path(word.strip('"')).expanduser()
        path = (path if path.is_absolute() else root / path).resolve()
        if path.is_dir():
            found.extend(sorted(p for p in path.iterdir() if p.suffix.lower() in SOURCE_SUFFIXES))
        elif path.is_file():
            found.append(path)
        else:
            raise ValidationError(f"no such file or folder: {word}")
    if not found:
        raise ValidationError("no .txt or .md documents there")
    if len(found) > MAX_SOURCE_FILES:
        raise ValidationError(
            f"{len(found)} documents: at most {MAX_SOURCE_FILES} at a time; name fewer"
        )
    unsupported = [p.name for p in found if p.suffix.lower() not in SOURCE_SUFFIXES]
    if unsupported:
        raise ValidationError(
            f"only .txt and .md documents can be used (not {', '.join(unsupported)}); "
            "convert others to text first"
        )
    return tuple(found)


def draft_pool(
    config: OpenAICompatibleConfig,
    policy: ExecutionPolicy,
    sources: tuple[Path, ...],
    *,
    max_candidates: int = 20,
) -> tuple[CandidatePoolManifest, tuple[DatasetCandidate, ...], tuple[str, ...]]:
    """Ask the model for candidate cases from `sources`. The documents go to the model's
    provider, so the policy has to allow both. Blocking, and it touches no database, so the
    chat runs it off its own thread; `store_pool` keeps what it returns."""
    denials = [*path_denials(policy, sources), *provider_denials(config, policy)]
    if denials:
        raise CandidateGenerationError("; ".join(denials))
    # One call writes all the cases, and a model that thinks first takes minutes: the chat's
    # own timeout (it answers a message in seconds) would cut it off.
    capped = config.model_copy(
        update={
            # Its own allowance, not the assistant's: one call writes every case, and a model
            # that thinks first needs the room (a 4,000-token chat setting cannot generate).
            "max_output_tokens": GENERATION_OUTPUT_TOKENS,
            "timeout_seconds": max(config.timeout_seconds, GENERATION_TIMEOUT_SECONDS),
        }
    )
    provider = OpenAICompatibleProvider(capped)
    dropped: list[str] = []
    try:
        manifest, candidates = generate_candidate_pool(
            sources,
            provider,
            pool_id="pool-" + uuid4().hex,
            source_split="development",
            max_candidates=max_candidates,
            dropped=dropped,
        )
        return manifest, candidates, tuple(dropped)
    finally:
        provider.close()


def store_pool(
    storage: Storage,
    manifest: CandidatePoolManifest,
    candidates: tuple[DatasetCandidate, ...],
    dropped: tuple[str, ...] = (),
) -> GeneratedPool:
    storage.commit_candidate_pool(manifest, candidates)
    duplicates = [
        {"source_ref": item.source_ref, "duplicate_of": item.duplicate_of}
        for item in manifest.sources
        if item.duplicate_of
    ]
    return GeneratedPool(
        manifest.pool_id, pool_rows(storage, manifest.pool_id), duplicates, dropped
    )


def _ordered(storage: Storage, pool_id: str) -> list[DatasetCandidate]:
    """A pool's candidates in the order the model wrote them: the numbers the user sees.
    (The database's own order is by a random id.)"""
    manifest = storage.get_candidate_pool(pool_id)
    if manifest is None:
        raise ValidationError(f"no candidate pool {pool_id!r}")
    position = {cid: n for n, cid in enumerate(manifest.candidate_ids)}
    return sorted(storage.list_candidates(pool_id), key=lambda c: position[c.candidate_id])


def newest_pool_id(storage: Storage) -> str | None:
    pools = storage.list_candidate_pools()
    return pools[-1].pool_id if pools else None


def pool_rows(storage: Storage, pool_id: str) -> list[dict[str, Any]]:
    """Every candidate of a pool, numbered from 1, beside the source it cites."""
    return [
        _row(number, candidate)
        for number, candidate in enumerate(_ordered(storage, pool_id), start=1)
    ]


def _row(number: int, candidate: DatasetCandidate) -> dict[str, Any]:
    reference = candidate.case.reference
    assert reference is not None
    span = candidate.source_spans[0]
    heading: str | None = None
    heading_after: str | None = None
    before = after = ""
    try:
        before, quote_text, after, heading, heading_after = source_surroundings(span)
        quote: str | None = quote_text
        verbatim = verify_source_quote(candidate).outcome == "passed"
        support = 1.0 if verbatim else answer_support(reference.answer, quote_text)
    except ValidationError:
        quote, verbatim, support = None, False, 0.0  # the document changed or moved since
    return {
        "number": number,
        "candidate_id": candidate.candidate_id,
        "status": candidate.status.value,
        "question": str(candidate.case.input),
        "answer": reference.answer,
        "source": f"{Path(span.source_ref).name}:{span.start_line}",
        "quote": quote,
        "verbatim": verbatim,
        "support": support,
        "heading": heading,
        "heading_after": heading_after,
        "before": before,
        "after": after,
    }


def select(storage: Storage, pool_id: str, selectors: list[str]) -> list[DatasetCandidate]:
    """`all`, or numbers as `pool_rows` shows them, or candidate ids."""
    candidates = _ordered(storage, pool_id)
    if selectors == ["all"]:
        return [c for c in candidates if c.status is CandidateStatus.CANDIDATE]
    chosen: list[DatasetCandidate] = []
    for word in selectors:
        if word.isdigit() and 1 <= int(word) <= len(candidates):
            chosen.append(candidates[int(word) - 1])
        elif word.isdigit():
            raise ValidationError(f"no case number {word}; this pool has {len(candidates)}")
        else:
            match = next((c for c in candidates if c.candidate_id == word), None)
            if match is None:
                raise ValidationError(f"no case {word!r} in this pool")
            chosen.append(match)
    unique: dict[str, DatasetCandidate] = {}
    for candidate in chosen:
        unique.setdefault(candidate.candidate_id, candidate)
    return list(unique.values())


def decide(
    storage: Storage, pool_id: str, selectors: list[str], *, accept: bool
) -> list[dict[str, Any]]:
    """Record the user's decision on the cases they name. Accepting is a human review: the
    case's status becomes reviewed under the user's own name, and only reviewed cases can be
    saved."""
    reviewer = getpass.getuser() or "local user"
    note = (
        "accepted in the chat after reading the question, answer and cited source"
        if accept
        else "rejected in the chat"
    )
    done = []
    for candidate in select(storage, pool_id, selectors):
        if candidate.status is not CandidateStatus.CANDIDATE:
            continue  # already decided
        record_candidate_review(
            storage,
            candidate.candidate_id,
            reviewer=reviewer,
            decision="human_reviewed" if accept else "reject",
            note=note,
        )
        done.append(candidate.candidate_id)
    numbers = {c.candidate_id: n for n, c in enumerate(_ordered(storage, pool_id), start=1)}
    return [{"number": numbers[cid], "candidate_id": cid} for cid in done]


def save_accepted(storage: Storage, pool_id: str, output: Path) -> tuple[Path, int]:
    """Write the accepted cases of a pool to a new .jsonl dataset (never replacing a file)."""
    accepted = tuple(c.candidate_id for c in _ordered(storage, pool_id) if c.status in _ACCEPTED)
    if not accepted:
        if any(c.status is CandidateStatus.PROMOTED for c in _ordered(storage, pool_id)):
            raise ValidationError(
                "the accepted cases are already saved; accept more first (/cases accept N), "
                "then save them to a new file"
            )
        raise ValidationError("no accepted cases yet: /cases accept 1 2 3 (or /cases accept all)")
    path, cases = promote_candidates(
        storage, pool_id, accepted, output, actor=getpass.getuser() or "local user"
    )
    return path, len(cases)
