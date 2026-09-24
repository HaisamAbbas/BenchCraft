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
