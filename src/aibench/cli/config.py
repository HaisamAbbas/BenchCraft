"""Inspect and edit effective project configuration and named assistant-provider profiles."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import typer
from pydantic import ValidationError as PydanticValidationError
from rich.markup import escape

from aibench import userconfig
from aibench.cli.errors import error_exit
from aibench.cli.global_options import current_global_options
from aibench.cli.output import Console
from aibench.config.model import AibenchConfig
from aibench.config.resolve import ResolvedConfig, load_mapping_file, resolve_config
from aibench.core.errors import AibenchError, ConfigError
from aibench.core.hashes import content_hash
from aibench.planning.openai_provider import OpenAICompatibleConfig
from aibench.security.redaction import sanitize_value
from aibench.security.secrets import SUPPORTED_SOURCES

app = typer.Typer(help="Inspect and manage project configuration.")
profiles_app = typer.Typer(help="Manage named assistant-provider profiles.")
app.add_typer(profiles_app, name="profiles")

console = Console()
err_console = Console(stderr=True)
_CONFIG_NAMES = ("aibench.json", "aibench.yaml", "aibench.yml", "config.json", "config.yaml")
_SECRET_FIELD = re.compile(r"(?i)(secret|token|password|api[_-]?key|credential|authorization|private)")
_SECRET_REFERENCE = re.compile(r"^env:[^\s:]+$")
_PROFILE_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
_PRECEDENCE = ["defaults", "config file", "permitted environment overrides", "CLI flags"]
_PROJECT = typer.Option(None, "--project", help="Project directory (default: cwd).")
_JSON = typer.Option(False, "--json", help="Machine-readable output.")
_VALIDATE_PATH = typer.Argument(None, help="Config file; defaults to project discovery.")
_PROVIDER_FILE = typer.Option(..., "--provider-config", help="OpenAI-compatible provider file.")


def _fail(message: str, *, json_output: bool) -> typer.Exit:
    safe_message = str(sanitize_value(message))
    return error_exit(
        safe_message,
        exit_code=2,
        json_output=json_output,
        console=console,
        err_console=err_console,
    )


def _config_path(project: Path | None, explicit: Path | None = None, *, create: bool = False) -> Path | None:
    options = current_global_options()
    selected = explicit or options.config
    if selected is not None:
        return (Path.cwd() / selected).resolve() if not selected.is_absolute() else selected.resolve()
    root = (project or Path.cwd()).resolve()
    discovered = next((root / name for name in _CONFIG_NAMES if (root / name).is_file()), None)
    return discovered or (root / "aibench.json" if create else None)


def _resolve(
    path: Path | None,
    *,
    environ: dict[str, str] | None = None,
    include_cli: bool = True,
):
    options = current_global_options()
    overrides = {"policy_path": str(options.policy.resolve())} if include_cli and options.policy else {}
    return resolve_config(
        config_path=path,
        cli_overrides=overrides,
        env=os.environ if environ is None else environ,
    )


def _sources(resolved: ResolvedConfig) -> dict[str, str]:
    return {
        name: resolved.sources.get(name, "default")
        for name in resolved.config.redacted()
    }


def _safe_config_hash(resolved: ResolvedConfig) -> str:
    """Hash exactly the redacted view shown by config commands, not hidden values."""
    return content_hash(sanitize_value(_redact_config(resolved.config.redacted())))


def _display(value: object) -> str:
    """Escape untrusted text for Rich after secret and terminal-control sanitizing."""
    return escape(str(sanitize_value(str(value))))


def _redact_config(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        if key is not None and key.lower() in {"secrets", "secret_env"}:
            return {str(name): _redact_config(item, "secret_ref") for name, item in value.items()}
        result: dict[str, Any] = {}
        for name, item in value.items():
            key_text = str(name)
            result[key_text] = _redact_config(item, key_text)
        return result
    if isinstance(value, list):
        return [_redact_config(item, key) for item in value]
    if isinstance(value, str):
        if key is not None and _SECRET_FIELD.search(key):
            return value if _SECRET_REFERENCE.fullmatch(value) else "[redacted]"
        if key is not None and key.lower() in {"url", "base_url", "endpoint", "api_endpoint"}:
            return _safe_endpoint(value)
        return sanitize_value(value)
    return value


def _safe_endpoint(value: str) -> str:
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return "(invalid endpoint)"
        netloc = parts.hostname
        if parts.port is not None:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        return "(invalid endpoint)"


def _validation_details(exc: PydanticValidationError) -> list[str]:
    details = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in error["loc"])
        # SecretRefStr's validator includes the rejected value in its error message.
        # Never put that message on the CLI, even though Pydantic omits `input` here.
        message = "must be a secret reference" if "api_key" in error["loc"] else error["msg"]
        details.append(f"{location}: {message}")
    return details


@app.command("show")
def show(
    project: Path | None = _PROJECT,
    json_output: bool = _JSON,
) -> None:
    """Show effective values, source precedence, and a redacted configuration hash."""
    path = _config_path(project)
    try:
        resolved = _resolve(path)
    except AibenchError as exc:
        raise _fail(str(exc), json_output=json_output) from exc
    except OSError as exc:
        raise _fail("could not read the selected config file", json_output=json_output) from exc
    sources = _sources(resolved)
    data = {
        "config_file": str(path) if path else None,
        "project_root": str(resolved.root),
        "effective": _redact_config(resolved.config.redacted()),
        "sources": sources,
        "precedence": _PRECEDENCE,
        "content_hash": _safe_config_hash(resolved),
    }
    assistant = userconfig.saved_provider()
    data["assistant_provider"] = (
        {
            "source": f"profile:{userconfig.active_provider_profile()}"
            if userconfig.active_provider_profile()
            else "setup",
            "model": assistant.model,
            "endpoint": _safe_endpoint(assistant.base_url),
            "api_key": str(assistant.api_key) if assistant.api_key else None,
        }
        if assistant is not None
        else None
    )
    if json_output:
        console.print_json(data=sanitize_value(data))
        return
    console.print(f"config: {_display(str(path) if path else '(defaults only)')}")
    console.print(f"project root: {_display(resolved.root)}")
    console.print("precedence: defaults < config file < permitted environment overrides < CLI flags")
    for name, value in data["effective"].items():
        source = sources.get(name, "default")
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        console.print(f"  {_display(name)} ({_display(source)}): {_display(rendered)}")
    provider = data["assistant_provider"]
    if isinstance(provider, dict):
        console.print(
            f"assistant provider ({_display(provider['source'])}): "
            f"{_display(provider['model'])} at {_display(provider['endpoint'])}"
        )
    else:
        console.print("assistant provider: not configured")
    console.print(f"configuration hash: {_safe_config_hash(resolved)}")


@app.command("validate")
def validate(
    path: Path | None = _VALIDATE_PATH,
    project: Path | None = _PROJECT,
    json_output: bool = _JSON,
) -> None:
    """Validate the config schema and effective values without contacting providers."""
    selected = _config_path(project, path)
    try:
        resolved = _resolve(selected)
    except AibenchError as exc:
        raise _fail(str(exc), json_output=json_output) from exc
    except OSError as exc:
        raise _fail("could not read the selected config file", json_output=json_output) from exc
    data = {
        "valid": True,
        "config_file": str(selected) if selected else None,
        "project_root": str(resolved.root),
        "content_hash": _safe_config_hash(resolved),
        "sources": _sources(resolved),
    }
    if json_output:
        console.print_json(data=sanitize_value(data))
    else:
        console.print(f"valid configuration: {_display(str(selected) if selected else '(defaults only)')}")


def _render_config(path: Path, value: dict[str, Any]) -> str:
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ConfigError("PyYAML is required to edit a YAML config; use JSON instead") from exc
        return yaml.safe_dump(value, sort_keys=False, allow_unicode=True)
    if path.suffix.lower() != ".json":
        raise ConfigError("config set writes only JSON or YAML config files")
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _set_path(config: dict[str, Any], dotted_key: str, value: Any) -> None:
    parts = dotted_key.split(".")
    allowed_roots = set(AibenchConfig.model_fields)
    if not parts or any(not part for part in parts) or parts[0] not in allowed_roots:
        raise ConfigError(f"unknown config field {dotted_key!r}")
    if len(parts) > 1 and (len(parts) != 2 or parts[0] not in {"secrets", "extensions"}):
        raise ConfigError("config set accepts nested keys only under secrets.* and extensions.*")
    if len(parts) == 1:
        config[parts[0]] = value
        return
    parent = config.setdefault(parts[0], {})
    if not isinstance(parent, dict):
        raise ConfigError(f"config field {parts[0]!r} must be an object before setting a nested key")
    parent[parts[1]] = value


def _parse_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


@app.command("set")
def set_value(
    key: str = typer.Argument(..., help="Config field, or secrets.NAME / extensions.NAME."),
    value: str = typer.Argument(..., help="JSON value; unquoted text is treated as a string."),
    project: Path | None = _PROJECT,
    json_output: bool = _JSON,
) -> None:
    """Validate and atomically set a project config value; secret values must be references."""
    path = _config_path(project, create=True)
    assert path is not None
    try:
        if path.exists():
            config = load_mapping_file(path)
        else:
            config = {}
        _set_path(config, key, _parse_value(value))
        rendered = _render_config(path, config)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.stem}.",
                suffix=path.suffix,
                delete=False,
            ) as handle:
                handle.write(rendered)
                temporary = Path(handle.name)
            resolved = _resolve(temporary, environ={}, include_cli=False)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except (AibenchError, OSError) as exc:
        raise _fail(str(exc), json_output=json_output) from exc
    stored: Any = config
    for part in key.split("."):
        stored = stored.get(part) if isinstance(stored, dict) else None
    data = {
        "updated": key,
        "stored_value": _redact_config({key.split(".")[-1]: stored}),
        "config_file": str(path),
        "content_hash": _safe_config_hash(resolved),
    }
    if json_output:
        console.print_json(data=sanitize_value(data))
    else:
        console.print(f"updated {_display(key)} in {_display(path)}")


def _load_provider(path: Path) -> OpenAICompatibleConfig:
    try:
        config = OpenAICompatibleConfig.model_validate(load_mapping_file(path))
    except PydanticValidationError as exc:
        details = "; ".join(_validation_details(exc))
        raise ConfigError(f"invalid provider config: {details}") from exc
    except (AibenchError, OSError) as exc:
        raise ConfigError(f"invalid provider config: {type(exc).__name__}") from exc
    try:
        parts = urlsplit(config.base_url)
        _ = parts.port
    except ValueError as exc:
        raise ConfigError("provider base_url has an invalid port or authority") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ConfigError(
            "provider base_url must be an http(s) URL without credentials, query, or fragment"
        )
    if config.api_key is not None and config.api_key.partition(":")[0] not in SUPPORTED_SOURCES:
        raise ConfigError("provider api_key must use a supported secret source (env)")
    return config


@profiles_app.command("add")
def add_profile(
    name: str = typer.Argument(..., help="Profile name ([a-z][a-z0-9_-]*)."),
    provider_config: Path = _PROVIDER_FILE,
    replace: bool = typer.Option(False, "--replace", help="Replace an existing profile."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Save a provider config by name; credentials remain secret references."""
    if _PROFILE_NAME.fullmatch(name) is None:
        raise _fail("profile name must match [a-z][a-z0-9_-]*", json_output=json_output)
    if name in userconfig.provider_profile_names() and not replace:
        raise _fail(f"provider profile {name!r} already exists; use --replace", json_output=json_output)
    try:
        config = _load_provider(provider_config)
        stored_at = userconfig.save_provider_profile(name, config)
    except (AibenchError, OSError, ValueError) as exc:
        raise _fail(str(exc), json_output=json_output) from exc
    data = {
        "name": name,
        "model": config.model,
        "endpoint": _safe_endpoint(config.base_url),
        "api_key": str(config.api_key) if config.api_key else None,
        "stored_at": str(stored_at),
        "active": userconfig.active_provider_profile() == name,
    }
    if json_output:
        console.print_json(data=sanitize_value(data))
    else:
        console.print(
            f"saved provider profile {_display(name)} ({_display(config.model)}) "
            f"in {_display(stored_at)}"
        )


