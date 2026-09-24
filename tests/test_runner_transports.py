"""Richer transports behind the runner contract (15-T1): Python callable, OpenAI-compatible
endpoint and container. Each is exercised end to end through `aibench app smoke` /
`aibench app describe` against a real fixture (a real interpreter process, a real HTTP
server, a real container), plus the failure paths of the lifecycle contract.

Container tests need a running container engine and the pinned fixture image. Without them
they are skipped with that reason: 15-G1 is then BLOCKED, never passed."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.errors import ConfigError, PolicyError
from aibench.core.models import (
    ApplicationSpec,
    ContainerTransport,
    EffectState,
    ErrorKind,
    ExecutionStatus,
)
from aibench.runners import InvocationContext, create_runner, load_application
from aibench.runners.bindings import AppInputEnvelope
from aibench.runners.container_runner import container_argv
from aibench.security.policy import ExecutionPolicy, application_denials

REPO = Path(__file__).resolve().parents[1]
APPS = REPO / "examples" / "apps"
SUPPORT = REPO / "examples" / "datasets" / "support.valid.jsonl"
IMAGE = "python@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"
cli = CliRunner()


def _json(args: list[str], code: int = 0) -> Any:
    result = cli.invoke(app, args)
    assert result.exit_code == code, result.output
    return json.loads(result.stdout)


def _write_app(path: Path, config: dict[str, Any]) -> Path:
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _invoke(app_path: Path, text: Any, *, trusted: bool = True, **env: str) -> Any:
    async def go() -> Any:
        runner = create_runner(
            load_application(app_path), trusted_local=trusted, environ={**os.environ, **env}
        )
        async with runner:
            return await runner.invoke(
                AppInputEnvelope(data={"case_id": "c1", "input": text}),
                InvocationContext(run_id="r", case_id="c1"),
            )

    return asyncio.run(go())


# --------------------------------------------------------------------------- Python callable


def test_python_callable_runs_end_to_end_through_the_cli(tmp_path: Path) -> None:
    data = _json(
        ["app", "smoke", str(APPS / "python_chatbot.app.json"), "--dataset", str(SUPPORT),
         "--trust-local-app", "--workspace", str(tmp_path), "--json"]
    )  # fmt: skip
    outputs = {r["case_id"]: (r["status"], r["output"]) for r in data["results"]}
    assert outputs["support-001"] == ("ok", "Refunds are available within 30 days of purchase.")
    assert outputs["support-003"][1].startswith("We ship to over 40 countries")
    described = cli.invoke(app, ["app", "describe", str(APPS / "python_chatbot.app.json")])
    assert described.exit_code == 0 and "python" in described.output
    assert "fresh interpreter" in described.output


def test_python_callable_needs_trusted_local_mode(tmp_path: Path) -> None:
    result = cli.invoke(
        app,
        ["app", "smoke", str(APPS / "python_chatbot.app.json"), "--dataset", str(SUPPORT),
         "--workspace", str(tmp_path)],
    )  # fmt: skip
    assert result.exit_code != 0 and "trusted-local" in result.output
    spec = load_application(APPS / "python_chatbot.app.json").spec
    assert any("trusted-local" in d for d in application_denials(ExecutionPolicy(), spec))


def _python_app(tmp_path: Path, callable_: str, **transport: Any) -> Path:
    return _write_app(
        tmp_path / "py.app.json",
        {
            "application_id": "py",
            "runner": "python",
            "target": callable_,
            "transport": {"kind": "python", "callable": callable_, **transport},
        },
    )


def test_a_coroutine_function_returning_a_bare_value_is_the_output(tmp_path: Path) -> None:
    app_path = _python_app(tmp_path, f"{APPS / 'python_chatbot.py'}:respond_async")
    outcome = _invoke(app_path, "Do you have a warranty?")
    assert outcome.status is ExecutionStatus.OK
    assert outcome.output == "All products carry a one-year limited warranty."


def test_an_exception_is_an_application_failure_with_its_traceback(tmp_path: Path) -> None:
    outcome = _invoke(_python_app(tmp_path, f"{APPS / 'python_chatbot.py'}:explode"), "hi")
    assert (outcome.status, outcome.error_kind) == (ExecutionStatus.ERROR, ErrorKind.NONZERO_EXIT)
    stderr = next(c for c in outcome.captures if c.name == "stderr").data.decode()
    assert "RuntimeError: the application failed on purpose" in stderr


def test_a_slow_callable_is_killed_at_its_timeout(tmp_path: Path) -> None:
    (tmp_path / "slow.py").write_text(
        "import time\ndef run(p):\n    time.sleep(60)\n    return 'late'\n", encoding="utf-8"
    )
    outcome = _invoke(_python_app(tmp_path, "slow.py:run", timeout_seconds=2, paths=["."]), "hi")
    assert (outcome.error_kind, outcome.effect_state) == (
        ErrorKind.TIMEOUT,
        EffectState.NONE_DECLARED,
    )
    assert float(outcome.timing["wall_ms"]) < 20_000


def test_reset_callable_receives_the_seed(tmp_path: Path) -> None:
    (tmp_path / "world.py").write_text(
        "import json, pathlib\n"
        "STATE = pathlib.Path(__file__).with_name('state.json')\n"
        "def reset(seed):\n    STATE.write_text(json.dumps(seed))\n"
        "def run(p):\n    return {'output': json.loads(STATE.read_text())}\n",
        encoding="utf-8",
    )
    app_path = _python_app(tmp_path, "world.py:run", reset_callable="world.py:reset")

    async def go() -> tuple[Any, Any]:
        runner = create_runner(load_application(app_path), trusted_local=True)
        async with runner:
            assert runner.resettable
            report = await runner.reset({"seats": 3})
            outcome = await runner.invoke(
                AppInputEnvelope(data={"case_id": "c", "input": None}),
                InvocationContext(run_id="r", case_id="c"),
            )
            return report, outcome

    report, outcome = asyncio.run(go())
    assert report.status == "reset" and outcome.output == {"seats": 3}


# --------------------------------------------------------------------------- OpenAI-compatible


@pytest.fixture
def stub() -> Iterator[Any]:
    from tests.runner_support import load_example

    server = load_example("openai_stub").make_server(port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _openai_app(tmp_path: Path, server: Any, **transport: Any) -> Path:
    return _write_app(
        tmp_path / "openai.app.json",
        {
            "application_id": "stub-endpoint",
            "runner": "openai_compatible",
            "target": "stub-1",
            "transport": {
                "kind": "openai_compatible",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                "model": "stub-1",
                **transport,
            },
        },
    )


def test_openai_compatible_endpoint_runs_end_to_end_through_the_cli(
    tmp_path: Path, stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_KEY", "sk-test-secret-value")
    app_path = _openai_app(
        tmp_path,
        stub,
        api_key="env:STUB_KEY",
        system_prompt="You answer support questions.",
        parameters={"temperature": 0},
    )
    data = _json(
        ["app", "smoke", str(app_path), "--dataset", str(SUPPORT), "--limit", "1",
         "--workspace", str(tmp_path), "--json"]
    )  # fmt: skip
    [row] = data["results"]
    assert (row["status"], row["output"]) == (
        "ok",
        "Refunds are available within 30 days of purchase.",
    )
    [request] = stub.requests
    assert request["authorization"] == "Bearer sk-test-secret-value"
    body = request["body"]
    assert body["model"] == "stub-1" and body["temperature"] == 0
    assert body["messages"][0] == {"role": "system", "content": "You answer support questions."}
    assert body["messages"][1] == {"role": "user", "content": "What is your refund policy?"}
    # The key never reaches stored captures.
    blobs = b"".join(
        p.read_bytes() for p in (tmp_path / ".aibench" / "artifacts").rglob("*") if p.is_file()
    )
    assert b"sk-test-secret-value" not in blobs


def test_usage_is_observed_and_tool_calls_are_requests_not_effects(
    tmp_path: Path, stub: Any
) -> None:
    tool = {"type": "function", "function": {"name": "book_flight", "parameters": {}}}
    outcome = _invoke(_openai_app(tmp_path, stub, tools=[tool]), "Please book BA117")
    assert outcome.status is ExecutionStatus.OK
    assert outcome.observations.usage["total_tokens"] > 0
    assert outcome.completeness["usage"]["state"] == "observed"
    [call] = outcome.observations.tool_events
    assert call["function"]["name"] == "book_flight"
    assert outcome.completeness["cost"]["detail"] == "not_bound"  # never invented


def test_a_remote_endpoint_needs_an_approved_origin_and_secret() -> None:
    spec = ApplicationSpec.model_validate(
        {
            "application_id": "hosted",
            "runner": "openai_compatible",
            "target": "gpt",
            "transport": {
                "kind": "openai_compatible",
                "base_url": "https://api.example.com/v1",
                "model": "m",
                "api_key": "env:KEY",
            },
        }
    )
    denials = application_denials(ExecutionPolicy(), spec)
    assert any("api.example.com" in d and "not an approved target" in d for d in denials)
    assert "secret env:KEY is not allowed by the policy" in denials
    allowed = ExecutionPolicy(
        allowed_http_origins=("https://api.example.com",), allowed_secret_refs=("env:KEY",)
    )
    assert application_denials(allowed, spec) == []


# --------------------------------------------------------------------------- container


def _engine_ready() -> str | None:
    if shutil.which("docker") is None:
        return "no container engine (docker) on PATH"
    done = subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        return f"container engine not running or fixture image {IMAGE} not pulled"
    return None


needs_engine = pytest.mark.skipif(
    _engine_ready() is not None, reason=f"15-G1 BLOCKED: {_engine_ready()}"
)


def test_container_command_line_is_hardened() -> None:
    t = ContainerTransport(image=IMAGE, argv=("python", "/app/app.py"))
    argv = container_argv(t, REPO, "aibench-x")
    joined = " ".join(argv)
    for flag in (
        "--pull=never", "--rm", "--read-only", "--network none", "--user 65534:65534", "--cap-drop ALL",
        "--security-opt no-new-privileges", "--memory 512m", "--memory-swap 512m",
        "--pids-limit 128", "--tmpfs /tmp:rw,noexec,nosuid,size=64m",
    ):  # fmt: skip
        assert flag in joined, flag
    assert argv[-3:] == [IMAGE, "python", "/app/app.py"]
    assert "docker.sock" not in joined and "--privileged" not in joined


def test_container_policy_approves_images_and_network_explicitly() -> None:
    spec = load_application(APPS / "container.app.json").spec
    assert any("not approved" in d for d in application_denials(ExecutionPolicy(), spec))
    approved = ExecutionPolicy(allowed_container_images=("python@sha256:*",))
    assert application_denials(approved, spec) == []
    networked = spec.model_copy(
        update={"transport": spec.transport.model_copy(update={"network": "bridge"})}
    )
    assert any("allow_container_network" in d for d in application_denials(approved, networked))
    # Containers are a configured sandbox: no trusted-local grant is involved.
    assert approved.allow_trusted_local is False


@needs_engine
def test_a_real_container_fixture_runs_end_to_end_through_the_cli(tmp_path: Path) -> None:
    data = _json(
        ["app", "smoke", str(APPS / "container.app.json"), "--dataset", str(SUPPORT),
         "--limit", "2", "--workspace", str(tmp_path), "--json"]
    )  # fmt: skip
    outputs = {r["case_id"]: (r["status"], r["output"]) for r in data["results"]}
    assert outputs["support-001"] == ("ok", "Refunds are available within 30 days of purchase.")
    assert outputs["support-002"][0] == "ok"
    described = cli.invoke(app, ["app", "describe", str(APPS / "container.app.json")])
    assert "container_per_invocation" in described.output and "network none" in described.output


@needs_engine
def test_the_container_runs_non_root_read_only_and_offline() -> None:
    outcome = _invoke(APPS / "container.app.json", "What is your refund policy?")
    assert outcome.status is ExecutionStatus.OK
    stdout = json.loads(next(c for c in outcome.captures if c.name == "stdout").data)
    env = stdout["environment"]
    assert env["uid"] == 65534
    assert env["write_app_dir"].startswith("denied")  # read-only source mount
    assert env["write_root_fs"].startswith("denied")  # read-only root filesystem
    assert env["write_tmp"] == "allowed"  # tmpfs scratch space
    assert env["network"].startswith("denied")  # --network none


@needs_engine
def test_a_container_timeout_kills_and_removes_the_container(tmp_path: Path) -> None:
    config = json.loads((APPS / "container.app.json").read_text(encoding="utf-8"))
    config["transport"]["argv"] = ["python", "-c", "import time; time.sleep(60)"]
    config["transport"]["timeout_seconds"] = 3
    config["transport"]["mounts"] = [{"source": str(APPS / "container_app"), "target": "/app"}]
    outcome = _invoke(_write_app(tmp_path / "slow.app.json", config), "hi")
    assert outcome.error_kind is ErrorKind.TIMEOUT
    name = "aibench-" + outcome.correlation_id[:40]
    left = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert left.stdout.strip() == ""  # nothing survives the invocation


@needs_engine
def test_an_image_that_is_not_present_is_refused_before_running(tmp_path: Path) -> None:
    config = json.loads((APPS / "container.app.json").read_text(encoding="utf-8"))
    config["transport"]["image"] = "python@sha256:" + "0" * 64
    config["transport"]["mounts"] = [{"source": str(APPS / "container_app"), "target": "/app"}]
    with pytest.raises(ConfigError, match="not present locally; nothing is pulled"):
        _invoke(_write_app(tmp_path / "missing.app.json", config), "hi")


def test_a_python_callable_without_trust_is_refused_by_the_runner(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="trusted-local"):
        _invoke(APPS / "python_chatbot.app.json", "hi", trusted=False)


def test_the_shim_needs_nothing_from_aibench(tmp_path: Path) -> None:
    """It runs in the application's interpreter, which may not have aibench installed."""
    import ast

    from aibench.runners.python_runner import SHIM

    tree = ast.parse(SHIM.read_text(encoding="utf-8"))
    imported = {
        (node.module or "") if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert not any(name.split(".")[0] == "aibench" for name in imported), imported
    done = subprocess.run(
        [sys.executable, "-I", str(SHIM), f"{APPS / 'python_chatbot.py'}:respond"],
        input='{"input": "refund?"}',
        capture_output=True,
        text=True,
        check=False,
    )
    assert json.loads(done.stdout) == {
        "output": "Refunds are available within 30 days of purchase."
    }
