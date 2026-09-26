"""00-T4: bootstrap import + CLI smoke tests."""

from typer.testing import CliRunner

from aibench.cli.main import app

runner = CliRunner()


def test_package_imports() -> None:
    import importlib.metadata

    import aibench

    # One source of truth: pyproject reads the version from `aibench.__version__`.
    assert aibench.__version__ == importlib.metadata.version("aibench")


def test_cli_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "benchcraft" in result.stdout.lower()


def test_cli_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    import aibench

    # Named after the command typed (`benchcraft` or `aibench`); the test runner is neither.
    assert result.stdout.strip() == f"benchcraft {aibench.__version__}"
