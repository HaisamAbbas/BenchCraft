# F06 independent review

Review performed against the full F06 diff and relevant callers/tests using the requested
read-only review-agent workflow.

The first review found one P1 acceptance gap: `chat --send "Please rescore this run"`
ignored the exit code in `TurnOutcome.rescores`, even though slash-command `/rescore`
already propagated it. The natural-language headless path now extracts those codes,
applies the same exit-severity ordering as launched runs, and combines them with any run
codes from the same exchange. A real scripted-provider CLI regression proves an
incomplete natural-language rescore returns code 3 both at process level and in JSON.

The review-agent rechecked the fix and reported **No findings**. The supplementary
outcome/chat/conversation suite passed 48 tests; Ruff and Mypy passed after the fix.
