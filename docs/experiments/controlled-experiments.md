# Controlled parameter experiments

The optional experiment module searches a bounded, explicit grid over environment
parameters that an application owner exposes in its `ApplicationSpec`. It reuses the run
engine, evaluator bindings, policy, code identity, and budget in a frozen plan. It does not
edit application source or evaluator rubrics.

## Contract

An experiment definition names a plan, development JSONL, protected holdout JSONL, intended
change, parameter values, one objective binding, optional constraints, and a trial budget.
Paths are relative to the definition file. Parameter names must match `exposed_parameters`
in the app config; values must be in that declaration's finite allowlist. Tunable
environment keys use the `AIBENCH_TUNABLE_` prefix. Each exposed setting declares a
default value and description. Grader configuration stays in the frozen plan and cannot be
part of the parameter space.

The search enumerates a deterministic finite grid (at most 128 combinations). The declared
default combination runs first as baseline. `budget.max_trials` caps each execution window;
remaining combinations stay pending after exhaustion and can be unlocked only with an
audited `resume --add-trials N`. The same `budget.seed` and plan budgets are recorded in each
run manifest. Every trial records its exact parameter map, parameter hash, run ID, frozen
plan and evaluator identity, budget, objective and constraints.

The objective and constraints refer to case-scoped scalar or boolean rate bindings by their
index in `plan.metrics`. Constraints compare their covered development mean to a declared
threshold. A trial is feasible only when its metric coverage and all constraints pass. A
non-baseline candidate can beat baseline only when strict run compatibility and paired
coverage gates pass and its paired 95% uncertainty interval excludes zero in the objective
direction. This implementation uses the existing grouped cluster bootstrap and fixed seed;
it does not collapse unrelated metrics into a composite score.

## Protected evaluation lifecycle

Creation reserves the holdout dataset digest in the workspace before a development trial
starts. The holdout JSONL is streamed only to compute its normalized dataset digest; no cases
are parsed or committed while development search is active. A dataset already reserved as a
holdout cannot later be used as development data in that workspace.

Once the development candidate is selected, the selection timestamp and holdout run IDs are
committed before holdout parsing begins. The final plan retains the frozen metrics,
repetitions, per-run budgets and application contract, but selects all cases from the
protected holdout. The baseline and selected configuration use that same plan. Their run
manifests and stored outputs feed one paired uncertainty comparison. No more development
trials can run after selection, and a completed holdout evaluation is idempotent rather than
repeatable. Changed plan, evaluator, app code, policy, development data or holdout data fails
closed.

Reports label development selection and final protected evaluation separately and include
only metric aggregates and run lineage, not holdout references or application inputs. The
holdout comparison can support a recommendation, but it never returns the experiment to
search. Live service behavior is not implied by the synthetic example.

## Commands

```text
aibench experiments create examples/experiments/known_objective/experiment.json --trust-local-app
aibench experiments run known-objective
aibench experiments resume known-objective --add-trials 1
aibench experiments evaluate-holdout known-objective
aibench experiments report known-objective
aibench experiments propose-adoption known-objective
```

The example has two combinations and a one-trial budget to demonstrate interruption and
explicit resume. Review its report before running `evaluate-holdout`. The adoption command
and conversational proposal only return a recommendation and record that proposal. Source
changes, deployment, or production configuration changes require their own explicit
authorization.

Conversation exposes `list_experiments`, `get_experiment_report`, and
`propose_experiment_adoption`. It explains development and holdout evidence, coverage, and
uncertainty in separate terms. There is no apply/adopt tool.
