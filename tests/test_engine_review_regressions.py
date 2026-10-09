"""Regression tests for the Prompt 06 independent review (see reports/06.md §4).

Each finding was reproduced first; the reproductions asserted the defect and passed on the
pre-fix code. These tests assert the corrected behavior."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aibench.core.models import ExecutionStatus, WorkItemState
from aibench.core.plans import BudgetLimits, ExecutablePlan, PluginEnvironmentRef
from aibench.engine.budget import BudgetLedger
from aibench.engine.compile import PolicyDenied, compile_plan
from aibench.engine.engine import RunController, RunState
from aibench.security.policy import ExecutionPolicy, plan_denials
from aibench.services.runs import RunError, execute_run
from aibench.storage.repositories import LeaseHeld, Storage
from tests.engine_support import Harness


class SimulatedCrash(BaseException):
    pass


def _states(h: Harness, run_id: str) -> dict[str, str]:
    storage, _ = h.storage()
    try:
        return {w.task_key: w.state.value for w in storage.list_work_items(run_id)}
    finally:
        storage.db.close()


def _events(h: Harness, run_id: str) -> list[dict[str, Any]]:
    storage, _ = h.storage()
    try:
        return storage.list_run_events(run_id)
    finally:
        storage.db.close()


def test_application_vcs_identity_captures_git_commit_and_tracked_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.core.hashes import bytes_hash
    from aibench.core.models import ApplicationSpec
    from aibench.services import runs as run_service

    spec = ApplicationSpec.model_validate(
        {
            "application_id": "local",
            "runner": "cli",
            "target": "app.py",
            "transport": {"kind": "cli", "argv": ["python", "app.py"]},
        }
    )
    (tmp_path / ".git").mkdir()
    commit = "a" * 40
    monkeypatch.setattr(
        run_service.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=f"{commit}\n"),
    )
    monkeypatch.setattr(
        run_service,
        "_git_tracked_diff_identity",
        lambda _path, _environment: ("dirty", bytes_hash(b"binary tracked diff")),
    )
    monkeypatch.setattr(
        run_service,
        "_git_untracked_files_identity",
        lambda _path, _environment: (1, bytes_hash(b"untracked prompt")),
    )

    assert run_service._application_vcs_identity(spec, tmp_path) == {
        "kind": "git",
        "commit": commit,
        "tracked_worktree": "dirty",
        "tracked_diff_hash": bytes_hash(b"binary tracked diff"),
        "untracked_file_count": 1,
        "untracked_files_hash": bytes_hash(b"untracked prompt"),
    }


def test_git_tracked_diff_identity_fails_closed_at_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    from aibench.services import runs as run_service

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"twelve bytes")
            self.returncode = 0
            self.killed = False

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

    process = FakeProcess()
    monkeypatch.setattr(run_service, "MAX_GIT_DIFF_BYTES", 8)
    monkeypatch.setattr(run_service, "GIT_DIFF_CHUNK_BYTES", 4)
    monkeypatch.setattr(run_service.subprocess, "Popen", lambda *_args, **_kwargs: process)

    assert run_service._git_tracked_diff_identity(tmp_path, {}) is None
    assert process.killed


def test_git_untracked_file_identity_fails_closed_at_name_list_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    from aibench.services import runs as run_service

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(b"prompt.md\0config.json\0")
            self.returncode = 0
            self.killed = False

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return self.returncode

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

    process = FakeProcess()
    monkeypatch.setattr(run_service, "MAX_GIT_UNTRACKED_LIST_BYTES", 8)
    monkeypatch.setattr(run_service, "GIT_DIFF_CHUNK_BYTES", 4)
    monkeypatch.setattr(run_service.subprocess, "Popen", lambda *_args, **_kwargs: process)

    assert run_service._git_untracked_files_identity(tmp_path, {}) is None
    assert process.killed


def test_git_untracked_file_identity_hashes_file_names_modes_and_contents(tmp_path: Path) -> None:
    import subprocess

    from aibench.services import runs as run_service

    (tmp_path / ".git").mkdir()
    (tmp_path / "prompt.md").write_text("before", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    before = run_service._git_untracked_files_identity(tmp_path, environment)
    (tmp_path / "prompt.md").write_text("after!", encoding="utf-8")
    after = run_service._git_untracked_files_identity(tmp_path, environment)

    assert before is not None and before[0] == 1
    assert before[1] != after[1]  # same untracked name, changed prompt bytes
    assert before[1].startswith("sha256:")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_git_untracked_file_identity_rejects_junctions_outside_app_root(tmp_path: Path) -> None:
    import subprocess

    from aibench.services import runs as run_service

    app_root = tmp_path / "app"
    external = tmp_path / "external"
    app_root.mkdir()
    external.mkdir()
    (external / "prompt.md").write_text("outside the app root", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=app_root, check=True)
    junction = app_root / "linked"
    junction_environment = {
        **os.environ,
        "BENCHCRAFT_TEST_JUNCTION": str(junction),
        "BENCHCRAFT_TEST_JUNCTION_TARGET": str(external),
    }
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "New-Item -ItemType Junction -Path $env:BENCHCRAFT_TEST_JUNCTION "
                "-Target $env:BENCHCRAFT_TEST_JUNCTION_TARGET | Out-Null"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=junction_environment,
    )
    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}

    assert run_service._git_untracked_files_identity(app_root, environment) is None


def test_run_refuses_unfingerprintable_git_diff_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.services import runs as run_service

    h = Harness(tmp_path)
    monkeypatch.setattr(
        run_service,
        "_application_vcs_identity",
        lambda _spec, _path: {
            "kind": "git",
            "commit": "a" * 40,
            "tracked_worktree": "unknown",
            "tracked_diff_hash": None,
            "untracked_file_count": 0,
            "untracked_files_hash": None,
        },
    )
    plan = h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app())

    with pytest.raises(RunError, match="tracked changes, or untracked files"):
        h.create(plan)
    assert h.count() == 0


def test_resume_refuses_application_git_revision_drift_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.services import runs as run_service

    h = Harness(tmp_path)
    frozen_identity = {
        "kind": "git",
        "commit": "a" * 40,
        "tracked_worktree": "clean",
        "tracked_diff_hash": "sha256:clean",
        "untracked_file_count": 0,
        "untracked_files_hash": "sha256:empty",
    }
    monkeypatch.setattr(
        run_service, "_application_vcs_identity", lambda _spec, _path: frozen_identity
    )
    run_id = h.create(h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app()))

    monkeypatch.setattr(
        run_service,
        "_application_vcs_identity",
        lambda _spec, _path: {
            "kind": "git",
            "commit": "b" * 40,
            "tracked_worktree": "clean",
            "tracked_diff_hash": "sha256:clean",
            "untracked_file_count": 0,
            "untracked_files_hash": "sha256:empty",
        },
    )
    with pytest.raises(RunError, match="Git commit or local working tree changed"):
        h.execute(run_id)
    assert h.count() == 0


def test_resume_refuses_changed_tracked_data_when_dirty_state_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.services import runs as run_service

    h = Harness(tmp_path)
    identities = iter(
        [
            {
                "kind": "git",
                "commit": "a" * 40,
                "tracked_worktree": "dirty",
                "tracked_diff_hash": "sha256:before",
                "untracked_file_count": 0,
                "untracked_files_hash": "sha256:empty",
            },
            {
                "kind": "git",
                "commit": "a" * 40,
                "tracked_worktree": "dirty",
                "tracked_diff_hash": "sha256:after",
                "untracked_file_count": 0,
                "untracked_files_hash": "sha256:empty",
            },
        ]
    )
    monkeypatch.setattr(
        run_service, "_application_vcs_identity", lambda _spec, _path: next(identities)
    )
    run_id = h.create(h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app()))

    with pytest.raises(RunError, match="Git commit or local working tree changed"):
        h.execute(run_id)
    assert h.count() == 0


def test_resume_refuses_changed_untracked_prompt_when_tracked_state_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.services import runs as run_service

    h = Harness(tmp_path)
    identities = iter(
        [
            {
                "kind": "git",
                "commit": "a" * 40,
                "tracked_worktree": "clean",
                "tracked_diff_hash": "sha256:clean",
                "untracked_file_count": 1,
                "untracked_files_hash": "sha256:prompt-before",
            },
            {
                "kind": "git",
                "commit": "a" * 40,
                "tracked_worktree": "clean",
                "tracked_diff_hash": "sha256:clean",
                "untracked_file_count": 1,
                "untracked_files_hash": "sha256:prompt-after",
            },
        ]
    )
    monkeypatch.setattr(
        run_service, "_application_vcs_identity", lambda _spec, _path: next(identities)
    )
    run_id = h.create(h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app()))

    with pytest.raises(RunError, match="Git commit or local working tree changed"):
        h.execute(run_id)
    assert h.count() == 0


@pytest.mark.parametrize("drift", ["source", "environment"])
def test_resume_refuses_application_drift_before_dispatch(tmp_path: Path, drift: str) -> None:
    h = Harness(tmp_path)
    app_config_path = h.root / h.cli_app()
    app_config = json.loads(app_config_path.read_text(encoding="utf-8"))
    app_config["transport"]["inherit_env"] = ["BENCHCRAFT_RESUME_DRIFT"]
    app_config_path.write_text(json.dumps(app_config), encoding="utf-8")
    plan = h.plan(
        dataset=h.dataset({"first": "hi", "slow-a": "slow 2", "slow-b": "slow 2"}),
        application=str(app_config_path),
    )
    before = dict(os.environ)
    before["BENCHCRAFT_RESUME_DRIFT"] = "before"
    run_id = h.create(plan, environ=before)
    storage, _ = h.storage()
    try:
        record = storage.get_run(run_id)
        assert record is not None
        assert record.manifest.parameters["application_identity_basis"]["kind"] == (
            "local_source_content_hash"
        )
    finally:
        storage.db.close()
    controller = RunController()

    async def interrupt_after_first_result(ctl: RunController, harness: Harness) -> None:
        await harness.wait_for_invocations(1)
        # Let the first completed invocation settle while the other slow work stays in flight.
        await asyncio.sleep(0.1)
        ctl.request("interrupt")
        await asyncio.sleep(0.1)
        ctl.request("interrupt")

    outcome = h.execute(
        run_id,
        controller=controller,
        during=interrupt_after_first_result,
        environ=before,
    )
    assert outcome.state is RunState.INTERRUPTED
    attempts = h.log.with_suffix(".attempts")
    attempts_before_resume = attempts.read_text(encoding="utf-8")
    assert h.count("first") == 1

    after = dict(before)
    if drift == "source":
        source = h.root / "app.py"
        source.write_text(
            source.read_text(encoding="utf-8").replace('"output": "yes"', '"output": "after"'),
            encoding="utf-8",
        )
    else:
        after["BENCHCRAFT_RESUME_DRIFT"] = "after"

    with pytest.raises(RunError, match="changed since this run was created"):
        h.execute(run_id, environ=after)
    assert attempts.read_text(encoding="utf-8") == attempts_before_resume


def test_remote_resume_identity_requires_owner_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.core.models import ApplicationSpec, CliTransport, HttpTransport
    from aibench.engine.cache import application_resume_identity_problem
    from aibench.services import runs as run_service
    from aibench.services.runs import (
        _application_environment_identity,
        _application_identity_basis,
    )

    monkeypatch.setattr(
        run_service,
        "environment_paths",
        lambda _python, **_kwargs: ([tmp_path], [tmp_path], True),
    )

    cli_python_environment = _application_environment_identity(
        ApplicationSpec(
            application_id="cli-python",
            runner="cli",
            target="app.py",
            transport=CliTransport(argv=(sys.executable, "app.py")),
        ),
        tmp_path,
        dict(os.environ),
    )
    assert cli_python_environment is not None
    assert cli_python_environment["kind"] == "python"
    assert cli_python_environment["binary"]
    assert cli_python_environment["runtime"]
    assert cli_python_environment["dependencies"]

    spec = ApplicationSpec(
        application_id="remote",
        runner="http",
        target="https://example.test/answer",
        transport=HttpTransport(url="https://example.test/answer"),
    )
    problem = application_resume_identity_problem(spec, tmp_path, {"code": None})
    assert problem is not None and "declare `revision` or `environment_digest`" in problem

    declared = spec.model_copy(update={"revision": "release-2026-10"})
    assert application_resume_identity_problem(declared, tmp_path, {"code": None}) is None
    assert _application_identity_basis(declared, {"code": None}, None) == {
        "kind": "owner_declared",
        "revision": "release-2026-10",
        "environment_digest": None,
        "local_source_digest": None,
    }


def test_pythonpath_import_source_is_part_of_resume_identity(tmp_path: Path) -> None:
    from aibench.core.models import ApplicationSpec, CliTransport
    from aibench.engine.cache import application_code_identity

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "main.py").write_text("from shared import answer\n", encoding="utf-8")
    import_dir = tmp_path / "shared-libraries"
    import_dir.mkdir()
    imported = import_dir / "shared.py"
    imported.write_text("answer = 42\n", encoding="utf-8")
    spec = ApplicationSpec(
        application_id="pythonpath-app",
        runner="cli",
        target="main.py",
        transport=CliTransport(
            argv=(sys.executable, "main.py"),
            env={"PYTHONPATH": str(import_dir)},
        ),
    )

    before = application_code_identity(spec, app_dir, {})
    imported.write_text("answer = 43\n", encoding="utf-8")
    after = application_code_identity(spec, app_dir, {})

    assert before["python_import_paths_untracked"] is False
    assert before != after


def test_opaque_pythonpath_requires_owner_environment_digest(tmp_path: Path) -> None:
    from aibench.core.models import ApplicationSpec, CliTransport
    from aibench.engine.cache import (
        application_code_identity,
        application_resume_identity_problem,
    )

    (tmp_path / "main.py").write_text("print('ok')\n", encoding="utf-8")
    spec = ApplicationSpec(
        application_id="secret-pythonpath-app",
        runner="cli",
        target="main.py",
        transport=CliTransport(
            argv=(sys.executable, "main.py"),
            secret_env={"PYTHONPATH": "env:BENCHCRAFT_SECRET_PYTHONPATH"},
        ),
    )
    identity = application_code_identity(spec, tmp_path, {})

    assert identity["python_import_paths_untracked"] is True
    assert "PYTHONPATH" in (application_resume_identity_problem(spec, tmp_path, identity) or "")
    declared = spec.model_copy(update={"environment_digest": "sha256:owner-pinned"})
    assert application_resume_identity_problem(declared, tmp_path, identity) is None


def test_editable_install_source_is_part_of_python_environment_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.core.models import ApplicationSpec, PythonTransport
    from aibench.services import runs as run_service

    editable_root = tmp_path / "editable-source"
    editable_root.mkdir()
    imported = editable_root / "installed_app.py"
    imported.write_text("VALUE = 1\n", encoding="utf-8")
    installed_site = tmp_path / "installed-site"
    distribution = installed_site / "fixture_package-1.0.dist-info"
    package = installed_site / "fixture_package"
    distribution.mkdir(parents=True)
    package.mkdir()
    (distribution / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: fixture-package\nVersion: 1.0\n",
        encoding="utf-8",
    )
    package_source = package / "module.py"
    package_source.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        run_service,
        "environment_paths",
        lambda _python, **_kwargs: (
            [installed_site, editable_root], [installed_site, editable_root], True
        ),
    )
    spec = ApplicationSpec(
        application_id="editable-python-app",
        runner="python",
        target="installed_app.py",
        transport=PythonTransport(
            callable="installed_app.py:answer",
            python=sys.executable,
        ),
    )

    before = run_service._application_environment_identity(spec, tmp_path, {})
    imported.write_text("VALUE = 2\n", encoding="utf-8")
    after = run_service._application_environment_identity(spec, tmp_path, {})
    monkeypatch.setattr(
        run_service,
        "environment_paths",
        lambda _python, **_kwargs: (
            [editable_root, installed_site], [editable_root, installed_site], True
        ),
    )
    reordered = run_service._application_environment_identity(spec, tmp_path, {})
    package_source.write_text("VALUE = 2\n", encoding="utf-8")
    changed_installed_package = run_service._application_environment_identity(
        spec, tmp_path, {}
    )

    assert before is not None and before["dependencies"]
    assert after is not None and before["dependencies"] != after["dependencies"]
    assert reordered is not None and reordered["dependencies"] != after["dependencies"]
    assert (
        changed_installed_package is not None
        and changed_installed_package["dependencies"] != reordered["dependencies"]
    )


def test_drift_does_not_prevent_a_dead_cancellation_from_finishing(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app())
    )
    storage, _ = h.storage()
    try:
        storage.update_run_status(run_id, "cancelling")
    finally:
        storage.db.close()

    source = h.root / "app.py"
    source.write_text(
        source.read_text(encoding="utf-8").replace('"output": "yes"', '"output": "changed"'),
        encoding="utf-8",
    )
    outcome = h.execute(run_id)
    assert outcome.state is RunState.CANCELLED
    assert h.count() == 0


def test_opaque_cli_runtime_requires_environment_digest_to_resume(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    executable = h.root / "opaque-tool.exe"
    executable.write_bytes(b"opaque executable identity")
    app_config_path = h.root / h.cli_app()
    app_config = json.loads(app_config_path.read_text(encoding="utf-8"))
    app_config["transport"]["argv"] = [str(executable), "app.py"]
    app_config_path.write_text(json.dumps(app_config), encoding="utf-8")
    run_id = h.create(
        h.plan(
            dataset=h.dataset({"a": "hi"}),
            application=str(app_config_path),
        )
    )
    storage, _ = h.storage()
    try:
        record = storage.get_run(run_id)
        assert record is not None
        basis = record.manifest.parameters["application_identity_basis"]
        assert basis["kind"] == "local_source_and_cli_executable_hash"
        assert basis["resume_requirement"] == "environment_digest for installed CLI dependencies"
        storage.update_run_status(run_id, "interrupted")
    finally:
        storage.db.close()

    with pytest.raises(RunError, match="declare an `environment_digest`"):
        h.execute(run_id)
    assert h.count() == 0


def test_pythonhome_requires_environment_digest_before_resume(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    app_config_path = h.root / h.cli_app()
    app_config = json.loads(app_config_path.read_text(encoding="utf-8"))
    app_config["transport"]["argv"] = [sys.executable, "app.py"]
    app_config["transport"]["env"]["PYTHONHOME"] = str(h.root / "alternate-python")
    app_config_path.write_text(json.dumps(app_config), encoding="utf-8")
    run_id = h.create(
        h.plan(dataset=h.dataset({"a": "hi"}), application=str(app_config_path))
    )
    storage, _ = h.storage()
    try:
        storage.update_run_status(run_id, "interrupted")
    finally:
        storage.db.close()

    with pytest.raises(RunError, match="could not be fully fingerprinted"):
        h.execute(run_id)
    assert h.count() == 0


def test_resume_refuses_standard_library_code_drift_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.core.models import ApplicationSpec
    from aibench.services import runs as run_service

    h = Harness(tmp_path)
    app_config_path = h.root / h.cli_app()
    app_config = json.loads(app_config_path.read_text(encoding="utf-8"))
    app_config["transport"]["argv"] = [sys.executable, "app.py"]
    app_config_path.write_text(json.dumps(app_config), encoding="utf-8")
    stdlib_root = h.root / "python-stdlib"
    stdlib_root.mkdir()
    standard_module = stdlib_root / "json.py"
    standard_module.write_text("VALUE = 'before'\n", encoding="utf-8")
    site_root = h.root / "python-site-packages"
    site_root.mkdir()
    monkeypatch.setattr(
        run_service,
        "environment_paths",
        lambda _python, **_kwargs: ([site_root], [stdlib_root], True),
    )
    environment = dict(os.environ)
    run_id = h.create(
        h.plan(dataset=h.dataset({"a": "hi"}), application=str(app_config_path)),
        environ=environment,
    )
    storage, _ = h.storage()
    try:
        record = storage.get_run(run_id)
        assert record is not None
        baseline_environment = record.manifest.parameters["application_environment_identity"]
        storage.update_run_status(run_id, "interrupted")
    finally:
        storage.db.close()

    standard_module.write_text("VALUE = 'after'\n", encoding="utf-8")
    current_environment = run_service._application_environment_identity(
        ApplicationSpec.model_validate(app_config), h.root, environment
    )
    assert current_environment != baseline_environment
    with pytest.raises(RunError, match="changed since this run was created"):
        h.execute(run_id, environ=environment)
    assert h.count() == 0


def test_resume_refuses_python_interpreter_environment_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.services import runs as run_service

    h = Harness(tmp_path)
    app_site_packages = h.root / "app-site-packages"
    test_distribution = app_site_packages / "benchmark_runtime_probe-1.0.dist-info"
    test_distribution.mkdir(parents=True)
    metadata = test_distribution / "METADATA"
    metadata.write_text(
        "Metadata-Version: 2.1\nName: benchmark-runtime-probe\nVersion: 1.0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        run_service,
        "environment_paths",
        lambda _python, **_kwargs: ([app_site_packages], [], True),
    )
    app_config = h.root / "python-app.json"
    app_config.write_text(
        json.dumps(
            {
                "application_id": "python-app",
                "runner": "python",
                "target": "app.py",
                "effects": "none",
                "transport": {
                    "kind": "python",
                    "callable": "app.py:answer",
                    "python": sys.executable,
                    "env": {"APP_LOG": str(h.log)},
                },
            }
        ),
        encoding="utf-8",
    )
    (h.root / "app.py").write_text(
        "import json, os, time\n"
        "def answer(request):\n"
        "    log = os.environ['APP_LOG']\n"
        "    with open(log + '.attempts', 'a') as stream:\n"
        "        stream.write(request['case_id'] + ' ')\n"
        "    if request['input'].startswith('slow'):\n"
        "        time.sleep(2)\n"
        "    with open(log, 'a') as stream:\n"
        "        stream.write(json.dumps({'case': request['case_id']}) + '\\n')\n"
        "    return {'output': 'yes'}\n",
        encoding="utf-8",
    )
    plan = h.plan(
        dataset=h.dataset({"first": "hi", "slow-a": "slow", "slow-b": "slow"}),
        application=str(app_config),
    )
    run_id = h.create(plan)
    storage, _ = h.storage()
    try:
        record = storage.get_run(run_id)
        assert record is not None
        identity = record.manifest.parameters["application_environment_identity"]
        assert identity["runtime"]
        assert identity["dependencies"]
    finally:
        storage.db.close()

    async def interrupt_after_first_result(ctl: RunController, harness: Harness) -> None:
        await harness.wait_for_invocations(1)
        await asyncio.sleep(0.1)
        ctl.request("interrupt")
        await asyncio.sleep(0.1)
        ctl.request("interrupt")

    outcome = h.execute(run_id, during=interrupt_after_first_result)
    assert outcome.state is RunState.INTERRUPTED
    attempts = h.log.with_suffix(".jsonl.attempts")
    attempts_before_resume = attempts.read_text(encoding="utf-8")
    assert h.count("first") == 1

    metadata.write_text(
        "Metadata-Version: 2.1\nName: benchmark-runtime-probe\nVersion: 2.0\n",
        encoding="utf-8",
    )
    with pytest.raises(RunError, match="interpreter or installed dependencies changed"):
        h.execute(run_id)
    assert attempts.read_text(encoding="utf-8") == attempts_before_resume


# --------------------------------------------------------------------------- single session


def test_a_second_session_cannot_resume_a_live_run(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(dataset=h.dataset({"a": "slow 1.0", "b": "hi"}), application=h.cli_app())
    )
    refused: dict[str, Any] = {}

    async def during(ctl: RunController, harness: Harness) -> None:
        attempts = harness.log.with_suffix(".attempts")
        while not attempts.exists():
            await asyncio.sleep(0.02)
        storage, artifacts = harness.storage()
        try:
            with pytest.raises(LeaseHeld) as info:
                await execute_run(
                    run_id, storage=storage, artifacts=artifacts, controller=RunController()
                )
            refused["message"] = str(info.value)
        finally:
            storage.db.close()

    outcome = h.execute(run_id, during=during)
    assert outcome.state is RunState.COMPLETED
    assert h.count("a") == 1 and h.count("b") == 1  # the live session's work ran once
    assert "being run by another session" in refused["message"]


def test_a_dead_sessions_lease_is_taken_over_and_its_time_recorded(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    run_id = h.create(h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app()))
    storage, _ = h.storage()
    try:
        now = time.time()
        storage.acquire_run_lease(
            run_id,
            owner="dead-session",
            host=socket.gethostname(),
            pid=2**22 + 7,  # most likely not a running process; the heartbeat is stale anyway
            now=now - 120,
            is_stale=lambda lease: True,
        )
        storage.heartbeat_run_lease(run_id, "dead-session", now - 90)
    finally:
        storage.db.close()
    assert h.execute(run_id).state is RunState.COMPLETED
    [lost] = [e for e in _events(h, run_id) if e["event_type"] == "run_session_lost"]
    assert lost["payload"]["session_elapsed_seconds"] == pytest.approx(30, abs=0.5)


# --------------------------------------------------------------------------- budgets on resume


def test_a_call_dispatched_before_a_crash_counts_against_the_hard_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    app_config_path = h.root / h.cli_app()
    app_config = json.loads(app_config_path.read_text(encoding="utf-8"))
    app_config["environment_digest"] = "test-runtime-pin"
    app_config_path.write_text(json.dumps(app_config), encoding="utf-8")
    run_id = h.create(
        h.plan(
            dataset=h.dataset({"a": "hi", "b": "hi"}),
            application=str(app_config_path),
            budgets={"max_application_calls": 2},
        )
    )

    def crash_before_commit(self: Storage, result: Any) -> Any:
        raise SimulatedCrash("before commit")

    monkeypatch.setattr(Storage, "commit_execution_attempt", crash_before_commit)
    with pytest.raises(SimulatedCrash):
        h.execute(run_id)
    monkeypatch.undo()
    outcome = h.execute(run_id)
    assert h.count() == 2  # never more than the hard limit
    assert outcome.state is RunState.BUDGET_EXHAUSTED
    assert "blocked" in outcome.counts["execution"]


def test_replayed_prior_spend_carries_tokens_known_cost_and_session_time() -> None:
    from aibench.services.runs import _replay_prior_spend

    class Recorded:
        def list_execution_attempts(self, run_id: str) -> list[Any]:
            return []

        def list_evaluation_attempts(self, run_id: str) -> list[Any]:
            resources = {"latency_ms": 1, "cost": 0.5, "tokens": {"input": 120}}
            return [SimpleNamespace(resources=resources)]

        def list_run_events(self, run_id: str) -> list[dict[str, Any]]:
            return [
                {"event_type": "run_session_ended", "payload": {"session_elapsed_seconds": 1.5}},
                {"event_type": "run_session_aborted", "payload": {"session_elapsed_seconds": 2}},
                {"event_type": "recovered", "payload": {"uncommitted_dispatches": 1}},
            ]

    ledger = BudgetLedger(BudgetLimits(max_judge_tokens=100, max_application_calls=1))
    _replay_prior_spend(Recorded(), "r", ledger)  # type: ignore[arg-type]
    assert ledger.reserve_evaluation() == "max_judge_tokens=100 reached"
    assert ledger.reserve_application() == "max_application_calls=1 reached"
    assert ledger.evaluator.known_cost == 0.5
    assert ledger.elapsed_before == 3.5  # per-session times, not cumulative totals re-summed


def test_session_time_is_not_double_counted_across_sessions(tmp_path: Path) -> None:
    from aibench.services.runs import _replay_prior_spend

    h = Harness(tmp_path)
    app_config_path = h.root / h.cli_app()
    app_config = json.loads(app_config_path.read_text(encoding="utf-8"))
    app_config["environment_digest"] = "test-runtime-pin"
    app_config_path.write_text(json.dumps(app_config), encoding="utf-8")
    run_id = h.create(
        h.plan(
            dataset=h.dataset({f"c{i}": "slow 0.5" for i in range(3)}),
            application=str(app_config_path),
        )
    )

    def interrupt_after(n: int) -> Any:
        async def during(ctl: RunController, harness: Harness) -> None:
            await harness.wait_for_invocations(n)
            ctl.interrupt()

        return during

    h.execute(run_id, during=interrupt_after(1))
    h.execute(run_id, during=interrupt_after(2))
    ends = [
        e["payload"]["budget"]["elapsed_seconds"]
        for e in _events(h, run_id)
        if e["event_type"] == "run_session_ended"
    ]
    storage, _ = h.storage()
    try:
        ledger = BudgetLedger(BudgetLimits())
        _replay_prior_spend(storage, run_id, ledger)
    finally:
        storage.db.close()
    assert ledger.elapsed_before == pytest.approx(ends[-1], abs=0.3)


# --------------------------------------------------------------------------- recovery


def test_recovery_never_retries_past_max_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(
            dataset=h.dataset({"a": "flaky 1"}),
            application=h.cli_app(timeout=1.0),
            retry={"max_attempts": 1},
        )
    )
    original = Storage.commit_execution_attempt

    def crash_after_commit(self: Storage, result: Any) -> Any:
        original(self, result)
        raise SimulatedCrash("after commit")

    monkeypatch.setattr(Storage, "commit_execution_attempt", crash_after_commit)
    with pytest.raises(SimulatedCrash):
        h.execute(run_id)
    monkeypatch.undo()
    h.execute(run_id)
    storage, _ = h.storage()
    try:
        attempts = [a.attempt_id for a in storage.list_execution_attempts(run_id)]
        item = storage.get_work_item_by_task_key(run_id, "exec:a:r0")
    finally:
        storage.db.close()
    assert attempts == [1]
    assert item is not None and item.state is WorkItemState.FAILED
    assert "retries exhausted after 1 attempts" in (item.last_error or "")


def test_editing_an_app_config_never_breaks_or_changes_existing_runs(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    first = h.create(h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app(timeout=20)))
    second = h.create(h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app(timeout=30)))
    storage, _ = h.storage()
    try:
        hashes = {r: storage.get_run(r).manifest.application_hash for r in (first, second)}  # type: ignore[union-attr]
    finally:
        storage.db.close()
    assert hashes[first] != hashes[second]
    # Each run resumes under its own frozen spec.
    assert h.execute(first).state is RunState.COMPLETED
    assert h.execute(second).state is RunState.COMPLETED


# --------------------------------------------------------------------------- run control


def test_cancel_during_retry_backoff_leaves_nothing_pending(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(
            dataset=h.dataset({"a": "flaky 1"}),
            application=h.cli_app(timeout=0.5),
            retry={
                "max_attempts": 3,
                "initial_backoff_seconds": 30,
                "max_backoff_seconds": 30,
                "jitter": 0,
            },
        )
    )

    async def during(ctl: RunController, harness: Harness) -> None:
        storage, _ = harness.storage()
        try:
            while not any(
                e["event_type"] == "retry_scheduled" for e in storage.list_run_events(run_id)
            ):
                await asyncio.sleep(0.05)
        finally:
            storage.db.close()
        ctl.cancel()

    started = time.monotonic()
    outcome = h.execute(run_id, during=during)
    assert time.monotonic() - started < 15  # did not sit out the 30 s backoff
    assert outcome.state is RunState.CANCELLED
    assert "pending" not in _states(h, run_id).values()
    assert outcome.counts["execution"] == {"cancelled": 1}


def test_a_second_interrupt_aborts_in_flight_work_but_keeps_the_run_resumable(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    app_config_path = h.root / h.cli_app()
    app_config = json.loads(app_config_path.read_text(encoding="utf-8"))
    app_config["environment_digest"] = "test-runtime-pin"
    app_config_path.write_text(json.dumps(app_config), encoding="utf-8")
    run_id = h.create(
        h.plan(
            dataset=h.dataset({"a": "slow 5", "b": "hi", "c": "hi"}),
            application=str(app_config_path),
        )
    )

    async def during(ctl: RunController, harness: Harness) -> None:
        attempts = harness.log.with_suffix(".attempts")
        while not attempts.exists():
            await asyncio.sleep(0.02)
        ctl.interrupt()
        ctl.interrupt()

    started = time.monotonic()
    first = h.execute(run_id, during=during)
    assert time.monotonic() - started < 4.5  # the 5 s call was aborted, not awaited
    assert first.state is RunState.INTERRUPTED
    assert first.counts["execution"] == {"pending": 3}  # aborted + unstarted: resumable
    second = h.execute(run_id)
    assert second.state is RunState.COMPLETED


def test_interrupt_requested_during_setup_is_honoured_before_any_dispatch(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    run_id = h.create(h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app()))
    ctl = RunController()
    ctl.request("interrupt")  # e.g. Ctrl+C while identities are verified
    outcome = h.execute(run_id, controller=ctl)
    assert outcome.state is RunState.INTERRUPTED
    assert h.count() == 0


def test_interrupt_returns_transient_evaluation_failures_to_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.services.scoring import BindingScorer

    h = Harness(tmp_path)
    run_id = h.create(h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app()))
    original = BindingScorer.score
    controllers: list[RunController] = []

    async def transient_while_interrupted(self: BindingScorer, execution: Any, cases: Any) -> Any:
        result = await original(self, execution, cases)
        controllers[0].interrupt()
        return result.model_copy(
            update={"status": ExecutionStatus.ERROR, "reason": "worker_failed:SIGINT"}
        )

    monkeypatch.setattr(BindingScorer, "score", transient_while_interrupted)
    ctl = RunController()
    controllers.append(ctl)
    outcome = h.execute(run_id, controller=ctl)
    monkeypatch.undo()
    assert outcome.state is RunState.INTERRUPTED
    assert outcome.counts["evaluation"] == {"pending": 1}  # not finalized as failed
    assert h.execute(run_id).counts["evaluation"] == {"succeeded": 1}


def test_evaluation_concurrency_cap_bounds_in_flight_evaluations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.services.scoring import BindingScorer

    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(
            dataset=h.dataset({f"c{i}": "hi" for i in range(6)}),
            application=h.cli_app(),
            concurrency={"application": 4, "evaluation": 2},
        )
    )
    original = BindingScorer.score
    live = {"now": 0, "max": 0}

    async def slow_score(self: BindingScorer, execution: Any, cases: Any) -> Any:
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        try:
            await asyncio.sleep(0.15)
            return await original(self, execution, cases)
        finally:
            live["now"] -= 1

    monkeypatch.setattr(BindingScorer, "score", slow_score)
    assert h.execute(run_id).state is RunState.COMPLETED
    assert live["max"] == 2


# --------------------------------------------------------------------------- policy


def test_plugin_import_paths_need_policy_approval_and_nothing_loads_when_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.registry import EvaluatorRegistry

    loads: list[Any] = []
    monkeypatch.setattr(
        EvaluatorRegistry, "load_plugin_environment", lambda self, *a, **k: loads.append(a)
    )
    python = sys.executable  # a real interpreter the policy allows
    policy = ExecutionPolicy(allowed_plugin_environments=(python,))
    plan = ExecutablePlan(
        plan_id="p",
        dataset="d",
        application="a",
        plugin_environments=(PluginEnvironmentRef(python=python, paths=("elsewhere/evil",)),),
    )
    assert plan_denials(policy, plan, tmp_path) == [
        "plugin path elsewhere/evil is not allowed by the policy"
    ]
    allowed = policy.model_copy(
        update={"allowed_plugin_paths": (str(tmp_path / "elsewhere/evil"),)}
    )
    assert plan_denials(allowed, plan, tmp_path) == []

    h = Harness(tmp_path)
    plan_path = h.plan(
        dataset=h.dataset({"a": "hi"}),
        application=h.cli_app(),
        plugin_environments=[{"python": python, "paths": ["elsewhere/evil"]}],
    )
    with pytest.raises(PolicyDenied, match="plugin path"):
        compile_plan(plan_path, policy=policy, trusted_local=True)
    assert loads == []  # the allowed interpreter never started


def test_data_roots_scope_the_plans_data(tmp_path: Path) -> None:
    from aibench.engine.compile import load_policy

    h = Harness(tmp_path / "project")
    plan = h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app())
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({"data_roots": ["somewhere-else"]}), encoding="utf-8")
    with pytest.raises(PolicyDenied) as info:
        compile_plan(plan, policy=load_policy(policy_file), trusted_local=True)
    assert sorted(info.value.denials) == [
        "application config app.json is outside the policy's data_roots",
        "dataset data.jsonl is outside the policy's data_roots",
    ]
    policy_file.write_text(json.dumps({"data_roots": ["project"]}), encoding="utf-8")
    compile_plan(plan, policy=load_policy(policy_file), trusted_local=True)  # relative to file
