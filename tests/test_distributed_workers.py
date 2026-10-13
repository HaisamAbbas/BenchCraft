from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from aibench import __version__
from aibench.cli import run as run_cli
from aibench.cli.main import app
from aibench.core.models import (
    ApplicationSpec,
    EffectLevel,
    EffectState,
    ErrorKind,
    ExecutionStatus,
    HttpTransport,
    RunnerKind,
)
from aibench.engine.compile import compile_plan
from aibench.runners.base import (
    BaseRunner,
    Capture,
    HealthReport,
    InvocationContext,
    InvocationOutcome,
    ResetReport,
)
from aibench.runners.bindings import AppInputEnvelope, ExtractedObservations
from aibench.security.policy import ExecutionPolicy
from aibench.services.distributed import CoordinatorRunner, _Task
from aibench.services.runs import create_run, execute_run
from aibench.services.worker_client import DistributedWorker
from aibench.services.worker_protocol import (
    MAX_CAPTURE_BYTES,
    WORKER_PROTOCOL,
    outcome_to_json,
)
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from tests.engine_support import Harness

_TOKEN = "a-strong-test-token-that-is-at-least-32-chars"
_CLI = CliRunner()


class _EchoRunner(BaseRunner):
    kind = "test"

    def _transport_observables(self) -> tuple[str, ...]:
        return ("wall_time",)

    def _isolation(self) -> str:
        return "test worker"

    def _limitations(self) -> tuple[str, ...]:
        return ()

    async def _prepare(self) -> None:
        return None

    async def _healthcheck(self) -> HealthReport:
        return HealthReport("healthy", "test")

    async def _invoke(
        self, envelope: AppInputEnvelope, ctx: InvocationContext
    ) -> InvocationOutcome:
        value = envelope.data.get("input", "")
        text = str(value.get("question", "")) if isinstance(value, dict) else str(value)
        return InvocationOutcome(
            status=ExecutionStatus.OK,
            effect_state=self.effect(EffectState.COMPLETED),
            correlation_id=ctx.correlation_id,
            timing={"wall_ms": 1.25},
            completeness={"output": {"state": "observed", "status": "present"}},
            output={"answer": text.upper()} if isinstance(value, dict) else text.upper(),
            observations=ExtractedObservations(usage={"tokens": 3}),
            captures=(Capture("request", text.encode(), "text/plain"),),
        )

    async def _reset(self, seed: Any) -> ResetReport:
        return ResetReport("not_needed", "test")

    async def _close(self) -> None:
        return None


def _spec() -> ApplicationSpec:
    return ApplicationSpec(
        application_id="distributed-test-app",
        runner=RunnerKind.HTTP,
        target="https://app.example.test/chat",
        revision="app-revision-1",
        environment_digest="app-environment-1",
        effects=EffectLevel.NONE,
        transport=HttpTransport(url="https://app.example.test/chat"),
    )


def _options(**overrides: Any) -> dict[str, Any]:
    return {
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "advertise_url": None,
        "tls_cert": None,
        "tls_key": None,
        "workers": 1,
        "startup_timeout_seconds": 4,
        "token_env": "AIBENCH_WORKER_TOKEN",
        "trust_local_app": False,
        **overrides,
    }