@profiles_app.command("list")
def list_profiles(
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List saved provider profile names and their selected state."""
    active = userconfig.active_provider_profile()
    rows = []
    for name in userconfig.provider_profile_names():
        config = userconfig.saved_provider(name)
        rows.append(
            {
                "name": name,
                "active": name == active,
                "model": config.model if config else None,
                "endpoint": _safe_endpoint(config.base_url) if config else None,
                "valid": config is not None,
            }
        )
    data = {"profiles": rows, "active": active}
    if json_output:
        console.print_json(data=sanitize_value(data))
    elif not rows:
        console.print("no provider profiles; add one with `aibench config profiles add NAME --provider-config FILE`")
    else:
        for row in rows:
            marker = "*" if row["active"] else " "
            state = f"{row['model']} at {row['endpoint']}" if row["valid"] else "invalid profile"
            console.print(f"{marker} {_display(row['name'])}: {_display(state)}")


@profiles_app.command("show")
def show_profile(
    name: str = typer.Argument(..., help="Provider profile name."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show a provider profile with its endpoint sanitized and API-key reference only."""
    config = userconfig.saved_provider(name)
    if config is None:
        raise _fail(f"provider profile {name!r} does not exist or is invalid", json_output=json_output)
    data = {
        "name": name,
        "active": userconfig.active_provider_profile() == name,
        "provider": {
            **config.model_dump(mode="json", exclude_none=True),
            "base_url": _safe_endpoint(config.base_url),
        },
    }
    if json_output:
        console.print_json(data=sanitize_value(data))
    else:
        console.print(f"provider profile: {_display(name)}")
        console.print(f"  model: {_display(config.model)}")
        console.print(f"  endpoint: {_display(_safe_endpoint(config.base_url))}")
        console.print(
            f"  API key reference: "
            f"{_display(str(config.api_key) if config.api_key else '(none)')}"
        )
        if data["active"]:
            console.print("  selected by default")


@profiles_app.command("use")
def use_profile(
    name: str = typer.Argument(..., help="Provider profile to select for chat by default."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Select the provider profile used by chat when --provider-profile is omitted."""
    try:
        path = userconfig.select_provider_profile(name)
    except ValueError as exc:
        raise _fail(str(exc), json_output=json_output) from exc
    except OSError as exc:
        raise _fail("could not update user provider settings", json_output=json_output) from exc
    data = {"active": name, "stored_at": str(path)}
    if json_output:
        console.print_json(data=sanitize_value(data))
    else:
        console.print(f"selected provider profile {_display(name)}")


@profiles_app.command("remove")
def remove_profile(
    name: str = typer.Argument(..., help="Provider profile to remove."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Remove a named provider profile from the current user's settings."""
    try:
        path = userconfig.remove_provider_profile(name)
    except KeyError as exc:
        raise _fail(str(exc), json_output=json_output) from exc
    except OSError as exc:
        raise _fail("could not update user provider settings", json_output=json_output) from exc
    data = {"removed": name, "active": userconfig.active_provider_profile(), "stored_at": str(path)}
    if json_output:
        console.print_json(data=sanitize_value(data))
    else:
        console.print(f"removed provider profile {_display(name)}")
