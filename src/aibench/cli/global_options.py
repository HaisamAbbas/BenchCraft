"""Options accepted before any command and shared with nested CLI commands."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from typer._click.globals import get_current_context
except ImportError:  # Typer before its vendored Click compatibility layer.
    from click.globals import get_current_context  # type: ignore[no-redef]


@dataclass(frozen=True)
class GlobalOptions:
    config: Path | None = None
    json_output: bool = False
    non_interactive: bool = False
    policy: Path | None = None
    quiet: bool = False
    verbose: bool = False


def current_global_options() -> GlobalOptions:
    """Return root options during command execution, or defaults for direct API calls."""
    context = get_current_context(silent=True)
    if context is None:
        return GlobalOptions()
    root = context.find_root()
    return root.obj if isinstance(root.obj, GlobalOptions) else GlobalOptions()


def json_output_enabled() -> bool:
    """Whether the active root or leaf command requested machine-readable output."""
    options = current_global_options()
    if options.json_output:
        return True
    context = get_current_context(silent=True)
    return bool(context and context.params.get("json_output"))


def command_defaults(command: Any, options: GlobalOptions) -> dict[str, Any]:
    """Build Click's nested default map for root flags that also exist on commands.

    Direct command-line values remain higher precedence, so a local flag can override a
    root default such as `--policy` or `--json`.
    """
    defaults: dict[str, Any] = {}
    for parameter in getattr(command, "params", ()):
        name = getattr(parameter, "name", None)
        if (
            (name == "json_output" and options.json_output)
            or (name == "non_interactive" and options.non_interactive)
            or (name == "quiet" and options.quiet)
            or (name == "verbose" and options.verbose)
        ):
            defaults[name] = True
        elif name in {"policy", "policy_path"} and options.policy is not None:
            defaults[name] = str(options.policy)

    for child_name, child in getattr(command, "commands", {}).items():
        child_defaults = command_defaults(child, options)
        if child_defaults:
            defaults[child_name] = child_defaults
    return defaults


def merge_default_maps(
    existing: Mapping[str, Any] | None, overrides: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge nested command defaults, with explicit root flags winning other defaults."""
    merged = dict(existing or {})
    for key, value in overrides.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = merge_default_maps(current, value)
        else:
            merged[key] = value
    return merged
