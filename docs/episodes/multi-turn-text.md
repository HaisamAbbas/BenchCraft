# Multi-turn text application episodes

Harness chat and application conversation evaluation are separate. Application turns are
ordinary dataset cases with one shared `group_id`; a `TextEpisodeManifest` supplies ordered
case IDs, development/validation/holdout identity, user-simulator provenance, a declared test
world, and independent final-state assertions.

Validate the manifest before a run:

```powershell
aibench dataset episodes validate examples/multi_turn_text/cases.jsonl `
  examples/multi_turn_text/episodes.json `
  --plan examples/multi_turn_text/plan.json --json
```

The validator checks that the selected turns exist, are contiguous and ordered, belong to
the declared episode, and place the success assertions on the final turn. The plan must
select the same dataset, the app must declare `reset_policy: per_episode`, and the plan must
select the episode's declared test world. The existing run engine
then resets before each episode, keeps the conversation state between turns, blocks later
turns after a failed/interrupted earlier turn, and does not retry episode turns.

The example app is a deterministic local test world. Its output includes captured `world_state`
from the fixture, and `native.final_state` checks each turn independently. Its scripted user
turns and their seeds are identified in the sidecar manifest. The fixture does not contact a
model or carry out a refund or exchange.
