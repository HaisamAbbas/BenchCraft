"""Deterministic judges for the real pinned Ragas 0.4.3 adapter tests.

The classes below subclass Ragas' real ``InstructorBaseRagasLLM`` and return the
exact response model classes used by the pinned Faithfulness implementation:

* ``StatementGeneratorOutput`` for statement extraction; and
* ``NLIStatementOutput``/``StatementFaithfulnessAnswer`` for verdicts.

No Ragas metric or result is mocked.  A factory returns a fresh judge for every
adapter case, which also makes state-sharing regressions observable.

Fixture convention: retrieved context contains ``FACT-<LETTER>`` tokens and an
answer contains ``CLAIM:FACT-<LETTER>`` tokens.  Thus a context containing FACT-A
and FACT-B yields 1.0, .5, or 0.0 for answers containing the corresponding
claims, without making a network call.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from ragas.llms.base import InstructorBaseRagasLLM
from ragas.metrics.collections.faithfulness.util import (
    NLIStatementOutput,
    StatementFaithfulnessAnswer,
    StatementGeneratorOutput,
)

_FACT = re.compile(r"\bFACT-[A-Z]\b")
_CLAIM = re.compile(r"CLAIM:(FACT-[A-Z])\b")


class TokenJudge(InstructorBaseRagasLLM):
    """A deterministic structured-output judge with per-instance state."""

    delay_seconds = 0.0
    emit_statements = True

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.calls = 0
        self.prompts: list[str] = []

    def _answer(self, prompt: str, response_model: Any) -> Any:
        self.calls += 1
        self.prompts.append(prompt)
        if response_model is StatementGeneratorOutput:
            claims = (
                []
                if not self.emit_statements or "NO_STATEMENTS" in prompt
                else _CLAIM.findall(prompt)
            )
            self.statements = [f"CLAIM:{claim}" for claim in claims]
            # This is the exact response model passed by Ragas 0.4.3.
            return StatementGeneratorOutput(statements=self.statements)
        if response_model is NLIStatementOutput:
            # Read only the context field.  The prompt also contains the claims
            # being judged; scanning the whole prompt would accidentally make
            # every claim look supported.
            context_marker = '"context":'
            context_start = prompt.rfind(context_marker)
            supported: set[str] = set()
            if context_start >= 0:
                encoded_context = prompt[context_start + len(context_marker) :].lstrip()
                try:
                    context_text, _ = json.JSONDecoder().raw_decode(encoded_context)
                    supported = set(_FACT.findall(context_text))
                except (json.JSONDecodeError, TypeError):
                    supported = set()
            verdicts = []
            for statement in self.statements:
                match = _CLAIM.search(statement)
                is_supported = match is not None and match.group(1) in supported
                verdicts.append(
                    StatementFaithfulnessAnswer(
                        statement=statement,
                        reason=(
                            f"{match.group(1)} is present in the retrieved context"
                            if is_supported and match is not None
                            else "the statement is not supported by the retrieved context"
                        ),
                        verdict=1 if is_supported else 0,
                    )
                )
            # This is the exact response model passed by Ragas 0.4.3.
            return NLIStatementOutput(statements=verdicts)
        raise TypeError(f"unsupported deterministic response model: {response_model!r}")

    def generate(self, prompt: str, response_model: Any) -> Any:
        return self._answer(prompt, response_model)

    async def agenerate(self, prompt: str, response_model: Any) -> Any:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self._answer(prompt, response_model)


class SlowTokenJudge(TokenJudge):
    """Yield to the event loop so concurrent adapter cases can interleave."""

    delay_seconds = 0.02


class LenientTokenJudge(TokenJudge):
    """A valid independent judge that treats extracted claims as supported.

    This deliberately models a different judge policy for the cross-ecosystem
    calibration check. It still returns Ragas' real structured response models;
    the harness must report the resulting disagreement without averaging scores.
    """

    def _answer(self, prompt: str, response_model: Any) -> Any:
        if response_model is NLIStatementOutput:
            self.calls += 1
            self.prompts.append(prompt)
            return NLIStatementOutput(
                statements=[
                    StatementFaithfulnessAnswer(
                        statement=statement,
                        reason="the independent judge accepts the extracted statement",
                        verdict=1,
                    )
                    for statement in self.statements
                ]
            )
        return super()._answer(prompt, response_model)


class NoStatementsJudge(TokenJudge):
    """Exercise Ragas' real no-statements/NaN branch."""

    emit_statements = False


class FailingJudge(TokenJudge):
    async def agenerate(self, prompt: str, response_model: Any) -> Any:
        raise RuntimeError("judge provider unavailable")


class BlockingJudge(TokenJudge):
    """Simulate a provider call that cannot be cancelled in its own process."""

    async def agenerate(self, prompt: str, response_model: Any) -> Any:
        time.sleep(120)  # noqa: ASYNC251 - intentionally blocks the worker
        raise AssertionError("unreachable")


def token_judge() -> TokenJudge:
    return TokenJudge()


def slow_token_judge() -> TokenJudge:
    return SlowTokenJudge()


def lenient_token_judge() -> TokenJudge:
    return LenientTokenJudge()


def no_statements_judge() -> TokenJudge:
    return NoStatementsJudge()


def failing_judge() -> TokenJudge:
    return FailingJudge()


def blocking_judge() -> TokenJudge:
    return BlockingJudge()


def not_a_judge() -> object:
    return object()
