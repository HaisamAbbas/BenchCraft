from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "annotate_pytest_failures.py"


def _write_junit(
    path: Path,
    failures: int = 1,
    *,
    class_name: str = "tests.test_example",
    failure_message: str = "AssertionError: private diagnostic value",
) -> None:
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    for index in range(failures):
        case = ET.SubElement(
            suite,
            "testcase",
            classname=class_name,
            name=f"test_failure_{index}",
        )
        ET.SubElement(case, "failure", message=failure_message)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _run_report(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_annotations_identify_failed_tests_without_exposing_failure_contents(
    tmp_path: Path,
) -> None:
    report = tmp_path / "junit.xml"
    _write_junit(report)

    result = _run_report(report)

    assert result.returncode == 0
    assert (
        "::error title=Pytest failure%3A tests.test_example%3A%3Atest_failure_0::" in result.stdout
    )
    assert "Failure type: AssertionError." in result.stdout
    assert "private diagnostic value" not in result.stdout


def test_annotation_title_escapes_newlines_and_command_delimiters(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    _write_junit(report, class_name="tests.example\n::error title=forged")

    result = _run_report(report)

    assert result.returncode == 0
    assert "%0A%3A%3Aerror title=forged" in result.stdout
    assert len(result.stdout.splitlines()) == 1


def test_unclassified_failure_message_is_not_exposed(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    _write_junit(report, failure_message="private assertion operand")

    result = _run_report(report)

    assert result.returncode == 0
    assert "Failure type: failure." in result.stdout
    assert "private assertion operand" not in result.stdout


def test_empty_failure_message_falls_back_to_generic_type(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    _write_junit(report, failure_message="")

    result = _run_report(report)

    assert result.returncode == 0
    assert "Failure type: failure." in result.stdout


def test_unbounded_exception_name_is_not_exposed(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    _write_junit(report, failure_message=f"{'A' * 81}Error: private detail")

    result = _run_report(report)

    assert result.returncode == 0
    assert "Failure type: failure." in result.stdout
    assert "private detail" not in result.stdout
    assert "A" * 81 not in result.stdout


def test_failure_annotations_are_capped(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    _write_junit(report, failures=25)

    result = _run_report(report)

    assert result.returncode == 0
    assert result.stdout.count("::error title=") == 20
    assert "5 additional failure(s) omitted" in result.stdout


def test_missing_junit_report_warns_without_failing_the_ci_step(tmp_path: Path) -> None:
    result = _run_report(tmp_path / "missing.xml")

    assert result.returncode == 0
    assert "::warning title=Pytest report unavailable::" in result.stdout


def test_malformed_junit_report_warns_without_failing_the_ci_step(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text("<testsuites>", encoding="utf-8")

    result = _run_report(report)

    assert result.returncode == 0
    assert "::warning title=Pytest report unavailable::" in result.stdout
