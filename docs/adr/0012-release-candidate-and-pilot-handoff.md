# ADR 0012: Release candidate and pilot handoff

Status: Accepted
Date: 2026-09-24
Prompt: 13 — Release candidate and pilot handoff

## Context

§22 step 13 asks for "two integration trials and documented limitations". §23 says a
release needs:
- all deterministic contract and recovery gates;
- compatible adapter fixtures;
- a fresh-install demo;
- no known reference leakage;
- clear limitations for unsupported environments.

Prompt 12's audit left several findings open:
- disk-full behaviour was never exercised;
- terminal resizing was untested;
- a recovery crash window was recorded;
- Python 3.11 and Linux were unverified;
- live, human-review and pilot items were blocked.

This prompt has to separate what blocks a technical release candidate from what only a
real pilot or live access can settle, and hide neither.

## Decisions

### 1. Findings triaged against §23's release bar (13-T1)

**Fixed now**, because each is a deterministic recovery gate:

- **A full disk** (Prompt 12 had never exercised it). Exercising it found a defect: a
  workspace write failure during a run marked every remaining case `failed`
  (`engine_error: OSError ENOSPC`) after still calling the application for it. It also
  left those calls out of the accounting, and ended the run `completed`, so it couldn't be
  resumed. For an effectful application, a call that happened was reported as a failure,
  not an unknown effect.

  **Decision:** a workspace storage failure is not a result of the item.
  - **What counts:** `OSError` with `ENOSPC`, `EDQUOT`, `EIO` or `EROFS`, and any
    `sqlite3.DatabaseError`. `engine.is_storage_failure` decides.
  - **What happens:** the engine leaves the item `running`, exactly as a crash would,
    stops dispatching, and ends `interrupted` (exit code 130) with a warning.
  - **On resume:** the existing recovery counts the possibly dispatched call. It repeats
    the call only if the application declares no effects.
  - **Not included:** other `OSError`s (for example `ENOENT`) remain one item's failure.

  **Alternative rejected:** retrying in place. The disk stays full, and the retry would call
  the application again.

