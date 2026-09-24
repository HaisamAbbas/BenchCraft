# ADR 0013: Ragas adapter and qualified run comparison

Status: Accepted
Date: 2026-09-24
Prompt: 14 — Second evaluator ecosystem and comparisons

## Context

Prompt 14 requires one evaluator ecosystem independent of the existing DeepEval adapter,
comparison of compatible stored runs, paired/grouped uncertainty, repeated-judge stability,
and a shared CLI/conversation service. The authoritative specification (§9, §12, §18 and
§23) requires adapters to score recorded outputs by default, preserves metric meaning rather
than normalizing unrelated scores, and refuses unqualified comparisons when identity changes.

The repository already has RAG datasets and applications that record observed retrieved
context, an isolated worker/plugin pattern, and a deterministic DeepEval adapter. Promptfoo
would add a Node/npm ecosystem that is not otherwise present in the repository. Ragas is
therefore the default named by Prompt 14 and the smaller evidence-backed fit.

## Verified upstream contract

The adapter is pinned to `ragas==0.4.3` (PyPI release 2026-01-13; Git tag commit
`4ecab384fda829ca50bec3f07cc49589d756e172`). The checked release uses:

- `ragas.metrics.collections.Faithfulness`;
- `Faithfulness(llm=llm)` where `llm` subclasses `InstructorBaseRagasLLM`;
- `await metric.ascore(user_input=..., response=..., retrieved_contexts=...)`;
- a `MetricResult.value` score.

The legacy `ragas.metrics.Faithfulness` import is deprecated. Ragas returns a non-finite
value when no statements are available; the adapter maps that to `not_applicable`, never to
zero or a vacuous perfect score. `RAGAS_DO_NOT_TRACK=true` is set before Ragas is imported.
Runtime version drift is refused because this field mapping was checked against one exact
release.

## Decisions

1. **Ragas, not Promptfoo.** Add `plugins/ragas` as a separately installable worker package,
   following `plugins/deepeval`. Existing RAG fixtures exercise the adapter and no Node
   runtime or npm lock is introduced.
2. **Recorded outputs only.** The manifest declares `consumes="recorded_outputs"`. The
   adapter receives only `case.input`, the stored output, and observed retrieved text. It
   never substitutes the Golden's reference context and has no application runner.
3. **Text-only compensating control.** Ragas 0.4.3 is affected by
   GHSA-95ww-475f-pr4f / CVE-2026-6587, with no patched release listed at decision time. The
   vulnerable functions are in multi-modal faithfulness URL/file processing. This adapter
   exposes only text `Faithfulness`, passes a validated list of strings, and never selects
   or calls the multi-modal metric or its URL/file helpers. The pinned collection import
   may load module definitions as a side effect, but no vulnerable entry point is reachable
   through the adapter. It still runs in an isolated worker with a minimal
   environment. This is a documented compensating control, not a claim that the package is
   vulnerability-free; reassess the pin when a patched release exists or if the adapter's
   exposure changes.
4. **Frozen compatibility identity.** Every scoring pass now freezes an additive
   `EvaluationCompatibilityIdentity` and each result carries a copy. It separates metric
   semantics/binding, plugin/package implementation, judge, rubric, dependency lock and
   observation-extraction identities. Historical records without it remain usable only in
   explicitly exploratory mode. The worker request is projected to the manifest's declared
   fields before crossing the process boundary, so an adapter cannot accidentally receive
   reference answers, unrelated fixtures, traces or tool events.
5. **Strict qualified comparison.** A normal comparison pairs `(case_id, repetition_id)` and
   checks dataset/case content, repetition policy, application instrumentation, evaluator
   binding and implementation, rule, judge and rubric identities. A changed or unknown
   required identity blocks an unqualified regression/improvement claim. An application
   implementation/target change is not itself a mismatch when its observation contract is
   unchanged.
6. **Exploratory diagnostic.** `--mode exploratory` may show explicitly non-qualified paired
   diagnostics when identities differ. It cannot emit a qualified verdict. Different metrics
   are never averaged.
7. **Cross-framework disagreement is diagnostic.** DeepEval and Ragas faithfulness keep
   separate IDs, scales, rules and raw semantics. A report may show their decision
   disagreement and separate distributions, but it must not treat the scores as equivalent
   votes.
8. **Coverage before claims.** Missing, failed, cancelled and not-applicable observations stay
   in the denominator. Paired means are case-level macro averages; uncertainty resamples
   independent case groups and reports its assumptions and sample size.
9. **Stored reads only.** Comparison invokes no application, evaluator or judge. Selecting a
   rescore pass is distinct from fresh execution, and the report states the selected scoring
   passes and whether paired execution identities are the same.
10. **Conversation ownership.** CLI can compare any two runs in one workspace. Conversation
   comparison uses the same service but remains restricted to runs started by that session;
   this preserves the existing authorization boundary while still supporting conversational
   comparison of the session's baseline and current run.

## Scope discrepancy

Specification §18's full Phase 2 exit gate also requires two real applications, demonstrated
time saved, limitations and a supported-version matrix. Prompt 14 implements the second
ecosystem and repeatable comparison workflow. Existing RAG/CLI applications remain the Phase
2 application evidence, while a time-saved study remains outside this prompt and is not
claimed complete.