def test_coordinator_worker_round_trip_uses_the_authenticated_api() -> None:
    async def scenario() -> None:
        spec = _spec().model_copy(update={"effects": EffectLevel.REVERSIBLE})
        ready: dict[str, Any] = {}
        worker_events: list[tuple[str, dict[str, Any]]] = []
        coordinator = CoordinatorRunner(
            spec,
            run_id="run-test",
            options=_options(),
            token=_TOKEN,
            on_ready=ready.update,
            event_sink=lambda name, payload: worker_events.append((name, payload)),
        )
        worker_runner = _EchoRunner(spec)
        coordinator_start = asyncio.create_task(coordinator.prepare())
        try:
            for _ in range(100):
                if ready:
                    break
                await asyncio.sleep(0.01)
            assert ready["url"].startswith("http://127.0.0.1:")
            async with httpx.AsyncClient(trust_env=False) as unauthenticated:
                rejected = await unauthenticated.post(
                    ready["url"] + "/v1/register",
                    headers={"Authorization": "Bearer wrong"},
                    json={"protocol": WORKER_PROTOCOL},
                )
            assert rejected.status_code == 401
            await worker_runner.prepare()
            worker = DistributedWorker(
                worker_runner,
                coordinator_url=ready["url"],
                token=_TOKEN,
                application_hash=coordinator.application_hash,
                report_runtime=True,
            )
            worker_loop = asyncio.create_task(worker.run())
            await coordinator_start
            context = InvocationContext(
                run_id="run-test", case_id="case-1", attempt_id=1, correlation_id="corr-123"
            )
            outcome = await asyncio.wait_for(
                coordinator.invoke(AppInputEnvelope({"input": {"question": "hello"}}), context),
                timeout=5,
            )
            assert outcome.status is ExecutionStatus.OK
            assert outcome.output == {"answer": "HELLO"}
            assert outcome.correlation_id == "corr-123"
            assert outcome.observations.usage == {"tokens": 3}
            assert outcome.captures[0].data == b"hello"
            registrations = [
                payload for name, payload in worker_events if name == "distributed_worker_connected"
            ]
            assert registrations
            registration = registrations[-1]
            runtime = registration["runtime"]
            assert isinstance(runtime["os"], str)
            assert isinstance(runtime["architecture"], str)
            assert isinstance(runtime["python"], str)
            assert runtime["logical_cpus"] is None or type(runtime["logical_cpus"]) is int
            worker_loop.cancel()
            await asyncio.gather(worker_loop, return_exceptions=True)
        finally:
            if not coordinator_start.done():
                coordinator_start.cancel()
                await asyncio.gather(coordinator_start, return_exceptions=True)
            await coordinator.close()
            await worker_runner.close()

    asyncio.run(scenario())


def test_registration_sanitizes_worker_reported_runtime_details() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    coordinator = CoordinatorRunner(
        _spec(),
        run_id="run-test",
        options=_options(),
        token=_TOKEN,
        event_sink=lambda name, payload: events.append((name, payload)),
    )
    status, _ = coordinator._register(
        {
            "protocol": WORKER_PROTOCOL,
            "client_id": "worker-one",
            "application_hash": coordinator.application_hash,
            "engine_version": __version__,
            "capacity": 1,
            "runtime": {
                "os": "L" * 100,
                "architecture": "\ninvalid",
                "python": "3.12.0",
                "logical_cpus": True,
                "hostname": "private-host-name",
            },
        }
    )
    assert status == 200
    runtime = events[0][1]["runtime"]
    assert runtime == {
        "os": "L" * 64,
        "architecture": None,
        "python": "3.12.0",
        "logical_cpus": None,
    }


def test_worker_runtime_reporting_is_opt_in() -> None:
    worker = DistributedWorker(
        _EchoRunner(_spec()),
        coordinator_url="http://127.0.0.1:8765",
        token=_TOKEN,
        application_hash="sha256:test",
    )
    assert "runtime" not in worker._registration_document()


def test_registration_without_runtime_preserves_connection_event_shape() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    coordinator = CoordinatorRunner(
        _spec(),
        run_id="run-test",
        options=_options(),
        token=_TOKEN,
        event_sink=lambda name, payload: events.append((name, payload)),
    )
    status, _ = coordinator._register(
        {
            "protocol": WORKER_PROTOCOL,
            "client_id": "worker-one",
            "application_hash": coordinator.application_hash,
            "engine_version": __version__,
            "capacity": 1,
        }
    )
    assert status == 200
    assert len(events) == 1
    name, payload = events[0]
    assert name == "distributed_worker_connected"
    assert set(payload) == {"worker_id", "capacity"}
    assert payload["capacity"] == 1


def test_registration_refuses_a_different_frozen_application_spec() -> None:
    async def scenario() -> None:
        runner = CoordinatorRunner(_spec(), run_id="run-test", options=_options(), token=_TOKEN)
        status, response = runner._register(
            {
                "protocol": WORKER_PROTOCOL,
                "client_id": "worker-one",
                "application_hash": "sha256:wrong",
                "engine_version": "0.0.0",
                "capacity": 1,
            }
        )
        assert status == 409
        assert "does not match" in response["error"]

    asyncio.run(scenario())


