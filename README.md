# aibench (working name: BenchCraft)

Conversational CLI for AI application benchmarking. See `docs/spec/implementation-plan.md`
for the authoritative specification and `docs/engineering/implementation-contract.md` for
the engineering process this repository follows.

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
