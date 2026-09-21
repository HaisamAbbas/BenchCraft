"""00-T4: bootstrap import + CLI smoke tests."""

from typer.testing import CliRunner

from aibench.cli.main import app

runner = CliRunner()


def test_package_imports() -> None:
    import aibench

    assert aibench.__version__ == "0.1.0"


def test_cli_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "aibench" in result.stdout.lower() or "conversational cli" in result.stdout.lower()


def test_cli_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.stdout
