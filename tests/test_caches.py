"""Explicit caches (16-T3, 16-G3): the invalidation matrix, provenance, and what a cache
hit may not claim. Runs go through the real engine against a real CLI application whose
invocation log counts every call."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.models import deep_unfreeze
from aibench.engine.compile import PlanInvalid
from aibench.security.policy import ExecutionPolicy
from tests.engine_support import Harness

cli = CliRunner()
CASES = {"a": "hi", "b": "hello"}
BOTH = {"executions": True, "evaluations": True}


def _run(h: Harness, *, cases: dict[str, str] = CASES, policy: ExecutionPolicy | None = None,
         **fields: Any) -> str:  # fmt: skip
    fields.setdefault("cache", BOTH)
    plan = h.plan(dataset=h.dataset(cases), application=h.cli_app(), **fields)
    run_id = h.create(plan, policy=policy)
    h.execute(run_id)
    return run_id


def _records(h: Harness, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    storage, _ = h.storage()
    try:
        executions = {e.case_id: e for e in storage.list_execution_attempts(run_id)}
        results = {r.case_id: r for r in storage.list_metric_results(run_id)}
    finally:
        storage.db.close()
    return executions, results


def _hits(h: Harness, run_id: str) -> tuple[set[str], set[str]]:
    executions, results = _records(h, run_id)
    return (
        {c for c, e in executions.items() if e.cache},
        {c for c, r in results.items() if (deep_unfreeze(r.provenance) or {}).get("cache")},
    )


def test_a_repeat_run_is_served_from_the_cache_and_labelled(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    first = _run(h)
    assert h.count() == 2 and _hits(h, first) == (set(), set())

    second = _run(h)
    assert h.count() == 2  # no application call
    assert _hits(h, second) == ({"a", "b"}, {"a", "b"})
    executions, results = _records(h, second)
    cached = executions["a"]
    assert (
        cached.cache["source_run_id"] == first and "not a fresh measurement" in cached.cache["note"]
    )
    assert cached.effect_state.value == "not_dispatched" and cached.timing["cached"] is True
    assert results["a"].resources["model_calls"] == 0
    assert results["a"].decision.value == "pass"

    storage, artifacts = h.storage()
    try:
        from aibench.services.reports import build_report

        report = build_report(storage, artifacts, second)
    finally:
        storage.db.close()
    assert report["cache"]["execution_hits"] == 2 and report["cache"]["evaluation_hits"] == 2
    latency = report["application"]["latency"]
    assert latency["successful_requests"] == 0  # cached outputs are not fresh latencies
    assert "2 cached execution(s) excluded" in latency["cache"]
    assert report["application"]["attempts"] == 2  # recorded, but no call was made


@pytest.mark.parametrize(
    ("change", "execution_hits", "evaluation_hits"),
    [
        # a changed app-visible input: that case is executed and evaluated afresh
        ("input", {"a"}, {"a"}),
        # a changed application config (revision): every execution is fresh; the same
        # outputs then hit the evaluation cache, which keys on content
        ("app_revision", set(), {"a", "b"}),
        # a changed policy: every execution and evaluation key changes
        ("policy", set(), set()),
        # a changed reference answer (case a): executions hit, a's evaluation is fresh
        ("reference", {"a", "b"}, {"b"}),
        # a changed rubric/parameter: every evaluation is fresh
        ("params", {"a", "b"}, set()),
    ],
)
def test_any_changed_identity_invalidates_the_relevant_entries(
    tmp_path: Path, change: str, execution_hits: set[str], evaluation_hits: set[str]
) -> None:
    h = Harness(tmp_path)
    _run(h)
    before = h.count()
    kwargs: dict[str, Any] = {}
    cases = dict(CASES)
    if change == "input":
        cases["b"] = "hello there"
    if change == "policy":
        kwargs["policy"] = ExecutionPolicy(allowed_applications=("instrumented",))
    if change == "params":
        kwargs["metrics"] = [{"metric": "native.exact_match", "params": {"case_sensitive": False}}]
    plan = h.plan(dataset=h.dataset(cases), application=h.cli_app(), cache=BOTH,
                  **({"metrics": kwargs["metrics"]} if "metrics" in kwargs else {}))  # fmt: skip
    if change == "app_revision":
        config = json.loads((h.root / "app.json").read_text(encoding="utf-8"))
        config["revision"] = "v2"
        (h.root / "app.json").write_text(json.dumps(config), encoding="utf-8")
    if change == "reference":
        rows = [json.loads(line) for line in (h.root / "data.jsonl").read_text().splitlines()]
        rows[0]["expected_output"] = "no"
        (h.root / "data.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    run_id = h.create(plan, policy=kwargs.get("policy"))
    h.execute(run_id)
    exec_hits, eval_hits = _hits(h, run_id)
    assert (exec_hits, eval_hits) == (execution_hits, evaluation_hits)
    assert h.count() - before == len(CASES) - len(execution_hits)  # a miss calls the app


def test_a_changed_plugin_version_invalidates_evaluations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.evaluators.native import ExactMatch

    h = Harness(tmp_path)
    _run(h)
    upgraded = ExactMatch.manifest.model_copy(update={"plugin_version": "9.9.9"})
    monkeypatch.setattr(ExactMatch, "manifest", upgraded)
    assert _hits(h, _run(h)) == ({"a", "b"}, set())


def test_caches_are_off_unless_asked_and_clear_invalidates(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    _run(h, cache={})
    _run(h, cache={})
    assert h.count() == 4  # no cache without opting in

    _run(h)  # stores entries
    root = str(h.workspace.root.parent)
    listed = cli.invoke(app, ["cache", "list", "--workspace", root, "--json"])
    assert listed.exit_code == 0 and len(json.loads(listed.stdout)["entries"]) == 4
    cleared = cli.invoke(app, ["cache", "clear", "--kind", "execution", "--workspace", root])
    assert cleared.exit_code == 0 and "invalidated 2" in cleared.stdout
    before = h.count()
    assert _hits(h, _run(h)) == (set(), {"a", "b"})
    assert h.count() - before == 2


@pytest.mark.parametrize(
    ("app_changes", "message"),
    [
        ({"effects": "reversible"}, "no test world snapshots its state"),
        ({"reset_policy": "shared"}, "shared state"),
    ],
)
def test_execution_caching_is_refused_where_state_matters(
    tmp_path: Path, app_changes: dict[str, str], message: str
) -> None:
    h = Harness(tmp_path)
    plan = h.plan(dataset=h.dataset(CASES), application=h.cli_app(), cache=BOTH)
    config = json.loads((h.root / "app.json").read_text(encoding="utf-8"))
    config.update(app_changes)
    (h.root / "app.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(PlanInvalid, match=message):
        h.create(plan, policy=ExecutionPolicy(max_effects="reversible"))
    assert h.count() == 0


def test_changed_application_code_or_inherited_environment_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The config alone is not the application: editing its code, or changing a variable
    it inherits, must not return the old outputs (review finding)."""
    from aibench.core.models import DEFAULT_INHERITED_ENV

    h = Harness(tmp_path)
    h.cli_app()
    config = json.loads((h.root / "app.json").read_text(encoding="utf-8"))
    config["transport"]["inherit_env"] = [*DEFAULT_INHERITED_ENV, "MODEL_NAME"]
    (h.root / "app.json").write_text(json.dumps(config), encoding="utf-8")

    def run() -> str:
        plan = h.plan(dataset=h.dataset(CASES), application="app.json", cache=BOTH)
        run_id = h.create(plan)
        h.execute(run_id)
        return run_id

    monkeypatch.setenv("MODEL_NAME", "small")
    run()
    assert _hits(h, run())[0] == {"a", "b"}  # unchanged: served from the cache
    with (h.root / "app.py").open("a", encoding="utf-8") as app_file:
        app_file.write("\n# a code change\n")
    assert _hits(h, run())[0] == set()
    monkeypatch.setenv("MODEL_NAME", "large")
    assert _hits(h, run())[0] == set()
    assert h.count() == 2 * 3  # three fresh runs of two cases


