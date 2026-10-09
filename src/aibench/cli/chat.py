"""`aibench chat` and bare `aibench` (§3, §13, 09-T1): the conversational session.

- In a terminal, `aibench` (or `aibench chat --project PATH`) opens the project's session.
  With existing sessions it shows a chooser rather than silently resuming one; `--resume
  SESSION_ID` and `--new` choose directly. Reopening restores the conversation and shows
  the actual run state; it never restarts work.
- The application and dataset come from the project config (`aibench.json`,
  `aibench.yaml`, `config.json` or `config.yaml` in the project: `application_target`,
  `dataset_path`, `policy_path`, `plan_path`), overridden by `--app`, `--dataset` and
  `--policy`.
- `--provider-config` names the assistant model (the planning role, §2). It is refused,
  with the reasons shown, unless the policy permits the endpoint; the session still opens
  and every slash command works without it.
- Without a terminal, `chat` needs `--send TEXT`: one message or slash command, answered
  non-interactively (`--json` for machine output). A run or controlled experiment it starts
  is followed to its end.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from pydantic import ValidationError as PydanticValidationError
from rich.console import Console

from aibench.config.resolve import load_mapping_file, resolve_config
from aibench.core.errors import AibenchError, ConfigError
from aibench.core.models import deep_unfreeze
from aibench.engine.compile import load_policy
from aibench.inspection.candidates import (
    RepositoryCandidateInventory,
    compatible_dataset_groups,
    discover_repository_candidates,
    select_unique_dataset,
)
from aibench.planning.planner import PlannerProvider
from aibench.security.redaction import sanitize_value
from aibench.services.plugins import session_plugins
from aibench.sessions.controller import PatchResult, SessionController
from aibench.sessions.store import SessionStore
from aibench.sessions.summary import SessionLine, describe_sessions
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from aibench.tui.render import safe

if TYPE_CHECKING:
    from aibench.planning.openai_provider import OpenAICompatibleConfig

console = Console(highlight=False, emoji=False)
err_console = Console(stderr=True, highlight=False, emoji=False)

EXIT_OK, EXIT_INVALID, EXIT_INCOMPLETE, EXIT_DENIED = 0, 2, 3, 4
_EXIT_SEVERITY = (0, 1, 3, 130)  # when an exchange followed several runs, the worst wins
CONFIG_NAMES = ("aibench.json", "aibench.yaml", "aibench.yml", "config.json", "config.yaml")


def interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _fail(message: str, code: int = EXIT_INVALID) -> typer.Exit:
    err_console.print(f"[red]{safe(message)}[/red]")
    return typer.Exit(code=code)


def project_settings(
    root: Path, app: Path | None, dataset: Path | None, policy: Path | None
) -> dict[str, Path | None]:
    """The project's application, dataset, policy and plan: config file < CLI flags."""
    config_path = next((root / n for n in CONFIG_NAMES if (root / n).is_file()), None)
    overrides = {
        "application_target": str(app.resolve()) if app else None,
        "dataset_path": str(dataset.resolve()) if dataset else None,
        "policy_path": str(policy.resolve()) if policy else None,
    }
    resolved = resolve_config(config_path=config_path, cli_overrides=overrides, env=os.environ)
    # Relative project settings belong to the configured project root. Without a config,
    # keep honoring the explicit --project root instead of resolve_config's cwd default.
    base = resolved.root if config_path else root
    config = resolved.config

    def path(value: str | None) -> Path | None:
        return None if value is None else (base / value).resolve()

    return {
        "application": path(config.application_target),
        "dataset": path(config.dataset_path),
        "policy": path(config.policy_path),
        "plan": path(config.plan_path),
        "config": config_path,
    }


def provider_config(config_path: Path) -> OpenAICompatibleConfig:
    """An OpenAI-compatible provider config file; raises `ConfigError` when invalid."""
    from aibench.planning.openai_provider import OpenAICompatibleConfig

    try:
        return OpenAICompatibleConfig.model_validate(load_mapping_file(config_path))
    except (PydanticValidationError, AibenchError, OSError) as exc:
        raise ConfigError(f"invalid provider config {config_path}: {exc}") from exc


