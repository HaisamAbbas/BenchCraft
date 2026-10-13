# G20.1 — Repeatable calibration observations

Status: delivered as the offline calibration core. `aibench evaluators calibrate` accepts
`--repetitions` (1–100), and the report schema is now
`aibench.judge-calibration-report/3`. Agreement, false-acceptance and false-rejection rates
count each decided observation across repeats; repeat stability counts a case only when every
requested run returns the same decided result. Errors and not-applicable cases stay undecided.

Calibration cases now preserve the original `input`, structured `reference`, `retrieved_context`,
`tool_events`, `metadata` and `expectations` in the evaluator view. The loader rejects duplicate
case IDs and sets larger than 1,000 cases. Reports include each repeat's decision and usage report;
missing model usage leaves cost or call totals unknown instead of implying zero.

The version 3 report also includes paired provider comparisons for image/vision cases. A shared
`pair_id` links variants that use the same reviewed image, prompt, reference, recorded answer,
label, and non-judge parameters. The report records per-repeat decision agreement and scalar
score differences, without asserting score equivalence. See the paired provider calibration
contract and limits in [G22.5](G22.5-image-vision.md#paired-provider-calibration).

Native/offline mode remains available without external calls. Live model judges run only through
the separate G20.2 path, which requires explicit authorization, policy approval and reviewed
labels. Existing v1 calibration sets remain readable; missing case inputs stay missing and are
never replaced by a synthetic prompt.

Verification for this slice:

- `tests/test_judge_calibration.py`: 9 passed, including model opt-in/cap, versioned
  `not_measured` cleanup, missing-evidence skips, duplicate/oversized sets, and evaluator recovery.
- Ruff check and format check passed for the changed service, CLI and test.
- Mypy passed for the changed service and CLI.
