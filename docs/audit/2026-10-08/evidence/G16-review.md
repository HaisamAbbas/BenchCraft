# G16 independent review

The review-agent performed read-only, defect-first passes over the complete G16 implementation diff. The reviews found eight actionable edge cases, all corrected with regression coverage:

- Excessively large reported `completion_tokens` could overflow token-rate calculation.
- A UTF-8 BOM could hide the first SSE `data` field.
- Events after `[DONE]` or after a choice's finish reason could mutate an answer still marked complete.
- Choice indexes outside the requested `n` range could satisfy the distinct-choice count.
- Parser work time could appear as inter-delta latency when events shared one received chunk.
- Standalone-CR SSE framing was not accepted.
- Repeated suffix slicing could cause avoidable work on a bounded chunk containing many events; line draining now uses a cursor.
- Invalid interval means could distort the aggregate weighted-mean denominator.

The token-rate summary also uses unit-neutral percentile keys within the `output_tokens_per_second` block. The final review confirmed all findings were fixed and reported no remaining actionable defects. The reviewer made no edits.

Focused verification by the reviewer: `tests/test_streaming.py tests/test_runner_transports.py` — 31 passed, 4 skipped.