def open_provider(
    source: Path | OpenAICompatibleConfig,
    policy_path: Path | None,
    *,
    user_approved: bool = False,
) -> tuple[PlannerProvider | None, list[str]]:
    """The assistant model, or (None, reasons) when the policy does not permit it.

    `user_approved`: the model the user chose in `benchcraft setup`. Choosing it approved
    its endpoint and key reference, so a project without a policy file uses it; a project
    policy, when there is one, still decides."""
    from aibench.planning.openai_provider import (
        OpenAICompatibleConfig,
        OpenAICompatibleProvider,
        provider_denials,
    )

    if isinstance(source, OpenAICompatibleConfig):
        config = source
    else:
        try:
            config = provider_config(source)
        except ConfigError as exc:
            return None, [str(exc)]
    policy = load_policy(policy_path)
    if user_approved and policy_path is None:
        policy = policy.model_copy(
            update={
                "allowed_planner_origins": (*policy.allowed_planner_origins, config.base_url),
                "allowed_secret_refs": (
                    *policy.allowed_secret_refs,
                    *([config.api_key] if config.api_key else []),
                ),
            }
        )
    denials = provider_denials(config, policy)
    if denials:
        return None, denials
    try:
        return OpenAICompatibleProvider(config), []
    except AibenchError as exc:
        return None, [str(exc)]


def _describe(line: SessionLine) -> str:
    what = "; ".join(o if len(o) <= 60 else o[:57] + "..." for o in line.objectives[:2])
    if len(line.objectives) > 2:
        what += f" (+{len(line.objectives) - 2} more)"
    ran = f"{line.runs} run(s)" if line.runs else "not run yet"
    return f"{line.session_id} | {line.updated_at:%Y-%m-%d %H:%M} | {what} | {ran}"


def _choose(sessions: list[SessionLine], hidden_empty: int = 0) -> str | None:
    """Interactive chooser over sessions worth resuming, newest first: a session ID, or
    None for a new one."""
    console.print("Sessions in this project (newest first):")
    for index, line in enumerate(sessions, start=1):
        console.print(safe(f"  [{index}] {_describe(line)}"))
    console.print(safe("  [n] new session"))
    if hidden_empty:
        console.print(
            safe(f"  ({hidden_empty} empty session(s) not listed; a new session reuses one)")
        )
    while True:
        answer = input("Resume which session? ").strip().lower()
        if answer in ("n", "new"):
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(sessions):
            return sessions[int(answer) - 1].session_id
        console.print("Type a number from the list, or n.")


def latest_session(store: SessionStore, root: Path) -> str:
    """`--resume` without an ID: the project's most recently used session that has an
    objective or a run, else its most recent session at all."""
    existing = [s for s in store.list_sessions() if Path(s.project_root) == root]
    if not existing:
        raise _fail(f"no session to resume in project {root}; `benchcraft` starts one")
    lines = sorted(describe_sessions(store, existing), key=lambda line: line.updated_at)
    worth = [line for line in lines if not line.empty]
    return (worth or lines)[-1].session_id


def _pick(
    lines: list[SessionLine],
    *,
    send: bool,
    chooser: Callable[[list[SessionLine], int], str | None],
) -> tuple[str | None, str | None]:
    """Which existing session to open: (session_id, None), or (None, error). `lines` are
    newest first. Sessions with an objective or a run are worth resuming; empty ones
    (opened and abandoned) are never listed and are reused when nothing else is wanted, so
    they do not pile up. None with no error means "start a new one"."""
    worth = [line for line in lines if not line.empty]
    empties = [line for line in lines if line.empty]
    if send:
        if len(worth) > 1:
            return None, (
                f"{len(worth)} sessions in this project have work in them; choose one with "
                "--resume SESSION_ID or start one with --new (aibench sessions list)"
            )
        return (worth or empties)[0].session_id, None
    if not worth:
        return empties[0].session_id, None
    return chooser(worth, len(empties)), None


