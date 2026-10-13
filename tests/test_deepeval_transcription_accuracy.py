"""Recorded provider-transcript scoring through pinned DeepEval G-Eval."""

from __future__ import annotations

from pathlib import Path

from aibench.planning.catalog import CONCEPTS
from aibench.registry import EvaluatorRegistry
from tests.deepeval_support import PLUGIN_ENV, plugin_python, requires_plugin_env

pytestmark = requires_plugin_env

_SETUP = """
import asyncio, json, os
os.environ.update(DEEPEVAL_TELEMETRY_OPT_OUT="1", DEEPEVAL_DISABLE_DOTENV="1")
from aibench.core.models import BenchmarkCase, ExecutionResult
from aibench.evaluators.protocol import EvaluationView, EvaluatorContext
from aibench_deepeval import EVALUATORS
from aibench_schema_judges import PROMPTS
BY_ID = {cls.manifest.evaluator_id: cls for cls in EVALUATORS}
CASE = BenchmarkCase(case_id="turn-2", input={"transcript":"Please renew the card."}, group_id="call-1")
EXECUTION = ExecutionResult(execution_id="e", run_id="r", case_id="turn-2", status="ok",
    output={"transcript":"I can help.", "provider_transcript":"Please renew the card."})
TURNS = [
    {"case_id":"turn-1", "input":{"transcript":"I need to change my address.",
     "diarization_reference":{"segments":[]}},
     "output":{"transcript":"Sure.", "provider_transcript":"I need to change my address.",
     "diarization":{"segments":[]}}},
    {"case_id":"turn-2", "input":{"transcript":"Please renew the card.",
     "diarization_reference":{"segments":[]}},
     "output":{"transcript":"I can help.", "provider_transcript":"Please renew the card.",
     "diarization":{"segments":[]}}},
]
async def outcome(turns=None, judge=None):
    evaluator = BY_ID["deepeval.transcription_accuracy_g_eval"]()
    await evaluator.prepare({"judge": judge or {"kind":"python_factory",
        "factory":"aibench_schema_judges:usage_recording_judge"}})
    view = EvaluationView(case=CASE, execution=EXECUTION,
        episode=tuple(TURNS if turns is None else turns))
    ctx = EvaluatorContext(run_id="r", scoring_id="s")
    result = await evaluator.evaluate(view, ctx)
    return result, ctx
"""


def test_transcription_evaluator_is_discovered_with_a_bounded_judge_contract() -> None:
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(
        PLUGIN_ENV, extra_paths=[Path(__file__).parent / "fixtures" / "deepeval_judges"]
    )
    assert [load.error for load in loads] == [None]
    manifest, _ = registry.resolve("deepeval.transcription_accuracy_g_eval@1")

    assert manifest.plugin_version == "0.2.0rc25"
    assert manifest.package_name == "deepeval"
    assert manifest.package_version == "4.2.5"
    assert manifest.uses_models and manifest.requires_worker
    assert manifest.internal_retries == 400
    assert manifest.internal_concurrency == 1
    assert set(manifest.concepts) <= set(CONCEPTS)
    assert {item.path for item in manifest.requires} == {"case.group_id", "episode.turns"}
    assert any(
        "does not ship a dedicated TranscriptionAccuracyMetric" in item
        for item in manifest.limitations
    )


def test_pinned_geval_scores_each_recorded_provider_transcript_and_reports_evidence() -> None:
    result = plugin_python(
        _SETUP
        + """
value, ctx = asyncio.run(outcome())
print(json.dumps({"status":value.status.value,
    "score":value.value.value if value.value else None,
    "metric":(value.raw or {}).get("metric"),
    "exchanges":(value.raw or {}).get("exchanges_scored"),
    "turns":(value.raw or {}).get("episode_turns"),
    "evidence":value.evidence, "prompts":PROMPTS,
    "usage_count":len(ctx.usage),
    "usage_calls":[item.calls for item in ctx.usage],
    "calls_without_tokens":[item.calls_without_tokens for item in ctx.usage]}))
"""
    )
    assert result["status"] == "ok"
    assert result["score"] == 1.0
    assert result["metric"] == "GEval"
    assert result["exchanges"] == result["turns"] == 2
    assert result["evidence"] == ["episode.turns"]
    assert len(result["prompts"]) == 4  # criteria generation and scoring for each exchange
    assert result["usage_count"] == 1 and result["usage_calls"] == [4]
    assert result["calls_without_tokens"] == [4]
    for transcript in ("Please renew the card.", "I need to change my address."):
        assert any(transcript in prompt for prompt in result["prompts"])