- **The recovery crash window** (Prompt 12 recorded it but didn't change it). Recovery moved
  items out of `running` one transaction at a time, then wrote the `recovered` event that is
  the only record of possibly dispatched calls. A crash in between lost that record, so a
  hard call limit could be overrun.

  **Decision:** `Storage.settle_work_items` applies every transition and appends the event in
  one transaction. Both the new test and a check against the old code show the difference.

- **Terminal resizing** (§23). A real ConPTY test now shrinks and grows the console, down
  to 8×20 where prompt_toolkit shows "Window too small". It checks that input still works,
  that a wrapped line is read whole, and that nothing crashes. No product defect was found.

- **Visibility.** Why a run stopped early (a storage failure, a lost lease) was only in the
  event log. `RunOutcome.warnings` now carries it into `run`/`resume` output, JSON,
  `runs status` and the chat.

**Recorded, not changed**, because none is a §23 release requirement:

- **The planner recall target** (17/21 against 0.85). §23 calls these "engineering
  targets, not evidence of achieved performance". Changing the annotations or the template
  after measurement would move the goalposts. It stays "not met".
- **Engine throughput** (about 7 cases/s). A performance limit, not a correctness one.
- **Live judge, model planner and conversation trials; human review of fixtures and
  labels; macOS; Linux locally.** These need access that isn't available. Each is listed as
  open, with its owner.

### 2. Distribution (13-T2)

- **Version `0.1.0rc1`.** This is PEP 440 pre-release syntax, so a candidate can't be mistaken for a final release.
  - The version has one source per package: `aibench.__version__`, and `aibench_deepeval._version.__version__`, which `pyproject.toml` reads dynamically.
  - The plugin requires `aibench>=0.1.0rc1,<0.2`. A bare `>=0.1` would exclude the candidate itself.
- **Artifacts.** Four files: the wheel and sdist of each package, with `SHA256SUMS`.
  - They are built by `scripts/release_check.py`. `python -m build` builds each wheel from its sdist, so stale working-tree files can't leak in.
  - The artifacts are local: nothing is published. Their checksums are recorded in `docs/engineering/evidence/13/`.
- **Validation from the built artifact** (13-G1). `release_check.py` installs the wheel into a clean venv for each interpreter. It runs the documented quickstart outside the repository and checks the exit codes and outputs the docs state. It then runs the 100-case acceptance workflow against the installed package.
  - `--plugin` installs both wheels and runs the adapter's contract tests against the installed plugin, with the real `deepeval` package.
  - A new CI job runs the same script on Ubuntu and Windows with Python 3.11 and 3.12. No CI result has been observed yet.
- **Workspace compatibility.**
  - **Upgrade:** migrations are forward-only and atomic (unchanged).
  - **Downgrade:** a workspace that records a migration this version doesn't know is refused (`WorkspaceTooNew`, exit code 2) and never written. Until now an older aibench would silently write to a newer schema.
  - **Entry point:** the console script moved to `aibench.cli.main:run`, a thin wrapper that reports this refusal without a traceback wherever a command opens a workspace.
- **Runs that span an upgrade.**
  - A run freezes each metric's identity: its binding hash, from the evaluator ID, semantic version and parameters. Resume refuses a change there ("version drift").
  - A packaging-only change (`plugin_version`) resumes, and each result records the implementation that produced it.
  - This was checked by a test rather than assumed. The first draft of the upgrade notes wrongly said every upgrade was refused.
- **Documents:**
  - `CHANGELOG.md` separates capabilities from metric semantics.
  - `docs/release/upgrade-and-recovery.md` covers installation, upgrade, backup and recovery, each recovery path citing its test.
  - The compatibility matrix is `docs/engineering/platform-matrix.md`.

### 3. Pilot package (13-T3)

- **Two recipes** matching what pilot teams usually have:
  - **A:** an HTTP RAG service, with retrieval evidence and a release gate.
  - **B:** a JSON-in/JSON-out command-line assistant, scored afterwards by the team's own domain oracle, without running the assistant again.
- **Recipes are executable, not prose.** Each has working files in `examples/pilot/<recipe>/`. `tests/test_pilot_recipes.py` runs its commands through the real CLI against a local stand-in, and asserts the outputs the recipe quotes. A recipe can't silently drift from the product.
- **The feedback form** measures:
  - setup effort: timestamps at milestones, files and lines edited, blockers, and error messages that didn't help;
  - value compared with direct evaluator use: estimated direct-use time, problems found, which aibench properties mattered, and anything that got in the way.

  Section F defines what counts as a real-team trial.
- **Real-team trials are pending.** Maintainer-run local trials are reported separately and never counted as pilots. No pilot user was contacted: the prompt forbids it without explicit authorization.

### 4. Readiness decision (13-T4)

`docs/engineering/release-readiness.md` separates two claims:
- **Technical release candidate:** the §23 mandatory local gates.
- **Real-user pilot validation:** §22 step 13's two integration trials by teams, and
  §18's time saved.

The first is claimed only where its evidence exists. The second is reported as not yet
started, not as a risk accepted on someone's behalf.

## Specification discrepancies

- **§22 step 13 "two integration trials".** The prompt pack splits this into local trials,
  which are done, and real-team trials, which stay pending until observed. The MVP's pilot
  exit therefore remains open after this prompt. It is not reinterpreted as met.
- **§23 disk-full.** Handling is exercised by making the real write path raise the OS error.
  A real full volume needs administrator rights to create here, so the evidence is recorded
  as simulated.

## Consequences

- **Pre-existing callers.** Anything that relied on a storage failure being recorded as a
  failed case now sees an interrupted run instead. No test relied on it.
- **Evaluator identities.** Native evaluator provenance now records `0.1.0rc1`. Metric
  identities, and so existing binding hashes, are unchanged, because a binding doesn't
  include the package version.
- **Workspaces from before this candidate** are migrated forward normally. An older
  development build refuses a workspace that this candidate has migrated to a newer schema.
