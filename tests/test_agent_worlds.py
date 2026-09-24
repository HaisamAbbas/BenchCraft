"""Stateful outcomes and isolated test worlds (15-T2; gates 15-G2, 15-G3).

End to end through `aibench run` against the booking test world
(`examples/apps/booking_world.py`, a test double: nothing real is booked). The copies of
`examples/agent_world` point at the port the fixture server got; everything else runs as
shipped. State reset and episode rules are asserted from what the server's own world
reported after each invocation and from the run's reset events."""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.engine.compile import PlanInvalid, PolicyDenied, compile_plan
from aibench.engine.engine import RunController, RunState
from aibench.security.policy import ExecutionPolicy
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

REPO = Path(__file__).resolve().parents[1]
cli = CliRunner()


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[Path, Any]]:
    """A copy of examples/agent_world pointed at a fresh booking server."""
    from tests.runner_support import load_example

    server = load_example("booking_world").make_server(port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    project = tmp_path / "agent_world"
    shutil.copytree(REPO / "examples" / "agent_world", project)
    base = f"http://127.0.0.1:{server.server_port}"
    for name in ("booking.app.json", "booking.episodes.app.json"):
        path = project / name
        text = path.read_text(encoding="utf-8").replace("http://127.0.0.1:8768", base)
        path.write_text(text, encoding="utf-8")
    try:
        yield project, server
    finally:
        server.shutdown()
        server.server_close()


def _run(project: Path, plan: str, code: int) -> str:
    result = cli.invoke(
        app,
        ["run", "--plan", str(project / plan), "--policy", str(project / "policy.json"),
         "--workspace", str(project), "--json"],
    )  # fmt: skip
    assert result.exit_code == code, result.output
    return json.loads(result.stdout)["run_id"]


def _storage(project: Path) -> Storage:
    return Storage(Database.open_workspace(Workspace.at(project)))


def _results(project: Path, run_id: str) -> dict[str, dict[str, Any]]:
    storage = _storage(project)
    try:
        table: dict[str, dict[str, Any]] = {}
        for r in storage.list_metric_results(run_id):
            value = r.value.value if r.value else None
            table.setdefault(r.case_id, {})[r.metric_id.split(".", 1)[1]] = value
        return table
    finally:
        storage.db.close()


def _world_after(project: Path, run_id: str) -> dict[str, Any]:
    storage = _storage(project)
    try:
        return {e.case_id: e.world_state for e in storage.list_execution_attempts(run_id)}
    finally:
        storage.db.close()


def _events(project: Path, run_id: str, kind: str) -> list[dict[str, Any]]:
    storage = _storage(project)
    try:
        return [e["payload"] for e in storage.list_run_events(run_id) if e["event_type"] == kind]
    finally:
        storage.db.close()


def _items(project: Path, run_id: str) -> dict[str, tuple[str, str | None]]:
    storage = _storage(project)
    try:
        return {
            w.task_key: (w.state.value, w.last_error)
            for w in storage.list_work_items(run_id)
            if w.kind == "execution"
        }
    finally:
        storage.db.close()


# --------------------------------------------------------------------------- 15-G3


def test_correct_tool_names_never_mask_a_failed_outcome(world: tuple[Path, Any]) -> None:
    project, _server = world
    # The final-state gate fails (3/5): exit code 1.
    run_id = _run(project, "plan.json", code=1)
    results = _results(project, run_id)
    assert results == {
        "book-by-code": {"tool_calls": True, "tool_outcomes": "succeeded", "final_state": True},
        "book-by-destination": {
            "tool_calls": True,
            "tool_outcomes": "succeeded",
            "final_state": True,
        },
        # The agent called book_flight and said "Booked", but nothing was booked:
        "code-as-written": {
            "tool_calls": True,
            "tool_outcomes": "argument_violation",
            "final_state": False,
        },
        "sold-out": {"tool_calls": True, "tool_outcomes": "failed", "final_state": False},
        # The booking landed, but the agent attempted a tool it was not allowed to use:
        "unasked-email": {
            "tool_calls": True,
            "tool_outcomes": "unauthorized_attempt",
            "final_state": True,
        },
    }
    report = cli.invoke(
        app, ["report", run_id, "--format", "markdown", "--out", "-", "--workspace", str(project)]
    )
    assert report.exit_code == 0
    assert "native.final\\_state" in report.stdout and "native.tool\\_calls" in report.stdout
    # The report records how state was reset and which frozen world the run used.
    assert "reset before every case (reset\\_url); test world two-seats (seed sha256:" in (
        report.stdout
    )
    assert "resets: reset 5" in report.stdout


# --------------------------------------------------------------------------- 15-G2


def test_state_resets_between_independent_cases(world: tuple[Path, Any]) -> None:
    project, server = world
    run_id = _run(project, "plan.json", code=1)
    after = _world_after(project, run_id)
    # Every successful booking case starts from the seed: exactly one booking each.
    for case_id in ("book-by-code", "book-by-destination", "unasked-email"):
        assert len(after[case_id]["bookings"]) == 1, case_id
    resets = _events(project, run_id, "app_reset")
    assert len(resets) == 5 and {r["status"] for r in resets} == {"reset"}
    assert {r["world"] for r in resets} == {"two-seats"}
    assert server.calls["/reset"] == 5 and server.calls["/agent"] == 5


def test_without_resets_state_leaks_between_cases(world: tuple[Path, Any]) -> None:
    """The contrast that makes the reset meaningful: a shared application keeps state."""
    project, _ = world
    config = json.loads((project / "booking.app.json").read_text(encoding="utf-8"))
    config["reset_policy"] = "shared"
    (project / "booking.app.json").write_text(json.dumps(config), encoding="utf-8")
    plan = json.loads((project / "plan.json").read_text(encoding="utf-8"))
    del plan["test_world"]
    (project / "shared.plan.json").write_text(json.dumps(plan), encoding="utf-8")
    run_id = _run(project, "shared.plan.json", code=1)
    after = _world_after(project, run_id)
    assert len(after["book-by-destination"]["bookings"]) == 2  # sees the earlier case's booking
    assert _events(project, run_id, "app_reset") == []
    report = cli.invoke(
        app, ["report", run_id, "--format", "json", "--out", "-", "--workspace", str(project)]
    )
    state = json.loads(report.stdout)["application"]["state"]
    assert (state["reset_mode"], state["test_world"], state["resets"]) == ("none", None, {})


def test_state_is_kept_within_an_episode_and_reset_between_episodes(
    world: tuple[Path, Any],
) -> None:
    project, _server = world
    run_id = _run(project, "episodes.plan.json", code=0)
    after = _world_after(project, run_id)
    assert [len(after[c]["bookings"]) for c in ("trip1-ada", "trip1-grace")] == [1, 2]
    assert [after[c]["flights"]["BA117"]["seats"] for c in ("trip1-ada", "trip1-grace")] == [1, 0]
    # trip-2 starts from the seed again, then its second turn cancels its own booking.
    assert len(after["trip2-ada"]["bookings"]) == 1
    assert len(after["trip2-cancel"]["bookings"]) == 0
    resets = _events(project, run_id, "app_reset")
    assert [(r["task_key"], r["episode"]) for r in resets] == [
        ("exec:trip1-ada:r0", "trip-1"),
        ("exec:trip2-ada:r0", "trip-2"),
    ]
    assert {_results(project, run_id)[c]["final_state"] for c in after} == {True}


def test_a_failed_turn_blocks_the_rest_of_its_episode(world: tuple[Path, Any]) -> None:
    project, server = world
    lines = (project / "episodes.jsonl").read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["input"] = "crash while booking"
    (project / "episodes.jsonl").write_text(
        "\n".join([json.dumps(first), *lines[1:]]) + "\n", encoding="utf-8"
    )
    run_id = _run(project, "episodes.plan.json", code=3)
    items = _items(project, run_id)
    assert items["exec:trip1-ada:r0"][0] == "failed"
    state, reason = items["exec:trip1-grace:r0"]
    assert state == "blocked" and reason.startswith("episode_broken")
    assert items["exec:trip2-ada:r0"][0] == items["exec:trip2-cancel:r0"][0] == "succeeded"
    assert server.calls["/agent"] == 3  # the blocked turn was never sent


def test_an_interrupted_episode_is_not_continued_on_resume(world: tuple[Path, Any]) -> None:
    from tests.engine_support import Harness

    project, server = world
    policy = ExecutionPolicy.model_validate_json((project / "policy.json").read_text())
    h = Harness(project / "h")
    run_id = h.create(project / "episodes.plan.json", policy=policy.resolved_against(project))

    async def interrupt_after_first_turn(ctl: RunController, harness: Any) -> None:
        import asyncio

        while server.calls["/agent"] < 1:
            await asyncio.sleep(0.01)
        ctl.request("interrupt")

    assert h.execute(run_id, during=interrupt_after_first_turn).state is RunState.INTERRUPTED
    assert h.execute(run_id).state is RunState.COMPLETED
    storage = Storage(Database.open_workspace(h.workspace))
    try:
        items = {w.task_key: w for w in storage.list_work_items(run_id) if w.kind == "execution"}
    finally:
        storage.db.close()
    blocked = items["exec:trip1-grace:r0"]
    assert blocked.state.value == "blocked"
    assert (blocked.last_error or "").startswith("episode_interrupted")
    assert items["exec:trip2-cancel:r0"].state.value == "succeeded"


def test_a_failed_reset_blocks_the_case_without_calling_the_app(
    world: tuple[Path, Any],
) -> None:
    project, server = world
    config = json.loads((project / "booking.app.json").read_text(encoding="utf-8"))
    config["transport"]["reset_url"] = config["transport"]["reset_url"].replace("/reset", "/nope")
    (project / "booking.app.json").write_text(json.dumps(config), encoding="utf-8")
    run_id = _run(project, "plan.json", code=3)
    items = _items(project, run_id)
    assert {s for s, _ in items.values()} == {"blocked"}
    assert all(reason.startswith("reset_failed") for _, reason in items.values())
    assert server.calls["/agent"] == 0


# --------------------------------------------------------------------------- compile rules


def _compile(project: Path, plan: dict[str, Any], **policy: Any) -> Any:
    base = json.loads((project / "policy.json").read_text(encoding="utf-8"))
    path = project / "variant.plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return compile_plan(
        path, policy=ExecutionPolicy.model_validate({**base, **policy}).resolved_against(project)
    )


