"""Paired image-judge calibration compares provider bindings on identical reviewed cases."""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest

from aibench.cli import score as score_cli
from aibench.core.models import (
    DecisionRule,
    EvaluatorManifest,
    FieldRequirement,
    MetricDirection,
)
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench.registry import EvaluatorRegistry
from aibench.services.calibration import CalibrationError, load_calibration_set, run_calibration


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    chunk = kind + payload
    return (
        struct.pack(">I", len(payload)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
    )


_PNG = (
    b"\x89PNG\r\n\x1a\n"
    + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    + _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    + _png_chunk(b"IEND", b"")
)
_IMAGE = base64.b64encode(_PNG).decode("ascii")
_CRITERIA = "Read the amount in the receipt and compare it to the recorded answer."


class VisionCalibrationJudge(Evaluator):
    manifest = EvaluatorManifest(
        evaluator_id="deepeval.vision_g_eval",
        version="1.0.0",
        plugin_id="test-vision-calibration",
        plugin_version="1.0.0",
        description="deterministic fixture for paired image-judge calibration",
        value_kind="scalar",
        direction=MetricDirection.HIGHER,
        aggregation="mean",
        requires=(
            FieldRequirement(path="case.input"),
            FieldRequirement(path="case.reference.answer"),
            FieldRequirement(path="execution.output", non_empty=False),
        ),
        default_rule=DecisionRule(comparator=">=", threshold=0.5),
        parameters_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["judge", "criteria"],
            "properties": {
                "judge": {"type": "object", "additionalProperties": True},
                "criteria": {"type": "string"},
            },
        },
        uses_models=True,
    )

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        judge = self.params["judge"]
        model = judge["model"]
        output = view.get("execution.output")
        score = {
            ("vision-a", "18.42"): 0.8,
            ("vision-b", "18.42"): 0.9,
            ("vision-a", "18.24"): 0.2,
            ("vision-b", "18.24"): 0.7,
        }[(model, output)]
        ctx.report_usage(provider=model, calls=1, cost=None)
        return EvaluationOutcome.ok("scalar", score)


def _judge(endpoint: str, model: str, key_name: str) -> dict[str, str]:
    return {
        "kind": "openai_compatible",
        "base_url": endpoint,
        "model": model,
        "api_key_env": key_name,
    }


def _variant(*, case_id: str, pair_id: str, model: str, output: str, label: str) -> dict[str, Any]:
    endpoint = (
        "https://vision-a.example/v1" if model == "vision-a" else "https://vision-b.example/v1"
    )
    key_name = "VISION_A_KEY" if model == "vision-a" else "VISION_B_KEY"
    return {
        "id": case_id,
        "pair_id": pair_id,
        "category": "receipt-reading",
        "metric": "deepeval.vision_g_eval",
        "params": {"judge": _judge(endpoint, model, key_name), "criteria": _CRITERIA},
        "input": {
            "prompt": "What is the total?",
            "image": {"mime_type": "image/png", "data_base64": _IMAGE},
        },
        "reference": "The receipt total is 18.42.",
        "output": output,
        "label": label,
    }


