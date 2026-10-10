# aibench (working name: BenchCraft)

Conversational CLI for AI application benchmarking. See `docs/spec/implementation-plan.md`
for the authoritative specification and `docs/engineering/implementation-contract.md` for
the engineering process this repository follows.

## Install

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/HaisamAbbas/BenchCraft/main/install.ps1 | iex
```

macOS and Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/HaisamAbbas/BenchCraft/main/install.sh | sh
```

The installer adds [uv](https://astral.sh/uv) if it is missing (it brings its own Python),
downloads the newest release's wheel, checks it against the release's `SHA256SUMS` and
installs `benchcraft` as an isolated tool: nothing is added to your projects. Run it again
to update; `uv tool uninstall aibench` removes it.

Then, in a new terminal:

```powershell
cd my-app-repo
benchcraft setup        # once: the assistant's model and key (the key stays in an env var)
benchcraft connect http --url http://localhost:8000/chat --dataset cases.jsonl `
  --input-path /question --output-path /answer --effects none
benchcraft              # say what to check; /plan, /run, /report
```

For a RAG app, add `--context-path /sources` (and `--context-text-path /text` when each
source is an object) so metrics such as faithfulness see the documents it retrieved.
`/plugins install deepeval` in the chat adds DeepEval's 40 metrics from the same release.

## Quickstart

```bash
aibench init support-bench && cd support-bench && aibench doctor
aibench chat --new --objective "answers are correct"     # then /plan, /run, /report
```

This creates a 10-case local project with a fixture app and opens the benchmark
conversation. Later, `benchcraft --continue` (or `-c`) reopens the session you last worked in;
`--resume SESSION_ID` opens a particular one. See [docs/quickstart.md](docs/quickstart.md)
and, for what is and isn't supported, [docs/support.md](docs/support.md).
Report latency, throughput, retries, and warmup definitions are documented in
[performance measurements](docs/performance-measurements.md).

To connect a configured JSON HTTP endpoint without its repository, see the bounded
`aibench connect http` flow in the quickstart; setup does not contact the endpoint.

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
core plus the separately packaged DeepEval and Ragas adapters with `SHA256SUMS`, installs
the core wheel into a fresh venv per interpreter, and runs the documented quickstart and
100-case acceptance workflow. `--plugin` checks each evaluator package in its own clean
environment. It publishes nothing.

## Status

**Phase 2 working tree:** a real Ragas adapter and stored-run comparison workflow are
implemented on top of the MVP release candidate. The candidate itself is not published and
has not yet been piloted by a real team.

- **Phase 2 comparison:** `aibench compare BASELINE CURRENT`, plus the session-owned
  `compare_runs` tool and `/compare`; see [ADR 0013](docs/adr/0013-ragas-adapter-and-comparison-compatibility.md).
- **What was verified, and what is still open:** [docs/engineering/release-readiness.md](docs/engineering/release-readiness.md).
- **Changes:** [CHANGELOG.md](CHANGELOG.md).
- **Upgrading and recovering interrupted runs:** [docs/release/upgrade-and-recovery.md](docs/release/upgrade-and-recovery.md).
- **Pilot recipes and feedback form:** [docs/pilot/](docs/pilot/README.md).

Per-prompt status is in `docs/engineering/phase-status.md`, and the completion reports are
in `docs/engineering/reports/`.
