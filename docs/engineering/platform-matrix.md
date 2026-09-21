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
