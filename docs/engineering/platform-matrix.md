# Supported Python / platform matrix (00-T2)

| Dimension | Supported | Actually exercised in this environment |
|---|---|---|
| Python | 3.11, 3.12 (`pyproject.toml` `requires-python = ">=3.11,<3.13"`) | 3.12.10 |
| OS | Windows, Linux, macOS | Windows 11 Pro (win32), via Git Bash / PowerShell |
| Install mode | Editable (`pip install -e .`), built wheel/sdist (`python -m build`) | Both — see `docs/engineering/reports/00.md` |

Rationale: Pydantic v2 and Typer both support 3.11–3.13; the upper bound is set to `<3.13`
pending an explicit test pass on 3.13, not because of a known incompatibility. Widen the
`requires-python` bound and this table together once 3.13 is actually exercised in CI.

CI (`.github/workflows/ci.yml`) runs the standard checks on the Python versions listed above on
`ubuntu-latest` and `windows-latest`; macOS is supported per the dependency matrix above but not
currently run in CI (no macOS runner configured yet) — tracked as a gap, not silently claimed as
tested.

## Runner process-tree cleanup (Prompt 03)

| Platform | Mechanism | Exercised |
|---|---|---|
| Windows | Job Object with `KILL_ON_JOB_CLOSE` (`src/aibench/runners/process_tree.py`) | Yes: Windows 11, Python 3.12.10, `tests/test_cli_runner.py` tree-cleanup tests |
| Linux / macOS | New session + `killpg(SIGKILL)` | Not locally (no usable Linux environment on the development machine). The CI workflow runs the same tests on `ubuntu-latest`, but no CI result for this change has been observed (changes are uncommitted; `gh` is unavailable here). macOS is not run anywhere |

Known containment gaps: a Windows descendant spawned in the instant between process creation
and job assignment, and a POSIX descendant that calls `setsid()` itself. See ADR 0002.