_PROJECT = typer.Option(None, "--project", help="Project directory (default: cwd).")
# A user coming back means the session they last worked in: `--continue` (or
# `--resume latest`) opens it without looking up an ID. `--resume` itself keeps its value
# required: an optional value would read the next option (`--resume --send ...`) as the ID.
LATEST = "latest"
_RESUME = typer.Option(None, "--resume", help="Session to continue: a SESSION_ID, or 'latest'.")
_CONTINUE = typer.Option(
    False, "--continue", "-c", help="Continue the latest session with work in it."
)
_NEW = typer.Option(False, "--new", help="Start a new session.")
_APP = typer.Option(None, "--app", help="Application config (overrides the project config).")
_DATASET = typer.Option(None, "--dataset", help="Dataset (overrides the project config).")
_POLICY = typer.Option(None, "--policy", help="Execution policy (default: conservative).")
_TRUST = typer.Option(
    False, "--trust-local-app", help="Grant trusted-local mode for a CLI application."
)
_PROVIDER = typer.Option(
    None, "--provider-config", help="Assistant model config (OpenAI-compatible)."
)
_SEND = typer.Option(None, "--send", help="Non-interactive: send one message or /command.")
_JSON = typer.Option(False, "--json", help="With --send: machine-readable output.")
_OBJECTIVE = typer.Option(
    [], "--objective", help="New session: what the benchmark should check (repeatable)."
)


def chat(
    project: Path | None = _PROJECT,
    resume: str | None = _RESUME,
    new: bool = _NEW,
    app: Path | None = _APP,
    dataset: Path | None = _DATASET,
    policy: Path | None = _POLICY,
    trust_local_app: bool = _TRUST,
    provider_config: Path | None = _PROVIDER,
    send: str | None = _SEND,
    json_output: bool = _JSON,
    objectives: list[str] = _OBJECTIVE,
    continue_latest: bool = _CONTINUE,
) -> None:
    """Open the benchmark conversation for a project."""
    if send is None and not interactive_terminal():
        raise _fail(
            "chat needs an interactive terminal; for scripts use `aibench chat --send TEXT "
            "--json` or the headless commands (aibench --help)"
        )
    if sum((bool(resume), new, continue_latest)) > 1:
        raise _fail("use one of --resume, --continue and --new")
    if continue_latest:
        resume = LATEST
    root = (project or Path.cwd()).resolve()
    try:
        settings = project_settings(root, app, dataset, policy)
    except AibenchError as exc:
        raise _fail(str(exc)) from exc
    chosen = _assistant_model(provider_config, interactive=send is None)
    workspace = Workspace.at(root)
    if settings["application"] is None and not workspace.db_path.is_file():
        # Not a BenchCraft project (e.g. the home or a system folder): explain how to
        # connect an app instead of creating .aibench/ here.
        raise _fail(_not_connected(root))
    try:
        storage = Storage(Database.open_workspace(workspace))
    except OSError as exc:
        raise _fail(
            f"BenchCraft cannot create its folder in {root} ({exc.strerror or exc}). "
            "Open a terminal in your project folder and run `benchcraft` there."
        ) from exc
    artifacts = ArtifactStore(workspace.artifacts_dir)
    provider: PlannerProvider | None = None
    dataset_notices: list[str] = []
    try:
        controller = _session(
            storage,
            artifacts,
            workspace,
            root,
            settings,
            resume,
            new,
            send,
            trust_local_app,
            tuple(objectives),
            interactive=send is None,
            dataset_notices=dataset_notices,
        )
        if chosen is not None:
            provider, denials = open_provider(
                chosen, settings["policy"], user_approved=provider_config is None
            )
            for denial in denials:
                err_console.print(f"[yellow]assistant model disabled:[/yellow] {safe(denial)}")

        def new_session() -> SessionController:
            return _create(
                storage,
                artifacts,
                workspace,
                root,
                settings,
                trust_local_app,
                tuple(objectives),
                interactive=send is None,
                dataset_notices=dataset_notices,
            )

        if send is not None:
            code = asyncio.run(
                _send(controller, provider, send, json_output, new_session, dataset_notices)
            )
            raise typer.Exit(code=code)
        from aibench.tui.app import ChatApp

        chat_app = ChatApp(
            controller,
            provider=provider,
            new_session=new_session,
            history_path=workspace.root / "chat_history",
            theme_path=workspace.root / "ui.json",
        )
        asyncio.run(chat_app.run())
    finally:
        try:
            close_provider = getattr(provider, "close", None)
            if callable(close_provider):
                close_provider()
        finally:
            storage.db.close()


def _not_connected(root: Path) -> str:
    return (
        f"no application is connected in {root}. Run `benchcraft` in your app's project "
        "folder, and connect the app once (it is not called during setup):\n"
        "  benchcraft connect http --url http://localhost:8000/chat --dataset cases.jsonl "
        "--input-path /question --output-path /answer --effects none\n"
        "To try BenchCraft on a sample app first: benchcraft init demo; cd demo; benchcraft"
    )


