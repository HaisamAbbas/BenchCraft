"""Live completion-function bridge: `python -m aibench_openai_evals_oss.bridge` (§11A).

The upstream eval runs here, in the plugin environment; its completion function asks the
harness for each answer, and the harness invokes the application through its runner and
records the execution. The harness owns the application; this worker never reaches it.

Protocol: one JSON object per line; requests on stdin, replies on the original stdout
(file descriptor 1 is redirected to stderr, so upstream prints cannot corrupt it).

    {"op": "hello"}                              -> {"ok": true, "evals": "3.0.1.post1", ...}
    {"op": "prompts", "eval_type", "params", "samples": [{"sample_id", "sample"}]}
        -> {"ok": true, "prompts": [{"sample_id", "prompt"} | {"sample_id", "error"}]}
    {"op": "run", "eval_type", "params", "samples": [...]}
        streams, per request:  {"op": "complete", "sample_id", "index", "prompt"}
          and reads back:      {"op": "completion", "text"} | {"op": "completion",
                                "refused": reason, "detail"}
        per sample:            {"op": "sample", "sample_id", "events", "requests",
                                "correct", "error", "reason"}
        finally:               {"op": "done", "ok": true}
    {"op": "close"}                              -> {"ok": true}   (then exits)

`prompts` asks the upstream eval for its first request without answering it, so the
harness can record each sample as a case whose input is exactly what the eval sends.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, TextIO

from aibench_openai_evals_oss import upstream
from aibench_openai_evals_oss._version import __version__

PROTOCOL = "aibench-openai-evals-oss-bridge/1"


def _protocol_stream() -> TextIO:
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(protocol_fd, "w", encoding="utf-8", buffering=1)


def _send(out: TextIO, message: dict[str, Any]) -> None:
    out.write(json.dumps(message, ensure_ascii=False) + "\n")
    out.flush()


def _discover(eval_type: str, params: dict[str, Any], sample: dict[str, Any]) -> Any:
    def stop(prompt: Any, index: int) -> str:
        raise upstream.RequestRefused("discovered", "prompt captured")

    run = upstream.run_sample(eval_type, params, sample, stop)
    if not run.requests:
        raise RuntimeError(run.error or "the eval made no request")
    return run.requests[0]


def main() -> int:
    out = _protocol_stream()
    upstream.require_pinned_evals()
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        op = request.get("op")
        if op == "hello":
            _send(
                out,
                {
                    "ok": True,
                    "protocol": PROTOCOL,
                    "plugin_version": __version__,
                    "evals": upstream.PINNED_EVALS,
                    "eval_types": sorted(upstream.ALLOWLIST),
                    "prompt_params": list(upstream.PROMPT_PARAMS),
                },
            )
        elif op == "close":
            _send(out, {"ok": True})
            return 0
        elif op in ("prompts", "run"):
            eval_type, params = request["eval_type"], request.get("params") or {}
            problems = upstream.check_params(eval_type, params)
            if problems:
                _send(out, {"ok": False, "error": "; ".join(problems)})
                continue
            if op == "prompts":
                prompts = []
                for item in request["samples"]:
                    try:
                        prompt = _discover(eval_type, params, item["sample"])
                        prompts.append({"sample_id": item["sample_id"], "prompt": prompt})
                    except Exception as exc:  # noqa: BLE001
                        prompts.append({"sample_id": item["sample_id"], "error": str(exc)[:500]})
                _send(out, {"ok": True, "prompts": prompts})
                continue
            for item in request["samples"]:
                sample_id = item["sample_id"]

                def answer(prompt: Any, index: int, sample_id: str = sample_id) -> str:
                    _send(
                        out,
                        {
                            "op": "complete",
                            "sample_id": sample_id,
                            "index": index,
                            "prompt": prompt,
                        },
                    )
                    reply = json.loads(sys.stdin.readline() or "{}")
                    if "text" in reply:
                        return str(reply["text"])
                    raise upstream.RequestRefused(
                        str(reply.get("refused") or "refused"), str(reply.get("detail") or "")
                    )

                run = upstream.run_sample(eval_type, params, item["sample"], answer)
                _send(
                    out,
                    {
                        "op": "sample",
                        "sample_id": sample_id,
                        "events": run.events,
                        "requests": len(run.requests),
                        "correct": run.correct,
                        "error": run.error,
                        "reason": run.reason,
                    },
                )
            _send(out, {"op": "done", "ok": True})
        else:
            _send(out, {"ok": False, "error": f"unknown op {op!r}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
