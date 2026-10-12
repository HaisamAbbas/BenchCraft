# Sequential audit implementation

The user authorized implementing every finding and recommended capability in
[REPORT.md](REPORT.md), [findings.csv](findings.csv), and
[feature-backlog.csv](feature-backlog.csv), with a separate verified commit and push per
improvement. The audit and its original evidence are historical; current implementation
state lives in [implementation-ledger.json](implementation-ledger.json).

Author: `Haisam Abbas <HaisamAbbas@outlook.com>`.
Delivery branch: `fix/plugin-core-match`, tracking `origin/fix/plugin-core-match`.

Each item follows the same sequence:

1. Implement its complete behavior and acceptance boundary.
2. Run meaningful affected tests and relevant lint/type checks.
3. Obtain an independent read-only review using the requested review-agent skill.
4. Resolve actionable review findings and verify the resulting change.
5. Commit, push without force, and verify the remote commit before beginning the next item.

G22.1-G22.4 cover batched work persistence, bounded queue paging, coordinator-backed
distributed workers, and open-loop/ramping load profiles. G22.5 adds image-first vision
scoring with bounded inline image input and paired reviewed-case calibration across judge
bindings; G22.6 adds a local read-only dashboard over shared stored-result services. Approved
live cross-provider calibration, cross-host performance evidence, and broader modality support
remain explicit limitations. No existing deferred label is treated as authorization to omit a
feature selected by the user.

G22.8 extends `deepeval.vision_g_eval` to score a recorded generated image beside the case
image, task, and reviewed answer. Both images use the strict inline PNG/JPEG/WebP contract;
remote paths and URLs remain rejected.

G23 adds source-turn planner usage attribution and combined workflow cost accounting. Reports
retain separate application, primary evaluator-pass, and planner views while the workflow total
includes every evaluator scoring pass. Planner dollar pricing and total-cost budget enforcement
remain unsupported; partial, canceled, concurrent duplicate, and legacy usage states stay
visible rather than being counted as complete.

The implementation order follows the report's remediation sequence. F14 is a small
prerequisite to obtain a clean type-check baseline; F03 and F05 then protect input
isolation and fidelity before scoring, budgets, identities, and CLI features are extended.

An item marked `verified` is ready for delivery. `delivered` requires a recorded commit and
successful push; Git and the remote ref remain authoritative. Reviews and checks are saved
under `evidence/`, and per-item implementation notes under `implementation/`.
