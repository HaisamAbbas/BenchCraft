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
# An arena contestant in DeepEval's prompt: its masked name opening its answer's JSON.
_CONTESTANT = re.compile(r'"([A-Z][a-z]+)": "\{')


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


class ArenaJudge(DeepEvalBaseLLM):
    """For DeepEval's ArenaGEval: picks the contestant whose answer contains `BEST`. With
    neither (or both) containing it, it picks the contestant listed first, the way a judge
    leans towards a position: judging both orders must turn that into a tie."""

    def __init__(self) -> None:
        super().__init__(model="arena-judge")

    def load_model(self, *args: Any, **kwargs: Any) -> ArenaJudge:
        return self

    def get_model_name(self, *args: Any, **kwargs: Any) -> str:
        return "arena-judge"

    def _answer(self, prompt: str, schema: Any) -> Any:
        from deepeval.metrics.arena_g_eval.schema import RewrittenReason, Steps, Winner

        if schema is Steps:
            return Steps(steps=["Compare the answers by the criteria."])
        if schema is RewrittenReason:
            # As real judges write it: the answers under the names in the prompt, "$name$".
            return RewrittenReason(rewritten_reason="$baseline$ and $current$ compared")
        if schema is Winner:
            # The contestants appear under masked names, each followed by its answer.
            names = [m for m in _CONTESTANT.finditer(prompt)]
            listed = []
            for index, match in enumerate(names):
                stop = names[index + 1].start() if index + 1 < len(names) else len(prompt)
                listed.append((match.group(1), prompt[match.end() : stop]))
            best = [name for name, answer in listed if "BEST" in answer]
            winner = best[0] if len(best) == 1 else listed[0][0]
            return Winner(winner=winner, reason=f"{winner} answers best")
        raise ValueError(f"ArenaJudge cannot answer schema {schema!r}")

    def generate(self, prompt: str, schema: Any = None, **kwargs: Any) -> Any:
        return self._answer(prompt, schema)

    async def a_generate(self, prompt: str, schema: Any = None, **kwargs: Any) -> Any:
        return self._answer(prompt, schema)


def arena_judge() -> ArenaJudge:
    return ArenaJudge()