def test_worker_cannot_clear_a_declared_application_effect() -> None:
    async def scenario() -> None:
        spec = _spec().model_copy(update={"effects": EffectLevel.REVERSIBLE})
        runner = CoordinatorRunner(spec, run_id="run-test", options=_options(), token=_TOKEN)
        status, registration = runner._register(
            {
                "protocol": WORKER_PROTOCOL,
                "client_id": "worker-one",
                "application_hash": runner.application_hash,
                "engine_version": __version__,
                "capacity": 1,
            }
        )
        assert status == 200
        worker_id = registration["worker_id"]
        context = InvocationContext(run_id="run-test", case_id="case-1", attempt_id=1)
        future: asyncio.Future[InvocationOutcome] = asyncio.get_running_loop().create_future()
        task = _Task(
            "task-one",
            {},
            context,
            future,
            worker_id=worker_id,
            lease_deadline=time.monotonic() + 45,
        )
        runner._tasks[task.task_id] = task
        runner._leases[task.task_id] = worker_id
        outcome = InvocationOutcome(
            status=ExecutionStatus.ERROR,
            effect_state=EffectState.NONE_DECLARED,
            correlation_id=context.correlation_id,
            timing={"wall_ms": 1},
            completeness={},
            error_kind=ErrorKind.TRANSPORT,
            error="fake worker transport failure",
        )
        status, response = runner._complete(
            {
                "worker_id": worker_id,
                "task_id": task.task_id,
                "outcome": outcome_to_json(outcome),
            }
        )
        assert status == 200 and response["accepted"] is True
        assert future.result().effect_state is EffectState.UNKNOWN

    asyncio.run(scenario())


def test_expired_remote_dispatch_is_recorded_as_unknown_and_never_requeued() -> None:
    async def scenario() -> None:
        spec = _spec().model_copy(update={"effects": EffectLevel.REVERSIBLE})
        runner = CoordinatorRunner(spec, run_id="run-test", options=_options(), token=_TOKEN)
        context = InvocationContext(run_id="run-test", case_id="case-1", attempt_id=1)
        loop = asyncio.get_running_loop()
        task = _Task(
            "task-one",
            {"protocol": WORKER_PROTOCOL},
            context,
            loop.create_future(),
            worker_id="worker-one",
            lease_deadline=time.monotonic() - 1,
        )
        runner._tasks[task.task_id] = task
        runner._leases[task.task_id] = "worker-one"
        await runner._expire_tasks()
        outcome = task.future.result()
        assert outcome.status is ExecutionStatus.ERROR
        assert outcome.effect_state is EffectState.UNKNOWN
        assert outcome.error_kind is ErrorKind.TIMEOUT
        assert not runner._queue
        assert not runner._tasks

    asyncio.run(scenario())


def test_expired_lease_rejects_late_heartbeat_and_result() -> None:
    async def scenario() -> None:
        spec = _spec().model_copy(update={"effects": EffectLevel.REVERSIBLE})
        runner = CoordinatorRunner(spec, run_id="run-test", options=_options(), token=_TOKEN)
        status, registration = runner._register(
            {
                "protocol": WORKER_PROTOCOL,
                "client_id": "worker-one",
                "application_hash": runner.application_hash,
                "engine_version": __version__,
                "capacity": 1,
            }
        )
        assert status == 200
        worker_id = registration["worker_id"]
        loop = asyncio.get_running_loop()
        for task_id, operation in (
            ("late-heartbeat", "heartbeat"),
            ("late-result", "complete"),
        ):
            context = InvocationContext(
                run_id="run-test", case_id="case-1", attempt_id=1, correlation_id=task_id
            )
            task = _Task(
                task_id,
                {},
                context,
                loop.create_future(),
                worker_id=worker_id,
                lease_deadline=time.monotonic() - 1,
            )
            runner._tasks[task_id] = task
            runner._leases[task_id] = worker_id
            if operation == "heartbeat":
                response = runner._heartbeat({"worker_id": worker_id, "task_id": task_id})
            else:
                response = runner._complete(
                    {"worker_id": worker_id, "task_id": task_id, "outcome": {}}
                )
            assert response[0] == 409
            assert task.future.result().effect_state is EffectState.UNKNOWN
            assert task_id not in runner._tasks
            assert task_id not in runner._leases

    asyncio.run(scenario())


