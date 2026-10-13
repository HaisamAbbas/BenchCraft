# G20.1 review

Independent read-only review used the requested `review-agent` workflow against the full
G20.1 diff, related evaluator contracts and call sites. Result: **No findings.** The reviewer
confirmed that version-qualified `not_measured` entries are filtered only when that evaluator
version was measured, and that an omitted required input prevents judge dispatch while explicit
JSON `null` remains a present value under the case protocol.

The reviewer called out malformed/oversized calibration sets, missing evidence and evaluator
lifecycle failures as test gaps. Regressions for duplicate and over-limit sets, a model judge
skipped before evaluation when required input is absent, and continuation/close behavior after
an evaluation failure were added and passed.

Verification performed on Windows with the repository `.venv`:

- `tests/test_judge_calibration.py`: 9 passed.
- Ruff check and format check passed for all changed Python files.
- Mypy passed for the changed service and CLI files.
- `git diff --check` passed.
- No live provider calls were made; this slice only exercises a local fake judge.

Implementation commit: [`e1236a7`](https://github.com/HaisamAbbas/BenchCraft/commit/e1236a7757db14eed1eae7d1470808d3d270ed0d).