def _write_set(root: Path, rows: list[dict[str, Any]]) -> Path:
    root.mkdir()
    (root / "set.json").write_text(
        json.dumps(
            {
                "schema": "aibench.judge-calibration/1",
                "version": "vision-pairs-v1",
                "review": {"status": "reviewed", "reviewer": "test reviewer"},
            }
        ),
        encoding="utf-8",
    )
    (root / "cases.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    return root


def test_vision_calibration_reports_paired_decision_and_score_differences(tmp_path: Path) -> None:
    rows = [
        _variant(
            case_id="correct-a",
            pair_id="receipt-correct",
            model="vision-a",
            output="18.42",
            label="acceptable",
        ),
        _variant(
            case_id="correct-b",
            pair_id="receipt-correct",
            model="vision-b",
            output="18.42",
            label="acceptable",
        ),
        _variant(
            case_id="wrong-a",
            pair_id="receipt-wrong",
            model="vision-a",
            output="18.24",
            label="unacceptable",
        ),
        _variant(
            case_id="wrong-b",
            pair_id="receipt-wrong",
            model="vision-b",
            output="18.24",
            label="unacceptable",
        ),
    ]
    root = _write_set(tmp_path / "vision-pairs", rows)
    registry = EvaluatorRegistry.with_native()
    registry.register(VisionCalibrationJudge)

    report = run_calibration(
        root,
        registry,
        repetitions=2,
        allow_models=True,
        max_model_evaluations=8,
    )

    assert report["schema"] == "aibench.judge-calibration-report/3"
    [comparison] = report["paired_comparisons"]["pairs"]
    assert report["paired_comparisons"]["group_count"] == 2
    assert report["paired_comparisons"]["paired_group_count"] == 2
    assert comparison["paired_cases"] == 2
    assert comparison["paired_observations"] == 4
    assert comparison["decision_agreement"] == {"value": 0.5, "numerator": 2, "denominator": 4}
    assert comparison["scores"] == {
        "observations": 4,
        "mean_a": 0.5,
        "mean_b": 0.8,
        "mean_delta_b_minus_a": 0.3,
        "mean_absolute_delta": 0.3,
    }
    assert len(comparison["disagreements"]) == 2
    assert report["usage"]["model_evaluations"] == 8
    assert _IMAGE not in json.dumps(report)


def test_vision_pair_rejects_mismatched_reviewed_sample_before_evaluation(
    tmp_path: Path,
) -> None:
    left = _variant(
        case_id="paired-a",
        pair_id="same-receipt",
        model="vision-a",
        output="18.42",
        label="acceptable",
    )
    right = _variant(
        case_id="paired-b",
        pair_id="same-receipt",
        model="vision-b",
        output="18.24",
        label="acceptable",
    )
    root = _write_set(tmp_path / "mismatched-pair", [left, right])

    with pytest.raises(CalibrationError, match="variants must use the same image"):
        load_calibration_set(root)


def test_vision_pair_limits_bindings_and_requires_distinct_judge_identity(
    tmp_path: Path,
) -> None:
    first = _variant(
        case_id="one", pair_id="too-many", model="vision-a", output="18.42", label="acceptable"
    )
    variants = [first]
    for index in range(1, 9):
        variant = json.loads(json.dumps(first))
        variant["id"] = f"variant-{index}"
        variant["params"]["judge"]["base_url"] = f"https://vision-{index}.example/v1"
        variant["params"]["judge"]["model"] = f"vision-{index}"
        variants.append(variant)
    too_many = _write_set(tmp_path / "too-many", variants)
    with pytest.raises(CalibrationError, match="8-binding comparison limit"):
        load_calibration_set(too_many)

    duplicate_a = _variant(
        case_id="duplicate-a",
        pair_id="same-provider",
        model="vision-a",
        output="18.42",
        label="acceptable",
    )
    duplicate_b = json.loads(json.dumps(duplicate_a))
    duplicate_b["id"] = "duplicate-b"
    duplicate_b["params"]["judge"]["api_key_env"] = "OTHER_VISION_KEY"
    duplicate = _write_set(tmp_path / "same-provider", [duplicate_a, duplicate_b])
    with pytest.raises(CalibrationError, match="repeats a judge endpoint/model identity"):
        load_calibration_set(duplicate)


def test_vision_pair_rejects_endpoint_and_model_aliases(tmp_path: Path) -> None:
    first = _variant(
        case_id="canonical-a",
        pair_id="canonical-provider",
        model="vision-a",
        output="18.42",
        label="acceptable",
    )
    second = json.loads(json.dumps(first))
    second["id"] = "canonical-b"
    second["params"]["judge"]["base_url"] = "HTTPS://VISION-A.EXAMPLE:443/v1/"
    second["params"]["judge"]["model"] = " vision-a "
    second["params"]["judge"]["api_key_env"] = "ANOTHER_KEY_REF"
    root = _write_set(tmp_path / "canonical-provider", [first, second])

    with pytest.raises(CalibrationError, match="repeats a judge endpoint/model identity"):
        load_calibration_set(root)


def test_vision_pair_rejects_unicode_and_punycode_endpoint_aliases(tmp_path: Path) -> None:
    first = _variant(
        case_id="idn-a",
        pair_id="idn-provider",
        model="vision-a",
        output="18.42",
        label="acceptable",
    )
    first["params"]["judge"]["base_url"] = "https://b\u00fccher.example/v1"
    second = json.loads(json.dumps(first))
    second["id"] = "idn-b"
    second["params"]["judge"]["base_url"] = "https://xn--bcher-kva.example/v1"
    second["params"]["judge"]["api_key_env"] = "ANOTHER_KEY_REF"
    root = _write_set(tmp_path / "idn-provider", [first, second])

    with pytest.raises(CalibrationError, match="repeats a judge endpoint/model identity"):
        load_calibration_set(root)


def test_vision_pair_matrix_has_a_global_report_size_limit(tmp_path: Path) -> None:
    rows = []
    for group_index in range(19):
        pair_id = f"matrix-{group_index}"
        base = _variant(
            case_id=f"{pair_id}-0",
            pair_id=pair_id,
            model="vision-a",
            output="18.42",
            label="acceptable",
        )
        for binding_index in range(8):
            variant = json.loads(json.dumps(base))
            variant["id"] = f"{pair_id}-{binding_index}"
            variant["params"]["judge"]["base_url"] = (
                f"https://provider-{group_index}-{binding_index}.example/v1"
            )
            variant["params"]["judge"]["model"] = f"vision-{group_index}-{binding_index}"
            rows.append(variant)
    root = _write_set(tmp_path / "large-pair-matrix", rows)

    with pytest.raises(CalibrationError, match="512-binding-pair report limit"):
        load_calibration_set(root)


def test_plain_calibration_summary_shows_paired_agreement_and_disagreements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines: list[str] = []
    monkeypatch.setattr(
        score_cli.console, "print", lambda value, **_kwargs: lines.append(str(value))
    )

    score_cli._print_paired_calibration(
        {
            "paired_comparisons": {
                "group_count": 1,
                "paired_group_count": 1,
                "unpaired_group_count": 0,
                "pairs": [
                    {
                        "binding_a": "vision-a",
                        "binding_b": "vision-b",
                        "decision_agreement": {
                            "value": 0.5,
                            "numerator": 1,
                            "denominator": 2,
                        },
                        "scores": {"mean_delta_b_minus_a": 0.1},
                        "undecided_observations": 0,
                        "disagreements": [
                            {
                                "pair_id": "receipt-1",
                                "repetition": 2,
                                "label": "acceptable",
                                "decision_a": "pass",
                                "decision_b": "fail",
                            }
                        ],
                    }
                ],
            }
        }
    )

    assert any("1/1 groups paired" in line for line in lines)
    assert any("decision agreement 1/2" in line and "+0.1000" in line for line in lines)
    assert any("receipt-1 repeat 2" in line and "pass vs fail" in line for line in lines)