def test_pending_invocation_fails_cleanly_when_all_workers_disappear() -> None:
    async def scenario() -> None:
        ready: dict[str, Any] = {}
        runner = CoordinatorRunner(
            _spec().model_copy(update={"effects": EffectLevel.REVERSIBLE}),
            run_id="run-test",
            options=_options(startup_timeout_seconds=1),
            token=_TOKEN,
            on_ready=ready.update,
        )
        start = asyncio.create_task(runner.prepare())
        try:
            for _ in range(100):
                if ready:
                    break
                await asyncio.sleep(0.01)
            status, _registration = runner._register(
                {
                    "protocol": WORKER_PROTOCOL,
                    "client_id": "worker-one",
                    "application_hash": runner.application_hash,
                    "engine_version": __version__,
                    "capacity": 1,
                }
            )
            assert status == 200
            await start
            runner._workers.clear()
            runner._clients.clear()
            context = InvocationContext(
                run_id="run-test", case_id="case-1", attempt_id=1, correlation_id="offline"
            )
            outcome = await asyncio.wait_for(
                runner.invoke(AppInputEnvelope({"input": "hello"}), context), timeout=2
            )
            assert outcome.status is ExecutionStatus.ERROR
            assert outcome.effect_state is EffectState.NOT_DISPATCHED
            assert outcome.error_kind is ErrorKind.TRANSPORT
            assert not runner._tasks
            assert not runner._leases
        finally:
            if not start.done():
                start.cancel()
                await asyncio.gather(start, return_exceptions=True)
            await runner.close()

    asyncio.run(scenario())


def test_worker_re_registers_after_coordinator_forgets_its_worker_id() -> None:
    async def scenario() -> None:
        ready: dict[str, Any] = {}
        spec = _spec()
        coordinator = CoordinatorRunner(
            spec,
            run_id="run-test",
            options=_options(),
            token=_TOKEN,
            on_ready=ready.update,
        )
        worker_runner = _EchoRunner(spec)
        coordinator_start = asyncio.create_task(coordinator.prepare())
        worker_loop: asyncio.Task[None] | None = None
        try:
            for _ in range(100):
                if ready:
                    break
                await asyncio.sleep(0.01)
            await worker_runner.prepare()
            worker = DistributedWorker(
                worker_runner,
                coordinator_url=ready["url"],
                token=_TOKEN,
                application_hash=coordinator.application_hash,
            )
            worker_loop = asyncio.create_task(worker.run())
            await coordinator_start
            for _ in range(100):
                if worker.worker_id is not None:
                    break
                if worker_loop.done():
                    await worker_loop
                await asyncio.sleep(0.01)
            original_id = worker.worker_id
            assert original_id is not None

            # Wake the outstanding long poll after simulating an expired coordinator record.
            coordinator._workers.clear()
            coordinator._clients.clear()
            coordinator._queue_changed.set()
            for _ in range(100):
                if worker.worker_id != original_id:
                    break
                await asyncio.sleep(0.01)
            assert worker.worker_id is not None
            assert worker.worker_id != original_id
            assert len(coordinator._workers) == 1
        finally:
            if worker_loop is not None:
                worker_loop.cancel()
                await asyncio.gather(worker_loop, return_exceptions=True)
            if not coordinator_start.done():
                coordinator_start.cancel()
                await asyncio.gather(coordinator_start, return_exceptions=True)
            await coordinator.close()
            await worker_runner.close()

    asyncio.run(scenario())


