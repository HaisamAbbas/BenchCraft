# Ticket ledger

Ticket IDs follow `NN-T#`; gate IDs follow `NN-G#`. Status: DONE | IN_PROGRESS | BLOCKED | TODO.

## Prompt 00 — Bootstrap and specification traceability

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 00-T1 | Establish source of truth: copy v1.1 plan + prompt pack to `docs/spec/`, record hash | DONE | `docs/spec/implementation-plan.md`, `docs/spec/SOURCE.md` |
| 00-T2 | Create development foundation: pyproject.toml, src/aibench, CLI entry, locked dev deps, platform matrix | DONE | `pyproject.toml`, `src/aibench/cli/main.py`, `requirements-dev.lock.txt`, `docs/engineering/platform-matrix.md` |
| 00-T3 | Persist engineering controls: contract, phase-status, requirements-matrix, tickets, ADR | DONE | `docs/engineering/*.md`, `docs/adr/0001-*.md` |
| 00-T4 | Create repeatable developer checks: lint/type/test/build/smoke, bootstrap tests, CI | DONE | `tests/test_bootstrap.py`, `.github/workflows/ci.yml`, this contract's "Standard check convention" |

Gates: 00-G1 (fresh install + `--help`), 00-G2 (requirements matrix complete),
00-G3 (no speculative components), 00-G4 (tests run + report). See
`docs/engineering/reports/00.md`.

## Prompt 01 — Canonical models, configuration, and datasets

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 01-T1 | Model immutable inputs/typed outputs; export JSON Schemas | DONE | `src/aibench/core/models.py` (deep-frozen via `FrozenValue`; explicit `application_id`/`observation_id`/`execution_id`/`artifact_id` identities), `schemas/*.json` |
| 01-T2 | Dataset normalization (shorthand, extensions, duplicate IDs, line errors) | DONE | `src/aibench/datasets/normalize.py` (namespaced-extension enforcement, explicit type checks on `reference`/`provenance`/`fixtures`/`expectations`/`metadata`, pydantic-error-to-line-error conversion), `src/aibench/datasets/ingest.py` (`retain_cases` bounded-memory mode, defensive exception boundary) |
| 01-T3 | Config precedence, path resolution, secret refs, hashes, redaction | DONE | `src/aibench/config/model.py`, `src/aibench/config/resolve.py` |
| 01-T4 | `aibench dataset validate PATH` + fixtures | DONE | `src/aibench/cli/dataset.py` (uses `retain_cases=False`), `examples/datasets/*` (+ `invalid.nested.jsonl`) |

Gates: 01-G1..G4, all satisfied — see `docs/engineering/reports/01.md` for the two review-driven
remediation passes: (1) deep immutability, bounded-memory ingestion mode, nested-input error
handling, extension namespacing, explicit identities; (2) disk-backed duplicate-ID index for
large files (`src/aibench/datasets/ingest.py::_DedupIndex`) and a more robust, sandbox-aware
pytest temp-directory selection (`tests/conftest.py`).
