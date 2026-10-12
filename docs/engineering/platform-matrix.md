# Platform and compatibility matrix

## Current candidate: `0.1.0rc35` (2026-10-13)

The source candidate is not published. The hosted workflow for the source fixes,
[38301696360](https://github.com/HaisamAbbas/BenchCraft/actions/runs/38301696360), tests
commit `64ea5c5d097435805941d0daa5b908d30d289eed`, which contains the current source fixes.
At the last status check of that run, it had no final conclusion and some jobs were still
queued or running. Treat the rc34 results below as the last fully verified platform matrix;
they do not establish rc35 compatibility.

## Last fully CI-verified source: `0.1.0rc34` at `af918898` (2026-10-11)

The CI workflow is configured to run the core checks and release-wheel journey on
Windows, Ubuntu Linux, and macOS with Python 3.11 and 3.12. The DeepEval and Ragas
real-package adapter jobs run on all three operating systems with Python 3.12. Hosted run
[38145775882](https://github.com/HaisamAbbas/BenchCraft/actions/runs/38145775882) passed all
18 jobs on source commit `af918898e8a7d14bff2b861c6b3140d3185844d9`.

| Area | Configured CI matrix | Observed results for rc34 |
|---|---|---|
| Core checks | Windows, Ubuntu Linux, macOS; Python 3.11 and 3.12 | Hosted: all six jobs passed. Ubuntu: 1,745 passed / 187 skipped per Python version; Windows: 1,742 / 190; macOS: 1,743 / 189. Local Windows 11, Python 3.12.10 targeted regressions passed. |
| DeepEval and Ragas adapters | Windows, Ubuntu Linux, macOS; Python 3.12, real pinned packages and deterministic judge | All six hosted contract jobs passed (both adapters on each OS). No live judge model was called. |
| Release wheel, quickstart, and acceptance journey | Windows, Ubuntu Linux, macOS; Python 3.11 and 3.12 | All six hosted clean-wheel release checks passed. Windows 11, Python 3.12.10 local rc34 build/install and release journey also passed 17/17. |
| Live hosted model providers | Not part of CI | Not exercised; no provider call was made |

CI is triggered by pushes and pull requests. Runner labels such as `macos-latest` follow
GitHub-hosted runner images and do not promise a specific patch-level OS release. Passing CI
does not establish behavior on every distribution or hardware configuration, live-provider
behavior, or release authorization.

## Historical evidence: release `0.1.0rc1`

The following sections preserve the platform and package evidence recorded for the older
rc1 artifact. Their "not exercised" entries describe that historical artifact only. The last
fully verified rc34 source results are listed above.

"Supported" is what the release declares. "Exercised" is what has actually run, and
where. Anything not exercised is a claim that nobody has checked yet, and is labelled
that way.

## Release 0.1.0rc1

| Dimension | Supported (declared) | Exercised for this release | Evidence |
|---|---|---|---|
| Python 3.12 | Yes | 3.12.10, Windows 11. Prompt 12 full suite (687 passed, 2 skipped); Prompt 13 regression suite (49 passed); clean wheel install, quickstart and 100-case acceptance | `../evidence/13/release-check.json`; report 13 |
| Python 3.11 | Yes | 3.11.16 (standalone CPython via uv), Windows 11. Clean wheel install, quickstart and 100-case acceptance from the built wheel (16 core steps); full test suite not run on 3.11 | `../evidence/13/py311/release-check.json` |
| Python 3.13 and later | No (`requires-python <3.13`) | Not run | — |
| Windows | Yes | Windows 11 Pro 10.0.26200 (x86-64): artifact checks on Python 3.11 and 3.12; Prompt 13 regression suite and real ConPTY tests, including resizing | Report 13; `tests/test_cli_chat_pty.py` |
| Linux | Yes | **Not exercised.** The CI workflow runs the checks and the release check on `ubuntu-latest`, but no CI result has been observed. No local Linux Python here; Docker was not approved | — |
| macOS | Yes (by dependency support) | **Not exercised anywhere.** No CI runner is configured | — |
| Install mode | Built wheel; sdist; `pip install .`; editable for development | Built wheel installed in clean venvs on 3.12 and 3.11. Both sdists built and wheels built from them. Direct sdist, `pip install .`, and editable 3.11 installs were not checked in Prompt 13 | `../evidence/13/` |
| Terminal chat | Any terminal prompt_toolkit supports | Windows ConPTY: start-up, slash commands, multiline, resizing between 8×20 and 50×200 | `tests/test_cli_chat_pty.py`, `tests/test_quickstart_pty.py` |
| Non-interactive use | Pipes, CI, `chat --send` | Windows (subprocess and CliRunner tests; the release check's `chat --send` steps) | Same |

## Evaluator plugins

| Plugin | Version | Requires | Exercised | Not exercised |
|---|---|---|---|---|
| Native (`native.exact_match@1.0.0`, `native.json_schema@1.0.0`) | Ships with aibench 0.1.0rc1 | — | Everywhere above | — |
| `aibench-deepeval` (`deepeval.faithfulness@1.0.0`) | 0.1.0rc1 | `aibench>=0.1.0rc1,<0.2`, `deepeval==4.2.5` (exact pin, enforced at run time), Python 3.11–3.12 | Built wheel installed into a clean Python 3.12 venv on Windows: adapter and worker contract tests against the real `deepeval` 4.2.5 package, with a deterministic local judge (29 passed) | A live judge model (paid, no key authorized); plugin not exercised on Python 3.11 or Linux/macOS |
| Custom Python evaluators | Protocol of aibench 0.1.0rc1 | Explicit `--trust-local-code` | `examples/evaluators/refund_window.py` (pilot recipe B trial) | — |
| `aibench-ragas` (`ragas.faithfulness@1.0.0`) | 0.1.0rc1 working tree | `aibench>=0.1.0rc1,<0.2`, `ragas==0.4.3` (exact pin, enforced at run time), Python 3.11–3.12; text-only worker adapter | Windows 11, Python 3.12.10: real Ragas worker contract, raw/NaN policy, and same-stored-output DeepEval/Ragas cross-ecosystem test; separate plugin environment and `pip check` passed | Live provider; Python 3.11/Linux/macOS; full transitive lock; patched upstream advisory release |
| `aibench-openai-evals-oss` (`openai_evals_oss.{match,includes,fuzzy_match,json_match}@1.0.0`) | 0.1.0rc1 working tree | `evals==3.0.1.post1` installed without its declared dependencies plus `requirements-lock.txt`; Python 3.12 | Windows 11, Python 3.12: real upstream eval classes in recorded replay and the live completion-function bridge (`tests/test_openai_evals_oss.py`) | Other eval types; Python 3.11/Linux/macOS |
| `aibench-openai-evals-api` (`openai_evals_api.criterion@1.0.0`, remote job) | 0.1.0rc1 working tree | `openai==3.19.2` (exact pin, enforced by the worker); Python 3.12 | Windows 11: real SDK against a local stand-in of the Evals API contract, with failure injection (`tests/test_openai_evals_api.py`) | The live hosted service (no authorized key) |
| Langfuse connector (core) | 0.1.0rc1 working tree | Langfuse public API v4 endpoints (shapes from `langfuse==4.15.6`) | Local stand-in, full dataset → run → traces → scores round trip (`tests/test_langfuse_connector.py`) | A live Langfuse deployment |
| Promptfoo | Not integrated | — | — | — |

## Application transports (Prompt 15)

| Transport | Exercised | Not exercised |
|---|---|---|
| `cli`, `http` | Windows 11, Python 3.12 and 3.11 (see Release 0.1.0rc1) | Not part of the rc1 evidence; the current source passed native hosted core checks on Linux and macOS (run 38145775882 above). |
| `python` (callable in a fresh interpreter) | Windows 11, Python 3.12: end to end through `aibench app smoke`, timeout kill, exception capture, reset callable (`tests/test_runner_transports.py`) | Other interpreters as the application's environment |
| `openai_compatible` | Windows 11, against the local stub `examples/apps/openai_stub.py`: request shape, bearer secret redaction, usage, tool-call requests, streamed response accumulation, TTFT/inter-delta metrics and completion integrity | Any live hosted endpoint |
| `container` | Docker Engine 29.7.2 via Docker Desktop (linux/amd64 VM) on Windows 11, image `python@sha256:2f17fc04...06a9`: non-root uid 65534, read-only root and mount, tmpfs `/tmp`, no network, timeout removal, missing-image refusal | Linux or macOS hosts, rootless engines, Podman, Windows containers |

## Model providers (assistant model, model planner, judges)

| Provider | Status |
|---|---|
| Scripted and fake providers (tests) | Exercised: the conversation, planner bounds and fallback, and claim checking |
| Local OpenAI-compatible HTTP endpoint (test server) | Exercised: the provider protocol and policy egress checks |
| Any live hosted model (for example OpenAI) | **Not exercised.** Configuration is documented, but no live call is part of this release's evidence |

Rationale for the Python bounds: Pydantic v2 and Typer both support 3.11–3.13. The upper
bound `<3.13` stays until 3.13 is actually exercised; it is not a known incompatibility.
Widen `requires-python` and this table together.

For this historical rc1 snapshot, CI was configured on Python 3.11 and 3.12, on
`ubuntu-latest` and `windows-latest`; no hosted result was recorded then. Current hosted
three-OS results are recorded at the top of this document.

## Runner process-tree cleanup (Prompt 03)

| Platform | Mechanism | Exercised |
|---|---|---|
| Windows | Job Object with `KILL_ON_JOB_CLOSE` (`src/aibench/runners/process_tree.py`) | Yes: Windows 11, Python 3.12.10, `tests/test_cli_runner.py` tree-cleanup tests |
| Linux / macOS | New session + `killpg(SIGKILL)` | Not locally in the rc1 evidence. The current source passed the hosted core matrix on both platforms (run 38145775882 above). |

Known containment gaps: a Windows descendant spawned in the instant between process creation
and job assignment, and a POSIX descendant that calls `setsid()` itself. See ADR 0002.