def test_coordinator_close_cancels_active_worker_long_poll() -> None:
    async def scenario() -> None:
        ready: dict[str, Any] = {}
        coordinator = CoordinatorRunner(
            _spec(),
            run_id="run-close-test",
            options=_options(),
            token=_TOKEN,
            on_ready=ready.update,
        )
        coordinator_start = asyncio.create_task(coordinator.prepare())
        client = httpx.AsyncClient(trust_env=False, timeout=None)
        poll: asyncio.Task[httpx.Response] | None = None
        headers = {"Authorization": f"Bearer {_TOKEN}"}
        try:
            for _ in range(100):
                if ready:
                    break
                await asyncio.sleep(0.01)
            assert ready["url"]
            registration = await client.post(
                ready["url"] + "/v1/register",
                headers=headers,
                json={
                    "protocol": WORKER_PROTOCOL,
                    "client_id": "closing-worker",
                    "application_hash": coordinator.application_hash,
                    "engine_version": __version__,
                    "capacity": 1,
                },
            )
            assert registration.status_code == 200, registration.text
            worker_id = registration.json()["worker_id"]
            await asyncio.wait_for(coordinator_start, timeout=1)
            poll = asyncio.create_task(
                client.post(
                    ready["url"] + "/v1/claim",
                    headers=headers,
                    json={"worker_id": worker_id},
                )
            )
            for _ in range(100):
                if coordinator._connections:
                    break
                await asyncio.sleep(0.01)
            assert coordinator._connections
            await asyncio.sleep(0.02)

            await asyncio.wait_for(coordinator.close(), timeout=1)

            assert coordinator._server is None
            assert not coordinator._connections
            with pytest.raises(httpx.HTTPError):
                await asyncio.wait_for(poll, timeout=1)
        finally:
            if poll is not None and not poll.done():
                poll.cancel()
                await asyncio.gather(poll, return_exceptions=True)
            if not coordinator_start.done():
                coordinator_start.cancel()
                await asyncio.gather(coordinator_start, return_exceptions=True)
            await coordinator.close()
            await client.aclose()

    asyncio.run(scenario())


def test_worker_command_uses_a_path_placeholder_for_the_remote_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output: list[str] = []
    monkeypatch.setattr(run_cli.err_console, "print", output.append)
    run_cli._worker_ready(
        {
            "url": "https://bench.example.test:8765",
            "application_config_path": r"C:\coordinator\app.yaml",
            "token_env": "AIBENCH_WORKER_TOKEN",
            "trust_local_app": False,
        }
    )
    rendered = "\n".join(output)
    assert "APP_CONFIG_PATH_ON_WORKER" in rendered
    assert r"C:\coordinator\app.yaml" not in rendered
    assert "AIBENCH_WORKER_TOKEN" in rendered


def test_oversized_worker_result_becomes_bounded_error_instead_of_crashing_worker() -> None:
    spec = _spec().model_copy(update={"effects": EffectLevel.REVERSIBLE})
    runner = _EchoRunner(spec)
    worker = DistributedWorker(
        runner,
        coordinator_url="http://127.0.0.1:8765",
        token=_TOKEN,
        application_hash="sha256:test",
    )
    context = InvocationContext(
        run_id="run-test", case_id="case-1", attempt_id=1, correlation_id="large"
    )
    oversized = InvocationOutcome(
        status=ExecutionStatus.OK,
        effect_state=EffectState.COMPLETED,
        correlation_id=context.correlation_id,
        timing={"wall_ms": 12.0},
        completeness={},
        captures=(Capture("response", b"x" * (MAX_CAPTURE_BYTES + 1), "text/plain"),),
    )
    document = worker._completion_document("task-large", context, oversized)
    assert document["outcome"]["status"] == ExecutionStatus.ERROR.value
    assert document["outcome"]["effect_state"] == EffectState.COMPLETED.value
    assert document["outcome"]["error_kind"] == ErrorKind.REQUEST_LIMIT.value
    assert document["outcome"]["captures"] == []


