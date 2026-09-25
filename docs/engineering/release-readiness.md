# Release readiness — aibench 0.1.0rc1

Date: 2026-09-24
Decision: **Local technical candidate ready for MVP review on Windows; not cleared for a broad external release.**

The package artifacts are local in [`evidence/13/dist/`](evidence/13/dist/), with SHA-256 values in [`evidence/13/SHA256SUMS`](evidence/13/SHA256SUMS). The core wheel's installed quickstart and 100-case acceptance workflow passed in clean Python 3.11 and 3.12 environments on Windows. The optional DeepEval adapter's installed-wheel contract tests passed with the pinned real package and a deterministic local judge.

## Prompt 13 gate evidence

| Gate | Result | Evidence |
|---|---|---|
| 13-G1 — built artifact installs and runs the documented demo | Satisfied on Windows, Python 3.11.16 and 3.12.10 | [`release-check.json`](evidence/13/release-check.json) (16/16 steps); [`py311/release-check.json`](evidence/13/py311/release-check.json) (19/19 steps, including plugin check) |
| 13-G2 — no critical open gate hidden | Satisfied | This report names unobserved platforms, live checks and real-team trials below |
| 13-G3 — compatibility, limitations and pilot status accurate | Satisfied | [`platform-matrix.md`](platform-matrix.md), [`docs/pilot/README.md`](../pilot/README.md), this report |
| 13-G4 — MVP stopping point before optional expansion | Satisfied | Stop at the MVP review checkpoint; Phase 2 remains optional |
| 13-G5 — relevant tests ran and ledgers/reports updated | Satisfied | [`reports/13.md`](reports/13.md), [`tickets.md`](tickets.md), [`requirements-matrix.md`](requirements-matrix.md) |

## Validation observed

- Windows 11 Pro 10.0.26200, x86-64.
- Python 3.12.10: 16/16 artifact build, clean install, quickstart and 100-case acceptance steps passed. The example quickstart intentionally has a failing quality gate; its expected exit code was 3, and report/rescore/chat commands behaved as documented.
- Python 3.11.16: 19/19 release-check steps passed, including the same installed-wheel demo and acceptance workflow. The real DeepEval 4.2.5 adapter/worker checks passed (29 passed); the paid live-provider smoke was skipped because no key or authorization was provided.
- The Prompt 13 regression set passed: 49 tests, including disk-full and SQLite `SQLITE_FULL` recovery, atomic recovery, workspace-version refusal, both local pilot trials, run and migration regressions, and real Windows ConPTY resize/control tests.
- Ruff passed; mypy passed for 94 source files. `git diff --check` exited 0 (Git printed only line-ending conversion warnings).
- The Prompt 12 full suite remains historical evidence (687 passed, 2 skipped); it was not rerun after the Prompt 13 changes.

The 3.11 and 3.12 artifact builds have different archive checksums, as expected for separately built archives, but their core and DeepEval wheel members were compared and had identical file names and contents. The distributed artifacts under `evidence/13/dist/` match the recorded Prompt 13 SHA-256 manifest.

## Open validation and limitations

- Linux is declared supported and CI is configured, but no CI result was observed. macOS was not exercised and has no configured CI runner. Do not describe either platform as validated.
- No hosted assistant, planner or judge provider was called. The live DeepEval test remains skipped; planner recall remains 17/21 against its 0.85 engineering target, and planner/judge fixture labels have not had human review.
- Both pilot recipes ran locally against stand-in applications. No real team has run them or returned a form. The two real-team trials and any claim of time saved remain pending. No pilot user was contacted.
- The 1,000-case workload measured roughly 7 cases/second in the Prompt 12 evidence. It is a recorded performance limit, not a release-blocking correctness defect.
- The disk-full path was exercised by making the real artifact write path raise `ENOSPC`; an actual full volume was not created.
- The source distributions were built and the wheels were built from them. The release check installed the wheels; it did not install an sdist directly.
- CI is configured but has no observed result. Package artifacts were not published or sent, and no deployment was made.

## Concurrent worktree changes

Prompt 14 Ragas files appeared in the shared worktree during Prompt 13 verification. They were left untouched and are excluded from the Prompt 13 artifact evidence. The Prompt 13 wheels and checks were produced before the Ragas addition to `scripts/release_check.py`; the saved wheel payloads are the ones validated above. Review the concurrent files as separate work and build a fresh candidate after that review before treating the combined worktree as release-ready.

## Phase 2 implementation status: Prompts 14 and 15

