"""Versioned JSON output contract shared by CLI commands."""

from __future__ import annotations

import json as json_module
from collections.abc import Callable, Mapping
from typing import Any

from rich.console import Console as RichConsole

CLI_OUTPUT_SCHEMA = "aibench.cli-output/1"


def _exit_code(data: Any, explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    if isinstance(data, Mapping):
        code = data.get("exit_code")
        if type(code) is int:
            return code
    return 0


def output_envelope(data: Any, *, exit_code: int | None = None) -> dict[str, Any]:
    """Attach stable CLI schema/exit metadata while preserving object result fields.

    Objects retain their existing top-level keys for compatibility. Array and scalar
    results use `data` because JSON arrays and scalars cannot carry metadata themselves.
    """
    code = _exit_code(data, exit_code)
    metadata = {"schema": CLI_OUTPUT_SCHEMA, "exit_code": code}
    if isinstance(data, Mapping):
        return {**data, "_cli": metadata}
    return {"_cli": metadata, "data": data}


class Console(RichConsole):
    """Rich console whose JSON output carries the shared BenchCraft CLI metadata."""

    def print_json(
        self,
        json: str | None = None,
        *,
        data: Any = None,
        indent: int | str | None = 2,
        highlight: bool = True,
        skip_keys: bool = False,
        ensure_ascii: bool = False,
        check_circular: bool = True,
        allow_nan: bool = True,
        default: Callable[[Any], Any] | None = None,
        sort_keys: bool = False,
        cli_exit_code: int | None = None,
    ) -> None:
        payload = json_module.loads(json) if json is not None else data
        self._print_json_envelope(
            output_envelope(payload, exit_code=cli_exit_code),
            indent=indent,
            highlight=highlight,
            skip_keys=skip_keys,
            ensure_ascii=ensure_ascii,
            check_circular=check_circular,
            allow_nan=allow_nan,
            default=default,
            sort_keys=sort_keys,
        )

    def _print_json_envelope(
        self,
        data: dict[str, Any],
        *,
        indent: int | str | None,
        highlight: bool,
        skip_keys: bool,
        ensure_ascii: bool,
        check_circular: bool,
        allow_nan: bool,
        default: Callable[[Any], Any] | None,
        sort_keys: bool,
    ) -> None:
        super().print_json(
            data=data,
            indent=indent,
            # Machine output must remain valid JSON even when stdout is a color TTY.
            highlight=False,
            skip_keys=skip_keys,
            ensure_ascii=ensure_ascii,
            check_circular=check_circular,
            allow_nan=allow_nan,
            default=default,
            sort_keys=sort_keys,
        )


def json_result(data: Any, *, exit_code: int) -> dict[str, Any]:
    """Return the shared envelope for CLI code paths that write JSON directly."""
    return output_envelope(data, exit_code=exit_code)
