# Multi-turn text episode fixture

This local HTTP test app keeps state across turns inside an episode and resets to the
declared test world before the next episode. It does not perform a real refund or exchange.
The scripted user turns have simulator provenance in `episodes.json`; the last turn of each
episode has independent `native.final_state` assertions over the app's captured test-world
state.

From the repository root, start the fixture in one terminal:

```powershell
.\.venv\Scripts\python.exe examples/apps/multi_turn_support.py
```

In a second terminal, validate the episode contract and run it:

```powershell
aibench dataset episodes validate examples/multi_turn_text/cases.jsonl examples/multi_turn_text/episodes.json --plan examples/multi_turn_text/plan.json
aibench run --plan examples/multi_turn_text/plan.json --policy examples/multi_turn_text/policy.json --json
```

The run needs the local test app above. The engine processes each `group_id` as a stateful
episode, does not retry turns, resets before each episode, and evaluates the recorded world
state independently of the assistant's text answer.