def test_test_world_rules_are_enforced_before_anything_runs(world: tuple[Path, Any]) -> None:
    project, server = world
    plan = json.loads((project / "plan.json").read_text(encoding="utf-8"))
    compiled = _compile(project, plan)
    assert compiled.world.world_id == "two-seats" and compiled.world.seed["allow_email"] is False

    with pytest.raises(PlanInvalid, match="not declared"):
        _compile(project, {**plan, "test_world": "production"})
    with pytest.raises(PolicyDenied, match="email-allowed is not approved"):
        _compile(project, {**plan, "test_world": "email-allowed"})
    with pytest.raises(PlanInvalid, match="Set concurrency.application to 1"):
        _compile(project, {**plan, "concurrency": {"application": 2, "evaluation": 2}})
    episodes = json.loads((project / "episodes.plan.json").read_text(encoding="utf-8"))
    with pytest.raises(PlanInvalid, match="cannot be retried"):
        _compile(project, {**episodes, "retry": {"max_attempts": 2}})
    assert sum(server.calls.values()) == 0  # compiling never calls the application


def test_a_test_world_needs_a_reset_hook(world: tuple[Path, Any]) -> None:
    project, _ = world
    config = json.loads((project / "booking.app.json").read_text(encoding="utf-8"))
    del config["transport"]["reset_url"]
    (project / "booking.app.json").write_text(json.dumps(config), encoding="utf-8")
    plan = json.loads((project / "plan.json").read_text(encoding="utf-8"))
    with pytest.raises(PlanInvalid, match="loaded through a reset hook"):
        _compile(project, plan)


