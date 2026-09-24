"""Deterministic judges implementing DeepEval's real `DeepEvalBaseLLM` contract, injected into
the real DeepEval package for adapter tests (loaded only inside the plugin worker).

Convention used by test data:
- retrieved context mentions facts as tokens `FACT-<LETTER>`;
- an answer states claims as `CLAIM:FACT-<LETTER>`.
The judge extracts truths/claims from the prompt DeepEval builds and answers each verdict
from what *this instance* extracted, so any sharing of judge or metric state between
concurrent cases would produce wrong verdicts.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from deepeval.metrics.faithfulness.schema import (
    Claims,
    FaithfulnessScoreReason,
    FaithfulnessVerdict,
    Truths,
    Verdicts,
)
from deepeval.models import DeepEvalBaseLLM

_FACT = re.compile(r"\bFACT-[A-Z]\b")
_CLAIM = re.compile(r"CLAIM:(FACT-[A-Z])\b")


class TokenJudge(DeepEvalBaseLLM):
    delay_seconds = 0.0

    def __init__(self) -> None:
        self.truths: list[str] = []
        self.claims: list[str] = []
        self.calls = 0
        super().__init__(model="token-judge")

    def load_model(self, *args: Any, **kwargs: Any) -> TokenJudge:
        return self

    def get_model_name(self, *args: Any, **kwargs: Any) -> str:
        return "token-judge"

    def _answer(self, prompt: str, schema: Any) -> Any:
        self.calls += 1
        if schema is Truths:
            self.truths = sorted(set(_FACT.findall(prompt)))
            return Truths(truths=self.truths)
        if schema is Claims:
            self.claims = _CLAIM.findall(prompt)
            return Claims(claims=self.claims)
        if schema is Verdicts:
            return Verdicts(
                verdicts=[
                    FaithfulnessVerdict(verdict="yes" if c in self.truths else "no", reason=c)
                    for c in self.claims
                ]
            )
        if schema is FaithfulnessScoreReason:
            return FaithfulnessScoreReason(
                reason=f"token judge: claims={self.claims} truths={self.truths}"
            )
        raise ValueError(f"TokenJudge cannot answer schema {schema!r}")

    def generate(self, prompt: str, schema: Any = None, **kwargs: Any) -> Any:
        return self._answer(prompt, schema)

    async def a_generate(self, prompt: str, schema: Any = None, **kwargs: Any) -> Any:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self._answer(prompt, schema)


class SlowTokenJudge(TokenJudge):
    """Yields to the event loop between calls, so concurrent cases interleave."""

    delay_seconds = 0.02


class FailingJudge(TokenJudge):
    async def a_generate(self, prompt: str, schema: Any = None, **kwargs: Any) -> Any:
        raise RuntimeError("judge provider unavailable")


class BlockingJudge(TokenJudge):
    """Blocks the worker thread, like a stuck synchronous SDK call."""

    async def a_generate(self, prompt: str, schema: Any = None, **kwargs: Any) -> Any:
        time.sleep(180)  # noqa: ASYNC251 - deliberately blocks, like a stuck sync SDK call
        raise AssertionError("unreachable")


def token_judge() -> TokenJudge:
    return TokenJudge()


def slow_token_judge() -> TokenJudge:
    return SlowTokenJudge()


def failing_judge() -> TokenJudge:
    return FailingJudge()


def blocking_judge() -> TokenJudge:
    return BlockingJudge()


def not_a_judge() -> object:
    return object()
