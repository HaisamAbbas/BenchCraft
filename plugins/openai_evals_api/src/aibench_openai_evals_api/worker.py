"""Hosted OpenAI Evals API worker: `python -m aibench_openai_evals_api.worker` (§11B).

Every remote call goes through the official `openai` SDK (pinned), with its automatic
retries off: a retried POST could create a second eval or run, and the harness decides
what a failed call means. Failures are classified so the harness knows whether a
submission may have happened:

- `ambiguous`: timeout, dropped connection or a 5xx; the service may have processed it.
- `rejected`: a 4xx; it was not processed.
- `rate_limited`: 429; not processed, try later (`retry_after` when given).
- `auth`: 401/403.

Protocol: JSON lines on stdin; replies on the original stdout (fd 1 goes to stderr).

    {"op": "hello"}
    {"op": "prepare", "name", "criteria", "items", "metadata", "models_allowed"}
        -> {"ok": true, "eval_request", "run_request"}     (no network)
    {"op": "create_eval", "request"}        {"op": "list_evals", "after"}
    {"op": "create_run", "eval_id", "request"}   {"op": "list_runs", "eval_id", "after"}
    {"op": "retrieve_run", "eval_id", "run_id"}  {"op": "cancel_run", "eval_id", "run_id"}
    {"op": "list_output_items", "eval_id", "run_id", "after", "limit"}
    {"op": "close"}
    -> {"ok": true, "result": ...} | {"ok": false, "error_kind", "status", "error"}

The API key comes from OPENAI_API_KEY and the base URL from AIBENCH_OPENAI_BASE_URL, both
set by the harness from the job's policy-checked configuration.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import sys
from typing import Any, TextIO

from aibench_openai_evals_api import contract
from aibench_openai_evals_api._version import __version__

PROTOCOL = "aibench-openai-evals-api-worker/1"
PAGE_LIMIT = 100


def _protocol_stream() -> TextIO:
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(protocol_fd, "w", encoding="utf-8", buffering=1)


def _require_pinned() -> None:
    installed = importlib.metadata.version("openai")
    if installed != contract.PINNED_OPENAI:
        raise RuntimeError(
            f"openai {installed} is installed but this adapter is pinned to "
            f"{contract.PINNED_OPENAI}; the Evals API contract was checked for that release"
        )


def _client() -> Any:
    from openai import DefaultHttpxClient, OpenAI

    timeout = float(os.environ.get("AIBENCH_OPENAI_TIMEOUT", "60"))
    return OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ["AIBENCH_OPENAI_BASE_URL"],
        max_retries=0,
        timeout=timeout,
        # The SDK follows redirects by default: a redirect from the approved origin would
        # carry the request body (every uploaded item) somewhere the policy never
        # approved. Redirects are refused, and proxies from the environment are ignored.
        http_client=DefaultHttpxClient(follow_redirects=False, trust_env=False, timeout=timeout),
    )


def _classify(exc: Exception, *, creating: bool) -> dict[str, Any]:
    """What a failure means. For a create, only an explicit client error (4xx other than
    408/409/429) proves nothing was created: anything else, including a success response
    that could not be read, is ambiguous and must be reconciled."""
    import openai

    status = getattr(exc, "status_code", None)
    retry_after = None
    if isinstance(exc, openai.RateLimitError):
        kind = "rate_limited"
        header = exc.response.headers.get("retry-after")
        retry_after = float(header) if header and header.replace(".", "", 1).isdigit() else None
    elif isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        kind = "auth"
    elif (
        isinstance(exc, openai.APIStatusError)
        and status is not None
        and (300 <= status < 500)
        and status not in (408, 409)
    ):
        kind = "rejected"  # a redirect or client error: the request was not processed
    elif creating or isinstance(exc, openai.APIConnectionError):
        kind = "ambiguous"
    else:
        kind = "client"
    return {
        "ok": False,
        "error_kind": kind,
        "status": status,
        "retry_after": retry_after,
        "error": f"{type(exc).__name__}: {exc}"[:1000],
    }


def _page(page: Any) -> dict[str, Any]:
    return {"data": [item.model_dump(mode="json") for item in page.data], "has_more": page.has_more}


def handle(request: dict[str, Any]) -> dict[str, Any]:
    op = request.get("op")
    if op == "hello":
        return {
            "ok": True,
            "protocol": PROTOCOL,
            "plugin_version": __version__,
            "openai": contract.PINNED_OPENAI,
            "graders": list(contract.RULE_GRADERS + contract.MODEL_GRADERS),
        }
    if op == "prepare":
        problems = contract.check_criteria(
            request["criteria"], models_allowed=bool(request.get("models_allowed"))
        )
        if problems:
            return {"ok": False, "error_kind": "contract", "error": "; ".join(problems)}
        try:
            run_request = contract.run_request(
                request["name"], request["items"], request["metadata"]
            )
            contract.check_run_request(run_request)
        except contract.ContractError as exc:
            return {"ok": False, "error_kind": "contract", "error": str(exc)}
        return {
            "ok": True,
            "eval_request": contract.eval_request(
                request["name"], request["criteria"], request["metadata"]
            ),
            "run_request": run_request,
            "uses_models": contract.uses_models(request["criteria"]),
        }
    client = _client()
    evals = client.evals
    try:
        if op == "create_eval":
            return {
                "ok": True,
                "result": evals.create(**request["request"]).model_dump(mode="json"),
            }
        if op == "list_evals":
            kwargs = {"after": request["after"]} if request.get("after") else {}
            return {
                "ok": True,
                "result": _page(evals.list(limit=PAGE_LIMIT, order="desc", **kwargs)),
            }
        if op == "create_run":
            contract.check_run_request(request["request"])  # the last word before sending
            run = evals.runs.create(request["eval_id"], **request["request"])
            return {"ok": True, "result": run.model_dump(mode="json")}
        if op == "list_runs":
            kwargs = {"after": request["after"]} if request.get("after") else {}
            page = evals.runs.list(request["eval_id"], limit=PAGE_LIMIT, order="desc", **kwargs)
            return {"ok": True, "result": _page(page)}
        if op == "retrieve_run":
            run = evals.runs.retrieve(request["run_id"], eval_id=request["eval_id"])
            return {"ok": True, "result": run.model_dump(mode="json")}
        if op == "cancel_run":
            run = evals.runs.cancel(request["run_id"], eval_id=request["eval_id"])
            return {"ok": True, "result": run.model_dump(mode="json")}
        if op == "list_output_items":
            kwargs = {"after": request["after"]} if request.get("after") else {}
            page = evals.runs.output_items.list(
                request["run_id"],
                eval_id=request["eval_id"],
                limit=int(request.get("limit") or PAGE_LIMIT),
                order="asc",
                **kwargs,
            )
            return {"ok": True, "result": _page(page)}
    except contract.ContractError as exc:
        return {"ok": False, "error_kind": "contract", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - every failure is classified for the harness
        return _classify(exc, creating=op in ("create_eval", "create_run"))
    finally:
        client.close()
    return {"ok": False, "error_kind": "client", "error": f"unknown op {op!r}"}


def main() -> int:
    out = _protocol_stream()
    _require_pinned()
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        if request.get("op") == "close":
            out.write(json.dumps({"ok": True}) + "\n")
            return 0
        out.write(json.dumps(handle(request), ensure_ascii=False) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