def _assistant_model(
    provider_config: Path | None, *, interactive: bool
) -> Path | OpenAICompatibleConfig | None:
    """`--provider-config` when given; else the model saved by `benchcraft setup`, asking
    for one on the first interactive start."""
    from aibench import userconfig

    if provider_config is not None:
        return provider_config
    if interactive and not userconfig.decided():
        return userconfig.run_setup(lambda line: console.print(safe(line)))
    return userconfig.saved_provider()


def _create(
    storage: Storage,
    artifacts: ArtifactStore,
    workspace: Workspace,
    root: Path,
    settings: dict[str, Path | None],
    trusted: bool,
    objectives: tuple[str, ...] = (),
    *,
    interactive: bool = False,
    dataset_notices: list[str] | None = None,
) -> SessionController:
    application, dataset = settings["application"], settings["dataset"]
    if application is None:
        raise _fail(_not_connected(root))
    if dataset is None:
        dataset = _discover_session_dataset(
            root,
            settings["policy"],
            interactive=interactive,
            notices=dataset_notices,
        )
        settings["dataset"] = dataset
    try:
        # Optional plugins the project installed (`aibench plugins install`); the policy
        # still decides whether they may load.
        environments, defaults = session_plugins(root)
        return SessionController.create(
            storage=storage,
            artifacts=artifacts,
            workspace_root=workspace.root,
            project_root=root,
            application=application,
            dataset=dataset,
            objectives=objectives,
            policy_path=settings["policy"],
            trusted_local=trusted,
            plugin_environments=environments,
            evaluator_defaults=defaults,
        )
    except AibenchError as exc:
        raise _fail(str(exc)) from exc


def _discover_session_dataset(
    root: Path,
    policy_path: Path | None,
    *,
    interactive: bool,
    notices: list[str] | None,
) -> Path:
    """Reuse only one compatible repository dataset inside the existing approved root."""
    try:
        policy = load_policy(policy_path)
        inventory = discover_repository_candidates(root, policy)
    except AibenchError as exc:
        raise _fail(
            "A new session needs a dataset. Repository discovery was not allowed by the "
            f"current policy ({exc}); pass --dataset or approve this project in "
            "inspection_roots."
        ) from exc

    decision = select_unique_dataset(inventory)
    if decision.state == "selected":
        assert decision.selected_path is not None
        selected = (root / Path(*Path(decision.selected_path).parts)).resolve()
        message = f"Using the only compatible dataset allowed by policy: {decision.selected_path}."
        if decision.equivalent_paths and len(decision.equivalent_paths) > 1:
            message += " Identical-content copies: " + ", ".join(decision.equivalent_paths) + "."
        _dataset_notice(message, interactive=interactive, notices=notices)
        return selected

    if decision.state == "ambiguous" and interactive:
        return _choose_dataset(inventory, root)

    if decision.state == "ambiguous":
        raise _fail(
            f"{decision.question} For non-interactive chat, pass --dataset PATH or create "
            "an explicit project dataset_path."
        )
    if interactive:
        answer = input(
            "No compatible approved dataset was found. Enter a dataset path (or leave blank to cancel): "
        ).strip()
        if answer:
            chosen = Path(answer).expanduser()
            return chosen.resolve() if chosen.is_absolute() else (root / chosen).resolve()
    raise _fail(
        "No compatible repository dataset is available under the current inspection policy. "
        "Which dataset path should be used? Pass --dataset PATH (or set dataset_path in the "
        "project config); policy-approved discovery requires this project in inspection_roots."
    )


def _dataset_notice(message: str, *, interactive: bool, notices: list[str] | None) -> None:
    if interactive:
        console.print(f"[dim]{safe(message)}[/dim]")
    elif notices is not None:
        notices.append(message)