def test_full_run_keeps_workspace_writes_on_the_coordinator(tmp_path) -> None:
    async def scenario() -> None:
        project = tmp_path / "project"
        project.mkdir()
        (tmp_path / "app.json").write_text(
            '{"application_id":"distributed-test-app","runner":"http",'
            '"target":"https://app.example.test/chat","revision":"app-revision-1",'
            '"environment_digest":"app-environment-1","reset_policy":"shared",'
            '"transport":{"kind":"http","url":"https://app.example.test/chat"}}',
            encoding="utf-8",
        )
        (tmp_path / "data.jsonl").write_text(
            '{"case_id":"case-1","input":"hello","expected_output":"HELLO"}\n',
            encoding="utf-8",
        )
        plan = tmp_path / "plan.json"
        plan.write_text(
            '{"plan_id":"distributed-run-test","dataset":"data.jsonl",'
            '"application":"app.json","metrics":[{"metric":"native.exact_match@1"}],'
            '"retry":{"max_attempts":1}}',
            encoding="utf-8",
        )
        token_env = {"AIBENCH_WORKER_TOKEN": _TOKEN}
        policy = ExecutionPolicy(
            allowed_http_origins=("https://app.example.test",),
            allowed_secret_refs=("env:AIBENCH_WORKER_TOKEN",),
        )
        compiled = compile_plan(plan, policy=policy)
        workspace = Workspace.at(project)
        workspace.ensure_directories()
        db = Database.open_workspace(workspace)
        storage = Storage(db)
        artifacts = ArtifactStore(workspace.artifacts_dir)
        options = _options()
        run_id = create_run(
            compiled,
            storage=storage,
            artifacts=artifacts,
            granted_by="test",
            environ=token_env,
            distributed_workers=options,
        )
        remote_worker_task: asyncio.Task[None] | None = None
        remote_runner: _EchoRunner | None = None

        def worker_ready(details: dict[str, Any]) -> None:
            nonlocal remote_worker_task, remote_runner

            async def connect() -> None:
                assert remote_runner is not None
                await remote_runner.prepare()
                client = DistributedWorker(
                    remote_runner,
                    coordinator_url=str(details["url"]),
                    token=_TOKEN,
                    application_hash=str(details["application_hash"]),
                )
                await client.run()

            remote_runner = _EchoRunner(compiled.application.spec)
            remote_worker_task = asyncio.create_task(connect())

        try:
            outcome = await execute_run(
                run_id,
                storage=storage,
                artifacts=artifacts,
                environ=token_env,
                on_worker_ready=worker_ready,
            )
            assert outcome.state.value == "completed"
            attempts = storage.list_execution_attempts(run_id)
            assert len(attempts) == 1
            assert attempts[0].output == "HELLO"
            assert attempts[0].status is ExecutionStatus.OK
            assert storage.count_work_items(run_id, kind="execution") == 1
        finally:
            if remote_worker_task is not None:
                remote_worker_task.cancel()
                await asyncio.gather(remote_worker_task, return_exceptions=True)
            if remote_runner is not None:
                await remote_runner.close()
            storage.db.close()

    asyncio.run(scenario())


def test_run_dry_run_validates_and_reports_distributed_worker_configuration(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path)
    application = harness.cli_app(environment_digest="worker-app-v1")
    plan = harness.plan(dataset=harness.dataset({"case-1": "hello"}), application=application)
    policy = tmp_path / "worker-policy.json"
    policy.write_text(
        '{"allow_trusted_local":true,"allowed_secret_refs":["env:AIBENCH_WORKER_TOKEN"]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("AIBENCH_WORKER_TOKEN", _TOKEN)
    result = _CLI.invoke(
        app,
        [
            "run",
            "--plan",
            str(plan),
            "--policy",
            str(policy),
            "--trust-local-app",
            "--workers",
            "1",
            "--worker-port",
            "0",
            "--dry-run",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    preview = json.loads(result.stdout)
    assert preview["distributed_workers"] == {
        "count": 1,
        "coordinator_host": "127.0.0.1",
        "coordinator_port": 0,
        "startup_timeout_seconds": 300,
    }
    assert _TOKEN not in result.stdout


def test_distributed_run_refuses_a_missing_token_before_creating_a_run(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path)
    application = harness.cli_app(environment_digest="worker-app-v1")
    plan = harness.plan(dataset=harness.dataset({"case-1": "hello"}), application=application)
    policy = tmp_path / "worker-policy.json"
    policy.write_text(
        '{"allow_trusted_local":true,"allowed_secret_refs":["env:AIBENCH_WORKER_TOKEN"]}',
        encoding="utf-8",
    )
    monkeypatch.delenv("AIBENCH_WORKER_TOKEN", raising=False)
    result = _CLI.invoke(
        app,
        [
            "run",
            "--plan",
            str(plan),
            "--policy",
            str(policy),
            "--trust-local-app",
            "--workers",
            "1",
            "--dry-run",
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert "environment variable 'AIBENCH_WORKER_TOKEN' is not set" in result.stdout
    assert not harness.workspace.db_path.exists()
