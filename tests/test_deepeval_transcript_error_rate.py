"""Bounded deterministic WER/CER over recorded application transcription output."""

from __future__ import annotations

from aibench.planning.catalog import CONCEPTS, concepts_in
from aibench.registry import EvaluatorRegistry
from tests.deepeval_support import PLUGIN_ENV, plugin_python, requires_plugin_env

pytestmark = requires_plugin_env

_SETUP = r"""
import asyncio, json
from aibench.core.models import BenchmarkCase, ExecutionResult
from aibench.evaluators.protocol import EvaluationView, EvaluatorContext
from aibench_deepeval.transcript_error_rate import (
    CharacterErrorRate, WordErrorRate, _edit_counts,
)

def case_view(reference, hypothesis):
    case = BenchmarkCase(case_id="utterance", input={"transcript":reference,
        "diarization_reference":{"segments":[]}}, group_id="voice-call")
    execution = ExecutionResult(execution_id="e", run_id="r", case_id="utterance",
        status="ok", output={"transcript":"spoken reply", "provider_transcript":hypothesis,
        "diarization":{"segments":[]}})
    return EvaluationView(case=case, execution=execution)

async def score(metric, reference, hypothesis, params=None):
    evaluator = metric()
    await evaluator.prepare(params or {})
    return await evaluator.evaluate(case_view(reference, hypothesis),
        EvaluatorContext(run_id="r", scoring_id="s"))

def summarize(result):
    return {"status":result.status.value, "reason":result.reason,
        "value":result.value.value if result.value else None, "raw":result.raw,
        "evidence":result.evidence}
"""


def test_error_rate_metrics_are_discovered_with_bounded_local_contracts() -> None:
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(PLUGIN_ENV)
    assert [load.error for load in loads] == [None]

    for name in ("word_error_rate", "character_error_rate"):
        manifest, _ = registry.resolve(f"deepeval.{name}@1")
        assert manifest.plugin_version == "0.2.0rc25"
        assert manifest.package_name == "aibench-deepeval"
        assert manifest.package_version == "0.2.0rc25"
        assert manifest.version == "1.0.0"
        assert manifest.direction.value == "lower"
        assert manifest.default_rule is None
        assert manifest.requires_worker and not manifest.uses_models
        assert not manifest.network_destinations
        assert manifest.internal_retries == 0
        assert manifest.internal_concurrency == 1
        assert {field.path for field in manifest.requires} == {
            "case.input",
            "execution.output",
        }
        assert set(manifest.concepts) <= set(CONCEPTS)
    assert "transcription_error_rate" in concepts_in("Minimize WER and character error rate")


def test_word_error_rate_uses_normalized_edits_and_exposes_components() -> None:
    result = plugin_python(
        _SETUP
        + """
import asyncio
value = asyncio.run(score(WordErrorRate, "TURN left, at 15 Oak Street!",
    "turn left at fifteen Oak Street"))
edits = _edit_counts(["a", "b", "c", "d"], ["a", "x", "c", "e", "f"])
print(json.dumps({"result":summarize(value), "edits":edits}))
"""
    )
    score = result["result"]
    assert score["status"] == "ok"
    assert score["value"] == 1 / 6
    assert score["raw"]["substitutions"] == 1
    assert score["raw"]["deletions"] == score["raw"]["insertions"] == 0
    assert score["raw"]["edit_errors"] == 1
    assert score["raw"]["reference_units"] == 6
    assert score["evidence"] == ["case.input", "execution.output"]
    assert result["edits"] == [2, 0, 1]


def test_character_rate_and_utterance_averaging_semantics_are_explicit() -> None:
    result = plugin_python(
        _SETUP
        + """
import asyncio
cer = summarize(asyncio.run(score(CharacterErrorRate, "café au lait", "cafe au lait")))
one_word = summarize(asyncio.run(score(WordErrorRate, "a", "b")))
ten_words = summarize(asyncio.run(score(WordErrorRate,
    "a b c d e f g h i j", "a b c d e f g h i x")))
print(json.dumps({"cer":cer,"rates":[one_word["value"],ten_words["value"]],
    "macro_mean":(one_word["value"] + ten_words["value"]) / 2}))
"""
    )
    assert result["cer"]["value"] == 1 / 12
    assert result["cer"]["raw"]["reference_units"] == 12
    assert result["rates"] == [1, 0.1]
    assert result["macro_mean"] == 0.55


def test_custom_normalization_empty_transcripts_and_alignment_bound_fail_closed() -> None:
    result = plugin_python(
        _SETUP
        + """
import asyncio
custom = summarize(asyncio.run(score(WordErrorRate, "Hi, A!", "hi a",
    {"lowercase":False,"strip_punctuation":False})))
missing = summarize(asyncio.run(score(WordErrorRate, "spoken words", "")))
empty_reference = summarize(asyncio.run(score(WordErrorRate, "!!!", "words")))
too_large = summarize(asyncio.run(score(WordErrorRate, "a " * 2000, "b " * 2000)))
over_one = summarize(asyncio.run(score(WordErrorRate, "a", "a b c")))
invalid_params = summarize(asyncio.run(score(WordErrorRate, "hello", "hello",
    {"lowercase":1})))
print(json.dumps([custom,missing,empty_reference,too_large,over_one,invalid_params]))
"""
    )
    custom, missing, empty_reference, too_large, over_one, invalid_params = result
    assert custom["value"] == 1
    assert custom["raw"]["normalization"] == {
        "unicode": "NFKC",
        "lowercase": False,
        "strip_punctuation": False,
    }
    assert missing["status"] == "ok" and missing["value"] == 1
    assert missing["raw"]["deletions"] == 2
    assert empty_reference["reason"] == "empty_normalized_reference"
    assert too_large["reason"] == "oversized:transcription_alignment"
    assert over_one["value"] == 2
    assert invalid_params["status"] == "error"
    assert invalid_params["reason"] == "invalid_transcript_error_rate_parameters"