def test_empty_asr_transcript_is_scored_as_no_words_recognized() -> None:
    empty = [
        {
            "case_id": "turn-1",
            "input": {"transcript": "Turn left at Oak Street."},
            "output": {"transcript": "I can do that.", "provider_transcript": ""},
        }
    ]
    result = plugin_python(
        _SETUP
        + f"""
value, _ = asyncio.run(outcome(turns={empty!r}))
print(json.dumps({{"status":value.status.value, "score":value.value.value if value.value else None,
    "prompts":PROMPTS}}))
"""
    )
    assert result["status"] == "ok"
    assert (
        result["score"] == 1.0
    )  # the deterministic wiring judge agrees; prompt carries the marker
    assert any("[NO WORDS RECOGNIZED]" in prompt for prompt in result["prompts"])


def test_missing_or_invalid_transcripts_stop_before_judge_calls() -> None:
    cases = [
        (
            [{"input": {"transcript": "spoken words"}, "output": {"transcript": "reply"}}],
            "not_applicable",
            "missing:episode.provider_transcript",
        ),
        (
            [
                {
                    "input": {"transcript": "scored words"},
                    "output": {"transcript": "reply", "provider_transcript": "scored words"},
                },
                {"input": {"transcript": "unscored words"}, "output": {"transcript": "reply"}},
            ],
            "not_applicable",
            "missing:episode.provider_transcript",
        ),
        (
            [
                {
                    "input": {"transcript": "spoken words"},
                    "output": {"transcript": "reply", "provider_transcript": None},
                }
            ],
            "not_applicable",
            "invalid:episode.transcription_evidence",
        ),
        (
            [
                {
                    "input": {"transcript": " "},
                    "output": {"transcript": "reply", "provider_transcript": "spoken words"},
                }
            ],
            "not_applicable",
            "invalid:episode.transcription_evidence",
        ),
        (
            [
                {
                    "input": {"transcript": "x" * 16_001},
                    "output": {"transcript": "reply", "provider_transcript": "x"},
                }
            ],
            "not_applicable",
            "invalid:episode.transcription_evidence",
        ),
    ]
    for turns, status, reason in cases:
        result = plugin_python(
            _SETUP
            + f"""
value, _ = asyncio.run(outcome(turns={turns!r}))
print(json.dumps({{"status":value.status.value, "reason":value.reason, "prompts":PROMPTS}}))
"""
        )
        assert result["status"] == status
        assert result["reason"] == reason
        assert result["prompts"] == []


def test_episode_evidence_limit_is_checked_before_constructing_judge() -> None:
    result = plugin_python(
        _SETUP
        + """
import aibench_deepeval.transcription_accuracy as module
turns = [{"input":{"transcript":"x" * 16_000},
          "output":{"transcript":"reply", "provider_transcript":"y" * 16_000}}
         for _ in range(40)]
async def main():
    evaluator = BY_ID["deepeval.transcription_accuracy_g_eval"]()
    evaluator.params = {"judge": {"kind":"python_factory",
        "factory":"aibench_schema_judges:usage_recording_judge"}}
    module.build_judge = lambda _params: (_ for _ in ()).throw(AssertionError("judge constructed"))
    view = EvaluationView(case=CASE, execution=EXECUTION, episode=tuple(turns))
    return await evaluator.evaluate(view, EvaluatorContext(run_id="r", scoring_id="s"))
value = asyncio.run(main())
print(json.dumps([value.status.value, value.reason]))
"""
    )
    assert result == ["not_applicable", "oversized:episode.transcription_evidence"]
