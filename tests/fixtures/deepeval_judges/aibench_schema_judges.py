"""A deterministic judge for *any* DeepEval metric: it answers each prompt with a valid
instance of the schema DeepEval asks for, built from the schema alone. Loaded only inside the
plugin worker, like `aibench_test_judges`.

`AgreeingJudge` answers every verdict with its first allowed value ("yes"), states one item
per list and gives the top score, so each metric runs its real scoring code end to end. It
proves wiring (fields, parameters, schemas, usage), not judgement; score semantics are
tested with the prompt-reading judges in `aibench_test_judges`.
"""

from __future__ import annotations

import enum
import types
import typing
from typing import Any

from deepeval.models import DeepEvalBaseLLM
from pydantic import BaseModel

_TEN_POINT = [False]  # G-Eval's schemas score 0..10; every other metric's score 0..1


def _value(annotation: Any, name: str) -> Any:
    origin = typing.get_origin(annotation)
    arguments = typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        options = [a for a in arguments if a is not type(None)]
        return _value(options[0], name) if options else None
    if origin is typing.Literal:
        return arguments[0]
    if origin in (list, tuple, set, typing.List):  # noqa: UP006 - old-style hints in DeepEval
        return [_value(arguments[0] if arguments else str, name)]
    if origin is dict:
        return {}
    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel):
            return build(annotation)
        if issubclass(annotation, enum.Enum):
            return next(iter(annotation))
        if issubclass(annotation, bool):
            return True
        top = "score" in name and _TEN_POINT[0]
        if issubclass(annotation, int):
            return 10 if top else 1
        if issubclass(annotation, float):
            return 10.0 if top else 1.0
    if name == "verdict":
        return "yes"
    return f"{name} (schema judge)"


def build(schema: type[BaseModel]) -> BaseModel:
    if "g_eval" in schema.__module__:
        _TEN_POINT[0] = True
    elif "deepeval" in schema.__module__:
        _TEN_POINT[0] = False
    return schema(
        **{name: _value(field.annotation, name) for name, field in schema.model_fields.items()}
    )


class AgreeingJudge(DeepEvalBaseLLM):
    def __init__(self) -> None:
        self.calls = 0
        super().__init__(model="schema-judge")

    def load_model(self, *args: Any, **kwargs: Any) -> AgreeingJudge:
        return self

    def get_model_name(self, *args: Any, **kwargs: Any) -> str:
        return "schema-judge"

    def generate(self, prompt: str, schema: Any = None, **kwargs: Any) -> Any:
        self.calls += 1
        return build(schema) if schema is not None else "yes"

    async def a_generate(self, prompt: str, schema: Any = None, **kwargs: Any) -> Any:
        return self.generate(prompt, schema)


def agreeing_judge() -> AgreeingJudge:
    return AgreeingJudge()


PROMPTS: list[str] = []


class RecordingJudge(AgreeingJudge):
    """Keeps every prompt it is sent (in-process tests read `PROMPTS`)."""

    def generate(self, prompt: str, schema: Any = None, **kwargs: Any) -> Any:
        PROMPTS.append(str(prompt))
        return super().generate(prompt, schema)


def recording_judge() -> RecordingJudge:
    return RecordingJudge()


class PlanlessJudge(AgreeingJudge):
    """Finds no plan in any trace (every `plan` list empty), as for an agent that records
    no planning or reasoning; otherwise agrees."""

    def generate(self, prompt: str, schema: Any = None, **kwargs: Any) -> Any:
        answer = super().generate(prompt, schema)
        if isinstance(answer, BaseModel) and isinstance(getattr(answer, "plan", None), list):
            answer = answer.model_copy(update={"plan": []})
        return answer


def planless_judge() -> PlanlessJudge:
    return PlanlessJudge()


class ScatteredJudge(AgreeingJudge):
    """A G-Eval judge that is not consistent: successive scores for the same answer are 2, 9
    and 10 out of 10 (what a small judge did on a correct answer: 0.2 once, 1.0 the next
    time). Everything else is answered as `AgreeingJudge` does."""

    SCORES = (2, 9, 10)

    def __init__(self) -> None:
        super().__init__()
        self.scored = 0

    def generate(self, prompt: str, schema: Any = None, **kwargs: Any) -> Any:
        answer = super().generate(prompt, schema)
        if isinstance(answer, BaseModel) and isinstance(getattr(answer, "score", None), (int, float)):
            answer = answer.model_copy(update={"score": float(self.SCORES[self.scored % 3])})
            self.scored += 1
        return answer


def scattered_judge() -> ScatteredJudge:
    return ScatteredJudge()
