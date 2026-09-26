"""Guided, no-repository setup for the configured JSON HTTP runner."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import typer
from pydantic import ValidationError as PydanticValidationError
from rich.console import Console

from aibench.config.model import AibenchConfig
from aibench.core.errors import AibenchError, ValidationError
from aibench.core.models import (
    ApplicationSpec,
    EffectLevel,
    HttpSecretHeader,
    HttpTransport,
    RunnerKind,
)
from aibench.core.plans import BudgetLimits
from aibench.datasets.ingest import ingest_dataset
from aibench.security.endpoints import is_loopback, origin_of
from aibench.security.policy import ExecutionPolicy
from aibench.tui.render import safe

app = typer.Typer(help="Connect an application without providing its repository.")
console = Console(highlight=False, emoji=False)
err_console = Console(stderr=True, highlight=False, emoji=False)
_APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def _fail(message: str, code: int = 2) -> typer.Exit:
    err_console.print(f"[red]{safe(message)}[/red]")
    return typer.Exit(code=code)


def _prompt(value: str | None, label: str, *, default: str | None = None) -> str:
    if value is not None:
        return value
    if not sys.stdin.isatty():
        if default is not None:
            return default
        raise _fail(f"{label} is required in non-interactive mode")
    return typer.prompt(label, default=default)


def _write_new_files(root: Path, files: dict[str, str]) -> None:
    destinations = [root / name for name in files]
    existing = [path.name for path in destinations if path.exists()]
    if existing:
        raise _fail(
            "setup did not change existing files: " + ", ".join(sorted(existing))
        )
    created: list[Path] = []
    try:
        for path, contents in zip(destinations, files.values(), strict=True):
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                created.append(path)
                stream.write(contents)
    except OSError as exc:
        for path in created:
            path.unlink(missing_ok=True)
        raise _fail(f"could not create the HTTP project configuration: {exc}") from exc


@app.command("http")
def setup_http(
    project: Path = typer.Option(Path("."), "--project", help="Project directory to configure."),  # noqa: B008
    url: str | None = typer.Option(None, "--url", help="JSON HTTP endpoint; never contacted during setup."),
    dataset: Path | None = typer.Option(None, "--dataset", help="Validated JSONL evaluation cases."),  # noqa: B008
    app_id: str | None = typer.Option(None, "--app-id", help="Stable application name."),
    input_path: str = typer.Option("/question", "--input-path", help="Request JSON pointer for case input."),
    output_path: str = typer.Option("/answer", "--output-path", help="Response JSON pointer for the answer."),
    input_field: str = typer.Option("input", "--input-field", help="Top-level case field sent as the question."),
    context_path: str | None = typer.Option(
        None, "--context-path", help="Response JSON pointer for the retrieved documents (RAG), e.g. /sources."
    ),
    context_text_path: str | None = typer.Option(
        None, "--context-text-path", help="Pointer to each document's text when they are objects, e.g. /text."
    ),
    effect: str | None = typer.Option(None, "--effects", help="Required declaration: none, reversible, or irreversible."),
    authorize_origin: str | None = typer.Option(
        None,
        "--authorize-origin",
        help="Explicitly authorize this exact remote HTTPS origin for app requests and benchmark-data egress.",
    ),
    bearer_secret_ref: str | None = typer.Option(
        None, "--bearer-secret-ref", help="Bearer-token secret reference, such as env:MY_API_TOKEN."
    ),
    max_calls: int = typer.Option(20, "--max-calls", min=1, max=1000, help="Hard application-call ceiling."),
) -> None:
    """Create a bounded API project without probing or calling its endpoint."""
    root = project.expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _fail(f"project directory is not writable: {exc}") from exc

    endpoint = _prompt(url, "HTTP endpoint URL")
    dataset_value = _prompt(str(dataset) if dataset is not None else None, "JSONL dataset path")
    app_name = _prompt(app_id, "Application ID", default="http-app")
    effects = _prompt(effect, "Declared application effects (none/reversible/irreversible)")
    if not _APP_ID.fullmatch(app_name):
        raise _fail("application ID must be 1–100 letters, numbers, dots, underscores, or hyphens")

    try:
        parts = urlsplit(endpoint)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("URL must use http or https and include a host")
        if parts.username or parts.password:
            raise ValueError("credentials in the URL are not accepted; use a secret reference")
        if parts.query or parts.fragment:
            raise ValueError("query strings and fragments are not accepted in the setup URL")
        origin = origin_of(endpoint)
        if parts.scheme == "http" and not is_loopback(parts.hostname):
            raise ValueError("plain HTTP is supported only for loopback fixtures")
        if parts.scheme == "https" and not is_loopback(parts.hostname):
            approved = authorize_origin
            if approved is None and sys.stdin.isatty():
                approved = typer.prompt(
                    f"To authorize benchmark traffic to exactly {origin}, type that origin"
                )
            if approved is None or origin_of(approved) != origin:
                raise ValueError(
                    f"remote requests are disabled; pass --authorize-origin {origin} "
                    "to authorize this exact HTTPS origin"
                )
        elif authorize_origin is not None and origin_of(authorize_origin) != origin:
            raise ValueError("--authorize-origin must exactly match the endpoint origin")

        dataset_path = Path(dataset_value).expanduser()
        if not dataset_path.is_absolute():
            dataset_path = (Path.cwd() / dataset_path).resolve()
        else:
            dataset_path = dataset_path.resolve()
        if not dataset_path.is_file():
            raise ValueError(f"dataset file does not exist: {dataset_path}")
        ingested = ingest_dataset(dataset_path, retain_cases=False)
        if not ingested.is_valid or ingested.manifest is None:
            raise ValidationError("dataset validation failed; run `aibench dataset validate` for details")

        declared_effect = EffectLevel(effects)
        secret_headers = {}
        allowed_secret_refs: tuple[str, ...] = ()
        if bearer_secret_ref:
            if not bearer_secret_ref.startswith("env:") or len(bearer_secret_ref) <= 4:
                raise ValueError("bearer secret must be an environment reference such as env:MY_API_TOKEN")
            secret_headers["Authorization"] = HttpSecretHeader(
                ref=bearer_secret_ref, prefix="Bearer "
            )
            allowed_secret_refs = (bearer_secret_ref,)

        # Validate JSON pointers and the discriminated runner contract before any file is written.
        from aibench.runners.bindings import InputBinding, OutputBinding

        InputBinding.from_spec({"fields": {input_path: f"/{input_field}"}})
        output_binding: dict[str, str] = {"output": output_path}
        if context_path:
            output_binding["retrieved_context"] = context_path
            if context_text_path:
                output_binding["retrieved_context_item"] = context_text_path
        elif context_text_path:
            raise ValueError("--context-text-path needs --context-path")
        OutputBinding.from_spec(output_binding)
        transport = HttpTransport(
            url=endpoint,
            method="POST",
            secret_headers=secret_headers,
            timeout_seconds=30,
            connect_timeout_seconds=5,
        )
        application = ApplicationSpec(
            application_id=app_name,
            runner=RunnerKind.HTTP,
            target=endpoint,
            revision="user-configured-http-v1",
            effects=declared_effect,
            transport=transport,
            input_binding={"fields": {input_path: f"/{input_field}"}},
            output_binding=output_binding,
        )
        # The assistant model chosen in `benchcraft setup` was approved by the user; the
        # project policy written here allows it so the chat can use it (reported below).
        from aibench import userconfig

        assistant = userconfig.saved_provider()
        assistant_refs = (assistant.api_key,) if assistant and assistant.api_key else ()
        policy_fields: dict[str, object] = {
            "allowed_planner_origins": (assistant.base_url,) if assistant else (),
            "allowed_applications": (app_name,),
            "allowed_http_origins": (origin,),
            "allowed_egress_origins": (origin,),
            "allowed_secret_refs": (*allowed_secret_refs, *assistant_refs),
            "max_effects": declared_effect,
            "allowed_evaluators": ("native.*",),
            "ceilings": BudgetLimits(
                max_application_calls=max_calls,
                max_evaluator_calls=max_calls,
                max_wall_seconds=1800,
            ),
        }
        policy = ExecutionPolicy.model_validate(policy_fields)
        config = AibenchConfig(
            application_target="application.http.json",
            dataset_path=str(dataset_path),
            policy_path="policy.json",
        )
    except (ValueError, AibenchError, PydanticValidationError) as exc:
        # Secrets are references only; URL credentials and query strings are rejected before
        # validation errors could echo them.
        raise _fail(str(exc)) from exc

    payloads = {
        "application.http.json": application.model_dump_json(indent=2) + "\n",
        "policy.json": policy.model_dump_json(indent=2) + "\n",
        "aibench.json": config.model_dump_json(indent=2) + "\n",
    }
    _write_new_files(root, payloads)
    console.print_json(
        data={
            "status": "configured",
            "project": str(root),
            "application_id": app_name,
            "endpoint_origin": origin,
            "dataset_cases": ingested.manifest.case_count,
            "max_application_calls": max_calls,
            "secret_value_stored": False,
            "network_requests_during_setup": 0,
            "assistant_model_allowed": (
                f"{assistant.model} at {assistant.base_url}" if assistant else None
            ),
            "next": "benchcraft doctor; then benchcraft (and say what to check)",
        }
    )
