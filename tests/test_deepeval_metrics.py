"""DeepEval's single-turn metrics and the `openai_compatible` judge, against the REAL pinned
DeepEval in its plugin environment. No DeepEval code is mocked: judges are deterministic
implementations of DeepEval's `DeepEvalBaseLLM` (tests/fixtures/deepeval_judges), or a local
Chat Completions server for the built-in judge. Skipped when the plugin environment is not
installed (see plugins/deepeval/README.md)."""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import ExecutionStatus, MetricBinding, ReferenceAnswer
from aibench.planning.catalog import CONCEPTS
from aibench.registry import BindingValidationError, EvaluatorRegistry
from tests.deepeval_support import JUDGES, PLUGIN_ENV, plugin_python, requires_plugin_env
from tests.scoring_support import Seeded, case, execution

pytestmark = requires_plugin_env

AGREEING = {"kind": "python_factory", "factory": "aibench_schema_judges:agreeing_judge"}
UNJUDGED = {"deepeval.exact_match", "deepeval.pattern_match", "deepeval.tool_permission"}
EXTRA_PARAMS: dict[str, dict[str, Any]] = {
    "deepeval.misuse": {"domain": "customer support"},
    "deepeval.non_advice": {"advice_types": ["legal advice"]},
    "deepeval.role_violation": {"role": "a polite support agent"},
    "deepeval.prompt_alignment": {"prompt_instructions": ["Answer in one sentence."]},
    "deepeval.tool_permission": {"allowed_tools": ["lookup"]},
    "deepeval.pattern_match": {"pattern": "Refunds.*"},
    "deepeval.g_eval": {"name": "politeness", "criteria": "Is the answer polite?"},
}
ALL_METRICS = [
    "faithfulness",
    "answer_relevancy",
    "contextual_precision",
    "contextual_recall",
    "contextual_relevancy",
    "hallucination",
    "bias",
    "toxicity",
    "pii_leakage",
    "misuse",
    "non_advice",
    "role_violation",
    "prompt_alignment",
    "summarization",
    "task_completion",
    "argument_correctness",
    "tool_correctness",
    "tool_permission",
    "exact_match",
    "pattern_match",
    "g_eval",
]

# A case and execution that feed every field any metric reads.
_VIEW_SETUP = """
import asyncio, json, os
os.environ.update(DEEPEVAL_TELEMETRY_OPT_OUT="1", DEEPEVAL_DISABLE_DOTENV="1")
from aibench.core.models import BenchmarkCase, ExecutionResult, ReferenceAnswer, ToolExpectation
from aibench.evaluators.protocol import EvaluationView, EvaluatorContext
from aibench_deepeval import EVALUATORS
BY_ID = {cls.manifest.evaluator_id: cls for cls in EVALUATORS}
CASE = BenchmarkCase(case_id="c", input="How do refunds work?", reference=ReferenceAnswer(
    answer="Refunds within 30 days.", context=("Refunds are allowed within 30 days.",),
    tools=ToolExpectation(tool_names=("lookup",))))
EXECUTION = ExecutionResult(execution_id="e", run_id="r", case_id="c", status="ok",
    output="Refunds within 30 days.", retrieved_context=("Refunds are allowed within 30 days.",),
    tool_events=({"name": "lookup", "arguments": {"q": "refunds"}, "result": "ok"},),
    observation_completeness={"tool_events": {"state": "observed"}})
def view(case=CASE, execution=EXECUTION):
    return EvaluationView(case=case, execution=execution)
async def outcome(metric_id, params, v=None):
    evaluator = BY_ID[metric_id]()
    await evaluator.prepare(params)
    ctx = EvaluatorContext(run_id="r", scoring_id="s")
    result = await evaluator.evaluate(v or view(), ctx)
    return result, ctx
"""


def _params(metric_id: str, judge: dict[str, Any] | None = None) -> dict[str, Any]:
    params = dict(EXTRA_PARAMS.get(metric_id, {}))
    if metric_id not in UNJUDGED:
        params["judge"] = judge or AGREEING
    return params


@pytest.fixture(scope="module")
def registry() -> EvaluatorRegistry:
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(PLUGIN_ENV, extra_paths=[JUDGES])
    assert [load.error for load in loads] == [None]
    return registry


