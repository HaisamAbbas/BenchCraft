"""Emit content-withheld GitHub annotations for failures in a pytest JUnit report."""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_ANNOTATIONS = 20
MAX_TEST_ID_LENGTH = 240


def _escape_command_property(value: str) -> str:
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def _escape_command_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _failure_records(report: Path) -> list[tuple[str, str]] | None:
    try:
        root = ET.parse(report).getroot()
    except (OSError, ET.ParseError):
        return None

    records: list[tuple[str, str]] = []
    for testcase in root.iter("testcase"):
        class_name = testcase.get("classname", "unknown")
        test_name = testcase.get("name", "unnamed")
        test_id = f"{class_name}::{test_name}"[:MAX_TEST_ID_LENGTH]
        for result in testcase:
            if result.tag not in {"failure", "error"}:
                continue
            raw_message = result.get("message", "")
            candidate_lines = raw_message.split(":", 1)[0].splitlines()
            candidate = candidate_lines[0].strip() if candidate_lines else ""
            is_exception_name = re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", candidate)
            exception_type = (
                candidate
                if (
                    is_exception_name
                    and len(candidate) <= 80
                    and candidate.endswith(("Error", "Exception"))
                )
                else result.tag
            )
            records.append((test_id, exception_type))
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit_report", type=Path)
    args = parser.parse_args(argv)

    failures = _failure_records(args.junit_report)
    if failures is None:
        print(
            "::warning title=Pytest report unavailable::JUnit report is missing or unreadable; "
            "check the pytest step and artifact."
        )
        return 0

    for test_id, exception_type in failures[:MAX_ANNOTATIONS]:
        title = _escape_command_property(f"Pytest failure: {test_id}")
        message = _escape_command_data(
            f"Failure type: {exception_type}. See the JUnit artifact or pytest log for details."
        )
        print(f"::error title={title}::{message}")

    omitted = len(failures) - MAX_ANNOTATIONS
    if omitted > 0:
        print(
            "::warning title=Pytest failure annotations capped::"
            f"{omitted} additional failure(s) omitted; see the JUnit artifact or pytest log."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