def test_rotating_an_explicit_application_secret_invalidates_execution_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.cli_app()
    config = json.loads((h.root / "app.json").read_text(encoding="utf-8"))
    config["transport"]["secret_env"] = {"APP_TENANT": "env:CACHE_TENANT"}
    (h.root / "app.json").write_text(json.dumps(config), encoding="utf-8")
    app_path = h.root / "app.py"
    source = app_path.read_text(encoding="utf-8")
    assert 'print(json.dumps({"output": "yes"}))' in source
    app_path.write_text(
        source.replace(
            'print(json.dumps({"output": "yes"}))',
            'print(json.dumps({"output": os.environ["APP_TENANT"]}))',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CACHE_TENANT", "tenant-one")

    def run() -> str:
        plan = h.plan(dataset=h.dataset(CASES), application="app.json", cache=BOTH)
        run_id = h.create(
            plan, policy=ExecutionPolicy(allowed_secret_refs=("env:CACHE_TENANT",))
        )
        h.execute(run_id)
        return run_id

    run()
    assert h.count() == 2
    monkeypatch.setenv("CACHE_TENANT", "tenant-two")
    second = run()

    assert h.count() == 4
    assert _hits(h, second)[0] == set()


def test_an_application_whose_code_cannot_be_read_needs_a_declared_revision(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    plan = h.plan(dataset=h.dataset(CASES), application="app.json", cache=BOTH)
    config = {
        "application_id": "remote",
        "runner": "http",
        "target": "http://127.0.0.1:9/answer",
        "transport": {"kind": "http", "url": "http://127.0.0.1:9/answer"},
        "input_binding": {"fields": {"/input": "/input"}},
    }
    (h.root / "app.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(PlanInvalid, match="declare `revision` or `environment_digest`"):
        h.create(plan)
    (h.root / "app.json").write_text(json.dumps({**config, "revision": "v1"}), encoding="utf-8")
    h.create(plan)  # a declared revision is the identity the key uses


def test_code_in_a_configured_python_import_path_participates_in_identity(
    tmp_path: Path,
) -> None:
    from aibench.core.models import ApplicationSpec, PythonTransport
    from aibench.engine.cache import application_code_identity

    base = tmp_path / "project"
    app_dir = base / "app"
    shared = base / "shared"
    app_dir.mkdir(parents=True)
    shared.mkdir()
    (app_dir / "app.py").write_text("def answer(value): return value", encoding="utf-8")
    helper = shared / "helper.py"
    helper.write_text("VALUE = 'first'", encoding="utf-8")
    spec = ApplicationSpec(
        application_id="python-app",
        runner="python",
        target="app/app.py",
        transport=PythonTransport(
            callable="app/app.py:answer", cwd="app", paths=("shared",)
        ),
    )

    before = application_code_identity(spec, base, {})
    helper.write_text("VALUE = 'second'", encoding="utf-8")
    after = application_code_identity(spec, base, {})

    assert before["code"] != after["code"]


def test_container_cache_requires_revision_when_host_mounts_are_mutable(
    tmp_path: Path,
) -> None:
    from aibench.core.models import ApplicationSpec, ContainerMount, ContainerTransport
    from aibench.engine.cache import code_identity_problem

    spec = ApplicationSpec(
        application_id="mounted-container",
        runner="container",
        target="python",
        transport=ContainerTransport(
            image="python:3.12@sha256:" + "a" * 64,
            argv=("python", "/app/main.py"),
            mounts=(ContainerMount(source="app", target="/app"),),
        ),
    )

    problem = code_identity_problem(spec, tmp_path)
    assert problem is not None and "host bind mounts" in problem
    assert code_identity_problem(spec.model_copy(update={"revision": "v2"}), tmp_path) is None


def test_a_judges_repeats_are_not_served_from_each_other(tmp_path: Path) -> None:
    """With repetitions, each repeat is evaluated afresh: the evaluation key includes the
    repetition, so repeats stay independent (review finding)."""
    h = Harness(tmp_path)
    first = _run(h, repetitions=2)
    storage, _ = h.storage()
    try:
        results = storage.list_metric_results(first)
    finally:
        storage.db.close()
    assert len(results) == 4
    assert not any((deep_unfreeze(r.provenance) or {}).get("cache") for r in results)


def test_a_comparison_against_a_cached_run_is_blocked(tmp_path: Path) -> None:
    """A run served from the cache measured nothing: comparing it with its source must not
    report "no change" (review finding)."""
    h = Harness(tmp_path)
    first, second = _run(h), _run(h)
    root = str(h.workspace.root.parent)
    result = cli.invoke(app, ["compare", first, second, "--workspace", root, "--json"])
    report = json.loads(result.stdout)
    assert report["status"] == "blocked", report["status"]
    checks = {c["name"]: c for c in report["global_checks"]}
    assert checks["fresh_executions"]["reason_code"] == "cached_executions_present"
    assert checks["fresh_executions"]["current"]["cached_executions"] == 2