def test_every_metric_is_discovered_with_an_honest_manifest(registry: EvaluatorRegistry) -> None:
    for name in ALL_METRICS:
        manifest, _ = registry.resolve(f"deepeval.{name}@1")
        assert manifest.requires_worker and manifest.plugin_id == "aibench-deepeval"
        assert manifest.plugin_version == "0.2.0rc2" and manifest.package_version == "4.2.5"
        assert manifest.direction.value == "higher" and manifest.value_kind == "scalar"
        assert manifest.concepts and set(manifest.concepts) <= set(CONCEPTS), name
        judged = "judge" in manifest.parameters_schema["properties"]
        assert manifest.uses_models is judged is (manifest.evaluator_id not in UNJUDGED)
        assert manifest.internal_retries == 0
    assert "deepeval" not in sys.modules  # discovery reads manifests in a worker


@pytest.mark.parametrize(
    ("metric", "params", "problem"),
    [
        ("deepeval.misuse", {"judge": AGREEING}, "domain"),
        # Either-or requirements (allowed or denied tools; criteria or steps) fail as a whole.
        ("deepeval.tool_permission", {}, "not valid under any"),
        ("deepeval.g_eval", {"judge": AGREEING, "name": "tone"}, "not valid under any"),
        ("deepeval.bias", {"judge": {"kind": "mystery"}}, "judge"),
        (
            "deepeval.bias",
            {"judge": {"kind": "openai_compatible", "base_url": "https://x", "model": "m"}},
            "judge",
        ),
    ],
)
def test_bindings_missing_what_a_metric_needs_are_refused(
    registry: EvaluatorRegistry, metric: str, params: dict[str, Any], problem: str
) -> None:
    with pytest.raises(BindingValidationError) as raised:
        registry.validate([MetricBinding(metric=metric, params=params)])
    assert problem in str(raised.value)


def test_every_metric_scores_through_the_real_package() -> None:
    """Every metric builds its test case, runs DeepEval's real scoring code and returns a
    normalized score, with its upstream detail kept for the raw artifact."""
    cases = {name: _params(f"deepeval.{name}") for name in ALL_METRICS}
    result = plugin_python(
        _VIEW_SETUP
        + f"""
async def main():
    report = {{}}
    for name, params in {cases!r}.items():
        result, ctx = await outcome("deepeval." + name, params)
        raw = result.raw or {{}}
        report[name] = [result.status.value, result.value.value if result.value else None,
                        raw.get("deepeval_version"), sum(u.calls or 0 for u in ctx.usage)]
    return report
print(json.dumps(asyncio.run(main())))
"""
    )
    assert set(result) == set(ALL_METRICS)
    for name, (status, value, version, _calls) in result.items():
        assert status == "ok", (name, result[name])
        assert 0.0 <= value <= 1.0 and version == "4.2.5", name


@pytest.mark.parametrize(
    ("metric", "change", "reason"),
    [
        ("deepeval.answer_relevancy", "execution.output='   '", "unscorable_output:blank"),
        ("deepeval.bias", "execution.output={'a': 1}", "unscorable_output:dict"),
        (
            "deepeval.contextual_relevancy",
            "execution.retrieved_context=('  ',)",
            "empty:execution.retrieved_context",
        ),
        (
            "deepeval.hallucination",
            "case.reference=ReferenceAnswer(answer='x')",
            "empty:case.reference.context",
        ),
        ("deepeval.argument_correctness", "execution.tool_events=()", "no_tool_calls"),
        (
            "deepeval.tool_correctness",
            "case.reference=ReferenceAnswer(answer='x', context=('c',))",
            "missing:case.reference.tools",
        ),
    ],
)
def test_missing_or_empty_evidence_is_not_applicable_never_a_score(
    metric: str, change: str, reason: str
) -> None:
    target, _, value = change.partition("=")
    owner, _, attribute = target.partition(".")
    source = "CASE" if owner == "case" else "EXECUTION"
    result = plugin_python(
        _VIEW_SETUP
        + f"""
changed = {source}.model_copy(update={{{attribute!r}: {value}}})
v = view(case=changed) if {source!r} == "CASE" else view(execution=changed)
result, _ = asyncio.run(outcome({metric!r}, {_params(metric)!r}, v))
print(json.dumps([result.status.value, result.reason]))
"""
    )
    assert result == ["not_applicable", reason]


def test_metric_parameters_reach_the_judge() -> None:
    """What the plan states (a role, a domain, instructions, G-Eval criteria) is what the
    judge is asked about."""
    expected = {
        "deepeval.role_violation": "a polite support agent",
        "deepeval.misuse": "customer support",
        "deepeval.non_advice": "legal advice",
        "deepeval.prompt_alignment": "Answer in one sentence.",
        "deepeval.g_eval": "Is the answer polite?",
    }
    recording = {"kind": "python_factory", "factory": "aibench_schema_judges:recording_judge"}
    result = plugin_python(
        _VIEW_SETUP
        + f"""
import aibench_schema_judges as judges
seen = {{}}
for metric in {list(expected)!r}:
    judges.PROMPTS.clear()
    asyncio.run(outcome(metric, {{**{EXTRA_PARAMS!r}[metric], "judge": {recording!r}}}))
    seen[metric] = "\\n".join(judges.PROMPTS)
print(json.dumps(seen))
"""
    )
    for metric, text in expected.items():
        assert text in result[metric], metric


