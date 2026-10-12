"""Deterministic bounded word and character error rates over recorded ASR transcripts."""

from __future__ import annotations

import math
import unicodedata
from typing import Any, ClassVar, Literal

from aibench.core.models import EvaluatorManifest, FieldRequirement, MetricDirection
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench_deepeval._version import __version__

MAX_TRANSCRIPT_CHARS = 16_000
MAX_EDIT_CELLS_PER_CASE = 4_000_000


def _manifest(evaluator_id: str, name: str, unit: str) -> EvaluatorManifest:
    return EvaluatorManifest(
        evaluator_id=f"deepeval.{evaluator_id}",
        version="1.0.0",
        plugin_id="aibench-deepeval",
        plugin_version=__version__,
        package_name="aibench-deepeval",
        package_version=__version__,
        description=(
            f"Deterministic {name} between the reference caller transcript and the voice agent's "
            "recorded provider transcription; lower values are better."
        ),
        limitations=(
            "Compares recorded transcript text only; it does not decode audio, transcribe speech, or call a model.",
            "Each case must have a non-empty reference transcript and a recorded provider_transcript; missing references or transcriptions are not applicable.",
            "Unicode text is normalized with NFKC. Case-folding and Unicode punctuation removal default to enabled and can be disabled independently.",
            f"{unit} units use a single Levenshtein alignment per case. The reported value is that case's edit errors divided by its reference {unit} count; the report's mean is an utterance-level macro-average.",
            "Reference transcripts that become empty after normalization are not applicable. Rates can exceed 1 when insertions outnumber reference units.",
            f"Reference and hypothesis text are limited to {MAX_TRANSCRIPT_CHARS} characters each, and alignment is limited to {MAX_EDIT_CELLS_PER_CASE:,} dynamic-programming cells per case.",
            "WER uses whitespace-delimited tokens; CER uses Unicode code points after NFKC normalization, including whitespace unless punctuation stripping removes it.",
        ),
        concepts=("transcription_error_rate",),
        value_kind="scalar",
        direction=MetricDirection.LOWER,
        aggregation="mean",
        requires=(
            FieldRequirement(path="case.input"),
            FieldRequirement(path="execution.output", non_empty=False),
        ),
        default_rule=None,
        parameters_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "lowercase": {"type": "boolean"},
                "strip_punctuation": {"type": "boolean"},
            },
        },
        uses_models=False,
        network_destinations=(),
        internal_retries=0,
        internal_concurrency=1,
        requires_worker=True,
    )


class TranscriptErrorRate(Evaluator):
    """Score one recorded reference/output utterance with a bounded edit-rate metric."""

    unit: ClassVar[Literal["word", "character"]]
    metric_name: ClassVar[str]

    async def prepare(self, params: Any) -> None:
        self.params = dict(params)

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        del ctx
        lowercase = self.params.get("lowercase", True)
        strip_punctuation = self.params.get("strip_punctuation", True)
        if not isinstance(lowercase, bool) or not isinstance(strip_punctuation, bool):
            return EvaluationOutcome.error("invalid_transcript_error_rate_parameters")

        reference_text, reference_error = _reference_text(view.get("case.input"))
        if reference_error is not None:
            return EvaluationOutcome.not_applicable(reference_error)
        hypothesis_text, hypothesis_error = _hypothesis_text(view.get("execution.output"))
        if hypothesis_error is not None:
            return EvaluationOutcome.not_applicable(hypothesis_error)
        assert reference_text is not None and hypothesis_text is not None
        if (
            len(reference_text) > MAX_TRANSCRIPT_CHARS
            or len(hypothesis_text) > MAX_TRANSCRIPT_CHARS
        ):
            return EvaluationOutcome.not_applicable("oversized:transcription_evidence")

        reference = _units(
            reference_text, self.unit, lowercase=lowercase, strip_punctuation=strip_punctuation
        )
        hypothesis = _units(
            hypothesis_text,
            self.unit,
            lowercase=lowercase,
            strip_punctuation=strip_punctuation,
        )
        if not reference:
            return EvaluationOutcome.not_applicable("empty_normalized_reference")
        edit_cells = (len(reference) + 1) * (len(hypothesis) + 1)
        if edit_cells > MAX_EDIT_CELLS_PER_CASE:
            return EvaluationOutcome.not_applicable("oversized:transcription_alignment")
        substitutions, deletions, insertions = _edit_counts(reference, hypothesis)

        errors = substitutions + deletions + insertions
        rate = errors / len(reference)
        if not math.isfinite(rate):
            return EvaluationOutcome.error("invalid_transcript_error_rate_score")
        return EvaluationOutcome.ok(
            "scalar",
            rate,
            evidence=("case.input", "execution.output"),
            raw={
                "metric": self.metric_name,
                "substitutions": substitutions,
                "deletions": deletions,
                "insertions": insertions,
                "edit_errors": errors,
                "reference_units": len(reference),
                "reference_chars": len(reference_text),
                "hypothesis_chars": len(hypothesis_text),
                "edit_cells": edit_cells,
                "normalization": {
                    "unicode": "NFKC",
                    "lowercase": lowercase,
                    "strip_punctuation": strip_punctuation,
                },
                "rate": rate,
            },
        )


