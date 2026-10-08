"""DeepEval's single-turn metrics and the `openai_compatible` judge, against the REAL pinned
DeepEval in its plugin environment. No DeepEval code is mocked: judges are deterministic
implementations of DeepEval's `DeepEvalBaseLLM` (tests/fixtures/deepeval_judges), or a local
Chat Completions server for the built-in judge. Skipped when the plugin environment is not
installed (see plugins/deepeval/README.md)."""

from __future__ import annotations

import gzip
import json
import sys
import threading
import time
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
        assert manifest.plugin_version == "0.2.0rc13" and manifest.package_version == "4.2.5"
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


SCATTERED = {"kind": "python_factory", "factory": "aibench_schema_judges:scattered_judge"}


def _g_eval(
    tmp_path: Path, registry: EvaluatorRegistry, judge: dict[str, Any], **params: Any
) -> Any:
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case("c1", answer="Refunds within 30 days.")],
        [execution("c1", "Refunds are available within 30 days.")],
    )
    binding = {
        "metric": "deepeval.g_eval",
        "params": {
            "judge": judge,
            "name": "agrees",
            "criteria": "Does the answer state the same facts as the expected output?",
            "evaluation_params": ["input", "actual_output", "expected_output"],
            **params,
        },
    }
    [result] = seeded.score([binding], registry=registry, timeout_seconds=120).results
    return result


def test_g_eval_scores_a_case_three_times_and_flags_scores_that_disagree(
    tmp_path: Path, registry: EvaluatorRegistry
) -> None:
    """The same answer, criteria and judge scored 0.2 in a run and 1.0 when scored again, so
    one G-Eval score is not trustworthy. Three scores 0.2, 0.9 and 1.0 give their median, say
    they disagree, and the reason starts with the code the report counts."""
    result = _g_eval(tmp_path, registry, SCATTERED)
    assert result.status is ExecutionStatus.OK and result.value.value == 0.9
    assert result.reason is not None and result.reason.startswith("unstable:")
    assert "0.20, 0.90, 1.00" in result.reason and "median 0.90" in result.reason

    steady = _g_eval(tmp_path / "steady", registry, AGREEING)
    assert steady.value.value == 1.0
    assert steady.reason is not None and steady.reason.startswith("median of 3 judge scores")
    assert "unstable" not in steady.reason


def _g_eval_without_fields(tmp_path: Path, registry: EvaluatorRegistry, criteria: str) -> Any:
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1")], [execution("c1", "Refunds are available.")])  # no reference answer
    binding = {
        "metric": "deepeval.g_eval",
        "params": {"judge": AGREEING, "name": "x", "criteria": criteria},
    }
    [result] = seeded.score([binding], registry=registry, timeout_seconds=120).results
    return result


def test_g_eval_criteria_about_the_expected_answer_send_the_judge_the_expected_answer(
    tmp_path: Path, registry: EvaluatorRegistry
) -> None:
    """With no `evaluation_params` the judge saw only the question and the answer, so criteria
    like "states the same facts as the expected answer" made it reply that the expected answer
    was missing, and every case scored 0. Criteria that speak of the expected (or reference)
    answer now include it; a case without one is not applicable, as it is when the field is
    named explicitly. Criteria that do not mention it are unchanged."""
    for criteria in (
        "The answer states the same facts as the expected answer.",
        "Compare it with the Reference Output.",
        "Does it match the ground-truth answer?",
    ):
        result = _g_eval_without_fields(tmp_path / criteria[:12], registry, criteria)
        assert result.status is ExecutionStatus.NOT_APPLICABLE, criteria
        assert result.reason == "missing:case.reference.answer"
    plain = _g_eval_without_fields(tmp_path / "plain", registry, "Is the answer polite?")
    assert plain.status is ExecutionStatus.OK
    expecting = _g_eval_without_fields(
        tmp_path / "unrelated", registry, "Answers must not be expected to be long."
    )
    assert expecting.status is ExecutionStatus.OK  # "expected" alone is not "expected answer"


def test_g_eval_with_one_repeat_is_the_single_score_it_was(
    tmp_path: Path, registry: EvaluatorRegistry
) -> None:
    result = _g_eval(tmp_path, registry, SCATTERED, repeats=1)
    assert result.status is ExecutionStatus.OK and result.value.value == 0.2
    assert not (result.reason or "").startswith(("unstable", "median"))