def _choose_dataset(inventory: RepositoryCandidateInventory, root: Path) -> Path:
    groups = compatible_dataset_groups(inventory)
    console.print("Compatible datasets with different content:")
    for index, group in enumerate(groups, start=1):
        paths = ", ".join(item.path for item in group)
        console.print(safe(f"  [{index}] {paths} ({group[0].case_count} case(s))"))
    while True:
        answer = input("Which dataset should this session use? ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(groups):
            chosen = groups[int(answer) - 1][0]
            message = f"Using the dataset you selected: {chosen.path}."
            _dataset_notice(message, interactive=True, notices=None)
            return (root / Path(*Path(chosen.path).parts)).resolve()
        console.print(f"Type a number from 1 to {len(groups)}.")


def _session(
    storage: Storage,
    artifacts: ArtifactStore,
    workspace: Workspace,
    root: Path,
    settings: dict[str, Path | None],
    resume: str | None,
    new: bool,
    send: str | None,
    trusted: bool,
    objectives: tuple[str, ...] = (),
    *,
    interactive: bool = False,
    dataset_notices: list[str] | None = None,
) -> SessionController:
    store = SessionStore(storage)
    if resume == LATEST:
        resume = latest_session(store, root)
    if resume is not None:
        session = store.get_session(resume)
        if session is None or Path(session.project_root) != root:
            raise _fail(f"no session {resume!r} in project {root}")
        return _with_project_plugins(
            SessionController(
                resume, storage=storage, artifacts=artifacts, workspace_root=workspace.root
            ),
            root,
        )
    existing = [s for s in store.list_sessions() if Path(s.project_root) == root]
    lines = describe_sessions(store, existing)
    if new or not existing:
        return _create(
            storage,
            artifacts,
            workspace,
            root,
            settings,
            trusted,
            objectives,
            interactive=interactive,
            dataset_notices=dataset_notices,
        )
    chosen, problem = _pick(lines, send=send is not None, chooser=_choose)
    if problem:
        raise _fail(problem)
    if chosen is None:  # a new session: an empty one is reused unless objectives were given
        empty = next((line for line in lines if line.empty), None)
        if empty is not None and not objectives:
            chosen = empty.session_id
    if chosen is None:
        return _create(
            storage,
            artifacts,
            workspace,
            root,
            settings,
            trusted,
            objectives,
            interactive=interactive,
            dataset_notices=dataset_notices,
        )
    return _with_project_plugins(
        SessionController(
            chosen, storage=storage, artifacts=artifacts, workspace_root=workspace.root
        ),
        root,
    )


def _with_project_plugins(controller: SessionController, root: Path) -> SessionController:
    """Bring a reopened session up to date with its project, as the next draft revision.

    A session keeps the plugin environments it was created with, so one opened before
    `plugins install` would never see the plugin: the project's current ones are loaded (the
    session's own are never dropped). A draft also records the policy limits it was made
    under, so a policy edited since (a raised call limit) is picked up by redrafting. A run
    in progress is left alone."""
    if controller.active_run() is not None:
        return controller
    try:
        environments, defaults = session_plugins(root)
    except AibenchError:
        environments, defaults = (), {}  # a broken config is reported where it is needed
    session = controller.session
    has = {json.dumps(deep_unfreeze(e), sort_keys=True) for e in session.plugin_environments}
    wanted = [json.dumps(e.model_dump(mode="json"), sort_keys=True) for e in environments]
    needs_plugins = bool(environments) and not (
        all(w in has for w in wanted) and deep_unfreeze(session.evaluator_defaults) == defaults
    )
    if needs_plugins:
        result: PatchResult | None = controller.use_plugin_environments(environments, defaults)
        done, failed = "loaded this project's plugins into the session", "load the plugins"
    else:
        result = controller.refresh_draft()
        done, failed = "refreshed the plan under the project's current policy", "refresh the plan"
    if result is None:
        return controller
    if result.status == "applied":
        err_console.print(f"[dim]{done} (draft revision {result.revision})[/dim]")
        # A plugin that no longer loads leaves its metrics out of the new draft: say why
        # here, not only when /run refuses.
        findings = (controller.current_decision().draft or {}).get("findings", [])
        for finding in findings:
            if finding.get("blocking") and finding.get("subject") == "plugins":
                err_console.print(
                    f"[yellow]the plan cannot run: {safe(finding['message'])}[/yellow]"
                )
    else:
        reason = safe("; ".join(result.problems) or result.status)
        err_console.print(f"[yellow]could not {failed}: {reason}[/yellow]")
    return controller


async def _send(
    controller: SessionController,
    provider: PlannerProvider | None,
    text: str,
    json_output: bool,
    new_session: Any,
    dataset_notices: list[str] | None = None,
) -> int:
    """One non-interactive exchange. A run or controlled experiment it starts is followed
    to its end (Ctrl+C stops dispatch and leaves it resumable, as in `aibench run`).

    Exit codes match the headless commands (11-T3): a run this exchange started exits as
    `aibench run` would (0, 1 gate failed, 3 incomplete, 130 interrupted); an action the
    policy denied exits 4; a refused or blocked action, a failed command or a failed model
    turn exits 2; anything else 0."""
    from aibench.conversation.agent import ConversationAgent
    from aibench.experiments.service import experiment_report
    from aibench.services.reports import build_report
    from aibench.services.runs import run_exit_code
    from aibench.tui.app import render_result
    from aibench.tui.commands import Commands

    live_before = set(controller.live_runs())
    experiment_tasks_before = controller.experiment_task_keys()
    command_code: int | None = None
    try:
        if text.strip().startswith("/"):
            result = await Commands(controller, new_session).run(text)
            payload: dict[str, Any] = {"session_id": controller.session_id, **result.as_dict()}
            ok = result.ok
            command_code = result.exit_code
            actions = [result.data] if result.kind == "action" else []
            if result.kind == "confirm":  # /run showed the plan first: nothing was started
                actions = [{"state": "confirmation_required"}]
            if not json_output:
                render_result(console, result)
        else:
            outcome = await ConversationAgent(controller, provider).handle_message(text)
            payload = {"session_id": controller.session_id, "outcome": outcome.as_dict()}
            ok = outcome.stopped is None or outcome.stopped == "no assistant model is configured"
            rescore_codes = [
                result["exit_code"]
                for result in outcome.rescores
                if type(result.get("exit_code")) is int and result["exit_code"] in _EXIT_SEVERITY
            ]
            if rescore_codes:
                command_code = max(rescore_codes, key=_EXIT_SEVERITY.index)
            actions = list(outcome.actions) + [
                {"state": item.get("status", "rejected")} for item in outcome.rejected
            ]
            if not json_output:
                console.print(safe(outcome.text))
                console.print(f"[dim]({safe(outcome.status_line)})[/dim]")
        # A one-shot status/control command must not wait on an already-running job. Wait
        # only for a run task this exchange launched (for example /run or a resumed run).
        started_here = set(controller.live_runs()) - live_before
        run_codes = []
        for run_id in sorted(started_here):
            finished = await controller.wait_for_run(run_id)
            report = build_report(controller.storage, controller.artifacts, run_id)
            run_code = run_exit_code(finished.state, report) if finished else EXIT_INCOMPLETE
            run_codes.append(run_code)
            payload.setdefault("runs", []).append(
                {
                    "run_id": run_id,
                    "state": finished.state.value if finished else None,
                    "gates": report["gates"],
                    "outcome": report["outcome"],
                    "exit_code": run_code,
                }
            )
        experiment_tasks_started = controller.experiment_task_keys() - experiment_tasks_before
        experiment_ids_started = sorted(
            {task_key.removesuffix(":holdout") for task_key in experiment_tasks_started}
        )
        for experiment_id in experiment_ids_started:
            record = await controller.wait_for_experiment(experiment_id)
            if record is not None:
                payload.setdefault("experiments", []).append(
                    experiment_report(
                        experiment_id,
                        storage=controller.storage,
                        artifacts=controller.artifacts,
                    )
                )
    except KeyboardInterrupt:
        await controller.close()
        return 130
    states = {a.get("state") for a in actions}
    if run_codes:
        codes = [*run_codes, *([command_code] if command_code in _EXIT_SEVERITY else [])]
        code = max(codes, key=_EXIT_SEVERITY.index)
    elif command_code is not None:
        code = command_code
    elif states & {"denied", "confirmation_required"}:
        code = EXIT_DENIED  # authorization required: nothing ran
    elif not ok or states & {"rejected", "blocked"}:
        code = EXIT_INVALID
    else:
        code = EXIT_OK
    payload["exit_code"] = code
    if dataset_notices:
        payload["dataset_selection"] = dataset_notices
    if json_output:
        json_payload = json.loads(json.dumps(payload, default=str))
        print(json.dumps(sanitize_value(json_payload)))
    elif dataset_notices:
        for notice in dataset_notices:
            console.print(f"[dim]{safe(notice)}[/dim]")
    return code
