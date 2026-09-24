# Changelog

Each release lists new capabilities separately from changes to metric semantics. A metric
whose meaning changes gets a new semantic version, and is listed under "Metric semantics".
Workspace schema changes are listed under "Workspace". Upgrade steps are in
[docs/release/upgrade-and-recovery.md](docs/release/upgrade-and-recovery.md).

## Unreleased Phase 2 working tree — Prompt 14

- Added the separately packaged `ragas.faithfulness@1` adapter, pinned to `ragas==0.4.3`
  and restricted to text stored outputs in an isolated worker.
- Added strict/exploratory stored-run comparison with case/repetition pairing, compatibility
  identities, coverage gates, case-level macro differences, grouped bootstrap intervals and
  stored-pass stability summaries.
- Added `aibench compare` plus the session-owned `compare_runs` tool and `/compare`; neither
  workflow invokes an application, evaluator or judge.
- Cross-framework results retain separate score semantics. Disagreement is diagnostic and
  is never averaged into a combined quality score.
- Known limitation: Ragas 0.4.3 has an open multi-modal SSRF advisory. The adapter exposes
  only text faithfulness; ADR 0013 records the compensating control.

## 0.1.0rc1 — MVP release candidate (not published)

The first release candidate of the MVP. It is a local technical candidate. **No real team
has piloted it yet** (see [docs/pilot/](docs/pilot/README.md)), and it is not published to
a package index. The readiness decision, with everything that is still open, is in
[docs/engineering/release-readiness.md](docs/engineering/release-readiness.md).

### Capabilities

- **Benchmark conversation.**
  - Bare `aibench` in a terminal opens a persistent two-way session: state a goal, answer clarifications, revise the draft plan, and run it.
  - While it runs you can ask questions and use `/status`, `/pause`, `/resume` and `/stop`.
  - A session is restored after exit or a crash without repeating any action.
  - Failures are discussed with evidence, and every number is traced to a query.
- **Headless commands** for everything the conversation does:
  - `init`, `doctor`;
  - `dataset validate`, `inspect`, `app describe` / `smoke`;
  - `plan`, `plan validate`, `plan benchmark`;
  - `run`, `resume`, `evaluate`, `score`;
  - `runs`, `report`, `benchmark`, `sessions`, `evaluators`, `plugins`.
- **Applications.** CLI and HTTP runners with explicit input and output bindings. Reference answers never reach the application. Retrieval, tools, usage and cost are recorded only when the application reports them.
- **Evaluators.**
  - Native exact match and JSON schema checks.
  - Trusted custom Python evaluators.
  - The DeepEval faithfulness adapter (`aibench-deepeval`, pinned to `deepeval==4.2.5`), run in its own environment.
  - An evaluator that lacks the evidence it needs reports a gap, not a score.
- **Planning.** Evidence-aware plans from declared capabilities and installed evaluators, within a policy. A bounded model planner with a deterministic template fallback. Plans are frozen and hashed before execution.
- **Execution.** Bounded concurrency, timeouts, retries, budgets, cancellation, and conservative resume after interruption or a crash. Effectful calls are never repeated automatically.
- **Reports.** JSON, Markdown and HTML, rebuilt from stored facts, with:
  - full denominators;
  - release gates;
  - latency definitions;
  - cost completeness (unknown is never $0);
  - case evidence.
- **Validation tools.** `aibench plan benchmark` (a planner fixture set) and `aibench evaluators calibrate` (judge calibration).

### Changes in this candidate (after the Prompt 12 acceptance audit)

- **A full disk no longer fails cases.** A workspace storage failure (disk full, quota, I/O error, read-only filesystem, database full) stops dispatching and leaves the run resumable, exit code 130, with the reason shown.
  - Before this, every remaining case was still sent to the application and marked `failed`, and the calls that couldn't be recorded were left out of the accounting.
- **Recovery can't lose a call from the accounting.** Recovery now commits its settlements together with the record of calls that may have reached the application. A crash in between used to lose that record.
- **Why a run stopped early** is shown in `run` and `resume` output, in `runs status` (`warnings`), and in the chat.
- **A newer workspace is refused.** A workspace upgraded by a newer aibench is refused with exit code 2, instead of being written by software that doesn't know its schema.
- **Version metadata.** The version is `0.1.0rc1` for both packages, from one source each. `aibench-deepeval` requires `aibench>=0.1.0rc1,<0.2`.

### Metric semantics

First release: `native.exact_match@1.0.0`, `native.json_schema@1.0.0` and
`deepeval.faithfulness@1.0.0`. Their limitations are listed in `aibench evaluators describe`.

### Workspace

Schema version 7, created on first use. See the upgrade notes for the forward-only
migration rule and the refusal of newer workspaces.

### Known limitations

- **Tested platforms.**
  - Tested on Windows 11 with Python 3.12 and 3.11.
  - Linux runs in CI, but no result has been observed.
  - macOS is not run anywhere.
- **No live model or judge tested.** Nothing in the conversation, model planner or DeepEval judge has been exercised against a live model or judge.
- **Planner recall.** The template planner's selection recall on the v1 fixture set is 17/21, below the 0.85 engineering target. Fixtures and calibration labels haven't been reviewed by people.
- **Throughput.** About 7 cases/s against an instant local service. Durable capture is the bottleneck.
- **Disk-full testing.** Handling is tested by making the real write path fail. A real full volume wasn't used.
