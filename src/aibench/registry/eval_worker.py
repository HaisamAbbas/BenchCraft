"""Evaluation worker: `python -m aibench.registry.eval_worker module:attr evaluator_id version`.

Runs one third-party evaluator inside its own environment (e.g. the DeepEval plugin's
virtualenv), driven by `aibench.evaluators.worker_client.WorkerEvaluator` in the harness.

Protocol: one JSON object per line. Requests on stdin, responses on the *original* stdout.
File descriptor 1 is redirected to stderr at startup, so anything the plugin prints — even
from C extensions — goes to stderr and can never corrupt the protocol stream.

    {"op": "prepare", "params": {...}}               -> {"ok": true}
    {"op": "evaluate", "case": {...}, "execution": {...}, "episode": [...]?}
        -> {"ok": true, "outcome": {...}, "usage": [...]}
    (`episode`, the conversation so far, is sent only to a metric requiring `episode.turns`;
    `trace`, the execution's span tree, only to one requiring `execution.trace`)
    {"op": "close"}                                   -> {"ok": true}   (then exits)
    any failure                                       -> {"ok": false, "error": "..."}

One request is handled at a time, on one persistent event loop. Artifact writing is not
available here; evaluators return raw payloads in their outcome instead, and the harness
persists them.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from typing import Any, TextIO

from aibench import __version__
from aibench.core.models import SCHEMA_VERSION, BenchmarkCase, ExecutionResult
from aibench.evaluators.protocol import EvaluationView, Evaluator, EvaluatorContext

MAX_ERROR_CHARS = 100_000  # a bound on the line, not the persisted text


def _protocol_stream() -> TextIO:
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(protocol_fd, "w", encoding="utf-8", buffering=1)


def _load(target: str, evaluator_id: str, version: str) -> Evaluator:
    module_name, _, attribute = target.partition(":")
    found = getattr(importlib.import_module(module_name), attribute)
    for factory in found if isinstance(found, (list, tuple)) else [found]:
        manifest = getattr(factory, "manifest", None)
        if manifest and (manifest.evaluator_id, manifest.version) == (evaluator_id, version):
            return factory()
    raise LookupError(f"{target} provides no evaluator {evaluator_id}@{version}")


def _outcome_json(outcome: Any, ctx: EvaluatorContext) -> dict[str, Any]:
    value = outcome.value
    return {
        "outcome": {
            "status": outcome.status.value,
            "value": None if value is None else value.model_dump(mode="json"),
            "reason": outcome.reason,
            "evidence": list(outcome.evidence),
            "raw": outcome.raw,
        },
        "usage": [
            {"provider": u.provider, "calls": u.calls, "tokens": u.tokens, "cost": u.cost}
            for u in ctx.usage
        ],
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: python -m aibench.registry.eval_worker module:attr id version", file=sys.stderr
        )
        return 64
    out = _protocol_stream()
    loop = asyncio.new_event_loop()
    evaluator: Evaluator | None = None

    def reply(payload: dict[str, Any]) -> None:
        try:
            line = json.dumps(payload, allow_nan=False, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            line = json.dumps({"ok": False, "error": f"unserializable response: {exc}"})
        out.write(line + "\n")
        out.flush()

    for raw_line in sys.stdin:
        try:
            request = json.loads(raw_line)
            op = request.get("op")
            if op == "prepare":
                evaluator = _load(*argv)
                loop.run_until_complete(evaluator.prepare(request.get("params") or {}))
                reply(
                    {"ok": True, "schema_version": SCHEMA_VERSION, "aibench_version": __version__}
                )
            elif op == "evaluate":
                if evaluator is None:
                    raise RuntimeError("evaluate before prepare")
                episode = request.get("episode")
                comparison = request.get("comparison")
                view = EvaluationView(
                    case=BenchmarkCase.model_validate(request["case"]),
                    execution=ExecutionResult.model_validate(request["execution"]),
                    episode=None if episode is None else tuple(episode),
                    trace=request.get("trace"),
                    comparison=None
                    if comparison is None
                    else ExecutionResult.model_validate(comparison),
                )
                ctx = EvaluatorContext(run_id=view.execution.run_id, scoring_id="worker")
                outcome = loop.run_until_complete(evaluator.evaluate(view, ctx))
                reply({"ok": True, **_outcome_json(outcome, ctx)})
            elif op == "close":
                if evaluator is not None:
                    loop.run_until_complete(evaluator.close())
                reply({"ok": True})
                return 0
            else:
                raise ValueError(f"unknown op {op!r}")
        except Exception as exc:  # noqa: BLE001 - every failure is reported to the harness
            # Not truncated here: the harness redacts secrets first, then truncates.
            reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
