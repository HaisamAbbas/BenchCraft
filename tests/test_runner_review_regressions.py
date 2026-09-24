"""Regressions from the independent review of Prompt 15: each test reproduces a finding
that was confirmed and fixed (docs/engineering/reports/15.md)."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import ContainerTransport
from aibench.engine.compile import PlanInvalid, compile_plan
from aibench.engine.engine import RunController, RunState
from aibench.evaluators.agent import check
from aibench.evaluators.protocol import MISSING
from aibench.runners import InvocationContext, create_runner, load_application
from aibench.runners.bindings import AppInputEnvelope
from aibench.runners.container_runner import container_argv
from aibench.security.policy import ExecutionPolicy

REPO = Path(__file__).resolve().parents[1]
APPS = REPO / "examples" / "apps"
IMAGE = "python@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"


# --------------------------------------------------------------------------- finding 1


@pytest.mark.parametrize(
    "transport",
    [
        {"user": "00:00"},  # uid 0 written with a leading zero
        {"user": "65534:0"},  # the root group
        {"mounts": [{"source": ".", "target": "/s,source=/var/run/docker.sock"}]},  # CSV field
        {"mounts": [{"source": "/var/run//docker.sock", "target": "/s"}]},
        {"mounts": [{"source": "/var/run/./docker.sock", "target": "/s"}]},
        {"mounts": [{"source": "/var/run", "target": "/s"}]},  # the socket's directory
        {"mounts": [{"source": ".", "target": "/app/../etc"}]},
        {"tmpfs": ["/tmp,exec"]},
        {"engine": "powershell.exe"},
        {"env": {"PATH": "attacker-controlled"}},
        {"env": {"LD_PRELOAD": "/tmp/host-injection.so"}},
        {"secret_env": {"DOCKER_HOST": "env:ATTACKER_DOCKER_HOST"}},
    ],
)
def test_container_hardening_and_engine_client_cannot_be_bypassed(
    transport: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        ContainerTransport(image=IMAGE, argv=("true",), **transport)


def test_a_host_path_with_a_comma_is_one_quoted_mount_field(tmp_path: Path) -> None:
    source = tmp_path / "a,b"
    source.mkdir()
    t = ContainerTransport(
        image=IMAGE, argv=("true",), mounts=({"source": str(source), "target": "/app"},)
    )
    [mount] = [a for a in container_argv(t, tmp_path, "n") if a.startswith("type=bind")]
    assert mount == f'type=bind,"source={source}",target=/app,readonly'


def _engine_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    done = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, check=False)
    return done.returncode == 0


@pytest.mark.skipif(not _engine_ready(), reason="15-G1 BLOCKED: no container engine or image")
def test_a_mount_from_a_path_with_a_comma_runs_in_a_real_container(tmp_path: Path) -> None:
    source = tmp_path / "app,v2"
    shutil.copytree(APPS / "container_app", source)
    config = json.loads((APPS / "container.app.json").read_text(encoding="utf-8"))
    config["transport"]["mounts"] = [{"source": str(source), "target": "/app"}]
    app = tmp_path / "comma.app.json"
    app.write_text(json.dumps(config), encoding="utf-8")
    outcome = _invoke(app, "What is your refund policy?")
    assert outcome.status.value == "ok", outcome.error
    assert outcome.output == "Refunds are available within 30 days of purchase."


def test_a_mount_that_exposes_the_engine_socket_after_resolution_is_refused(
    tmp_path: Path,
) -> None:
    holder = tmp_path / "holder"
    holder.mkdir()
    (holder / "docker.sock").write_text("", encoding="utf-8")  # stands in for the socket
    config = json.loads((APPS / "container.app.json").read_text(encoding="utf-8"))
    config["transport"]["mounts"] = [{"source": str(holder), "target": "/app"}]
    app = tmp_path / "sock.app.json"
    app.write_text(json.dumps(config), encoding="utf-8")
    if not _engine_ready():
        pytest.skip("the check runs in prepare, after the engine and image checks")
    from aibench.core.errors import ConfigError

    with pytest.raises(ConfigError, match="exposes a container engine socket"):
        _invoke(app, "hi")


# --------------------------------------------------------------------------- findings 2, 8


def _invoke(app_path: Path, text: Any) -> Any:
    async def go() -> Any:
        runner = create_runner(load_application(app_path), trusted_local=True)
        async with runner:
            return await runner.invoke(
                AppInputEnvelope(data={"case_id": "c", "input": text}),
                InvocationContext(run_id="r", case_id="c"),
            )

    return asyncio.run(go())


def _python_app(tmp_path: Path, source: str) -> Path:
    (tmp_path / "unicode_app.py").write_text(source, encoding="utf-8")
    app = tmp_path / "py.app.json"
    app.write_text(
        json.dumps(
            {
                "application_id": "py",
                "runner": "python",
                "target": "unicode_app.py:run",
                "transport": {"kind": "python", "callable": "unicode_app.py:run"},
            }
        ),
        encoding="utf-8",
    )
    return app


def test_the_python_shim_speaks_utf8_whatever_the_locale(tmp_path: Path) -> None:
    app = _python_app(
        tmp_path,
        "def run(payload):\n"
        "    text = payload['input']\n"
        "    return {'output': f'café {text}', 'length': len(text)}\n",
    )
    outcome = _invoke(app, "☃")
    assert outcome.status.value == "ok", outcome.error
    assert outcome.output == "café ☃"
    stdout = next(c for c in outcome.captures if c.name == "stdout").data
    assert json.loads(stdout)["length"] == 1


def test_prints_and_shim_imports_cannot_corrupt_the_result(tmp_path: Path) -> None:
    app = _python_app(
        tmp_path,
        "import importlib.util\n"
        "def run(payload):\n"
        "    print('debugging noise')\n"
        "    shadowed = importlib.util.find_spec('bindings') is not None\n"
        "    return {'output': 'ok', 'aibench_module_visible': shadowed}\n",
    )
    outcome = _invoke(app, "x")
    assert outcome.status.value == "ok", outcome.error
    assert outcome.output == "ok"
    document = json.loads(next(c for c in outcome.captures if c.name == "stdout").data)
    assert document["aibench_module_visible"] is False
    assert b"debugging noise" in next(c for c in outcome.captures if c.name == "stderr").data


# --------------------------------------------------------------------------- findings 3-5


@pytest.fixture
def world(tmp_path: Path) -> Path:
    project = tmp_path / "agent_world"
    shutil.copytree(REPO / "examples" / "agent_world", project)
    return project


def _compile(project: Path, plan: str = "plan.json", **changes: Any) -> Any:
    data = json.loads((project / plan).read_text(encoding="utf-8"))
    data.update(changes)
    path = project / "variant.plan.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    policy = ExecutionPolicy.model_validate_json((project / "policy.json").read_text())
    return compile_plan(path, policy=policy.resolved_against(project))


def _edit_app(project: Path, name: str, **changes: Any) -> None:
    path = project / name
    config = json.loads(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if key.startswith("world_"):
            config["test_worlds"]["two-seats"][key[6:]] = value
        else:
            config[key] = value
    path.write_text(json.dumps(config), encoding="utf-8")


def test_a_shared_application_cannot_select_a_test_world(world: Path) -> None:
    _edit_app(world, "booking.app.json", reset_policy="shared")
    with pytest.raises(PlanInvalid, match="never reset, so a test world could not be loaded"):
        _compile(world)


def test_a_selection_cannot_split_an_episode(world: Path) -> None:
    with pytest.raises(PlanInvalid, match="splits episode 'trip-1'"):
        _compile(world, "episodes.plan.json", selection={"case_ids": ["trip1-grace", "trip2-ada"]})
    # Whole episodes, or an episode's leading turns, are fine.
    assert _compile(
        world, "episodes.plan.json", selection={"case_ids": ["trip1-ada", "trip2-ada"]}
    ).cases


def test_seeds_must_be_objects_inside_the_application_directory(
    world: Path, tmp_path: Path
) -> None:
    (world / "worlds" / "null.json").write_text("null", encoding="utf-8")
    _edit_app(world, "booking.app.json", world_seed_file="worlds/null.json")
    with pytest.raises(PlanInvalid, match="a seed must be a JSON object or list"):
        _compile(world)
    (tmp_path / "outside.json").write_text('{"secret": 1}', encoding="utf-8")
    _edit_app(world, "booking.app.json", world_seed_file="../outside.json")
    with pytest.raises(PlanInvalid, match="outside the application's directory"):
        _compile(world)


# --------------------------------------------------------------------------- finding 6


@pytest.mark.parametrize(
    ("actual", "constraint", "ok"),
    [
        (5, {"present": False}, False),
        (MISSING, {"present": False}, True),
        (False, {"equals": 0}, False),
        (True, 1, False),
        (True, {"in": [1]}, False),
        (0, {"not_equals": False}, True),
        (1, 1.0, True),
        ({"a": [True]}, {"equals": {"a": [1]}}, False),
    ],
)
def test_constraints_are_json_strict(actual: Any, constraint: Any, ok: bool) -> None:
    assert (check(actual, constraint) is None) is ok


# --------------------------------------------------------------------------- finding 7


@pytest.fixture
def slow_reset_app(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / "slow.py").write_text(
        "import time\ndef reset(seed):\n    time.sleep(8)\ndef run(payload):\n    return 'ok'\n",
        encoding="utf-8",
    )
    app = tmp_path / "slow.app.json"
    app.write_text(
        json.dumps(
            {
                "application_id": "slow",
                "runner": "python",
                "target": "slow.py:run",
                "transport": {
                    "kind": "python",
                    "callable": "slow.py:run",
                    "reset_callable": "slow.py:reset",
                },
            }
        ),
        encoding="utf-8",
    )
    yield app


def test_cancelling_during_a_reset_stops_promptly(tmp_path: Path, slow_reset_app: Path) -> None:
    from tests.engine_support import Harness

    h = Harness(tmp_path / "h")
    plan = h.plan(dataset=h.dataset({"a": "hi"}), application=str(slow_reset_app))
    plan_data = json.loads(plan.read_text(encoding="utf-8"))
    plan_data["concurrency"] = {"application": 1, "evaluation": 1}
    plan.write_text(json.dumps(plan_data), encoding="utf-8")
    run_id = h.create(plan)

    async def cancel_soon(ctl: RunController, _: Any) -> None:
        await asyncio.sleep(1.5)
        ctl.request("cancel")

    started = time.monotonic()
    outcome = h.execute(run_id, during=cancel_soon)
    assert outcome.state is RunState.CANCELLED
    assert time.monotonic() - started < 6  # the 8 s reset was abandoned, not awaited
    assert outcome.counts["execution"] == {"cancelled": 1}