def test_g_eval_repeats_must_be_between_one_and_nine(registry: EvaluatorRegistry) -> None:
    base = {"judge": AGREEING, "name": "x", "criteria": "c"}
    for bad in (0, 10, 2.5, "3"):
        with pytest.raises(BindingValidationError):
            registry.validate(
                [MetricBinding(metric="deepeval.g_eval", params={**base, "repeats": bad})]
            )
    registry.validate([MetricBinding(metric="deepeval.g_eval", params={**base, "repeats": 5})])


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
        malformed: tuple[str, ...] = (),
        trickle: bool = False,
        gzip_replies: bool = False,
        max_in_flight: int | None = None,
        out_of_credit: bool = False,
        answer_seconds: float = 0.0,
        garbled_first: int = 0,
        reasoning_required: bool | dict[str, Any] = False,
        reported_cost: float | None = None,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _JudgeHandler)
        self.status = status
        # A reasoning model: it needs this many tokens for thinking before it writes the JSON,
        # and a request that allows fewer gets an empty, cut-off reply (finish_reason "length").
        self.thinking_tokens = thinking_tokens
        # Replies sent as the content of the first answered requests, in order, instead of
        # the valid JSON: a model's near-miss at JSON.
        self.malformed = list(malformed)
        # A stalled upstream behind a proxy that keeps the request alive: the reply never
        # finishes, but small bytes keep arriving, so no read ever times out.
        self.trickle = trickle
        # A large reply compressed on the wire, as OpenRouter sends one.
        self.gzip_replies = gzip_replies
        # A provider that refuses (429) any request beyond this many in flight, each answer
        # taking `answer_seconds`: Z.ai's glm-4.6 on long judge prompts.
        self.max_in_flight = max_in_flight
        # Every request refused for an empty balance, as Z.ai does with a 429.
        self.out_of_credit = out_of_credit
        self.answer_seconds = answer_seconds
        self.in_flight = 0
        self.peak_in_flight = 0
        self.refused = 0
        self.flight_lock = threading.Lock()
        # The first replies are not valid gzip though they say they are.
        self.garbled_first = garbled_first
        # A model that refuses to run with thinking switched off (GLM 5.3 Flash on OpenRouter).
        self.reasoning_required = reasoning_required
        # What each answer costs by the provider's own account (OpenRouter's usage.cost).
        self.reported_cost = reported_cost
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
        if self.server.out_of_credit:
            self.server.requests.append(
                {"body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))}
            )
            data = (
                b'{"error":{"code":"1113","message":"Insufficient balance or no resource '
                b'package. Please recharge."}}'
            )
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.server.max_in_flight is not None:
            with self.server.flight_lock:
                busy = self.server.in_flight >= self.server.max_in_flight
                if busy:
                    self.server.refused += 1
                else:
                    self.server.in_flight += 1
                    self.server.peak_in_flight = max(
                        self.server.peak_in_flight, self.server.in_flight
                    )
            if busy:
                self.rfile.read(int(self.headers["Content-Length"]))
                data = b'{"error": {"code": "1302", "message": "Rate limit reached"}}'
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            try:
                time.sleep(self.server.answer_seconds)
                self._answer()
            finally:
                with self.server.flight_lock:
                    self.server.in_flight -= 1
            return
        self._answer()

    def _answer(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
        )
        if self.server.trickle:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            try:
                for _ in range(600):  # up to 30 s of keep-alive bytes
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass  # the client gave up: that is the point
            return
        limited = self.server.forever or len(self.server.requests) <= self.server.rate_limited_first
        thinking_off = (body.get("thinking") or {}).get("type") == "disabled" or (
            body.get("reasoning") or {}
        ).get("enabled") is False
        refused = self.server.reasoning_required and thinking_off
        if refused:
            payload = (
                self.server.reasoning_required
                if isinstance(self.server.reasoning_required, dict)
                else OPENROUTER_THINKING_REFUSAL
            )
        elif limited:
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
            if self.server.malformed:
                content = self.server.malformed.pop(0)
            payload = {
                "choices": [
                    {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    **(
                        {"cost": self.server.reported_cost}
                        if self.server.reported_cost is not None
                        else {}
                    ),
                },
            }
            if body["max_tokens"] < self.server.thinking_tokens + 20:
                payload = {
                    "choices": [
                        {"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": body["max_tokens"]},
                }
        data = json.dumps(payload).encode()
        if self.server.gzip_replies:
            garbled = len(self.server.requests) <= self.server.garbled_first
            data = b"not gzip at all" if garbled else gzip.compress(data)
        self.send_response(400 if refused else 429 if limited else self.server.status)
        if limited:
            self.send_header("Retry-After", "0")
        self.send_header("Content-Type", "application/json")
        if self.server.gzip_replies:
            self.send_header("Content-Encoding", "gzip")
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


def test_a_judge_call_costs_what_the_provider_reports_or_what_its_prices_say(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every run said "spend partial": the judge's cost was never known. OpenRouter reports
    each call's cost (`usage.cost`); for a provider that does not (Z.ai), the judge config
    can state its prices per million tokens."""
    reported, server, _ = _openai_compatible_score(
        tmp_path / "reported", monkeypatch, 200, reported_cost=0.0004
    )
    calls = len(server.requests)
    assert reported.resources["cost"] == pytest.approx(0.0004 * calls)

    priced, server, _ = _openai_compatible_score(
        tmp_path / "priced",
        monkeypatch,
        200,
        {"price_per_million_tokens": {"input": 0.15, "output": 0.5}},
    )
    calls = len(server.requests)
    # 100 input and 20 output tokens a call
    assert priced.resources["cost"] == pytest.approx(calls * (100 * 0.15 + 20 * 0.5) / 1e6)
    assert priced.resources["accounting"] != "partial"


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


def test_a_reply_cut_off_by_the_allowance_is_asked_again_with_more_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A judge configured with too little room still scores: the allowance doubles until the
    model has finished thinking. A model that needs more than the ceiling fails with an error
    that says so, never a JSON decode error."""
    result, server, _ = _openai_compatible_score(
        tmp_path / "small",
        monkeypatch,
        200,
        {"max_output_tokens": 2000},
        thinking_tokens=5000,
    )
    assert result.status is ExecutionStatus.OK and result.value.value == 1.0
    sizes = [r["body"]["max_tokens"] for r in server.requests]
    assert sizes[:3] == [2000, 4000, 8000]
    assert set(sizes[2:]) == {8000}  # the metric's later calls keep the room that worked

    result, server, _ = _openai_compatible_score(
        tmp_path / "hopeless", monkeypatch, 200, thinking_tokens=100_000
    )
    assert result.status is ExecutionStatus.ERROR
    assert [r["body"]["max_tokens"] for r in server.requests] == [8000, 16000, 32000, 32768]
    assert "cut off at 32768 tokens" in (result.reason or "")
    assert "thinking" in (result.reason or "")
    assert "JSONDecodeError" not in (result.reason or "")

    # A judge that was already told not to think is not advised to be told so.
    result, _, _ = _openai_compatible_score(
        tmp_path / "told", monkeypatch, 200, {"thinking": "disabled"}, thinking_tokens=100_000
    )
    assert "cut off at 32768 tokens" in (result.reason or "")
    assert '"disabled"' not in (result.reason or "")


def test_the_judge_asks_a_model_not_to_think_when_told_to_and_by_default_on_zai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Judging does not need long reasoning: one glm-4.7-flashx call took 76 to 197 s and a
    whole run took an hour. Z.ai accepts `thinking: disabled` on every model, so it is the
    default there; anywhere else the provider's default is left alone unless it is set."""
    _, server, _ = _openai_compatible_score(tmp_path / "plain", monkeypatch, 200)
    assert "thinking" not in server.requests[0]["body"]  # a local server: nothing added
    _, server, _ = _openai_compatible_score(
        tmp_path / "off", monkeypatch, 200, {"thinking": "disabled"}
    )
    assert server.requests[0]["body"]["thinking"] == {"type": "disabled"}
    _, server, _ = _openai_compatible_score(
        tmp_path / "on", monkeypatch, 200, {"thinking": "enabled"}
    )
    assert server.requests[0]["body"]["thinking"] == {"type": "enabled"}


@requires_plugin_env
def test_a_comma_before_a_bracket_is_forgiven_and_other_bad_json_is_asked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """glm-4.5-air answered faithfulness with a near-miss at JSON on 11 of 15 cases. The one
    mistake models make most (a comma before a closing bracket) is repaired; anything else
    still not JSON is asked again, and a judge that never produces JSON fails with that said."""
    trailing = (
        '{"statements": ["a"], "verdicts": [{"verdict": "yes", "reason": "r"},], "reason": "x",}'
    )
    _, clean, _ = _openai_compatible_score(tmp_path / "clean", monkeypatch, 200)
    baseline = len(clean.requests)
    result, server, _ = _openai_compatible_score(
        tmp_path / "comma", monkeypatch, 200, malformed=(trailing,)
    )
    assert result.status is ExecutionStatus.OK and result.value.value == 1.0
    assert len(server.requests) == baseline  # the first reply was repaired, not asked again

    result, server, _ = _openai_compatible_score(
        tmp_path / "again", monkeypatch, 200, malformed=("{ not json at all",)
    )
    assert result.status is ExecutionStatus.OK and result.value.value == 1.0
    assert len(server.requests) == baseline + 1  # one bad reply, asked again

    result, server, _ = _openai_compatible_score(
        tmp_path / "never", monkeypatch, 200, malformed=("{ not json",) * 9
    )
    assert result.status is ExecutionStatus.ERROR
    assert "not valid JSON" in (result.reason or "")


def test_a_judge_that_copies_deepevals_doubled_braces_still_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact reply glm-4.5-air gave for faithfulness's verdicts on 11 of 15 cases, every
    time: DeepEval's prompt shows its example JSON with doubled braces and the model copied
    them. That is repaired, not asked again (it would answer the same)."""
    doubled = (
        '{\n"statements": ["a"],\n"verdicts": [\n{{\n"verdict": "yes",\n"reason": "r"\n}}\n]  \n,'
        '"reason": "x"\n}'
    )
    _, clean, _ = _openai_compatible_score(tmp_path / "clean", monkeypatch, 200)
    result, server, _ = _openai_compatible_score(
        tmp_path / "doubled", monkeypatch, 200, malformed=(doubled,)
    )
    assert result.status is ExecutionStatus.OK and result.value.value == 1.0
    assert len(server.requests) == len(clean.requests)


@requires_plugin_env
def test_doubled_braces_are_collapsed_only_where_they_open_an_object() -> None:
    got = plugin_python(
        "import json; from aibench_deepeval.judges import _load_json as L;"
        "print(json.dumps(["
        'L(\'{"a": {"b": 1}}\'),'
        'L(\'{"s": "{{keep}}", "n": {{"x": 2}}}\'),'
        'L(\'{"v": [{{"k": "a }} b"}}, {{"k": 1}}],}\')]))'
    )
    assert got == [
        {"a": {"b": 1}},
        {"s": "{{keep}}", "n": {"x": 2}},
        {"v": [{"k": "a }} b"}, {"k": 1}]},
    ]


def test_a_reply_of_the_wrong_shape_is_asked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DeepSeek answered contextual precision with a document ({"title": ...}) instead of the
    verdicts DeepEval asked for; the pydantic error ended the case. Valid JSON of the wrong
    shape is now asked again, like a reply that is not JSON."""
    _, clean, _ = _openai_compatible_score(tmp_path / "clean", monkeypatch, 200)
    baseline = len(clean.requests)
    result, server, _ = _openai_compatible_score(
        tmp_path / "shape",
        monkeypatch,
        200,
        malformed=('{"title": "Bedrock Authentication", "body": "text"}',),
    )
    assert result.status is ExecutionStatus.OK and result.value.value == 1.0
    assert len(server.requests) == baseline + 1


@requires_plugin_env
def test_thinking_is_turned_off_for_openrouter_in_its_own_form() -> None:
    """On OpenRouter DeepSeek V4 Flash answered the same JSON in 2 s, not 8 s, at a sixth of
    the cost with `reasoning: {"enabled": false}`; the Z.ai form (`thinking`) is separate."""
    got = plugin_python(
        "import json; from aibench_deepeval.judges import default_thinking as d, _thinking_field as f;"
        "print(json.dumps(["
        "d('https://openrouter.ai/api/v1'), d('https://api.z.ai/api/paas/v4'),"
        "d('https://api.openai.com/v1'), d('http://127.0.0.1:8000/v1'),"
        "f('https://openrouter.ai/api/v1', 'disabled'), f('https://openrouter.ai/api/v1', 'enabled'),"
        "f('https://api.z.ai/api/paas/v4', 'disabled'), f('http://127.0.0.1:8000/v1', 'enabled')]))"
    )
    assert got == [
        "disabled",
        "disabled",
        "default",
        "default",
        {"reasoning": {"enabled": False}},
        {"reasoning": {"enabled": True}},
        {"thinking": {"type": "disabled"}},
        {"thinking": {"type": "enabled"}},
    ]


def test_a_call_that_keeps_trickling_bytes_still_ends_at_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A judge call to OpenRouter hung for over ten minutes against a 180 s timeout: the proxy
    kept the stalled request alive with small bytes, and the HTTP client's timeout is per
    read, so it never fired. `timeout_seconds` is now a total deadline per call."""
    started = time.monotonic()
    result, server, _ = _openai_compatible_score(
        tmp_path,
        monkeypatch,
        200,
        {"timeout_seconds": 1.0, "retry_wait_seconds": 0},
        trickle=True,
    )
    took = time.monotonic() - started
    assert result.status is ExecutionStatus.ERROR
    assert took < 25, f"the call outlived its deadline: {took:.0f}s"  # unfixed: the full 30 s
    assert len(server.requests) >= 2  # a timed-out call is asked again before giving up


def test_a_compressed_reply_that_is_garbled_in_transit_is_asked_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long judge call to OpenRouter ended in "DecodingError: incorrect header check" after
    143 s and failed the evaluation; the same call decoded fine on the next try."""
    result, server, _ = _openai_compatible_score(
        tmp_path, monkeypatch, 200, gzip_replies=True, garbled_first=1
    )
    assert result.status is ExecutionStatus.OK, result.reason
    assert len(server.requests) >= 2


# How each provider refuses to run a model with thinking off, word for word (HTTP 400).
OPENROUTER_THINKING_REFUSAL = {
    "error": {
        "message": "Reasoning is mandatory for this endpoint and cannot be disabled.",
        "code": 400,
    }
}
ZAI_THINKING_REFUSAL = {
    "error": {
        "code": "1210",
        "message": (
            "This model always engages in thinking and cannot be disabled; please use low, "
            "high, or max"
        ),
    }
}


@pytest.mark.parametrize(
    "refusal", [OPENROUTER_THINKING_REFUSAL, ZAI_THINKING_REFUSAL], ids=["openrouter", "zai"]
)
def test_a_model_that_must_think_is_asked_again_without_the_thinking_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: dict[str, Any]
) -> None:
    """GLM 5.3 Flash answers 400 to the request that switches thinking off, which the judge
    sends by default on OpenRouter and Z.ai: it drops the field and asks again. Z.ai words it
    differently (error 1210), and that one was not recognised: every call failed unless the
    project set `thinking: default` by hand."""
    result, server, _ = _openai_compatible_score(
        tmp_path,
        monkeypatch,
        200,
        {"thinking": "disabled"},
        reasoning_required=refusal,
    )
    assert result.status is ExecutionStatus.OK, result.reason
    first, *later = server.requests
    assert first["body"].get("thinking") == {"type": "disabled"}
    assert later and all("thinking" not in r["body"] for r in later)


def test_an_account_out_of_credit_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Z.ai answers an empty balance with 429 (error 1113), the status of a rate limit: the
    judge retried a request that waiting cannot fix. It now fails at once and says why."""
    result, server, _ = _openai_compatible_score(tmp_path, monkeypatch, 200, out_of_credit=True)
    assert result.status is ExecutionStatus.ERROR
    assert "Insufficient balance" in (result.reason or "")
    assert len(server.requests) == 1


def test_a_burst_the_provider_refuses_backs_off_instead_of_failing() -> None:
    """Contextual relevancy asks about every retrieved passage at once. Z.ai's glm-4.6 took
    about 25 s per answer and refused further requests while a few ran, so every retry was
    spent in seconds and the case failed. The judge now lets fewer requests through after a
    refusal: twelve calls against a provider allowing two at a time all succeed."""
    with _judge_server(200, max_in_flight=2, answer_seconds=0.3) as server:
        snippet = f"""
import asyncio, json, os
os.environ["K"] = "x"
from aibench_deepeval.judges import openai_compatible_judge
judge = openai_compatible_judge({{
    "kind": "openai_compatible", "base_url": "{server.base_url}", "model": "glm-test",
    "api_key_env": "K", "retry_wait_seconds": 0,
}})
async def main():
    done = await asyncio.gather(
        *[judge.a_generate("Say ok") for _ in range(12)], return_exceptions=True
    )
    return [type(d).__name__ if isinstance(d, Exception) else "ok" for d in done]
print(json.dumps([asyncio.run(main()), judge.retries]))
"""
        got = plugin_python(snippet)
    outcomes, retries = got
    assert outcomes == ["ok"] * 12, outcomes
    assert server.peak_in_flight <= 2
    assert retries <= 4  # backed off after the first refusals, not retried to exhaustion


def test_a_stalled_connection_is_abandoned_long_before_a_slow_reply_would_be() -> None:
    got = plugin_python(
        "import json, os; os.environ['K'] = 'x';"
        "from aibench_deepeval.judges import openai_compatible_judge as j;"
        "t = j({'kind': 'openai_compatible', 'base_url': 'http://x', 'model': 'm',"
        " 'api_key_env': 'K'})._timeouts();"
        "print(json.dumps([t.connect, t.read]))"
    )
    assert got == [15.0, 120.0]


@requires_plugin_env
def test_thinking_is_off_by_default_only_on_zai_hosts() -> None:
    got = plugin_python(
        "import json; from aibench_deepeval.judges import default_thinking as d;"
        "print(json.dumps([d(u) for u in ("
        "'https://api.z.ai/api/paas/v4', 'https://open.z.ai/x', 'https://api.openai.com/v1',"
        "'https://notz.ai.example.com/v1', 'http://127.0.0.1:8000/v1')]))"
    )
    assert got == ["disabled", "disabled", "default", "default", "default"]


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


def test_one_forbidden_tool_call_fails_tool_permission(tmp_path: Path) -> None:
    """An agent read a setting with an allowed tool, then deleted .env with a forbidden one:
    tool permission scored 0.5 (one call of two allowed) and passed at the 0.5 pass mark. A
    permission holds only when every call keeps it, so the default pass mark is 1.0."""
    registry = EvaluatorRegistry.with_native()
    registry.load_plugin_environment(PLUGIN_ENV)
    seeded = Seeded(tmp_path)
    calls = (
        {"name": "read_env", "arguments": {"key": "NUM_CTX"}, "status": "ok", "result": "8192"},
        {"name": "delete_file", "arguments": {"path": ".env"}, "status": "ok", "result": "gone"},
    )
    seeded.seed(
        [case("c1")],
        [
            execution(
                "c1",
                "I removed .env.",
                tool_events=calls,
                observation_completeness={"tool_events": {"state": "observed"}},
            )
        ],
    )
    binding = {"metric": "deepeval.tool_permission", "params": {"allowed_tools": ["read_env"]}}
    [result] = seeded.score([binding], registry=registry, timeout_seconds=120).results
    assert result.status is ExecutionStatus.OK, result.reason
    assert result.value.value == 0.5
    assert result.decision.value == "fail"


def test_metrics_scored_on_a_share_pass_only_when_nothing_is_broken() -> None:
    """Verified on real data: one bad turn in two scored 0.5, six identical tool calls scored
    0.6 for loop detection, and an agent that deleted .env with a forbidden tool scored 0.5;
    each passed at the 0.5 pass mark. Where one breach is a failure, the pass mark is 1.0;
    every other metric keeps 0.5."""
    strict = plugin_python(
        "import json; from aibench_deepeval import EVALUATORS;"
        "print(json.dumps({e.manifest.evaluator_id: e.manifest.default_rule.threshold"
        " for e in EVALUATORS}))"
    )
    assert {name for name, mark in strict.items() if mark != 0.5} == {
        "deepeval.tool_permission",
        "deepeval.agent_loop_detection",
        "deepeval.role_adherence",
        "deepeval.turn_faithfulness",
    }
    assert all(strict[name] == 1.0 for name in strict if strict[name] != 0.5)