def test_g_eval_reads_only_the_fields_the_plan_names(
    tmp_path: Path, registry: EvaluatorRegistry
) -> None:
    """Through the real worker: G-Eval asked to compare with the reference answer is not
    applicable where a case has none, and scores where it has one."""
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case("with", answer="Refunds within 30 days."), case("without")],
        [execution("with", "Refunds within 30 days."), execution("without", "Refunds.")],
    )
    binding = {
        "metric": "deepeval.g_eval",
        "params": {
            "judge": AGREEING,
            "name": "matches reference",
            "criteria": "Does the answer agree with the expected output?",
            "evaluation_params": ["input", "actual_output", "expected_output"],
        },
    }
    report = seeded.score([binding], registry=registry, timeout_seconds=120)
    by_case = {r.case_id: r for r in report.results}
    assert by_case["with"].status is ExecutionStatus.OK
    assert (by_case["without"].status, by_case["without"].reason) == (
        ExecutionStatus.NOT_APPLICABLE,
        "missing:case.reference.answer",
    )


# --------------------------------------------------------------------------- openai_compatible


class _JudgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        status: int,
        *,
        rate_limited_first: int = 0,
        forever: bool = False,
        thinking_tokens: int = 0,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _JudgeHandler)
        self.status = status
        # A reasoning model: it needs this many tokens for thinking before it writes the JSON,
        # and a request that allows fewer gets an empty, cut-off reply (finish_reason "length").
        self.thinking_tokens = thinking_tokens
        # The first `rate_limited_first` requests get a 429 (or every one, with `forever`).
        self.rate_limited_first = rate_limited_first
        self.forever = forever
        self.requests: list[dict[str, Any]] = []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


