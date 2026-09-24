"""Exposure boundaries (17-T4): integrations are discoverable in the CLI, planning and chat
with their exact supported modes and data destinations, and stay unavailable (with the
reason) where a plugin, an approval or a credential is missing. Nothing here starts plugin
code or contacts a service.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.conversation.agent import ConversationAgent
from aibench.tui.commands import Commands
from tests.session_support import ScriptedProvider, SessionHarness, call, say

REPO = Path(__file__).resolve().parents[1]
_BIN = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
API_ENV = REPO / "plugins" / "openai_evals_api" / ".venv" / _BIN
OSS_ENV = REPO / "plugins" / "openai_evals_oss" / ".venv" / _BIN
needs_envs = pytest.mark.skipif(
    not (API_ENV.is_file() and OSS_ENV.is_file()), reason="both OpenAI plugin environments"
)
cli = CliRunner()
APPROVING = {
    "allowed_evaluators": ["native.*", "openai_evals_oss.*", "openai_evals_api.*"],
    "allowed_plugin_environments": [str(OSS_ENV), str(API_ENV)],
    "allowed_egress_origins": ["https://api.openai.com", "https://langfuse.example"],
    "allowed_secret_refs": [
        "env:OPENAI_API_KEY", "env:LANGFUSE_PUBLIC_KEY", "env:LANGFUSE_SECRET_KEY",
    ],
}  # fmt: skip


def _list(tmp_path: Path, policy: dict[str, Any] | None, *extra: str) -> dict[str, Any]:
    args = ["integrations", "list", "--json", *extra]
    if policy is not None:
        (tmp_path / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
        args += ["--policy", str(tmp_path / "policy.json")]
    result = cli.invoke(app, args)
    assert result.exit_code == 0, result.output
    return {i["id"]: i for i in json.loads(result.stdout)["integrations"]}


def test_without_a_policy_nothing_is_available_and_modes_are_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    found = _list(tmp_path, None)
    assert set(found) == {"openai_evals_oss", "openai_evals_api", "langfuse"}
    assert all(not i["status"]["available"] for i in found.values())
    api = found["openai_evals_api"]
    modes = {m["mode"]: m["supported"] for m in api["modes"]}
    assert modes == {"stored_output_grading": True, "model_generation": False}
    assert api["data_destinations"][0]["url"] == "https://api.openai.com/v1"
    assert "credential env:OPENAI_API_KEY is not set" in api["status"]["reasons"]
    assert found["openai_evals_oss"]["data_destinations"] == []
    assert {m["mode"] for m in found["langfuse"]["modes"]} == {
        "import-dataset", "import-traces", "export-scores",
    }  # fmt: skip
    assert all("not verified against the live service" in found[k]["live_verification"]
               for k in ("openai_evals_api", "langfuse"))  # fmt: skip


@needs_envs
def test_availability_follows_the_policy_and_the_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("OPENAI_API_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.setenv(name, "set-for-the-test")
    found = _list(tmp_path, APPROVING, "--langfuse-host", "https://langfuse.example")
    assert {k: i["status"]["available"] for k, i in found.items()} == {
        "openai_evals_oss": True, "openai_evals_api": True, "langfuse": True,
    }  # fmt: skip
    assert found["openai_evals_api"]["plugin_environment"] == str(API_ENV)

    # A missing credential keeps the hosted bridge unavailable, with the reason.
    monkeypatch.delenv("OPENAI_API_KEY")
    found = _list(tmp_path, APPROVING, "--langfuse-host", "https://langfuse.example")
    api = found["openai_evals_api"]["status"]
    assert api == {"available": False, "reasons": ["credential env:OPENAI_API_KEY is not set"]}
    # An unapproved destination does too; the OSS bridge sends nothing and is unaffected.
    found = _list(tmp_path, {**APPROVING, "allowed_egress_origins": []})
    assert any(
        "allowed_egress_origins" in r for r in found["openai_evals_api"]["status"]["reasons"]
    )
    assert found["openai_evals_oss"]["status"]["available"] is True
    assert (
        "no Langfuse host configured (LANGFUSE_HOST or --host)"
        in found["langfuse"]["status"]["reasons"]
    )


@needs_envs
def test_planning_sees_modes_and_destinations_and_never_plans_a_remote_job(
    tmp_path: Path,
) -> None:
    from aibench.inspection.dataset_summary import summarize_dataset
    from aibench.inspection.profile import inspect_application
    from aibench.planning.catalog import build_catalog
    from aibench.registry import EvaluatorRegistry
    from aibench.security.policy import ExecutionPolicy

    registry = EvaluatorRegistry.with_native()
    registry.load_plugin_environment(OSS_ENV)
    registry.load_plugin_environment(API_ENV)
    (tmp_path / "data.jsonl").write_text(
        json.dumps({"case_id": "a", "input": "hi", "expected_output": "hi"}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "app.json").write_text(
        json.dumps({"application_id": "demo", "runner": "http", "target": "http://127.0.0.1:9/",
                    "transport": {"kind": "http", "url": "http://127.0.0.1:9/"}}),
        encoding="utf-8",
    )  # fmt: skip
    catalog = build_catalog(
        registry,
        inspect_application(tmp_path / "app.json"),
        summarize_dataset(tmp_path / "data.jsonl"),
        ExecutionPolicy(**{k: v for k, v in APPROVING.items() if k != "allowed_egress_origins"}),
    )
    options = {o.evaluator_id: o.as_dict() for o in catalog}
    remote = options["openai_evals_api.criterion"]
    assert remote["consumes"] == "remote_job" and remote["eligible"] is False
    assert any("remote job" in r for r in remote["reasons"])
    assert remote["network_destinations"]
    replay = options["openai_evals_oss.match"]
    assert replay["consumes"] == "recorded_outputs" and replay["network_destinations"] == []


def test_chat_lists_integrations_without_claiming_unavailable_ones_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "hi"}, policy={"allow_trusted_local": True})
    provider = ScriptedProvider([call("list_integrations"), say("Here is what is available.")])
    outcome = asyncio.run(ConversationAgent(ctl, provider).handle_message("what can connect?"))
    assert {"tool": "list_integrations", "subject": "integrations"} in outcome.explained
    # What the assistant saw: every integration unavailable, each with its reasons.
    tool_result = json.loads(provider.calls[-1][-1]["content"])
    assert {i["id"]: i["status"]["available"] for i in tool_result} == {
        "openai_evals_oss": False, "openai_evals_api": False, "langfuse": False,
    }  # fmt: skip

    result = asyncio.run(Commands(ctl).run("/integrations"))
    assert result.ok and result.kind == "integrations"
    assert [i["id"] for i in result.data["integrations"]] == [
        "openai_evals_oss", "openai_evals_api", "langfuse",
    ]  # fmt: skip
    ctl.storage.db.close()
