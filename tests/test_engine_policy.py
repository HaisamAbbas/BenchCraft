"""Plan validation and policy (06-T1, 06-T2; gate 06-G2): invalid plans and denied actions
dispatch zero application and judge calls — checked against the application's own log, the
HTTP server's request list, and the absence of any run record."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import EvaluatorManifest, MetricDirection
from aibench.engine.compile import PlanInvalid, PolicyDenied, compile_plan
from aibench.security.policy import ExecutionPolicy, evaluator_denials
from tests.engine_support import Harness


def _assert_nothing_dispatched(h: Harness) -> None:
    assert h.count() == 0 and not h.log.exists()
    storage, _ = h.storage()
    try:
        assert storage.list_runs() == []
    finally:
        storage.db.close()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"dataset": "missing.jsonl"}, "dataset file not found"),
        ({"application": "missing.json"}, "application config not found"),
        ({"metrics": [{"metric": "native.nope"}]}, "unknown evaluator"),
        (
            {"metrics": [{"metric": "native.exact_match", "params": {"typo": 1}}]},
            "Additional properties",
        ),
        ({"selection": {"case_ids": ["a", "zzz"]}}, "not in the dataset: zzz"),
        (
            {"retry": {"initial_backoff_seconds": 9, "max_backoff_seconds": 1}},
            "exceeds retry.max_backoff",
        ),
        ({"concurrency": {"application": 0}}, "greater than or equal to 1"),
    ],
)
def test_invalid_plans_dispatch_nothing(
    tmp_path: Path, overrides: dict[str, Any], expected: str
) -> None:
    h = Harness(tmp_path)
    fields = {"dataset": h.dataset({"a": "hi"}), "application": h.cli_app(), **overrides}
    with pytest.raises(PlanInvalid, match=expected):
        compile_plan(h.plan(**fields), policy=ExecutionPolicy(), trusted_local=True)
    _assert_nothing_dispatched(h)


def test_duplicate_selected_case_ids_are_refused(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    (tmp_path / "dup.jsonl").write_text(
        '{"case_id":"a","input":"x"}\n{"case_id":"a","input":"y"}\n', encoding="utf-8"
    )
    with pytest.raises(PlanInvalid, match="duplicate case_ids"):
        compile_plan(
            h.plan(dataset="dup.jsonl", application=h.cli_app()),
            policy=ExecutionPolicy(),
            trusted_local=True,
        )
    _assert_nothing_dispatched(h)


def test_all_problems_are_reported_together(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan = h.plan(dataset="missing.jsonl", application="missing.json")
    with pytest.raises(PlanInvalid) as info:
        compile_plan(plan, policy=ExecutionPolicy(), trusted_local=True)
    assert len(info.value.problems) >= 2


@pytest.mark.parametrize(
    ("policy", "trusted", "app_kwargs", "plan_fields", "expected"),
    [
        (ExecutionPolicy(), False, {}, {}, "requires trusted-local mode"),
        (ExecutionPolicy(), True, {"effects": "reversible"}, {}, "declares reversible effects"),
        (
            ExecutionPolicy(allowed_applications=("other-*",)),
            True,
            {},
            {},
            "not an approved target",
        ),
        (
            ExecutionPolicy(allowed_evaluators=("native.json_schema",)),
            True,
            {},
            {},
            "not allowed by the policy",
        ),
        (
            ExecutionPolicy(ceilings={"max_application_calls": 5}),
            True,
            {},
            {},
            "the plan must set it",
        ),
        (
            ExecutionPolicy(ceilings={"max_application_calls": 5}),
            True,
            {},
            {"budgets": {"max_application_calls": 50}},
            "exceeds the policy ceiling",
        ),
        (
            ExecutionPolicy(),
            True,
            {},
            {"plugin_environments": [{"python": "somewhere/python"}]},
            "plugin environment somewhere/python is not allowed",
        ),
    ],
)
def test_denied_actions_dispatch_nothing(
    tmp_path: Path,
    policy: ExecutionPolicy,
    trusted: bool,
    app_kwargs: dict[str, Any],
    plan_fields: dict[str, Any],
    expected: str,
) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({"a": "hi"}), application=h.cli_app(**app_kwargs), **plan_fields
    )
    with pytest.raises(PolicyDenied, match=expected):
        compile_plan(plan, policy=policy, trusted_local=trusted)
    _assert_nothing_dispatched(h)


def test_denied_http_target_and_secret_never_receive_a_request(tmp_path: Path) -> None:
    from tests.runner_support import load_example, serving

    effect_app = load_example("effect_counter_app")
    server = effect_app.make_server(port=0)
    h = Harness(tmp_path)
    with serving(server) as base:
        config = {
            "application_id": "remote",
            "runner": "http",
            "target": "t",
            "transport": {
                "kind": "http",
                "url": f"{base}/book",
                "secret_headers": {"Authorization": {"ref": "env:APP_TOKEN"}},
            },
        }
        (tmp_path / "remote.json").write_text(json.dumps(config), encoding="utf-8")
        plan = h.plan(dataset=h.dataset({"a": "hi"}), application="remote.json")
        with pytest.raises(PolicyDenied, match="secret env:APP_TOKEN is not allowed"):
            compile_plan(plan, policy=ExecutionPolicy())
        # Loopback is allowed by default; a real remote origin needs an explicit allow.
        config["transport"] = {"kind": "http", "url": "https://rag.example.com/answer"}
        (tmp_path / "remote.json").write_text(json.dumps(config), encoding="utf-8")
        with pytest.raises(
            PolicyDenied, match="https://rag.example.com:443/ is not an approved target"
        ):
            compile_plan(plan, policy=ExecutionPolicy())
        compile_plan(
            plan, policy=ExecutionPolicy(allowed_http_origins=("https://rag.example.com",))
        )
    assert server.received == []


def test_model_evaluators_need_explicit_data_egress_permission() -> None:
    manifest = EvaluatorManifest(
        evaluator_id="vendor.judge",
        version="1.0.0",
        plugin_id="vendor",
        plugin_version="1",
        description="judge",
        value_kind="scalar",
        direction=MetricDirection.HIGHER,
        aggregation="mean",
        uses_models=True,
    )
    denials = evaluator_denials(ExecutionPolicy(allowed_evaluators=("vendor.*",)), [manifest])
    expected = (
        "evaluator vendor.judge@1.0.0 sends case data to a model judge; "
        "the policy does not allow model-backed evaluators"
    )
    assert denials == [expected]
    assert (
        evaluator_denials(
            ExecutionPolicy(allowed_evaluators=("vendor.*",), allow_model_evaluators=True),
            [manifest],
        )
        == []
    )


def test_policy_file_loading_and_default_is_conservative(tmp_path: Path) -> None:
    from aibench.engine.compile import load_policy

    default = load_policy(None)
    assert (default.allow_trusted_local, default.max_effects.value, default.allowed_evaluators) == (
        False,
        "none",
        ("native.*",),
    )
    path = tmp_path / "policy.json"
    path.write_text('{"allow_trusted_local": true, "unknown_key": 1}', encoding="utf-8")
    with pytest.raises(PlanInvalid, match="invalid policy"):
        load_policy(path)