def test_final_state_needs_the_world_state_to_be_observable(world: tuple[Path, Any]) -> None:
    project, _ = world
    config = json.loads((project / "booking.app.json").read_text(encoding="utf-8"))
    del config["output_binding"]["world_state"]
    (project / "booking.app.json").write_text(json.dumps(config), encoding="utf-8")
    plan = json.loads((project / "plan.json").read_text(encoding="utf-8"))
    with pytest.raises(PlanInvalid, match="world_state is not declared"):
        _compile(project, plan)


def test_the_seed_is_frozen_with_the_run(world: tuple[Path, Any]) -> None:
    from aibench.services.runs import RunError
    from tests.engine_support import Harness

    project, server = world
    policy = ExecutionPolicy.model_validate_json((project / "policy.json").read_text())
    h = Harness(project / "h")
    run_id = h.create(project / "plan.json", policy=policy.resolved_against(project))
    # Editing the world file after the run was created changes nothing it runs with...
    seed_file = project / "worlds" / "two-seats.json"
    seed_file.write_text(json.dumps({"flights": {}, "bookings": []}), encoding="utf-8")
    storage = Storage(Database.open_workspace(h.workspace))
    try:
        world_params = storage.get_run(run_id).manifest.parameters["test_world"]
        ref = storage.get_artifact(world_params["seed_artifact_id"])
    finally:
        storage.db.close()
    # ...and tampering with the frozen seed itself is refused.
    Path(ref.uri).write_bytes(b'{"flights": {}}')
    with pytest.raises(RunError, match="test world seed"):
        h.execute(run_id)
    assert server.calls["/agent"] == 0
