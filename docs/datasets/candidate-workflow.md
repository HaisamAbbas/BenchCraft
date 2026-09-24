# Candidate dataset workflow

Generated cases are stored in `.aibench/aibench.db` as a development-only candidate pool.
They are not regular dataset rows, so run planning and dataset summaries cannot consume them
as trusted Goldens. Generation makes one provider call, accepts at most 50 Q/A candidates,
reads at most eight UTF-8 `.txt`/`.md` files, and caps selected source text at 512 KiB.
Exact-content duplicate source documents are recorded and sent only once.

The caller must explicitly identify the source split. `--split development` is the only
accepted value; case JSONL datasets, validation data, and holdout data are not accepted as
generation sources. When `data_roots` is configured, every source must resolve inside one of
those roots. The provider origin and API-key secret reference must also be approved by the
policy. The selected source text is sent to that provider; only the generated question,
answer, source label, and exact quote are accepted back.

```powershell
aibench dataset candidates generate examples/datasets/refund-policy-source.md `
  --split development --max-candidates 10 `
  --provider-config provider.json --policy policy.json --json
```

Generation records the source digest and exact quote location, generator identity, prompt
hash, and `synthetic_unverified` reference status. Inspect a candidate beside the unchanged
source before deciding:

```powershell
aibench dataset candidates show CANDIDATE_ID
aibench dataset candidates review CANDIDATE_ID `
  --reviewer reviewer@example --decision source_verified `
  --note "Answer is supported by the cited policy text."
```

Use `--decision human_reviewed` for an expert review, or `--decision reject` to retain an
audited rejection. The optional executable verifier `aibench dataset candidates verify ID`
uses the strict `aibench.source_quote_presence.v1` oracle: the full expected answer must be
an exact substring of the cited span. A failed check remains recorded and does not authorize
promotion. This narrow oracle proves exact support only; it does not establish semantic
correctness for paraphrases.

Promotion is a separate explicit command, takes one or more selected IDs, and creates a new
JSONL file without replacing an existing path:

```powershell
aibench dataset candidates promote POOL_ID reviewed.jsonl `
  --candidate CANDIDATE_ID --actor dataset-owner
aibench dataset validate reviewed.jsonl
```

Only candidates with a passed human review or executable check can be promoted. The output
case carries the resulting reference status and reviewer identity. Every action is appended
to the candidate event history. Editing a source after generation blocks review, verification,
and promotion because the recorded evidence digest no longer matches.

Generation uses an OpenAI-compatible chat-completions provider configuration, reusing the
policy-checked planner provider boundary. No live provider request is part of the fixture or
test suite; pass the generate command only when sending those source documents to the selected
destination is intended.
