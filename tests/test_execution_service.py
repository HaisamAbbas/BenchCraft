"""Persisting invocations through Prompt 02 storage (03-T4; gates 03-G1, 03-G4)."""

from __future__ import annotations

import json
from pathlib import Path

from aibench.core.models import (
    DatasetManifest,
    EffectState,
    ErrorKind,
    ExecutionStatus,
    RedactionClass,
)
from aibench.datasets.ingest import ingest_dataset
from aibench.runners import CliRunner, create_runner, load_application
from aibench.services.execution import invoke_and_record, run_developer_smoke
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from tests.runner_support import EXAMPLE_APPS, REPO_ROOT, SENTINEL, golden_case, misbehaving, run


def _workspace(tmp_path: Path) -> tuple[Workspace, Storage, ArtifactStore]:
    ws = Workspace.at(tmp_path)
    ws.ensure_directories()
    return ws, Storage(Database.open_workspace(ws)), ArtifactStore(ws.artifacts_dir)


def _dataset(name: str) -> tuple[DatasetManifest, list]:
    report = ingest_dataset(REPO_ROOT / "examples" / "datasets" / name)
    assert report.manifest is not None
    return report.manifest, report.cases


def test_smoke_run_persists_results_and_verified_artifacts_across_restart(tmp_path: Path) -> None:
    ws, storage, artifacts = _workspace(tmp_path)
    app = load_application(EXAMPLE_APPS / "cli_chatbot.app.json")
    manifest, cases = _dataset("chatbot.valid.jsonl")
    runner = create_runner(app, trusted_local=True)

    async def scenario():
        async with runner:
            return await run_developer_smoke(
                runner, app.spec, manifest, cases, storage=storage, artifacts=artifacts
            )

    report = run(scenario())
    storage.db.close()

    # Reopen: everything must come back from disk, not from memory.
    storage = Storage(Database.open_workspace(ws))
    record = storage.get_run(report.run_id)
    assert record is not None and record.status == "completed"
    assert record.manifest.parameters["mode"] == "developer_smoke"
    assert record.manifest.parameters["evaluation"] == "none"
    stored = storage.list_execution_attempts(report.run_id)
    assert {r.case_id for r in stored} == {"chat-001", "chat-002"}
    assert storage.list_cases(manifest.content_hash)  # Goldens persisted for later scoring
    for result in stored:
        assert result.status is ExecutionStatus.OK
        assert result.timing["wall_ms"] > 0
        assert len(result.trace_refs) == 3
        for artifact_id in result.trace_refs:
            ref = storage.get_artifact(artifact_id)
            assert ref is not None and ref.redaction is RedactionClass.RESTRICTED
            artifacts.read_bytes(ref)  # verified: path, size and digest all match
    storage.db.close()


def test_repeated_smoke_runs_on_the_same_dataset_do_not_conflict(tmp_path: Path) -> None:
    _, storage, artifacts = _workspace(tmp_path)
    app = load_application(EXAMPLE_APPS / "blackbox_cli.app.json")
    runner = create_runner(app, trusted_local=True)

    async def one_run():
        manifest, cases = _dataset("chatbot.valid.jsonl")  # fresh created_at each time
        async with runner.__class__(app.spec, base_dir=app.base_dir, trusted_local=True) as r:
            return await run_developer_smoke(
                r, app.spec, manifest, cases, storage=storage, artifacts=artifacts
            )

    first, second = run(one_run()), run(one_run())
    assert first.run_id != second.run_id
    assert len(storage.list_runs()) == 2
    storage.db.close()


def test_failed_attempts_are_recorded_honestly_with_unknown_observations(tmp_path: Path) -> None:
    _, storage, artifacts = _workspace(tmp_path)
    spec = misbehaving("sleep", "30", timeout_seconds=1, effects="irreversible")
    manifest, _ = _dataset("chatbot.valid.jsonl")
    case = golden_case()

    async def scenario():
        async with CliRunner(spec, base_dir=EXAMPLE_APPS, trusted_local=True) as runner:
            report = await run_developer_smoke(
                runner, spec, manifest, [case], storage=storage, artifacts=artifacts
            )
            return report.results[0]

    result = run(scenario())
    stored = storage.get_execution_attempt(result.execution_id)
    assert stored == result
    assert stored.status is ExecutionStatus.ERROR
    assert stored.error_kind is ErrorKind.TIMEOUT
    assert stored.effect_state is EffectState.UNKNOWN
    assert stored.output is None
    assert stored.retrieved_context is None and stored.usage is None and stored.cost is None
    completeness = json.loads(stored.model_dump_json())["observation_completeness"]
    assert completeness["usage"]["state"] == "unknown"
    assert completeness["output"]["state"] == "unknown"
    storage.db.close()


def test_persisted_request_artifact_contains_no_judge_only_data(tmp_path: Path) -> None:
    _, storage, artifacts = _workspace(tmp_path)
    app = load_application(EXAMPLE_APPS / "cli_chatbot.app.json")
    manifest, _ = _dataset("chatbot.valid.jsonl")
    storage.commit_dataset(manifest)
    storage.commit_application(app.spec)
    from aibench.core.models import RunManifest

    storage.commit_run(
        RunManifest(
            run_id="r1", dataset_hash=manifest.content_hash, application_hash="a", plan_hash="p"
        )
    )

    async def scenario():
        async with create_runner(app, trusted_local=True) as runner:
            return await invoke_and_record(
                runner, golden_case(), storage=storage, artifacts=artifacts, run_id="r1"
            )

    result = run(scenario())
    for artifact_id in result.trace_refs:
        ref = storage.get_artifact(artifact_id)
        assert ref is not None
        assert SENTINEL.encode() not in artifacts.read_bytes(ref)
    storage.db.close()
