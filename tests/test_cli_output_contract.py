from __future__ import annotations

import io
import json

from aibench.cli.output import CLI_OUTPUT_SCHEMA, Console, output_envelope


def test_object_output_keeps_domain_fields_and_adds_cli_metadata() -> None:
    result = output_envelope({"status": "failed", "exit_code": 1})

    assert result["status"] == "failed"
    assert result["exit_code"] == 1
    assert result["_cli"] == {"schema": CLI_OUTPUT_SCHEMA, "exit_code": 1}


def test_explicit_exit_code_wins_over_payload_default() -> None:
    result = output_envelope({"status": "complete"}, exit_code=3)

    assert result["_cli"]["exit_code"] == 3
    assert result["status"] == "complete"


def test_array_and_scalar_output_use_data_wrapper() -> None:
    array_result = output_envelope([1, 2])
    scalar_result = output_envelope("ready")

    assert array_result["data"] == [1, 2]
    assert array_result["_cli"]["schema"] == CLI_OUTPUT_SCHEMA
    assert scalar_result["data"] == "ready"


def test_json_stays_parseable_when_stdout_is_a_color_terminal() -> None:
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, color_system="truecolor")

    console.print_json(data={"ok": True})

    output = stream.getvalue()
    assert "\x1b[" not in output
    assert json.loads(output)["ok"] is True
