"""Optional plugins from the user's side: `/plugins` and `aibench plugins install`, the
policy and config they change, and a chat session that then plans and scores with DeepEval.

Installs adopt the existing DeepEval plugin environment (`--use-env`), so no package is
downloaded; creating a fresh environment runs the same code after a `venv` + `pip install`."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from typer.testing import CliRunner

from aibench.cli.main import app as cli_app
from aibench.core.models import EvaluatorManifest, MetricDirection
from aibench.core.sessions import PlanPatch
from aibench.planning.catalog import concepts_in, with_default_params
from aibench.planning.openai_provider import OpenAICompatibleConfig, OpenAICompatibleProvider
from aibench.quickstart import create_project
from aibench.services.plugins import (
    PluginInstallError,
    judge_from_provider,
    plan_install,
    plugin_status,
    session_plugins,
)
from aibench.sessions.controller import SessionController
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from aibench.tui.app import ChatApp
from tests.deepeval_support import PLUGIN_ENV, requires_plugin_env
from tests.test_deepeval_metrics import _judge_server

JUDGE, SECRETS = judge_from_provider("https://api.z.ai/api/paas/v4", "glm-4.6", "env:ZAI_API_KEY")


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "support-bench"
    create_project(root, python=sys.executable)
    return root


def _files(root: Path) -> dict[str, str]:
    return {
        p.name: p.read_text(encoding="utf-8") for p in (root / "aibench.json", root / "policy.json")
    }


# --------------------------------------------------------------------------- planning


@pytest.mark.parametrize(
    ("text", "concepts"),
    [
        ("answers must be relevant", ("relevancy",)),
        ("check contextual relevancy of retrieval", ("retrieval_relevancy",)),
        ("answers must be polite and not toxic", ("toxicity", "custom_criteria")),
        ("no bias please", ("bias",)),
        ("must not leak personal data", ("privacy",)),
        ("no hallucinations", ("groundedness",)),
        ("don't care about bias", ()),
        ("check summarization quality", ("summarization",)),
        ("summaries stay faithful to the source", ("groundedness",)),
        ("only use allowed tools", ("tool_use", "tool_permissions")),
    ],
)
def test_objectives_name_the_new_concepts(text: str, concepts: tuple[str, ...]) -> None:
    assert concepts_in(text) == concepts


def _manifest(evaluator_id: str, properties: dict[str, Any]) -> EvaluatorManifest:
    return EvaluatorManifest(
        evaluator_id=evaluator_id,
        version="1.0.0",
        plugin_id="p",
        plugin_version="1",
        description="d",
        value_kind="scalar",
        direction=MetricDirection.HIGHER,
        aggregation="mean",
        parameters_schema={"type": "object", "properties": properties},
    )


def test_default_params_fill_only_what_a_metric_accepts_and_never_override_the_user() -> None:
    manifests = [
        _manifest("deepeval.bias", {"judge": {}}),
        _manifest("deepeval.exact_match", {}),  # takes no judge
        _manifest("deepeval.g_eval", {"judge": {}, "criteria": {}}),
        _manifest("native.exact_match", {"judge": {}}),  # not matched by the pattern
    ]
    user = {"deepeval.g_eval": {"criteria": "polite", "judge": {"kind": "mine"}}}
    merged = with_default_params(manifests, {"deepeval.*": {"judge": JUDGE}}, user)
    assert merged == {
        "deepeval.bias": {"judge": JUDGE},
        "deepeval.g_eval": {"criteria": "polite", "judge": {"kind": "mine"}},
    }


def test_a_default_for_one_metric_wins_over_the_wildcard_in_any_order() -> None:
    """A judge that does not think suits the retrieval metrics (a thinking one ran past 32,768
    tokens judging 16 passages, and scored one case 0.00 then 0.84), so a project gives
    contextual precision its own judge. Written before `deepeval.*`, it was overwritten by the
    general judge: defaults were applied in file order."""
    manifests = [
        _manifest("deepeval.contextual_precision", {"judge": {}}),
        _manifest("deepeval.bias", {"judge": {}}),
    ]
    quick = {"kind": "openai_compatible", "model": "glm-4.6", "thinking": "disabled"}
    for defaults in (
        {"deepeval.contextual_precision": {"judge": quick}, "deepeval.*": {"judge": JUDGE}},
        {"deepeval.*": {"judge": JUDGE}, "deepeval.contextual_precision": {"judge": quick}},
    ):
        merged = with_default_params(manifests, defaults, {})
        assert merged["deepeval.contextual_precision"] == {"judge": quick}
        assert merged["deepeval.bias"] == {"judge": JUDGE}


# --------------------------------------------------------------------------- install


def test_plan_install_previews_every_change_and_writes_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path)
    before = _files(root)
    plan = plan_install(
        "deepeval", root, policy_path=root / "policy.json", judge=JUDGE, secret_env=SECRETS
    )
    summary = plan.summary()
    assert summary["creates_environment"] is True
    assert summary["environment"].endswith(
        str(
            Path(".aibench/plugins/deepeval/venv")
            / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        )
    )
    assert summary["judge"] == "glm-4.6 at https://api.z.ai/api/paas/v4"
    assert summary["secret_env"] == {"AIBENCH_JUDGE_KEY": "env:ZAI_API_KEY"}
    assert summary["policy_changes"] == [
        "allowed_plugin_environments: + .aibench/plugins/deepeval/venv/"
        + ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"),
        "allowed_evaluators: + deepeval.*",
        "allow_model_evaluators: false -> true (its metrics call a judge)",
        "allowed_secret_refs: + env:ZAI_API_KEY",
    ]
    assert len(summary["metrics"]) == 40
    assert _files(root) == before and not (root / "policy.json.bak").exists()


def test_installing_again_keeps_the_judge_the_project_already_uses(tmp_path: Path) -> None:
    """Upgrading the plugin swapped a paid glm-4.7-flashx judge for the assistant's free
    glm-4.5-flash, silently. An existing judge (and its key reference) stays."""
    root = _project(tmp_path)
    mine, mine_secrets = judge_from_provider(
        "https://api.z.ai/api/paas/v4", "glm-4.5-air", "env:MY_JUDGE_KEY"
    )
    config = json.loads((root / "aibench.json").read_text(encoding="utf-8"))
    config["plugin_environments"] = [
        {
            "name": "deepeval",
            "python": ".aibench/plugins/deepeval/venv/python",
            "secret_env": mine_secrets,
            "default_params": {"deepeval.*": {"judge": mine}},
        }
    ]
    (root / "aibench.json").write_text(json.dumps(config), encoding="utf-8")

    plan = plan_install(
        "deepeval", root, policy_path=root / "policy.json", judge=JUDGE, secret_env=SECRETS
    )
    assert plan.judge_kept is True
    assert plan.summary()["judge"] == "glm-4.5-air at https://api.z.ai/api/paas/v4"
    [entry] = plan.config["plugin_environments"]
    assert entry["default_params"] == {"deepeval.*": {"judge": mine}}
    assert entry["secret_env"]["AIBENCH_JUDGE_KEY"] == "env:MY_JUDGE_KEY"  # theirs wins

    # With no assistant model offered at all, an existing judge is enough to reinstall.
    again = plan_install("deepeval", root, policy_path=root / "policy.json")
    assert again.judge_kept is True and again.judge == mine

    # A first install still needs a judge from somewhere.
    fresh = _project(tmp_path / "fresh")
    first = plan_install(
        "deepeval", fresh, policy_path=fresh / "policy.json", judge=JUDGE, secret_env=SECRETS
    )
    assert first.judge_kept is False


@pytest.mark.parametrize(
    ("name", "judge", "problem"),
    [
        ("promptfoo", JUDGE, "no optional plugin 'promptfoo'"),
        ("deepeval", None, "judged by a model"),
    ],
)
def test_plan_install_refuses_what_it_cannot_do(
    tmp_path: Path, name: str, judge: dict[str, Any] | None, problem: str
) -> None:
    root = _project(tmp_path)
    with pytest.raises(PluginInstallError, match=problem):
        plan_install(name, root, policy_path=root / "policy.json", judge=judge)


def test_an_environment_without_the_plugin_changes_no_file(tmp_path: Path) -> None:
    from aibench.services.plugins import install

    root = _project(tmp_path)
    before = _files(root)
    plan = plan_install(
        "deepeval",
        root,
        policy_path=root / "policy.json",
        judge=JUDGE,
        secret_env=SECRETS,
        existing_python=Path(sys.executable),  # the core environment: no aibench-deepeval
    )
    with pytest.raises(PluginInstallError, match="has no deepeval"):
        install(plan, progress=lambda _line: None)
    assert _files(root) == before


@requires_plugin_env
def test_cli_install_writes_config_and_policy_with_a_backup(tmp_path: Path) -> None:
    root = _project(tmp_path)
    provider = root / "zai.provider.json"
    provider.write_text(
        json.dumps(
            {
                "kind": "openai_compatible",
                "base_url": "https://api.z.ai/api/paas/v4",
                "model": "glm-4.6",
                "api_key": "env:ZAI_API_KEY",
            }
        ),
        encoding="utf-8",
    )
    original_policy = (root / "policy.json").read_text(encoding="utf-8")
    result = CliRunner().invoke(
        cli_app,
        [
            "plugins",
            "install",
            "deepeval",
            "--project",
            str(root),
            "--judge-provider",
            str(provider),
            "--use-env",
            str(PLUGIN_ENV),
            "--yes",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    done = json.loads(result.stdout)
    assert len(done["evaluators"]) == 40 and "deepeval.knowledge_retention" in done["evaluators"]
    assert (root / "policy.json.bak").read_text(encoding="utf-8") == original_policy
    [entry] = json.loads((root / "aibench.json").read_text(encoding="utf-8"))["plugin_environments"]
    assert entry["name"] == "deepeval" and entry["secret_env"] == SECRETS
    assert entry["default_params"] == {"deepeval.*": {"judge": JUDGE}}
    from aibench.engine.compile import load_policy

    [row] = [
        r for r in plugin_status(root, load_policy(root / "policy.json")) if r["name"] == "deepeval"
    ]
    assert (row["state"], row["missing"]) == ("installed", [])
    environments, defaults = session_plugins(root)
    assert [e.python for e in environments] == [str(PLUGIN_ENV.resolve())]
    assert defaults == {"deepeval.*": {"judge": JUDGE}}


# --------------------------------------------------------------------------- chat, end to end


def _controller(root: Path) -> SessionController:
    workspace = Workspace.at(root)
    storage = Storage(Database.open_workspace(workspace))
    environments, defaults = session_plugins(root)
    return SessionController.create(
        storage=storage,
        artifacts=ArtifactStore(workspace.artifacts_dir),
        workspace_root=workspace.root,
        project_root=root,
        application=root / "support.app.json",
        dataset=root / "dataset.jsonl",
        policy_path=root / "policy.json",
        trusted_local=True,
        plugin_environments=environments,
        evaluator_defaults=defaults,
    )


@requires_plugin_env
def test_chat_installs_deepeval_then_plans_and_scores_with_the_assistants_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole journey in the real prompt loop: /plugins shows DeepEval is missing;
    /plugins install previews and changes nothing; --yes installs it into the open session;
    an objective about relevancy now plans DeepEval answer relevancy with the assistant's
    own model as judge; the run is scored by the real DeepEval worker through that judge."""
    monkeypatch.setenv("AIBENCH_TEST_JUDGE_KEY", "sk-test-judge-secret-123456")
    root = _project(tmp_path)
    ctl = _controller(root)
    output = io.StringIO()
    with _judge_server() as server:
        provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(
                base_url=server.base_url, model="glm-test", api_key="env:AIBENCH_TEST_JUDGE_KEY"
            )
        )
        chat = ChatApp(
            ctl,
            provider=provider,
            console=Console(file=output, width=120, highlight=False),
            output=DummyOutput(),
        )
        before = _files(root)
        use_env = f'--use-env "{PLUGIN_ENV}"'

        async def scenario() -> None:
            with create_pipe_input() as pipe:
                chat.input = pipe
                task = asyncio.create_task(chat.run())
                pipe.send_text("/plugins\r")
                pipe.send_text(f"/plugins install deepeval {use_env}\r")
                await _until(lambda: "Nothing has changed yet" in output.getvalue())
                assert _files(root) == before
                pipe.send_text(f"/plugins install deepeval {use_env} --yes\r")
                await _until(lambda: "deepeval installed" in output.getvalue(), timeout=240)
                pipe.send_text("/exit\r")
                await asyncio.wait_for(task, timeout=30)

        try:
            asyncio.run(scenario())
            shown = output.getvalue()
            assert (
                "deepeval not installed" in shown and "enable: /plugins install deepeval" in shown
            )
            assert "allow_model_evaluators: false -> true" in shown
            assert "judge: glm-test at " + server.base_url in shown

            reopened = SessionController(
                ctl.session_id,
                storage=ctl.storage,
                artifacts=ctl.artifacts,
                workspace_root=ctl.workspace_root,
            )
            result = reopened.apply_patch(
                PlanPatch(add_objectives=("answers are relevant",)),
                expected_revision=reopened.session.revision,
            )
            assert result.status == "applied", result.problems
            draft = reopened.state()["draft"]
            assert [m["metric"] for m in draft["metrics"]] == ["deepeval.answer_relevancy@1.0.0"]
            assert draft["executable"], draft

            async def run() -> str:
                action = await reopened.start_run(
                    action_id="act-1", expected_revision=reopened.session.revision
                )
                assert action.state.value == "done", action
                await reopened.wait_for_run(action.run_id)
                return str(action.run_id)

            run_id = asyncio.run(run())
            report = reopened.report_facts(run_id)
            [metric] = report["metrics"]
            assert metric["metric"].startswith("deepeval.answer_relevancy")
            # support-010 is the fixture's crashing case: 9 answers are scored by the judge.
            assert metric["completed"] == 9 and metric["decisions"].get("pass") == 9
            assert server.requests and {r["body"]["model"] for r in server.requests} == {"glm-test"}
        finally:
            provider.close()
            ctl.storage.db.close()


async def _until(predicate: Any, *, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.05)