class WordErrorRate(TranscriptErrorRate):
    """Word Error Rate (WER), in errors per reference word."""

    unit = "word"
    metric_name = "WordErrorRate"
    manifest = _manifest("word_error_rate", "word error rate", "word")


class CharacterErrorRate(TranscriptErrorRate):
    """Character Error Rate (CER), in errors per reference Unicode code point."""

    unit = "character"
    metric_name = "CharacterErrorRate"
    manifest = _manifest("character_error_rate", "character error rate", "character")


def _reference_text(value: Any) -> tuple[str | None, str | None]:
    transcript: Any
    if isinstance(value, str):
        transcript = value
    elif isinstance(value, dict):
        transcript = value.get("transcript")
    else:
        return None, "invalid:case.input"
    if transcript is None:
        return None, "missing:case.input.transcript"
    if not isinstance(transcript, str):
        return None, "invalid:case.input.transcript"
    if not transcript.strip():
        return None, "missing:case.input.transcript"
    return transcript, None


def _hypothesis_text(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, dict):
        return None, "missing:execution.provider_transcript"
    if "provider_transcript" not in value:
        return None, "missing:execution.provider_transcript"
    transcript = value["provider_transcript"]
    if not isinstance(transcript, str):
        return None, "invalid:execution.provider_transcript"
    return transcript, None


def _units(
    text: str,
    unit: Literal["word", "character"],
    *,
    lowercase: bool,
    strip_punctuation: bool,
) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text)
    if lowercase:
        normalized = normalized.casefold()
    if strip_punctuation:
        normalized = "".join(
            character
            for character in normalized
            if not unicodedata.category(character).startswith("P")
        )
    return normalized.split() if unit == "word" else list(normalized)


def _edit_counts(reference: list[str], hypothesis: list[str]) -> tuple[int, int, int]:
    """Return substitution, deletion, insertion counts from a minimum-cost alignment."""
    reference_length = len(reference)
    hypothesis_length = len(hypothesis)
    width = hypothesis_length + 1
    directions = bytearray((reference_length + 1) * width)
    for row in range(1, reference_length + 1):
        directions[row * width] = 1  # delete a reference unit
    for column in range(1, width):
        directions[column] = 2  # insert a hypothesis unit

    previous = list(range(width))
    for row, reference_unit in enumerate(reference, start=1):
        current = [row] + [0] * hypothesis_length
        row_offset = row * width
        for column, hypothesis_unit in enumerate(hypothesis, start=1):
            diagonal = previous[column - 1] + (reference_unit != hypothesis_unit)
            deletion = previous[column] + 1
            insertion = current[column - 1] + 1
            best = min(diagonal, deletion, insertion)
            current[column] = best
            directions[row_offset + column] = (
                0 if diagonal == best else 1 if deletion == best else 2
            )
        previous = current

    substitutions = deletions = insertions = 0
    row = reference_length
    column = hypothesis_length
    while row or column:
        direction = directions[row * width + column]
        if direction == 0:
            substitutions += reference[row - 1] != hypothesis[column - 1]
            row -= 1
            column -= 1
        elif direction == 1:
            deletions += 1
            row -= 1
        else:
            insertions += 1
            column -= 1
    return substitutions, deletions, insertions


TRANSCRIPT_ERROR_EVALUATORS = (WordErrorRate, CharacterErrorRate)
