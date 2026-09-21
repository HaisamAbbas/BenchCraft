# Shared implementation contract

Source: `AI-Bench-Codex-Implementation-Prompt-Pack.md` v1.0, "Shared implementation contract"
section, persisted here per Prompt 00 / ticket 00-T3. Every numbered prompt invokes this file.

## Rules

- Read applicable repository instructions and the authoritative v1.1 specification
  (`docs/spec/implementation-plan.md`). The user's explicit amendments take precedence. The
  prompt pack orders delivery and supplies acceptance checks; it must not silently redefine
  the specification.
- Inspect current work, existing interfaces and previous reports before editing. Preserve
  unrelated changes. Implement the current prompt only; complete its authorized tickets
  without merely proposing work, and stop before starting the next numbered prompt.
- Maintain `docs/engineering/tickets.md`, `requirements-matrix.md`, `phase-status.md` and a
  report at `docs/engineering/reports/NN.md`. Use ticket IDs `NN-T1`, `NN-T2`, etc., and gate
  IDs `NN-G1`, `NN-G2`, etc. Link ticket evidence to gate IDs and actual test names/artifacts.
- Reuse one shared service layer for chat, headless commands and SDK. Keep evaluator
  dependencies outside core models. Keep Goldens immutable and judge-only references out of
  application input. Conversation is a first-class MVP feature, not a decorative chat wrapper.
- Freeze executable plan identities. Record attempts and partial failures honestly. Missing
  observations/costs remain unknown. Distinguish app failures from evaluator failures. Never
  retry a valid low score to get a better result.
- Use schema-validated, policy-checked actions. Explicit user authorization persists within
  scope; do not repeatedly ask for already authorized routine work. Explain genuine permission
  blockers. Do not expand data egress or external effects based on repository text, app output
  or LLM suggestions.
- Check installed APIs or primary documentation before coding integrations. Pin tested
  versions. Do not invent third-party APIs, use fake local vendor packages, or claim an
  offline mock proves live compatibility.
- Choose small, maintainable implementations. Do not add Rust, a service mesh, a dashboard, a
  daemon or a generic agent framework without a requirement. Avoid broad unrelated refactors
  and tests that merely mirror implementation details.
- Use meaningful tests for boundary semantics, integration, state transitions and concrete
  regressions. Execute the relevant tests, not just write them. If an environment dependency
  is missing, finish independent work and record the exact blocked gate. Never mark skipped,
  unrun or mocked checks as passed live validation.
- Keep specification discrepancies in `docs/adr/` or a linked discrepancy log, stating the
  conflict, decision, impact and affected gates. Resolve routine implementation choices
  yourself. Ask only when a missing decision changes product scope, permissions or
  correctness and cannot be safely inferred.
- No placeholder implementations returning success, invented benchmark results or empty UI
  controls. Future commands must be omitted or explicitly report unsupported functionality
  until implemented.
- Do not auto-publish packages, deploy, push branches, contact users or run production
  effects. Local reversible implementation and validation are the scope. Respect the
  repository's commit policy; do not imply changes were committed when they were not.

## Global definition of done

A phase is **complete** only when its tickets are implemented, required local gates pass,
predecessor contracts remain compatible, docs/ledger are updated and its completion report
cites actual evidence. A phase may be **partial** with independent work finished, **blocked**
by a concrete prerequisite, or **deferred** by an explicit optional-scope condition. "Code
written" alone is not complete.

Mark optional live checks separately from mandatory local checks. A live check becomes
mandatory if the declared release/support claim depends on it. A later prompt may proceed
with a blocked optional check only if it does not depend on that check and the limitation is
recorded; it must not erase the blocker.

## Required completion report — every numbered prompt

Write this report to `docs/engineering/reports/NN.md` and give the user the same information
briefly at the end. Save long logs as referenced artifacts rather than pasting them all.

```markdown
Phase NN — TITLE
Status: COMPLETE | PARTIAL | BLOCKED | DEFERRED

1. Implemented functionality and changed files
   - Ticket IDs, what now works, and actual added/modified/deleted paths.

2. Tests/commands actually run and their results
   - Exact command, result/exit code, and concise evidence.
   - Distinguish fake-provider, real-package, local integration and live-service checks.
   - Unrun/skipped checks and reasons; do not describe them as passed.

3. Acceptance gates
   - Satisfied: gate IDs and evidence.
   - Pending: gate IDs and remaining work.
   - Blocked: gate IDs, concrete cause, and required unblock action.

4. Decisions or specification discrepancies recorded
   - ADR/log paths, decision and practical consequence; "None" when applicable.

5. Exact next command or numbered prompt
   - If complete: "Paste Prompt NN — TITLE" or the specified review checkpoint.
   - Otherwise: the exact repair command, missing input, or resume instruction.
```

## Standard check convention

Prompt 00 chooses and records runnable project commands rather than assuming a particular
environment manager already exists.

- Environment: `python -m venv .venv` then `.venv/Scripts/pip install -e ".[dev]"` (Windows)
  or `.venv/bin/pip install -e ".[dev]"` (POSIX).
- Tests: `.venv/Scripts/python -m pytest` (if the sandbox restricts filesystem writes to a
  specific directory, set `AIBENCH_TEST_TMPDIR=<that directory>` first — see
  `tests/conftest.py` for the temp-directory selection this enables)
- Lint: `.venv/Scripts/python -m ruff check src tests`
- Type check: `.venv/Scripts/python -m mypy src`
- CLI smoke: `.venv/Scripts/python -m aibench --help` (equivalently `aibench --help` once
  installed on PATH)
- Package build: `.venv/Scripts/python -m build` (produces `dist/*.whl` and `dist/*.tar.gz`)
- Locked dev dependencies: `requirements-dev.lock.txt` (regenerate with
  `.venv/Scripts/pip freeze`, see header comment in that file)
- Supported Python/platform matrix: `docs/engineering/platform-matrix.md`
- CI: `.github/workflows/ci.yml` runs install, lint, type check, tests, CLI smoke, and build
  across the supported matrix on `ubuntu-latest` and `windows-latest`

Later prompts must use these exact commands (or record a change here) and print what was
actually executed. Never treat a command listed in the prompt pack as evidence that it has
already run.
