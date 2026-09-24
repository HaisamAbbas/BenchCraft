"""The pinned openai/evals framework, driven one sample at a time (§11A, 17-T1).

Only an explicit allowlist of upstream eval classes is supported: the basic single-request
evals whose samples are `{"input": ..., "ideal": ...}` and whose verdict is recorded as a
`match` event. Everything else (model-graded evals, solvers, multi-turn and tool evals)
is refused by name, not approximated.

Upstream `evals.registry` constructs an OpenAI client when it is imported, and fails
without an API key. No OpenAI completion function is ever used here, so the worker sets a
placeholder key and points the OpenAI base URL at a closed loopback port before the first
import: any accidental call fails locally instead of reaching the network.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import random
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

os.environ["OPENAI_API_KEY"] = "aibench-placeholder-not-a-key"
os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:9/v1"

PINNED_EVALS = "3.0.1.post1"

# eval type -> (upstream class path, constructor parameters the adapter accepts)
ALLOWLIST: dict[str, tuple[str, tuple[str, ...]]] = {
    "match": ("evals.elsuite.basic.match:Match", ("num_few_shot", "few_shot")),
    "includes": ("evals.elsuite.basic.includes:Includes", ("ignore_case",)),
    "fuzzy_match": ("evals.elsuite.basic.fuzzy_match:FuzzyMatch", ()),
    "json_match": ("evals.elsuite.basic.json_match:JsonMatch", ()),
}


# Parameters that change the request the eval sends (the few-shot expansion). A delegated
# run records the expanded request as the case input, so its replay leaves them out.
PROMPT_PARAMS = ("num_few_shot", "few_shot")


class UnsupportedEval(ValueError):
    """An eval type or parameter outside the allowlist."""


class RequestRefused(RuntimeError):
    """The eval asked for something this completion function cannot honestly answer."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


def require_pinned_evals() -> None:
    try:
        installed = importlib.metadata.version("evals")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("evals is not installed in this plugin environment") from exc
    if installed != PINNED_EVALS:
        raise RuntimeError(
            f"evals {installed} is installed but this adapter is pinned to {PINNED_EVALS}; "
            "its eval classes have not been verified for other versions"
        )


def canonical(value: Any) -> str:
    """The exact-match form of a prompt, tagged by type so a string can never equal a list
    of chat messages."""
    tag = "text" if isinstance(value, str) else "json"
    return json.dumps({tag: value}, sort_keys=True)


@dataclass
class SampleRun:
    events: list[dict[str, Any]] = field(default_factory=list)
    requests: list[Any] = field(default_factory=list)
    error: str | None = None
    reason: str | None = None

    @property
    def correct(self) -> bool | None:
        matches = [e for e in self.events if e["type"] == "match"]
        if len(matches) != 1:
            return None
        return bool(matches[0]["data"].get("correct"))


def _eval_class(eval_type: str) -> Any:
    if eval_type not in ALLOWLIST:
        raise UnsupportedEval(
            f"eval type {eval_type!r} is not supported; supported: {', '.join(sorted(ALLOWLIST))}"
        )
    target, _ = ALLOWLIST[eval_type]
    module, _, name = target.partition(":")
    return getattr(importlib.import_module(module), name)


def check_params(eval_type: str, params: Mapping[str, Any]) -> list[str]:
    if eval_type not in ALLOWLIST:
        return [f"eval type {eval_type!r} is not in the allowlist"]
    allowed = ALLOWLIST[eval_type][1]
    return [f"parameter {k!r} is not supported for {eval_type}" for k in params if k not in allowed]


def run_sample(
    eval_type: str,
    params: Mapping[str, Any],
    sample: Mapping[str, Any],
    answer: Callable[[Any, int], str],
) -> SampleRun:
    """Run one sample through the upstream eval. `answer(prompt, request_index)` supplies
    each completion; it raises `RequestRefused` for a request it cannot honestly answer."""
    import evals.record
    from evals.api import CompletionResult
    from evals.base import RunSpec

    problems = check_params(eval_type, params)
    if problems:
        raise UnsupportedEval("; ".join(problems))
    run = SampleRun()

    class _Result(CompletionResult):
        def __init__(self, text: str) -> None:
            self.text = text

        def get_completions(self) -> list[str]:
            return [self.text]

    def completion_fn(prompt: Any, **_: Any) -> CompletionResult:
        run.requests.append(prompt)
        return _Result(answer(prompt, len(run.requests) - 1))

    kwargs: dict[str, Any] = {}
    # Under the working directory: upstream reads data through blobfile, which does not
    # recognise Windows drive paths but does read a relative local path.
    with tempfile.TemporaryDirectory(prefix="aibench-evals-", dir=os.getcwd()) as scratch:
        if params.get("few_shot"):
            path = os.path.join(scratch, "few_shot.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.writelines(json.dumps(line) + "\n" for line in params["few_shot"])
            kwargs["few_shot_jsonl"] = os.path.relpath(path)
        for key in ("num_few_shot", "ignore_case"):
            if key in params:
                kwargs[key] = params[key]
        cls = _eval_class(eval_type)
        instance = cls(
            completion_fns=[completion_fn],
            samples_jsonl="aibench-samples.jsonl",  # samples are passed one at a time
            eval_registry_path=scratch,
            name=f"aibench.{eval_type}",
            **kwargs,
        )
        spec = RunSpec(
            completion_fns=["aibench"],
            eval_name=f"aibench.{eval_type}",
            base_eval="aibench",
            split=eval_type,
            run_config={},
            created_by="aibench",
        )
        recorder = evals.record.DummyRecorder(run_spec=spec, log=False)
        try:
            with recorder.as_default_recorder(sample_id="sample"):
                instance.eval_sample(dict(sample), random.Random(0))
        except RequestRefused as exc:
            run.reason, run.error = exc.reason, str(exc)
        except Exception as exc:  # noqa: BLE001 - an upstream failure is recorded, not raised
            run.reason, run.error = "upstream_error", f"{type(exc).__name__}: {exc}"[:500]
        run.events = [
            {"type": e.type, "data": json.loads(json.dumps(e.data, default=str))}
            for e in recorder._events
        ]
    return run
