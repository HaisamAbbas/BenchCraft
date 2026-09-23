# aibench (working name: BenchCraft)

Conversational CLI for AI application benchmarking. See `docs/spec/implementation-plan.md`
for the authoritative specification and `docs/engineering/implementation-contract.md` for
the engineering process this repository follows.

## Quickstart

```bash
aibench init support-bench && cd support-bench && aibench doctor
aibench chat --new --objective "answers are correct"     # then /plan, /run, /report
```

This creates a 10-case local project with a fixture app and opens the benchmark
conversation. See [docs/quickstart.md](docs/quickstart.md)
and, for what is and isn't supported, [docs/support.md](docs/support.md).

## Development setup

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
aibench --help
```

Locked dev dependency versions: `requirements-dev.lock.txt`. Supported Python/platform
matrix: `docs/engineering/platform-matrix.md`. CI: `.github/workflows/ci.yml`.

## Build

```bash
.venv/Scripts/pip install build
.venv/Scripts/python -m build
```

Produces `dist/aibench-<version>-py3-none-any.whl` and `dist/aibench-<version>.tar.gz`,
installable standalone (without editable mode or dev extras) in any environment matching the
supported Python versions.

## Status

MVP under construction. See `docs/engineering/phase-status.md` for current phase status
and `docs/engineering/reports/` for per-prompt completion reports.