Prompts 14 and 15 are implemented locally. Prompt 14's real Ragas 0.4.3 worker contract,
same-stored-execution DeepEval/Ragas diagnostic and read-only paired comparison are recorded
in `reports/14.md`. Prompt 15's callable, OpenAI-compatible and container runners, state
reset/episode behavior and separate agent outcome checks are recorded in `reports/15.md`.
These additions do not change the Prompt 13 release decision: the artifacts above predate
Prompts 14 and 15 and need a fresh build/review before release. Linux/macOS, live providers,
Python 3.11 Ragas, a patched Ragas release, and the Phase 2 time-saved study remain
unobserved. No package was published and no pilot user was contacted.

## Next action

Review the combined Prompts 14 and 15 implementation and build a fresh candidate before
making a release decision. External pilot validation, live-provider checks, and any
cross-platform release claim require their own observed evidence.

## Prompt 22 audit: technical readiness and external validation (2026-09-25)

These are two separate statuses. Neither implies the other.

### Technical readiness (local, Windows): READY FOR REVIEW

- **Clean install.** The deterministic journeys E2E-01–07 pass from a clean install of a
  freshly built wheel: 30/30 tests (`evidence/22/e2e-suite.json`, `scripts/e2e_suite.py`).
- **The conversational loop.** It is now exercised through the installed `aibench chat`
  in a real terminal (`tests/test_e2e_cli_journey.py`):
  - first natural-language message → clarification → revised draft → run on a local
    fixture;
  - a question mid-run → pause, with no new dispatch → resume;
  - evidence-grounded failure discussion → report → close and reopen.

  The application received exactly 20 calls, one per case.
- **One defect fixed.** A pause during run start-up was not reflected in the committed
  status. It is fixed, with a regression test.
- **The full suite.** It was run once for this audit; the result is in report 22 §2.
- **Limits of this readiness.**
  - Only Windows 11 and Python 3.12 were observed. Linux and macOS CI has never been
    observed, and Python 3.11 was last observed at Prompt 13.
  - The version is still `0.1.0rc1`. The Prompt 13 artifacts predate Prompts 14–22, so a
    release needs a fresh build and review.

### External validation: NOT STARTED

| Gate | Status |
|---|---|
| Live assistant, planner or judge provider | Not run. No key or budget was authorized. Every conversational check uses a scripted model |
| Human review of planner and judge fixture labels | Not done |
| Planner quality | 17/21 recall, against a 0.85 target (Prompt 12). The model planner is not used by chat |
| Installed-agent trials (a real model operating the CLI) | Not run |
| Repeated-trial coverage with a real model | Not run |
| Real pilot teams and time-saved study | Not run. No pilot user was contacted |
| Hosted OpenAI Evals, live Langfuse | Not run (local contract stand-ins only) |
| Cost limits against real provider billing | Not observed. Monetary caps are soft limits over estimates |
| Public release authorization | Not requested. Nothing was published |

No official leaderboard score, model-quality claim or public release is made.

## Prompt 29 v4 acceptance update — 2026-09-25

The clean-installed deterministic suite passed 31 tests across E2E-01–07. Current inspector,
discovery/conversation and pinned real-package adapter checks also passed, with the documented
symlink skip and live-provider exclusions. See docs/engineering/reports/29.md and
docs/engineering/evidence/29/.

Repository-aware local-pilot readiness is **NOT READY**: one required journey is not proven,
namely fresh chat receiving static source findings before plan/run/report. Follow-up 30-T1 is
open. Full v3-derived readiness is also **NOT READY**; generic browser automation is deferred,
hybrid repository/trace transition is missing, and the source v3 pack is unavailable. No
live provider, paid endpoint, human review or pilot was exercised.

## Prompt 31 final v4 local-scope acceptance — 2026-09-25

The earlier Prompt 29 partial status is superseded for deterministic local acceptance by
`docs/engineering/reports/31.md`. The current wheel was built, installed into an isolated
Windows/Python 3.12.10 virtual environment, and passed **32 tests across E2E-01..08** with
zero skips. E2E-08 exercises repository findings in a fresh conversation through a stored
report and rescore. The changed-path batch passed 16 tests; Ruff, `mypy src` and CLI help
passed. Pinned DeepEval/Ragas package evidence remains separately recorded in
`docs/engineering/evidence/29/`; neither adapter changed in this follow-up.

**Declared deterministic local scope: READY FOR TECHNICAL REVIEW.** This is not authorization
to publish a release or claim broad v3 coverage. Generic website/browser execution, wide parser
support, live model/judge behavior, human review, real-agent trials, product-market fit and
public release authorization remain unverified or deferred. No model API credential was
needed for these local tests. A live smoke would need the configured provider secret and an
approved cost budget.