class _JudgeHandler(BaseHTTPRequestHandler):
    server: _JudgeServer

    def log_message(self, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
        )
        limited = self.server.forever or len(self.server.requests) <= self.server.rate_limited_first
        if limited:
            payload: Any = {"error": {"code": "1302", "message": "rate limit reached"}}
        elif self.server.status != 200:
            payload = {"error": f"bad key {self.headers.get('Authorization')}"}
        else:
            # One object with every key answer relevancy asks for (DeepEval's schemas
            # ignore the rest), fenced as some models do.
            answer = {
                "statements": ["Refunds are available within 30 days."],
                "verdicts": [{"verdict": "yes", "reason": "on topic"}],
                "reason": "The answer addresses refunds.",
            }
            content = "```json\n" + json.dumps(answer) + "\n```"
            payload = {
                "choices": [
                    {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }
            if body["max_tokens"] < self.server.thinking_tokens + 20:
                payload = {
                    "choices": [
                        {"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": body["max_tokens"]},
                }
        data = json.dumps(payload).encode()
        self.send_response(429 if limited else self.server.status)
        if limited:
            self.send_header("Retry-After", "0")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@contextmanager
def _judge_server(status: int = 200, **limits: Any) -> Iterator[_JudgeServer]:
    server = _JudgeServer(status, **limits)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _openai_compatible_score(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    judge_extra: dict[str, Any] | None = None,
    **limits: Any,
) -> tuple[Any, _JudgeServer, Seeded]:
    monkeypatch.setenv("AIBENCH_TEST_JUDGE_KEY", "sk-test-judge-secret-123456")
    registry = EvaluatorRegistry.with_native()
    registry.load_plugin_environment(
        PLUGIN_ENV, secret_env={"JUDGE_KEY": "env:AIBENCH_TEST_JUDGE_KEY"}
    )
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1")], [execution("c1", "Refunds are available within 30 days.")])
    with _judge_server(status, **limits) as server:
        binding = {
            "metric": "deepeval.answer_relevancy",
            "params": {
                "judge": {
                    "kind": "openai_compatible",
                    "base_url": server.base_url,
                    "model": "glm-test",
                    "api_key_env": "JUDGE_KEY",
                    "retry_wait_seconds": 0,  # tests do not wait between attempts
                    **(judge_extra or {}),
                }
            },
        }
        [result] = seeded.score([binding], registry=registry, timeout_seconds=120).results
    return result, server, seeded


def test_openai_compatible_judge_scores_with_its_key_and_counts_its_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GLM-style endpoint as judge, end to end: the key goes only from the harness's
    secret reference into the worker, requests ask for JSON, calls and tokens are counted,
    and cost stays unknown."""
    result, server, seeded = _openai_compatible_score(tmp_path, monkeypatch, 200)
    assert result.status is ExecutionStatus.OK and result.value.value == 1.0
    assert server.requests and all(r["path"] == "/v1/chat/completions" for r in server.requests)
    assert {r["auth"] for r in server.requests} == {"Bearer sk-test-judge-secret-123456"}
    body = server.requests[0]["body"]
    assert body["model"] == "glm-test" and body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    calls = len(server.requests)
    assert result.resources["model_calls"] == calls
    assert result.resources["tokens"] == {"input": 100 * calls, "output": 20 * calls}
    # Calls and tokens are known, the price is not: partial, never a cost of zero.
    assert result.resources["cost"] is None and result.resources["accounting"] == "partial"
    raw = seeded.artifacts.read_bytes(seeded.storage.get_artifact(result.raw_artifact_ref))
    assert b"sk-test-judge-secret" not in raw


def test_openai_compatible_judge_failure_is_an_error_that_never_shows_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, server, _ = _openai_compatible_score(tmp_path, monkeypatch, 401)
    assert result.status is ExecutionStatus.ERROR and result.value is None
    assert "judge HTTP 401" in (result.reason or "")
    assert "sk-test-judge-secret" not in (result.reason or "")
    assert len(server.requests) == 1  # a wrong key is not retried


def test_openai_compatible_judge_waits_out_a_rate_limit_and_still_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Free endpoints answer 429 under load (Z.ai: code 1302). The judge waits (the
    server's Retry-After) and tries again instead of failing the case."""
    result, server, _ = _openai_compatible_score(tmp_path, monkeypatch, 200, rate_limited_first=3)
    assert result.status is ExecutionStatus.OK and result.value.value == 1.0
    limited = [r for r in server.requests[:3]]
    assert len(limited) == 3 and len(server.requests) > 3
    # Only answered calls are counted as the judge's calls and tokens.
    answered = len(server.requests) - 3
    assert result.resources["model_calls"] == answered
    assert result.resources["tokens"]["input"] == 100 * answered


def test_openai_compatible_judge_gives_up_after_its_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, server, _ = _openai_compatible_score(tmp_path, monkeypatch, 200, forever=True)
    assert result.status is ExecutionStatus.ERROR
    assert "judge HTTP 429" in (result.reason or "")
    assert len(server.requests) == 5  # one call, five attempts, then the error


def test_openai_compatible_judge_leaves_room_for_a_reasoning_model_to_think(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """glm-4.7-flashx spent its whole 2000-token allowance thinking and returned nothing, so
    faithfulness and contextual precision failed on every case. The default allowance leaves
    room, and a reply cut off by the allowance says so instead of a JSON decode error."""
    result, server, _ = _openai_compatible_score(tmp_path, monkeypatch, 200, thinking_tokens=5000)
    assert result.status is ExecutionStatus.OK and result.value.value == 1.0
    assert server.requests[0]["body"]["max_tokens"] >= 8000

    result, _, _ = _openai_compatible_score(
        tmp_path / "small",
        monkeypatch,
        200,
        {"max_output_tokens": 2000},
        thinking_tokens=5000,
    )
    assert result.status is ExecutionStatus.ERROR
    assert "cut off at 2000 tokens" in (result.reason or "")
    assert "max_output_tokens" in (result.reason or "")
    assert "JSONDecodeError" not in (result.reason or "")


def test_reference_context_and_retrieval_are_never_swapped(
    tmp_path: Path, registry: EvaluatorRegistry
) -> None:
    """Hallucination reads only the reviewed reference context; retrieval metrics read only
    what the application retrieved."""
    seeded = Seeded(tmp_path)
    golden = case("c1").model_copy(
        update={"reference": ReferenceAnswer(answer="a", context=("REFERENCE",))}
    )
    seeded.seed([golden], [execution("c1", "answer")])  # retrieval not observed
    bindings = [
        {"metric": "deepeval.contextual_relevancy", "params": {"judge": AGREEING}},
        {"metric": "deepeval.hallucination", "params": {"judge": AGREEING}},
    ]
    results = {
        r.metric_id: r
        for r in seeded.score(bindings, registry=registry, timeout_seconds=120).results
    }
    assert (
        results["deepeval.contextual_relevancy"].status,
        results["deepeval.contextual_relevancy"].reason,
    ) == (
        ExecutionStatus.NOT_APPLICABLE,
        "missing:execution.retrieved_context",
    )
    assert results["deepeval.hallucination"].status is ExecutionStatus.OK
