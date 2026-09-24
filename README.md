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

This produces `dist/aibench-<version>-py3-none-any.whl` and `dist/aibench-<version>.tar.gz`.
They install standalone, without editable mode or dev extras, in any environment with a
supported Python version.

To build every release artifact and prove it works from a clean install:
`python scripts/release_check.py --out DIR [--python PATH ...] [--plugin]`. It builds the
core plus the separately packaged DeepEval adapter with `SHA256SUMS`, installs
the core wheel into a fresh venv per interpreter, and runs the documented quickstart and
100-case acceptance workflow. `--plugin` checks the DeepEval plugin in its own clean
environment. It publishes nothing.

## Status

**MVP release candidate:** `0.1.0rc1` is a local candidate, ready for MVP review and not
broad external release. Linux/macOS, live-provider, human fixture review and real-team pilot
checks remain open. See [release readiness](docs/engineering/release-readiness.md).

- **Changes:** [CHANGELOG.md](CHANGELOG.md).
- **Upgrading and recovering interrupted runs:** [docs/release/upgrade-and-recovery.md](docs/release/upgrade-and-recovery.md).
- **Pilot recipes and feedback form:** [docs/pilot/](docs/pilot/README.md).

Per-prompt status is in `docs/engineering/phase-status.md`, and the completion reports are
in `docs/engineering/reports/`.
